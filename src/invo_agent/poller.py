from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

import httpx
from rich.console import Console

from invo_agent.api.client import InvolioAPIError, InvolioClient
from invo_agent.api.models import ActionType, InvestmentUpdate, Notification, TradeAction
from invo_agent.auth.email_otp import login_with_otp
from invo_agent.auth.session import SessionStore
from invo_agent.config import Settings
from invo_agent.db import AgentDB, OpenCopy, PendingIntent
from invo_agent.dex.budget import BudgetDeniedError
from invo_agent.dex.executor import DexExecutor, RecoveryResult
from invo_agent.dex.hl_trader import (
    GhostCopyError,
    InsufficientMarginError,
    IocUnfilledError,
    NotOpenCopyError,
    PriceNotBetterError,
    RecoveryParkedError,
    SameCoinOpenError,
    SideMismatchError,
)
from invo_agent.dex.pricing import (
    classify_pending_wait,
    plan_resting_adopt,
    queued_ready_locally,
)
from invo_agent.dex.session import (
    DexSessionError,
    DexSessionStore,
    establish_dex_session,
    refresh_dex_session,
    refresh_failed_because_key_dead,
    session_is_expired,
    session_should_proactive_renew,
    session_ttl_remaining_seconds,
)
from invo_agent.engine import CopyEngine
from invo_agent.events import log_event, log_event_throttled
from invo_agent.labels import (
    format_action_summary,
    merge_update_tags,
    tags_from_summary,
    trader_from_action,
)
from invo_agent.logging_setup import prune_logs
from invo_agent.notify import notify_mac

console = Console()
logger = logging.getLogger(__name__)

CATCHUP_META = "catchup_completed_at"
HEARTBEAT_EVERY = 8  # polls between "still alive" lines when idle
# Don't hammer Turnkey OTP if auto dex-login keeps failing.
DEX_RECOVER_COOLDOWN_SECONDS = 10 * 60
# Transient network / 5xx: log quietly until this many in a row.
TRANSIENT_NOTIFY_AFTER = 5
# Live market-close retry after a transient HL failure. After this, recovery
# parks or flattens from PnL — do not dump hours later at a worse print.
CLOSE_LIVE_RETRY_SECONDS = 120
# A CLOSE this old is a missed close, not a live one: let recovery price it
# (flatten if we are green / less red, park if a dump would be worse).
CLOSE_LATE_RECOVERY_SECONDS = 600
# If the local book still says "blocked", still hit HL this often in case cache is stale.
QUEUED_PRICE_FALLBACK_SECONDS = 60.0
QUEUED_MARGIN_FALLBACK_SECONDS = 900.0
# Even when local math says "fillable now", leave a gap between HL attempts on
# the same queued row. Poll interval is ~4s; one IoC per poll rebuilt the cap.
QUEUED_LOCAL_MIN_GAP_SECONDS = 15.0
# Queue work is opportunistic. Cap HL order writes per poll so a backlog can
# never starve the writes that actually protect money (closes).
QUEUED_MAX_OPENS_PER_POLL = 3
QUEUED_MAX_ADDS_PER_POLL = 2
# When HL says "too many requests", stop opportunistic queue work for a while so
# the remaining budget goes to closes.
HL_RATE_LIMIT_COOLDOWN_SECONDS = 120.0


def locked_queued_entry_size(
    queued: float | None, live: float | None
) -> float | None:
    """Drain/refresh size: never raise above the first queued entrySize."""
    if queued is None:
        return live
    if live is None:
        return queued
    return min(float(queued), float(live))


class NotificationPoller:
    def __init__(
        self,
        client: InvolioClient,
        db: AgentDB,
        settings: Settings,
        executor: DexExecutor | None = None,
        session_store: SessionStore | None = None,
    ):
        self.client = client
        self.db = db
        self.settings = settings
        self.engine = CopyEngine(client, db, settings)
        self.executor = executor or DexExecutor(settings, client=client, db=db)
        if self.executor.db is None:
            self.executor.db = db
        self.session_store = session_store or client.session
        self._idle_polls = 0
        # Only act on notifications created after this timestamp (set on catch-up).
        self._watch_started_at: datetime | None = None
        self._dex_recover_after: float = 0.0
        self._dex_dead_notified = False
        self._transient_failures = 0
        self._last_log_prune = 0.0
        self._last_missed_close = 0.0
        self._hl_cooldown_until = 0.0
        self._force_missed_close = False

    @property
    def is_dry_run(self) -> bool:
        return bool(self.settings.dry_run) or not bool(self.settings.live)

    def needs_catchup(self) -> bool:
        return self.db.get_meta(CATCHUP_META) is None

    def _tracked_followup(self, notification: Notification) -> bool:
        """True when this UPDATE/CLOSE belongs to a copy we already hold or queued."""
        if notification.action_type not in {ActionType.UPDATE, ActionType.CLOSE}:
            return False
        base_id = (
            str(notification.investment_base_id)
            if notification.investment_base_id
            else None
        )
        if not base_id:
            return False
        if self.db.get_open_copy(base_id) is not None:
            return True
        return self.db.get_pending_intent(base_id) is not None

    def catch_up(self, *, pages: int = 3) -> int:
        """Mark notifications already in the feed as seen — do not execute them.

        Called on every process start so downtime gaps are not replayed as live
        trades (e.g. a 6‑minute‑old OPEN sitting in the inbox).

        UPDATE/CLOSE for a copy we already track is left unprocessed so the
        first poll can queue a size-add or flatten — catch-up must not eat those.
        """
        marked = 0
        for page in range(1, pages + 1):
            resp = self.client.get_notifications(page=page)
            if not resp.items:
                break
            for notification in resp.items:
                nid = str(notification.id)
                if self._tracked_followup(notification):
                    prior = self.db.processed_action(nid)
                    if prior and (
                        prior.startswith("CATCHUP_") or prior == "STARTUP_SKIP"
                    ):
                        self.db.unmark_processed(nid)
                    continue
                if self.db.is_processed(nid):
                    continue
                action = (
                    notification.action_type.value
                    if notification.action_type is not ActionType.IGNORE
                    else "IGNORE"
                )
                if not self.engine.should_consider(notification):
                    action = "IGNORE"
                self.db.mark_processed(nid, f"CATCHUP_{action}", notification.content)
                marked += 1
            if len(resp.items) < self.settings.notification_page_size:
                break

        now = datetime.now(UTC)
        self._watch_started_at = now
        self.db.set_meta(CATCHUP_META, now.isoformat())
        self.db.set_meta("watch_started_at", now.isoformat())
        self.db.set_meta("last_poll_at", now.isoformat())
        return marked

    def ensure_catchup(self, *, force: bool = True) -> None:
        """Skip existing inbox items. Default force=True on every start."""
        if not force and not self.needs_catchup():
            if self._watch_started_at is None:
                raw = self.db.get_meta("watch_started_at") or self.db.get_meta(CATCHUP_META)
                if raw:
                    try:
                        self._watch_started_at = datetime.fromisoformat(raw)
                    except ValueError:
                        self._watch_started_at = datetime.now(UTC)
            return
        console.print(
            "[yellow]Startup catch-up:[/yellow] marking existing notifications as seen "
            "(only trades that arrive after this will be acted on)..."
        )
        marked = self.catch_up()
        msg = f"Marked {marked} existing as seen. Watching for brand-new trades only."
        console.print(f"[green]Catch-up done.[/green] {msg}")
        logger.info("startup catch-up done: %s watch_started_at=%s", msg, self._watch_started_at)
        log_event(
            "startup_catchup",
            marked=marked,
            watch_started_at=self._watch_started_at.isoformat() if self._watch_started_at else None,
        )
        notify_mac(
            "invo-agent",
            msg,
            subtitle="Catch-up complete",
            sound=None,
            enabled=self.settings.mac_notify,
            sticky=self.settings.mac_notify_sticky,
        )

    @staticmethod
    def _investment_closed(inv: InvestmentUpdate) -> bool:
        if inv.is_open is False:
            return True
        if inv.closing_price is not None or inv.closed_at is not None:
            return True
        return False

    def reconcile_missed_closes(self) -> list[RecoveryResult]:
        """If Involio closed offline while we still hold HL, flatten or park TP/SL."""
        if not self.settings.recovery_enabled:
            logger.info("missed-close recovery disabled")
            return []

        open_copies = self.db.list_open_copies()
        if not open_copies:
            logger.info("missed-close reconcile: no open copies")
            return []

        console.print(
            f"[yellow]Missed-close reconcile:[/yellow] checking {len(open_copies)} open copy(ies)…"
        )
        results: list[RecoveryResult] = []
        if not self.is_dry_run:
            self._ensure_dex_session()
            try:
                self.executor._ensure_trader().refresh_book()
            except Exception as exc:  # noqa: BLE001
                logger.warning("recovery snapshot failed: %s", exc)

        for copy in open_copies:
            try:
                result = self._reconcile_one_copy(copy)
            except Exception as exc:  # noqa: BLE001
                if self._is_dex_session_error(exc) and self._recover_dex_session():
                    try:
                        result = self._reconcile_one_copy(copy)
                    except Exception as retry_exc:  # noqa: BLE001
                        logger.exception(
                            "missed-close reconcile failed base_id=%s: %s",
                            copy.investment_base_id,
                            retry_exc,
                        )
                        notify_mac(
                            "invo-agent · RECOVERY ERROR",
                            f"{copy.ticker or copy.investment_base_id}: {retry_exc}",
                            sound=self.settings.mac_notify_sound or None,
                            enabled=self.settings.mac_notify,
                            sticky=self.settings.mac_notify_sticky,
                        )
                        continue
                else:
                    logger.exception(
                        "missed-close reconcile failed base_id=%s: %s",
                        copy.investment_base_id,
                        exc,
                    )
                    notify_mac(
                        "invo-agent · RECOVERY ERROR",
                        f"{copy.ticker or copy.investment_base_id}: {exc}",
                        sound=self.settings.mac_notify_sound or None,
                        enabled=self.settings.mac_notify,
                        sticky=self.settings.mac_notify_sticky,
                    )
                    continue
            if result is not None:
                results.append(result)

        closed_n = sum(1 for r in results if r.status in {"closed", "already_flat"})
        parked_n = sum(1 for r in results if r.status == "parked")
        # Parks that were already resting changed nothing — don't re-alert.
        changed = [r for r in results if r.changed]
        log_event(
            "missed_close_reconcile_done",
            checked=len(open_copies),
            results=len(results),
            closed=closed_n,
            parked=parked_n,
            changed=len(changed),
        )
        if changed:
            summary = "; ".join(r.message for r in changed[:5])
            if len(changed) > 5:
                summary += f" (+{len(changed) - 5} more)"
            notify_mac(
                "invo-agent · RECOVERY",
                summary,
                sound=self.settings.mac_notify_sound or None,
                enabled=self.settings.mac_notify,
                sticky=self.settings.mac_notify_sticky,
            )
        elif results:
            console.print(
                f"[dim]Missed-close reconcile: {len(results)} park(s) still resting[/dim]"
            )
        else:
            console.print("[green]Missed-close reconcile:[/green] nothing to recover")
        return results

    def _reconcile_one_copy(self, copy: OpenCopy) -> RecoveryResult | None:
        resp = self.client.get_current_shadow_post_by_investment_base_id(
            copy.investment_base_id
        )
        inv = resp.post.update if resp.post else None
        if inv is None:
            logger.warning(
                "shadow post missing for open copy base_id=%s",
                copy.investment_base_id,
            )
            return None
        if not self._investment_closed(inv):
            logger.debug(
                "open copy still open on Involio base_id=%s ticker=%s",
                copy.investment_base_id,
                inv.ticker or copy.ticker,
            )
            return None

        coin = (inv.ticker or inv.name or copy.ticker or "").upper()
        if not coin:
            logger.warning(
                "closed shadow post has no ticker base_id=%s",
                copy.investment_base_id,
            )
            return None

        result = self.executor.recover_missed_close(
            coin=coin,
            inv=inv,
            our_entry_px=copy.our_entry_px,
            our_side=copy.side,
            our_size=copy.size,
            copy_base_id=copy.investment_base_id,
            parked_tp=copy.recovery_tp if copy.recovery_pending else None,
            parked_sl=copy.recovery_sl if copy.recovery_pending else None,
            our_leverage=copy.leverage,
        )
        if result.status in {"closed", "already_flat"}:
            self.db.close_copy(copy.investment_base_id)
            self.db.log_action(
                action="RECOVERY_CLOSE",
                summary=result.message,
                notification_id=None,
                dry_run=self.is_dry_run,
            )
        elif result.status == "parked":
            self.db.set_recovery_state(
                copy.investment_base_id,
                pending=True,
                recovery_tp=result.tp,
                recovery_sl=result.sl,
                trader_closing_px=inv.closing_price,
            )
            # A park that is already resting is a no-op — don't refill Recent
            # with the same line every reconcile.
            if result.changed:
                self.db.log_action(
                    action="RECOVERY_PARK",
                    summary=result.message,
                    notification_id=None,
                    dry_run=self.is_dry_run,
                )
        elif result.status == "dry_run":
            self.db.log_action(
                action="RECOVERY_DRY_RUN",
                summary=result.message,
                notification_id=None,
                dry_run=True,
            )
        return result

    def _route_late_close(self, nid: str, action: TradeAction) -> bool:
        """Old CLOSE → price it like a missed close. True if handled here.

        A close we only get to minutes later must not blind-market-dump: recovery
        flattens when we are green or less red than the trader, and parks TP/SL
        when dumping would lock a bigger loss than the trader took.
        """
        if self.is_dry_run:
            return False
        age = self._notification_age_seconds(action.notification)
        if age < CLOSE_LATE_RECOVERY_SECONDS:
            return False
        if not self.settings.recovery_enabled:
            return False
        inv = action.investment
        base_id = None
        if inv is not None and inv.base_id:
            base_id = str(inv.base_id)
        elif action.notification.investment_base_id:
            base_id = str(action.notification.investment_base_id)
        if not base_id:
            return False
        copy = self.db.get_open_copy(base_id)
        if copy is None or copy.recovery_pending:
            # Not ours / already parked — the normal path logs the right skip.
            return False
        logger.warning(
            "CLOSE %s is %.0fs old — routing to recovery instead of market dump",
            action.summary,
            age,
        )
        try:
            result = self._reconcile_one_copy(copy)
        except Exception as exc:  # noqa: BLE001
            self._note_hl_rate_limit(exc)
            logger.warning("late CLOSE recovery failed %s: %s", action.summary, exc)
            self._record_executor_error(nid, action, exc)
            return True
        if result is None:
            # Involio does not show it closed yet — let the live path decide.
            return False
        self.db.mark_processed(nid, "CLOSE_LATE_RECOVERY", result.message[:500])
        log_event(
            "close_late_recovery",
            action=action.action.value,
            summary=action.summary,
            status=result.status,
            reason=result.reason,
            age_seconds=age,
            notification_id=nid,
        )
        notify_mac(
            "invo-agent · RECOVERY",
            result.message,
            subtitle=f"Late CLOSE ({age / 60:.0f}m old)",
            sound=self.settings.mac_notify_sound or None,
            enabled=self.settings.mac_notify,
            sticky=False,
        )
        return True

    def _logged_summary(self, action: TradeAction) -> str:
        summary = format_action_summary(action)
        if action.action is not ActionType.UPDATE or tags_from_summary(summary):
            return summary
        extra = merge_update_tags(getattr(self.executor, "last_update_tags", None))
        if not extra:
            return summary
        return f"{summary} · {' · '.join(extra)}"

    def _notification_age_seconds(self, notification: Notification) -> float:
        created = notification.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        else:
            created = created.astimezone(UTC)
        return (datetime.now(UTC) - created).total_seconds()

    def _prepare_action(self, notification: Notification) -> TradeAction | None:
        """Enrich, stub CLOSE from open_copy, or leave unprocessed to retry."""
        action = self.engine.enrich(notification)
        if action.investment is not None or action.action is ActionType.IGNORE:
            return action
        if action.action is ActionType.CLOSE:
            stub = self.engine.stub_close_from_copy(notification)
            if stub is not None:
                return stub
        nid = str(notification.id)
        age = self._notification_age_seconds(notification)
        if age >= float(self.settings.pending_max_age_seconds):
            self.db.mark_processed(nid, "ERROR_ENRICH_TIMEOUT", action.reason)
            self.db.log_action(
                action="ERROR_ENRICH_TIMEOUT",
                summary=f"{action.summary}: {action.reason or 'no investment'}"[:500],
                notification_id=nid,
                dry_run=self.is_dry_run,
            )
            logger.error("enrich timeout %s: %s", action.summary, action.reason)
            return None
        logger.warning(
            "enrich miss %s (will retry): %s",
            action.summary,
            action.reason or "no investment",
        )
        log_event(
            "enrich_retry",
            notification_id=nid,
            action=action.action.value,
            reason=action.reason,
            age_seconds=age,
        )
        return None

    def _reconcile_ghosts(self) -> None:
        if self.is_dry_run:
            return
        try:
            trader = self.executor._ensure_trader()
            cleared = self.executor.reconcile_ghost_copies(trader)
        except Exception as exc:  # noqa: BLE001
            logger.warning("ghost reconcile skipped: %s", exc)
            return
        if cleared:
            logger.info("ghost reconcile cleared %s", cleared)

    def _notify_if_size_queued(self, action: TradeAction) -> bool:
        """True when an UPDATE left a size-add waiting (margin / price)."""
        if action.action is not ActionType.UPDATE or action.investment is None:
            return False
        if action.investment.base_id is None:
            return False
        copy = self.db.get_open_copy(str(action.investment.base_id))
        if copy is None or float(copy.pending_add_notional or 0) <= 0:
            return False
        reason = copy.pending_add_reason or "size"
        self.db.log_action(
            action="QUEUED_SIZE",
            summary=f"{format_action_summary(action)} ({reason} +${copy.pending_add_notional:.2f})",
            notification_id=str(action.notification.id),
            dry_run=self.is_dry_run,
        )
        notify_mac(
            "invo-agent · queued",
            f"Waiting ({reason}) — {action.summary}",
            subtitle="Size add · entry-or-better / margin",
            sound=None,
            enabled=self.settings.mac_notify,
            sticky=False,
        )
        return True

    def _notify_action(self, action: TradeAction) -> None:
        mode = "DRY-RUN" if self.is_dry_run else "LIVE"
        title = f"invo-agent · {action.action.value}"
        body = self._logged_summary(action)
        notify_mac(
            title,
            body,
            subtitle=mode,
            sound=self.settings.mac_notify_sound or None,
            enabled=self.settings.mac_notify,
            sticky=self.settings.mac_notify_sticky,
        )

    def poll_once(self) -> list[TradeAction]:
        unseen = self.client.unseen_count()
        resp = self.client.get_notifications(page=1)
        self.db.set_meta("last_poll_at", datetime.now(UTC).isoformat())
        self.db.set_meta("last_unseen_count", str(unseen))

        actions: list[TradeAction] = []
        if not self.is_dry_run:
            self._ensure_dex_session()
            self._refresh_account()
            self._refresh_pending_reasons()
            self._adopt_resting_fills()
            self._reconcile_inverse_ghosts()
        items = sorted(resp.items, key=lambda n: n.created_at)
        self._supersede_batch(items)
        for notification in items:
            nid = str(notification.id)
            if self.db.is_processed(nid):
                continue

            # Belt-and-suspenders: never act on anything older than process start.
            if self._watch_started_at is not None:
                created = notification.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                else:
                    created = created.astimezone(UTC)
                if created <= self._watch_started_at and not self._tracked_followup(
                    notification
                ):
                    self.db.mark_processed(
                        nid, "STARTUP_SKIP", notification.content
                    )
                    logger.info(
                        "skip pre-start notification %s created=%s",
                        nid,
                        created.isoformat(),
                    )
                    log_event(
                        "startup_skip",
                        notification_id=nid,
                        created_at=created.isoformat(),
                        content=notification.content,
                    )
                    continue

            if not self.engine.should_consider(notification):
                self.db.mark_processed(nid, "IGNORE", notification.content)
                continue

            action = self._prepare_action(notification)
            if action is None:
                continue
            if not self.is_dry_run:
                self._ensure_dex_session()

            if action.action is ActionType.CLOSE:
                self._cancel_pending_for_action(action, reason="trader_close")
                if self._route_late_close(nid, action):
                    continue
            elif (
                action.action is ActionType.UPDATE
                and self._refresh_pending_from_update(action)
            ):
                self.db.mark_processed(nid, "QUEUED_UPDATE", format_action_summary(action))
                self.db.log_action(
                    action="QUEUED_UPDATE",
                    summary=format_action_summary(action),
                    notification_id=nid,
                    dry_run=self.is_dry_run,
                )
                logger.info("queued UPDATE refreshed pending %s", action.summary)
                continue

            try:
                self.executor.handle(action)
            except Exception as exc:  # noqa: BLE001
                if self._handle_live_failure(nid, action, exc):
                    continue
            if action.action is ActionType.OPEN and action.investment:
                coin = (action.investment.ticker or action.investment.name or "").upper()
                if coin:
                    self.db.delete_pending_for_coin(
                        coin,
                        exclude_base_id=str(action.investment.base_id)
                        if action.investment.base_id
                        else None,
                    )
                if action.investment.base_id:
                    self.db.delete_pending_intent(str(action.investment.base_id))
            self.engine.remember(action)
            self.db.log_action(
                action=action.action.value,
                summary=self._logged_summary(action),
                notification_id=nid,
                dry_run=self.is_dry_run,
            )
            if not self._notify_if_size_queued(action):
                self._notify_action(action)
            logger.info("%s %s", action.action.value, action.summary)
            actions.append(action)
        if not self.is_dry_run:
            self._reconcile_ghosts()
            self._expire_pending()
            self._drain_pending()
            self._drain_pending_adds()
            if self._force_missed_close:
                # A CLOSE we could not execute is money still exposed.
                self._maybe_reconcile_missed_closes()
        return actions

    def _handle_live_failure(
        self, nid: str, action: TradeAction, exc: BaseException
    ) -> bool:
        """Return True if the caller should skip remember (failure handled)."""
        if self._is_dex_session_error(exc):
            self.executor.invalidate_trader()
            if self._recover_dex_session():
                try:
                    self.executor.handle(action)
                except Exception as retry_exc:  # noqa: BLE001
                    if self._is_dex_session_error(retry_exc):
                        # Leave unprocessed so the next poll retries after cooldown.
                        logger.error(
                            "DEX still broken after renew for %s: %s",
                            action.summary,
                            retry_exc,
                        )
                        self._notify_dex_login_needed(retry_exc)
                        return True
                    if self._queue_open_from_error(nid, action, retry_exc):
                        return True
                    if self._queue_update_from_error(nid, action, retry_exc):
                        return True
                    self._record_executor_error(nid, action, retry_exc)
                    return True
                return False  # retry succeeded — fall through to remember
            logger.warning(
                "DEX session error for %s (will retry): %s", action.summary, exc
            )
            self._notify_dex_login_needed(exc)
            return True
        if self._queue_open_from_error(nid, action, exc):
            return True
        if self._queue_update_from_error(nid, action, exc):
            return True
        self._record_executor_error(nid, action, exc)
        return True

    def _queueable_open_reason(self, action: TradeAction, exc: BaseException) -> str | None:
        if action.action is not ActionType.OPEN:
            return None
        if isinstance(exc, PriceNotBetterError) or isinstance(exc, IocUnfilledError):
            return "price"
        if isinstance(exc, SameCoinOpenError):
            return "same_coin"
        if isinstance(exc, BudgetDeniedError):
            return "ratelimit"
        if self._is_hl_rate_limit_error(exc):
            return "ratelimit"
        if self._is_below_hl_min_error(exc):
            return "margin"
        if self._is_insufficient_margin_error(exc):
            return "margin"
        return None

    def _queue_open_from_error(
        self, nid: str, action: TradeAction, exc: BaseException
    ) -> bool:
        self._note_hl_rate_limit(exc)
        reason = self._queueable_open_reason(action, exc)
        if reason is None:
            return False
        intent = self._intent_from_action(action, reason)
        if intent is None:
            return False
        other = self.db.get_pending_for_coin(
            intent.coin, exclude_base_id=intent.investment_base_id
        )
        if other is not None:
            self.db.mark_processed(nid, "QUEUED_SKIP_SAME_COIN", action.summary)
            self.db.log_action(
                action="QUEUED_SKIP_SAME_COIN",
                summary=(
                    f"{action.summary}: pending "
                    f"{other.base_short_id or other.investment_base_id}"
                ),
                notification_id=nid,
                dry_run=self.is_dry_run,
            )
            logger.info(
                "skip newer pending %s — already queued %s %s",
                action.summary,
                other.coin,
                other.base_short_id,
            )
            log_event(
                "pending_skip_same_coin",
                coin=intent.coin,
                kept=other.investment_base_id,
                skipped=intent.investment_base_id,
            )
            notify_mac(
                "invo-agent · queued skip",
                f"Already waiting on {intent.coin} — {action.summary}",
                subtitle="One pending per coin",
                sound=None,
                enabled=self.settings.mac_notify,
                sticky=False,
            )
            return True
        intent.last_tried_at = datetime.now(UTC).isoformat()
        self.db.upsert_pending_intent(intent)
        self.db.mark_processed(nid, "QUEUED_OPEN", f"{reason}: {exc}"[:500])
        self.db.log_action(
            action="QUEUED_OPEN",
            summary=f"{action.summary} ({reason})",
            notification_id=nid,
            dry_run=self.is_dry_run,
        )
        logger.info("queued OPEN %s reason=%s", action.summary, reason)
        log_event(
            "pending_enqueue",
            coin=intent.coin,
            reason=reason,
            base_id=intent.investment_base_id,
            trader_entry=intent.trader_entry,
            error=str(exc),
        )
        notify_mac(
            "invo-agent · queued",
            f"Waiting ({reason}) — {action.summary}",
            subtitle="Entry-or-better / margin / coin",
            sound=None,
            enabled=self.settings.mac_notify,
            sticky=False,
        )
        return True

    def _size_add_gap_notional(
        self, copy: OpenCopy, inv: InvestmentUpdate
    ) -> float | None:
        trader = getattr(self.executor, "_trader", None)
        coin = (copy.ticker or inv.ticker or inv.name or "").upper()
        if trader is None or not coin:
            return None
        mid = trader.cached_mid(coin)
        if mid is None or mid <= 0:
            return None
        equity = trader.cached_equity()
        if equity is None or equity <= 0:
            return None
        current = float(copy.size or 0) * float(mid)
        need, _meta = self.executor._size_add_need(
            inv=inv,
            current_notional=current,
            equity=float(equity),
            pending_add=0.0,
            pending_only=False,
            exclude_base_id=copy.investment_base_id,
        )
        return max(0.0, need)

    def _ensure_queued_size_add(self, action: TradeAction, *, reason: str) -> bool:
        inv = action.investment
        if inv is None or inv.base_id is None:
            return False
        copy = self.db.get_open_copy(str(inv.base_id))
        if copy is None:
            return False
        leftover = float(copy.pending_add_notional or 0)
        if leftover <= 1e-9:
            leftover = self._size_add_gap_notional(copy, inv) or 0.0
        if leftover <= 1e-9:
            return False
        entry = inv.entry_price or copy.pending_add_entry or copy.our_entry_px
        self.db.set_pending_add_notional(
            str(inv.base_id), leftover, entry=entry, reason=reason
        )
        return True

    def _queue_update_from_error(
        self, nid: str, action: TradeAction, exc: BaseException
    ) -> bool:
        """Size↑ blocked by the request cap → queue like a blocked OPEN."""
        if action.action is not ActionType.UPDATE:
            return False
        if not (
            isinstance(exc, BudgetDeniedError) or self._is_hl_rate_limit_error(exc)
        ):
            return False
        self._note_hl_rate_limit(exc)
        if not self._ensure_queued_size_add(action, reason="ratelimit"):
            return False
        copy = self.db.get_open_copy(str(action.investment.base_id))
        add = float(copy.pending_add_notional) if copy else 0.0
        summary = f"{format_action_summary(action)} (ratelimit +${add:.2f})"
        self.db.mark_processed(nid, "QUEUED_SIZE", str(exc)[:500])
        self.db.log_action(
            action="QUEUED_SIZE",
            summary=summary,
            notification_id=nid,
            dry_run=self.is_dry_run,
        )
        logger.info("queued size↑ %s", summary)
        log_event(
            "pending_add_enqueue",
            reason="ratelimit",
            summary=action.summary,
            error=str(exc),
            pending_add_notional=add,
            notification_id=nid,
        )
        notify_mac(
            "invo-agent · queued",
            f"Waiting (ratelimit) — {action.summary}",
            subtitle="Size add · request cap",
            sound=None,
            enabled=self.settings.mac_notify,
            sticky=False,
        )
        return True

    def _record_executor_error(
        self, nid: str, action: TradeAction, exc: BaseException
    ) -> None:
        self._note_hl_rate_limit(exc)
        if isinstance(exc, RecoveryParkedError):
            self.db.mark_processed(nid, "SKIP_PARKED_CLOSE", str(exc)[:500])
            self.db.log_action(
                action="SKIP_PARKED_CLOSE",
                summary=f"{format_action_summary(action)}: {exc}"[:500],
                notification_id=nid,
                dry_run=self.is_dry_run,
            )
            logger.info("skip (recovery parked) %s: %s", action.summary, exc)
            log_event(
                "skip_recovery_parked",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=nid,
                will_retry=False,
            )
            notify_mac(
                "invo-agent · kept park",
                f"Recovery TP/SL still on {action.summary}",
                subtitle="No market dump",
                sound=None,
                enabled=self.settings.mac_notify,
                sticky=False,
            )
            return

        # A CLOSE must never be filed as "no margin" — reducing needs none, and
        # dropping it would leave us holding a trade the trader already exited.
        if (
            self._is_insufficient_margin_error(exc)
            and action.action is not ActionType.CLOSE
        ):
            self.db.mark_processed(
                nid, f"SKIP_NO_MARGIN_{action.action.value}", str(exc)[:500]
            )
            self.db.log_action(
                action=f"SKIP_NO_MARGIN_{action.action.value}",
                summary=f"{format_action_summary(action)}: {exc}"[:500],
                notification_id=nid,
                dry_run=self.is_dry_run,
            )
            logger.warning("skip (no free margin) %s: %s", action.summary, exc)
            log_event(
                "skip_no_margin",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=nid,
                will_retry=False,
            )
            notify_mac(
                "invo-agent · skipped",
                f"No free margin for {action.summary}",
                subtitle="Won't retry — wait for next signal",
                sound=None,
                enabled=self.settings.mac_notify,
                sticky=False,
            )
            return

        if isinstance(exc, SameCoinOpenError):
            self.db.mark_processed(
                nid, f"SKIP_SAME_COIN_{action.action.value}", str(exc)[:500]
            )
            self.db.log_action(
                action=f"SKIP_SAME_COIN_{action.action.value}",
                summary=f"{format_action_summary(action)}: {exc}"[:500],
                notification_id=nid,
                dry_run=self.is_dry_run,
            )
            logger.warning("skip (same coin open) %s: %s", action.summary, exc)
            log_event(
                "skip_same_coin",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=nid,
                will_retry=False,
            )
            notify_mac(
                "invo-agent · skipped",
                f"Already in that coin — {action.summary}",
                subtitle="One position per coin",
                sound=None,
                enabled=self.settings.mac_notify,
                sticky=False,
            )
            return

        if isinstance(exc, GhostCopyError):
            self.db.mark_processed(nid, "GHOST_CLEAR", str(exc)[:500])
            self.db.log_action(
                action="GHOST_CLEAR",
                summary=f"{format_action_summary(action)}: {exc}"[:500],
                notification_id=nid,
                dry_run=self.is_dry_run,
            )
            logger.info("ghost clear %s: %s", action.summary, exc)
            log_event(
                "ghost_clear_skip",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=nid,
            )
            return

        if isinstance(exc, SideMismatchError):
            self.db.mark_processed(
                nid, f"SKIP_SIDE_MISMATCH_{action.action.value}", str(exc)[:500]
            )
            self.db.log_action(
                action=f"SKIP_SIDE_MISMATCH_{action.action.value}",
                summary=f"{format_action_summary(action)}: {exc}"[:500],
                notification_id=nid,
                dry_run=self.is_dry_run,
            )
            logger.warning("skip (side mismatch) %s: %s", action.summary, exc)
            log_event(
                "skip_side_mismatch",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=nid,
                will_retry=False,
            )
            notify_mac(
                "invo-agent · skipped",
                f"Side mismatch — kept tracking {action.summary}",
                subtitle="CLOSE did not flatten",
                sound=None,
                enabled=self.settings.mac_notify,
                sticky=False,
            )
            return

        if self._is_transient_exec_error(exc):
            age = self._notification_age_seconds(action.notification)
            if action.action is ActionType.CLOSE and age < CLOSE_LIVE_RETRY_SECONDS:
                # Leave unprocessed: a fresh close is worth retrying at market.
                logger.warning(
                    "transient %s age=%.0fs — retry CLOSE next poll: %s",
                    action.summary,
                    age,
                    exc,
                )
                log_event_throttled(
                    "exec_transient",
                    key=nid,
                    action=action.action.value,
                    summary=action.summary,
                    error=str(exc)[:300],
                    notification_id=nid,
                    will_retry=True,
                )
                return
            logger.warning(
                "transient %s — not live-retrying (age=%.0fs): %s",
                action.summary,
                age,
                exc,
            )
            self.db.mark_processed(
                nid, f"ERROR_{action.action.value}", str(exc)[:500]
            )
            self.db.log_action(
                action=f"ERROR_{action.action.value}",
                summary=f"{format_action_summary(action)}: {exc}"[:500],
                notification_id=nid,
                dry_run=self.is_dry_run,
            )
            log_event(
                "exec_transient",
                action=action.action.value,
                summary=action.summary,
                error=str(exc)[:300],
                notification_id=nid,
                will_retry=False,
                age_seconds=age,
            )
            if action.action is ActionType.CLOSE:
                # Hand straight to recovery instead of waiting out the 5 min timer:
                # it still holds on HL and the trader is out.
                self._force_missed_close = True
                notify_mac(
                    "invo-agent · WARN",
                    f"CLOSE still stuck — recovery will price it: {action.summary}",
                    subtitle="Flatten or park",
                    sound=self.settings.mac_notify_sound or None,
                    enabled=self.settings.mac_notify,
                    sticky=False,
                )
            return

        if isinstance(exc, NotOpenCopyError):
            self.db.mark_processed(
                nid, f"SKIP_NOT_OPEN_{action.action.value}", str(exc)[:500]
            )
            self.db.log_action(
                action=f"SKIP_NOT_OPEN_{action.action.value}",
                summary=f"{format_action_summary(action)}: {exc}"[:500],
                notification_id=nid,
                dry_run=self.is_dry_run,
            )
            logger.info("skip (not our copy) %s: %s", action.summary, exc)
            log_event(
                "skip_not_open",
                action=action.action.value,
                summary=action.summary,
                error=str(exc),
                notification_id=nid,
                will_retry=False,
            )
            return

        # Mark processed so we don't retry the same trade every poll.
        self.db.mark_processed(nid, f"ERROR_{action.action.value}", str(exc)[:500])
        self.db.log_action(
            action=f"ERROR_{action.action.value}",
            summary=f"{format_action_summary(action)}: {exc}"[:500],
            notification_id=nid,
            dry_run=self.is_dry_run,
        )
        logger.exception("executor failed for %s", action.summary)
        if action.action is ActionType.CLOSE:
            # We still hold a trade the trader exited — let recovery decide now.
            self._force_missed_close = True
        notify_mac(
            "invo-agent · ERROR",
            f"{action.summary}: {exc}",
            subtitle=action.action.value,
            sound=self.settings.mac_notify_sound or None,
            enabled=self.settings.mac_notify,
            sticky=self.settings.mac_notify_sticky,
        )

    @staticmethod
    def _is_hl_rate_limit_error(exc: BaseException) -> bool:
        msg = str(exc).lower()
        return (
            "too many cumulative requests" in msg
            or "too many requests" in msg
        )

    def _note_hl_rate_limit(self, exc: BaseException) -> None:
        """Back off opportunistic queue work so closes keep their budget."""
        if not self._is_hl_rate_limit_error(exc):
            return
        until = time.monotonic() + HL_RATE_LIMIT_COOLDOWN_SECONDS
        if until <= self._hl_cooldown_until:
            return
        self._hl_cooldown_until = until
        logger.warning(
            "HL request cap hit — pausing queue drains for %.0fs",
            HL_RATE_LIMIT_COOLDOWN_SECONDS,
        )
        log_event_throttled(
            "hl_cooldown",
            key="hl_cooldown",
            seconds=HL_RATE_LIMIT_COOLDOWN_SECONDS,
            error=str(exc)[:300],
        )

    def _hl_cooling_down(self) -> bool:
        return time.monotonic() < self._hl_cooldown_until

    def _is_transient_exec_error(self, exc: BaseException) -> bool:
        """Worth retrying as-is: rate cap, unfilled IoC, network / gateway blips."""
        if self._is_hl_rate_limit_error(exc) or isinstance(exc, IocUnfilledError):
            return True
        if isinstance(exc, httpx.HTTPError):
            return True
        msg = str(exc).lower()
        return any(
            needle in msg
            for needle in (
                "timeout",
                "timed out",
                "temporarily",
                "connection reset",
                "connect error",
                "disconnected",
                "bad gateway",
                "service unavailable",
                "gateway time",
                " 502",
                " 503",
                " 504",
            )
        )

    def _tried_within(self, raw: str | None, seconds: float) -> bool:
        if not raw:
            return False
        try:
            last = datetime.fromisoformat(raw)
        except ValueError:
            return False
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        return (datetime.now(UTC) - last.astimezone(UTC)).total_seconds() < seconds

    @staticmethod
    def _is_insufficient_margin_error(exc: BaseException) -> bool:
        if isinstance(exc, InsufficientMarginError):
            return True
        if NotificationPoller._is_below_hl_min_error(exc):
            return False
        msg = str(exc).lower()
        return "insufficient margin" in msg or "free margin too small" in msg

    @staticmethod
    def _is_below_hl_min_error(exc: BaseException) -> bool:
        msg = str(exc).lower()
        return "minimum value of $10" in msg or "below_hl_min" in msg

    def _notify_dex_login_needed(self, exc: BaseException) -> None:
        if self._dex_dead_notified:
            return
        self._dex_dead_notified = True
        notify_mac(
            "invo-agent · DEX LOGIN NEEDED",
            "Turnkey session invalid and auto dex-login failed. "
            f"Check IMAP / run: invo-agent dex-login ({exc})",
            subtitle="Trading paused",
            sound=self.settings.mac_notify_sound or None,
            enabled=self.settings.mac_notify,
            sticky=self.settings.mac_notify_sticky,
        )

    @staticmethod
    def _is_dex_session_error(exc: BaseException) -> bool:
        if isinstance(exc, DexSessionError):
            return True
        msg = str(exc).lower()
        needles = (
            "api_key_expired",
            "expired api key",
            "public_key_not_found",
            "could not find public key",
            "unauthenticated",
        )
        if any(n in msg for n in needles):
            return True
        # Any Turnkey HTTP 401 on signing means the local API key is dead.
        return "turnkey" in msg and "401" in msg

    def _ensure_dex_session(self) -> None:
        """Cheap local TTL log only — never OTP on the trade path."""
        data = self.executor.dex_store.load()
        if data is None:
            return
        remaining = session_ttl_remaining_seconds(data)
        if remaining is not None and remaining <= 60:
            logger.info(
                "DEX key locally expired (remaining=%.0fs) — trade may 401 then recover",
                remaining,
            )

    def _maybe_proactive_dex_renew(self) -> None:
        """Refresh the Turnkey key before expiry, off the OPEN hot path.

        Uses the current API key (no OTP) like the website. OTP only if that
        key is already dead.
        """
        if self.is_dry_run:
            return
        data = self.executor.dex_store.load()
        if data is None:
            return
        if not session_should_proactive_renew(
            data, renew_before_seconds=self.settings.dex_renew_before_seconds
        ):
            return
        remaining = session_ttl_remaining_seconds(data)
        logger.info(
            "DEX key TTL remaining=%.0fs — background renew",
            remaining if remaining is not None else -1,
        )
        console.print("[yellow]DEX key near expiry — background refresh...[/yellow]")
        self._recover_dex_session()

    def _mark_dex_renewed(self, *, via: str, remaining: float | None) -> bool:
        self.executor.invalidate_trader()
        if remaining is not None and remaining < 120:
            logger.warning(
                "DEX key TTL after %s is only %.0fs — backing off renew",
                via,
                remaining,
            )
            self._dex_recover_after = time.monotonic() + DEX_RECOVER_COOLDOWN_SECONDS
        else:
            self._dex_recover_after = 0.0
        self._dex_dead_notified = False
        console.print(f"[green]DEX session renewed ({via}).[/green]")
        logger.info("DEX renew succeeded via=%s remaining_s=%s", via, remaining)
        log_event("dex_auto_login", ok=True, via=via, remaining_seconds=remaining)
        notify_mac(
            "invo-agent",
            f"DEX session renewed ({via})",
            sound=self.settings.mac_notify_sound or None,
            enabled=self.settings.mac_notify,
            sticky=self.settings.mac_notify_sticky,
        )
        return True

    def _recover_dex_session(self) -> bool:
        """Refresh via current API key; IMAP OTP only if that key is dead.

        Keeps the old key on disk until the new session is saved.
        """
        now = time.monotonic()
        if now < self._dex_recover_after:
            logger.info(
                "DEX renew cooling down (%.0fs left)",
                self._dex_recover_after - now,
            )
            return False

        store = DexSessionStore(self.settings.dex_session_path)
        try:
            console.print("[yellow]Refreshing DEX session (no OTP)...[/yellow]")
            logger.info("attempting DEX API-key refresh")
            session = refresh_dex_session(store, self.client)
            remaining = session_ttl_remaining_seconds(session)
            return self._mark_dex_renewed(via="api-key refresh", remaining=remaining)
        except Exception as refresh_exc:  # noqa: BLE001
            logger.info("DEX API-key refresh failed: %s", refresh_exc)
            log_event("dex_refresh", ok=False, error=str(refresh_exc))
            current = store.load()
            key_dead = refresh_failed_because_key_dead(refresh_exc) or (
                current is None or session_is_expired(current)
            )
            if not key_dead:
                self._dex_recover_after = now + DEX_RECOVER_COOLDOWN_SECONDS
                console.print(f"[red]DEX refresh failed:[/red] {refresh_exc}")
                return False

        if not self.settings.imap_configured:
            console.print("[red]DEX key dead and IMAP not configured — cannot OTP.[/red]")
            self._dex_recover_after = now + DEX_RECOVER_COOLDOWN_SECONDS
            return False

        console.print("[yellow]DEX key dead — auto dex-login via Turnkey OTP...[/yellow]")
        logger.info("attempting auto dex-login via IMAP")
        try:
            establish_dex_session(self.client, self.settings, store)
            new = store.load()
            remaining = session_ttl_remaining_seconds(new) if new else None
            return self._mark_dex_renewed(via="otp", remaining=remaining)
        except Exception as exc:  # noqa: BLE001
            self._dex_recover_after = now + DEX_RECOVER_COOLDOWN_SECONDS
            console.print(f"[red]Auto dex-login failed:[/red] {exc}")
            logger.error("auto dex-login failed: %s", exc)
            log_event("dex_auto_login", ok=False, via="otp", error=str(exc))
            return False

    def _intent_from_action(self, action: TradeAction, reason: str) -> PendingIntent | None:
        inv = action.investment
        if inv is None or inv.base_id is None:
            return None
        coin = (inv.ticker or inv.name or "").upper()
        if not coin:
            return None
        now = datetime.now(UTC).isoformat()
        return PendingIntent(
            investment_base_id=str(inv.base_id),
            coin=coin,
            side="long" if inv.direction_long else "short",
            reason=reason,
            created_at=now,
            updated_at=now,
            base_short_id=inv.base_short_id,
            notification_id=str(action.notification.id),
            investment_id=str(inv.id) if inv.id else None,
            leverage=float(inv.leverage) if inv.leverage is not None else None,
            trader_entry=float(inv.entry_price) if inv.entry_price else None,
            tp=float(inv.price_target) if inv.price_target else None,
            sl=float(inv.stop_loss) if inv.stop_loss else None,
            entry_size=float(inv.entry_size) if inv.entry_size is not None else None,
            portfolio_id=str(inv.portfolio.id)
            if inv.portfolio and inv.portfolio.id
            else (
                str(action.notification.portfolio_id)
                if action.notification.portfolio_id
                else None
            ),
            summary=action.summary,
            trader=trader_from_action(action),
        )

    def _refresh_pending_from_update(self, action: TradeAction) -> bool:
        inv = action.investment
        if inv is None or inv.base_id is None:
            return False
        # Already our live copy — do not treat this as a queued-open refresh.
        if self.db.get_open_copy(str(inv.base_id)) is not None:
            return False
        if inv.base_short_id and self.db.get_open_copy_by_short_id(inv.base_short_id):
            return False
        existing = self.db.get_pending_intent(str(inv.base_id))
        if existing is None and inv.base_short_id:
            for row in self.db.list_pending_intents():
                if row.base_short_id == inv.base_short_id:
                    existing = row
                    break
        if existing is None:
            return False
        refreshed = self._intent_from_action(action, existing.reason)
        if refreshed is None:
            return False
        refreshed.reason = existing.reason
        refreshed.entry_size = locked_queued_entry_size(
            existing.entry_size, refreshed.entry_size
        )
        entry_moved = (
            existing.trader_entry is not None
            and refreshed.trader_entry is not None
            and abs(float(existing.trader_entry) - float(refreshed.trader_entry))
            > max(1e-8, 1e-6 * abs(float(existing.trader_entry)))
        )
        if entry_moved and existing.resting_oid:
            self._cancel_pending_resting(existing)
            existing.resting_oid = None
            existing.resting_cloid = None
            existing.resting_px = None
            existing.resting_sz = None
            existing.resting_at = None
        refreshed.resting_oid = existing.resting_oid
        refreshed.resting_cloid = existing.resting_cloid
        refreshed.resting_px = existing.resting_px
        refreshed.resting_sz = existing.resting_sz
        refreshed.resting_at = existing.resting_at
        self.db.upsert_pending_intent(refreshed)
        log_event(
            "pending_refresh",
            base_id=refreshed.investment_base_id,
            trader_entry=refreshed.trader_entry,
            entry_size=refreshed.entry_size,
            entry_moved=entry_moved,
        )
        return True

    def _cancel_pending_for_action(self, action: TradeAction, *, reason: str) -> bool:
        inv = action.investment
        base_id = None
        if inv and inv.base_id:
            base_id = str(inv.base_id)
        elif action.notification.investment_base_id:
            base_id = str(action.notification.investment_base_id)
        if not base_id:
            return False
        existing = self.db.get_pending_intent(base_id)
        if existing is None and inv and inv.base_short_id:
            for row in self.db.list_pending_intents():
                if row.base_short_id == inv.base_short_id:
                    existing = row
                    base_id = row.investment_base_id
                    break
        if existing is None:
            return False
        self._cancel_pending_resting(existing)
        self.db.delete_pending_intent(base_id)
        logger.info("cancelled pending %s (%s)", existing.summary or base_id, reason)
        log_event(
            "pending_cancel",
            base_id=base_id,
            coin=existing.coin,
            reason=reason,
        )
        notify_mac(
            "invo-agent · queue",
            f"Cancelled pending {existing.coin} ({reason})",
            sound=None,
            enabled=self.settings.mac_notify,
            sticky=False,
        )
        return True

    def _pending_age_seconds(self, intent: PendingIntent) -> float:
        try:
            created = datetime.fromisoformat(intent.created_at)
        except ValueError:
            return 0.0
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return (datetime.now(UTC) - created.astimezone(UTC)).total_seconds()

    def _expire_pending(self) -> None:
        max_age = float(self.settings.pending_max_age_seconds)
        for intent in self.db.list_pending_intents():
            age = self._pending_age_seconds(intent)
            if age < max_age:
                continue
            still_open = False
            shadow_ok = False
            try:
                resp = self.client.get_current_shadow_post_by_investment_base_id(
                    intent.investment_base_id
                )
                inv = resp.post.update if resp.post else None
                shadow_ok = True
                if inv is not None and not self._investment_closed(inv):
                    still_open = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "pending expire shadow failed base_id=%s: %s",
                    intent.investment_base_id,
                    exc,
                )
            self._cancel_pending_resting(intent)
            self.db.delete_pending_intent(intent.investment_base_id)
            logger.warning(
                "expired pending %s age=%.0fs still_open=%s shadow_ok=%s",
                intent.summary or intent.investment_base_id,
                age,
                still_open,
                shadow_ok,
            )
            log_event(
                "pending_expire",
                base_id=intent.investment_base_id,
                coin=intent.coin,
                age_seconds=age,
                still_open=still_open,
                shadow_ok=shadow_ok,
            )
            notify_mac(
                "invo-agent · queue",
                f"Dropped stale pending {intent.coin} after {age / 3600:.1f}h",
                sound=None,
                enabled=self.settings.mac_notify,
                sticky=False,
            )

    def _refresh_account(self) -> None:
        raw = self.db.get_meta("hl_fills_since_ms")
        try:
            since = int(raw) if raw else int((time.time() - 3600) * 1000)
        except ValueError:
            since = int((time.time() - 3600) * 1000)
        try:
            self.executor.refresh_account(fills_since_ms=since)
        except Exception as exc:  # noqa: BLE001
            logger.warning("account snapshot failed: %s", exc)
            return
        self.db.set_meta("hl_fills_since_ms", str(int(time.time() * 1000) - 5000))

    def _supersede_batch(self, items: list) -> None:
        """One investment → last UPDATE; drop UPDATEs when a CLOSE is in the same poll."""
        by_base: dict[str, list] = {}
        for notification in items:
            nid = str(notification.id)
            if self.db.is_processed(nid):
                continue
            if notification.investment_base_id is None:
                continue
            if notification.action_type not in {ActionType.UPDATE, ActionType.CLOSE}:
                continue
            by_base.setdefault(str(notification.investment_base_id), []).append(
                notification
            )
        for group in by_base.values():
            group.sort(key=lambda n: n.created_at)
            has_close = any(n.action_type is ActionType.CLOSE for n in group)
            last_update = None
            for notification in group:
                if notification.action_type is ActionType.UPDATE:
                    last_update = notification
            for notification in group:
                if notification.action_type is not ActionType.UPDATE:
                    continue
                nid = str(notification.id)
                if has_close:
                    self.db.mark_processed(
                        nid, "SUPERSEDED_BY_CLOSE", notification.content
                    )
                    log_event(
                        "superseded",
                        notification_id=nid,
                        reason="close_in_batch",
                    )
                elif last_update is not None and notification.id != last_update.id:
                    self.db.mark_processed(
                        nid, "SUPERSEDED_UPDATE", notification.content
                    )
                    log_event(
                        "superseded",
                        notification_id=nid,
                        reason="older_update",
                    )

    def _cancel_pending_resting(self, intent: PendingIntent) -> None:
        if not intent.resting_oid:
            return
        try:
            self.executor.cancel_resting_oid(intent.coin, int(intent.resting_oid))
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "cancel resting %s oid=%s failed: %s",
                intent.coin,
                intent.resting_oid,
                exc,
            )
        self.db.clear_pending_resting(intent.investment_base_id)

    def _adopt_resting_fills(self) -> None:
        trader = getattr(self.executor, "_trader", None)
        book = getattr(trader, "_book", None) if trader is not None else None
        if book is None:
            return
        for intent in self.db.list_pending_intents():
            if not intent.resting_oid and not intent.resting_cloid:
                continue
            want_cloid = (intent.resting_cloid or "").lower().removeprefix("0x")
            matched = [
                fill
                for fill in book.fills
                if fill.coin == intent.coin.upper()
                and (
                    (want_cloid and fill.cloid == want_cloid)
                    or (
                        intent.resting_oid is not None
                        and fill.oid == intent.resting_oid
                    )
                )
            ]
            pos = book.position(intent.coin)
            if not matched and pos is None:
                continue
            try:
                resp = self.client.get_current_shadow_post_by_investment_base_id(
                    intent.investment_base_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "adopt shadow failed base_id=%s: %s",
                    intent.investment_base_id,
                    exc,
                )
                continue
            inv = resp.post.update if resp.post else None
            if inv is None:
                continue
            if matched:
                filled = sum(fill.sz for fill in matched)
                notional = sum(fill.sz * fill.px for fill in matched)
                px = notional / filled if filled else float(intent.resting_px or 0)
            else:
                filled = float(pos.sz) if pos else 0.0
                px = float(pos.entry_px) if pos and pos.entry_px else float(
                    intent.resting_px or 0
                )
            copy = self.db.get_open_copy(intent.investment_base_id)
            booked = float(copy.size) if copy and copy.size else 0.0
            hl_sz = float(pos.sz) if pos else 0.0
            others = self.executor._other_open_copies_for_coin(
                intent.coin, intent.investment_base_id
            )
            held, keep_pending = plan_resting_adopt(
                posted_sz=float(intent.resting_sz or 0),
                filled_sz=filled,
                booked_sz=booked,
                hl_sz=hl_sz,
                only_copy=not others,
            )
            if held <= 0 or px <= 0:
                continue
            is_long = intent.side.lower() == "long"
            if copy is None or held + 1e-9 > booked:
                try:
                    self.executor.adopt_open_fill(
                        inv=inv,
                        coin=intent.coin,
                        is_long=is_long,
                        sz=held,
                        px=px,
                        leverage=int(intent.leverage or 1),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("adopt fill failed %s: %s", intent.coin, exc)
                    continue
            if keep_pending:
                # Keep the original posted size — shrinking it to leftover made
                # cumulative fills look complete (0.6 of 0.5 leftover).
                self.db.set_pending_resting(
                    intent.investment_base_id,
                    oid=intent.resting_oid,
                    cloid=intent.resting_cloid,
                    px=intent.resting_px,
                    sz=float(intent.resting_sz or held),
                )
                logger.info(
                    "resting OPEN %s filled %s/%s — keep leftover GTC",
                    intent.summary or intent.coin,
                    held,
                    intent.resting_sz,
                )
                continue
            action = self._action_from_pending(intent, inv)
            self.db.delete_pending_intent(intent.investment_base_id)
            self.db.delete_pending_for_coin(
                intent.coin, exclude_base_id=intent.investment_base_id
            )
            self.engine.remember(action)
            self.db.log_action(
                action="OPEN",
                summary=action.summary,
                notification_id=intent.notification_id,
                dry_run=False,
            )
            self._notify_action(action)
            logger.info("adopted resting OPEN %s sz=%s px=%s", action.summary, held, px)
            log_event(
                "pending_fill",
                via="resting",
                base_id=intent.investment_base_id,
                coin=intent.coin,
                sz=held,
                px=px,
            )

    def _reconcile_inverse_ghosts(self) -> None:
        try:
            coins = self.executor.untracked_hl_coins()
        except Exception as exc:  # noqa: BLE001
            logger.warning("inverse ghost scan failed: %s", exc)
            return
        trader = getattr(self.executor, "_trader", None)
        book = getattr(trader, "_book", None) if trader is not None else None
        for coin in coins:
            pending = self.db.get_pending_for_coin(coin)
            pos = book.position(coin) if book is not None else None
            if pending is not None and pos is not None:
                logger.info(
                    "inverse ghost %s: adopting pending %s",
                    coin,
                    pending.base_short_id or pending.investment_base_id,
                )
                try:
                    resp = self.client.get_current_shadow_post_by_investment_base_id(
                        pending.investment_base_id
                    )
                    inv = resp.post.update if resp.post else None
                    if inv is None:
                        raise RuntimeError("no shadow post")
                    self.executor.adopt_open_fill(
                        inv=inv,
                        coin=coin,
                        is_long=pos.is_long,
                        sz=pos.sz,
                        px=float(pos.entry_px or pending.trader_entry or 0),
                        leverage=int(pos.leverage or pending.leverage or 1),
                    )
                    action = self._action_from_pending(pending, inv)
                    self.db.delete_pending_intent(pending.investment_base_id)
                    self.engine.remember(action)
                    self.db.log_action(
                        action="OPEN",
                        summary=f"{action.summary} (adopted)",
                        notification_id=pending.notification_id,
                        dry_run=False,
                    )
                    continue
                except Exception as exc:  # noqa: BLE001
                    logger.warning("inverse ghost adopt failed %s: %s", coin, exc)
            logger.error(
                "HL holds %s with no open_copy — not flattening (external?)",
                coin,
            )
            log_event("inverse_ghost", coin=coin, pending=pending is not None)
            notify_mac(
                "invo-agent · WARN",
                f"HL holds {coin} with no tracked copy",
                subtitle="Not flattening",
                sound=self.settings.mac_notify_sound or None,
                enabled=self.settings.mac_notify,
                sticky=self.settings.mac_notify_sticky,
            )

    def _queued_fallback_seconds(self, reason: str | None) -> float:
        if (reason or "") == "price":
            return QUEUED_PRICE_FALLBACK_SECONDS
        return QUEUED_MARGIN_FALLBACK_SECONDS

    def _ensure_book_snapshot(self) -> None:
        try:
            self.executor._ensure_trader().refresh_book()
        except Exception as exc:  # noqa: BLE001
            logger.warning("book snapshot failed: %s", exc)

    def _coin_is_free(self, coin: str) -> bool:
        want = coin.upper()
        return not any((c.ticker or "").upper() == want for c in self.db.list_open_copies())

    def _should_attempt_queued(
        self, *, reason: str | None, last_tried_at: str | None, local_ok: bool
    ) -> bool:
        budget = self.executor.budget()
        if budget is not None and not budget.can_spend(1, essential=False):
            return False
        if local_ok:
            # Fillable per the cached book — go, but not on every 4s poll.
            return not self._tried_within(last_tried_at, QUEUED_LOCAL_MIN_GAP_SECONDS)
        return not self._tried_within(
            last_tried_at, self._queued_fallback_seconds(reason)
        )

    def _need_margin_usd(
        self,
        *,
        entry_size: float | None,
        leverage: float | None,
        leftover_notional: float | None = None,
    ) -> float | None:
        """Cash this clip must lock — trader % after the $10 notional bump."""
        lev = max(1, int(leverage or 1))
        hl_min = float(self.settings.hl_min_order_usd)
        min_margin = hl_min / lev
        if leftover_notional is not None and leftover_notional > 0:
            return max(float(leftover_notional) / lev, min_margin)
        trader = getattr(self.executor, "_trader", None)
        equity = trader.cached_equity() if trader is not None else None
        if equity is None or equity <= 0:
            return min_margin
        margin, _ = self.executor._resolve_margin(
            equity=float(equity), leverage=lev, entry_size=entry_size
        )
        return max(float(margin), min_margin)

    def _can_spend_opportunistic(self) -> bool | None:
        budget = self.executor.budget()
        if budget is None:
            return None
        return budget.can_spend(1, essential=False)

    def _live_pending_reason(self, intent: PendingIntent) -> str:
        if intent.resting_oid:
            # Own GTC still working — a partial fill already in open_copies
            # must not rebrand this row as same_coin and hide the leftover.
            return intent.reason or "price"
        trader = getattr(self.executor, "_trader", None)
        return classify_pending_wait(
            coin_free=self._coin_is_free(intent.coin),
            mid=trader.cached_mid(intent.coin) if trader is not None else None,
            trader_entry=intent.trader_entry,
            is_long=intent.side.lower() == "long",
            available=trader.cached_available() if trader is not None else None,
            leverage=float(intent.leverage or 1),
            hl_min_order_usd=self.settings.hl_min_order_usd,
            need_margin=self._need_margin_usd(
                entry_size=intent.entry_size, leverage=intent.leverage
            ),
            can_spend=self._can_spend_opportunistic(),
            prior=intent.reason,
        )

    def _live_pending_add_reason(self, copy: OpenCopy) -> str:
        trader = getattr(self.executor, "_trader", None)
        coin = (copy.ticker or "").upper()
        leftover = float(copy.pending_add_notional or 0)
        return classify_pending_wait(
            coin_free=True,
            mid=trader.cached_mid(coin) if trader is not None and coin else None,
            trader_entry=copy.pending_add_entry,
            is_long=(copy.side or "long").lower() == "long",
            available=trader.cached_available() if trader is not None else None,
            leverage=float(copy.leverage or 1),
            hl_min_order_usd=self.settings.hl_min_order_usd,
            need_margin=self._need_margin_usd(
                entry_size=None,
                leverage=copy.leverage,
                leftover_notional=leftover,
            ),
            can_spend=self._can_spend_opportunistic(),
            prior=copy.pending_add_reason,
        )

    def _refresh_pending_reasons(self) -> None:
        """Rewrite stale queue tokens from the current book (margin vs price vs ratelimit)."""
        for intent in self.db.list_pending_intents():
            new = self._live_pending_reason(intent)
            if new == intent.reason:
                continue
            logger.info(
                "pending reason %s %s → %s",
                intent.coin,
                intent.reason,
                new,
            )
            intent.reason = new
            self.db.upsert_pending_intent(intent)
        for copy in self.db.list_open_copies():
            add = float(copy.pending_add_notional or 0)
            if add <= 0:
                continue
            new = self._live_pending_add_reason(copy)
            if new == (copy.pending_add_reason or ""):
                continue
            logger.info(
                "pending add reason %s %s → %s",
                copy.ticker,
                copy.pending_add_reason,
                new,
            )
            self.db.set_pending_add_notional(
                copy.investment_base_id,
                add,
                reason=new,
                entry=copy.pending_add_entry,
            )

    def _pending_open_local_ok(self, intent: PendingIntent) -> bool:
        if intent.resting_oid:
            return False
        trader = getattr(self.executor, "_trader", None)
        why = (intent.reason or "").lower()
        if why in {"ratelimit", "budget"}:
            budget = self.executor.budget()
            return bool(budget and budget.can_spend(1, essential=False))
        if why == "price":
            # A resting GTC waits on the book — we only need free margin + a free coin.
            why = "margin"
        return queued_ready_locally(
            reason=why,
            mid=trader.cached_mid(intent.coin) if trader is not None else None,
            available=trader.cached_available() if trader is not None else None,
            trader_entry=intent.trader_entry,
            is_long=intent.side.lower() == "long",
            hl_min_order_usd=self.settings.hl_min_order_usd,
            leverage=float(intent.leverage or 1),
            coin_free=self._coin_is_free(intent.coin),
            need_margin=self._need_margin_usd(
                entry_size=intent.entry_size, leverage=intent.leverage
            ),
        )

    def _pending_add_local_ok(self, copy: OpenCopy) -> bool:
        trader = getattr(self.executor, "_trader", None)
        coin = (copy.ticker or "").upper()
        why = (copy.pending_add_reason or "margin").lower()
        if why in {"ratelimit", "budget"}:
            budget = self.executor.budget()
            return bool(budget and budget.can_spend(1, essential=False))
        leftover = float(copy.pending_add_notional or 0)
        return queued_ready_locally(
            reason=why,
            mid=trader.cached_mid(coin) if trader is not None and coin else None,
            available=trader.cached_available() if trader is not None else None,
            trader_entry=copy.pending_add_entry,
            is_long=(copy.side or "long").lower() == "long",
            leftover_notional=leftover,
            hl_min_order_usd=self.settings.hl_min_order_usd,
            leverage=float(copy.leverage or 1),
            need_margin=self._need_margin_usd(
                entry_size=None,
                leverage=copy.leverage,
                leftover_notional=leftover,
            ),
        )

    def _drain_pending(self) -> None:
        intents = self.db.list_pending_intents()
        if not intents:
            return
        if self._hl_cooling_down():
            logger.info("skip pending drain — HL cooldown active")
            return
        self._ensure_book_snapshot()
        candidates = [
            intent
            for intent in intents
            if self._should_attempt_queued(
                reason=intent.reason,
                last_tried_at=intent.last_tried_at,
                local_ok=self._pending_open_local_ok(intent),
            )
        ]
        # Least-recently-tried first so the per-poll cap can't starve a row.
        candidates.sort(key=lambda i: i.last_tried_at or "")
        for intent in candidates[:QUEUED_MAX_OPENS_PER_POLL]:
            self.db.mark_pending_tried(intent.investment_base_id)
            try:
                resp = self.client.get_current_shadow_post_by_investment_base_id(
                    intent.investment_base_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "pending drain shadow failed base_id=%s: %s",
                    intent.investment_base_id,
                    exc,
                )
                continue
            inv = resp.post.update if resp.post else None
            if inv is None:
                continue
            owner = inv.owner.username if inv.owner and inv.owner.username else None
            if owner and intent.trader != owner:
                intent.trader = owner
                self.db.upsert_pending_intent(intent)
            if self._investment_closed(inv):
                self._cancel_pending_resting(intent)
                self.db.delete_pending_intent(intent.investment_base_id)
                log_event(
                    "pending_cancel",
                    base_id=intent.investment_base_id,
                    coin=intent.coin,
                    reason="trader_closed_on_drain",
                )
                continue
            if intent.resting_oid:
                continue
            action = self._action_from_pending(intent, inv)
            try:
                placed = self.executor.place_resting_open(action)
            except Exception as exc:  # noqa: BLE001
                if self._is_dex_session_error(exc):
                    self.executor.invalidate_trader()
                    if self._recover_dex_session():
                        try:
                            placed = self.executor.place_resting_open(action)
                        except Exception as retry_exc:  # noqa: BLE001
                            if self._apply_drain_failure(intent, action, retry_exc):
                                if self._is_insufficient_margin_error(retry_exc):
                                    break
                            continue
                    else:
                        break
                elif self._apply_drain_failure(intent, action, exc):
                    if self._is_insufficient_margin_error(exc):
                        break
                    continue
                else:
                    logger.exception(
                        "pending drain failed %s: %s",
                        intent.summary or intent.investment_base_id,
                        exc,
                    )
                    continue
            filled_sz = placed.get("filled_sz")
            posted_sz = float(placed.get("sz") or 0)
            oid = placed.get("oid")
            if filled_sz and float(filled_sz) > 0:
                try:
                    self.executor.adopt_open_fill(
                        inv=inv,
                        coin=intent.coin,
                        is_long=intent.side.lower() == "long",
                        sz=float(filled_sz),
                        px=float(placed.get("filled_px") or placed.get("px") or 0),
                        leverage=int(intent.leverage or 1),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("immediate rest fill adopt failed: %s", exc)
                leftover = max(0.0, posted_sz - float(filled_sz))
                if leftover > 1e-9 and oid is not None:
                    self.db.set_pending_resting(
                        intent.investment_base_id,
                        oid=int(oid),
                        cloid=placed.get("cloid"),
                        px=placed.get("px"),
                        sz=posted_sz,
                    )
                    logger.info(
                        "resting OPEN %s partial fill sz=%s/%s oid=%s — keep leftover",
                        action.summary,
                        filled_sz,
                        posted_sz,
                        oid,
                    )
                    continue
                self.db.delete_pending_intent(intent.investment_base_id)
                self.db.delete_pending_for_coin(
                    intent.coin, exclude_base_id=intent.investment_base_id
                )
                self.engine.remember(action)
                self.db.log_action(
                    action="OPEN",
                    summary=action.summary,
                    notification_id=intent.notification_id,
                    dry_run=False,
                )
                self._notify_action(action)
                logger.info("drained pending OPEN %s (immediate fill)", action.summary)
                continue
            oid = placed.get("oid")
            if oid is None:
                logger.warning(
                    "resting OPEN %s placed but no oid — will retry",
                    intent.summary or intent.coin,
                )
                continue
            self.db.set_pending_resting(
                intent.investment_base_id,
                oid=int(oid),
                cloid=placed.get("cloid"),
                px=placed.get("px"),
                sz=placed.get("sz"),
            )
            logger.info(
                "resting OPEN %s oid=%s px=%s sz=%s",
                action.summary,
                oid,
                placed.get("px"),
                placed.get("sz"),
            )
            log_event(
                "pending_resting",
                base_id=intent.investment_base_id,
                coin=intent.coin,
                oid=oid,
                px=placed.get("px"),
                sz=placed.get("sz"),
            )

    def _pending_add_age_seconds(self, copy: OpenCopy) -> float:
        raw = copy.pending_add_at
        if not raw:
            return 0.0
        try:
            created = datetime.fromisoformat(raw)
        except ValueError:
            return 0.0
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        return (datetime.now(UTC) - created.astimezone(UTC)).total_seconds()

    def _expire_pending_adds(self) -> None:
        """Size-adds stay queued while the copy is live. Kept as a no-op."""
        return

    def _drain_pending_adds(self) -> None:
        waiting = [
            copy
            for copy in self.db.list_open_copies()
            if float(copy.pending_add_notional or 0) > 0
        ]
        if not waiting:
            return
        if self._hl_cooling_down():
            logger.info("skip size-add drain — HL cooldown active")
            return
        self._ensure_book_snapshot()
        candidates = [
            copy
            for copy in waiting
            if self._should_attempt_queued(
                reason=copy.pending_add_reason,
                last_tried_at=copy.pending_add_tried_at,
                local_ok=self._pending_add_local_ok(copy),
            )
        ]
        candidates.sort(key=lambda c: c.pending_add_tried_at or "")
        for copy in candidates[:QUEUED_MAX_ADDS_PER_POLL]:
            self.db.mark_pending_add_tried(copy.investment_base_id)
            try:
                resp = self.client.get_current_shadow_post_by_investment_base_id(
                    copy.investment_base_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "pending size-add shadow failed base_id=%s: %s",
                    copy.investment_base_id,
                    exc,
                )
                continue
            inv = resp.post.update if resp.post else None
            if inv is None or self._investment_closed(inv):
                self.db.set_pending_add_notional(copy.investment_base_id, 0.0)
                log_event(
                    "pending_add_cancel",
                    base_id=copy.investment_base_id,
                    coin=copy.ticker,
                    reason="trader_closed_on_drain",
                )
                continue
            action = self._action_from_pending_add(copy, inv)
            try:
                self.executor.handle(action)
            except Exception as exc:  # noqa: BLE001
                self._note_hl_rate_limit(exc)
                if self._is_dex_session_error(exc):
                    self.executor.invalidate_trader()
                    if self._recover_dex_session():
                        try:
                            self.executor.handle(action)
                        except Exception as retry_exc:  # noqa: BLE001
                            self._note_hl_rate_limit(retry_exc)
                            if self._is_insufficient_margin_error(retry_exc):
                                break
                            continue
                    else:
                        break
                elif self._is_insufficient_margin_error(exc):
                    break
                elif isinstance(exc, NotOpenCopyError):
                    self.db.set_pending_add_notional(copy.investment_base_id, 0.0)
                    continue
                else:
                    logger.exception(
                        "pending size-add drain failed %s: %s",
                        copy.ticker or copy.investment_base_id,
                        exc,
                    )
                    continue
            still = self.db.get_open_copy(copy.investment_base_id)
            leftover = float(still.pending_add_notional or 0) if still else 0.0
            if leftover > 0:
                continue
            self.db.log_action(
                action="UPDATE",
                summary=format_action_summary(action),
                notification_id=copy.last_notification_id,
                dry_run=False,
            )
            self._notify_action(action)
            logger.info("drained pending size-add %s", action.summary)
            log_event(
                "pending_add_fill",
                base_id=copy.investment_base_id,
                coin=copy.ticker,
                trader_entry=inv.entry_price,
            )

    def _action_from_pending_add(
        self, copy: OpenCopy, inv: InvestmentUpdate
    ) -> TradeAction:
        try:
            nid = (
                UUID(copy.last_notification_id)
                if copy.last_notification_id
                else UUID(int=0)
            )
        except ValueError:
            nid = UUID(int=0)
        try:
            created = datetime.fromisoformat(copy.pending_add_at or copy.opened_at)
        except ValueError:
            created = datetime.now(UTC)
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        coin = (copy.ticker or inv.ticker or "?").upper()
        notification = Notification.model_validate(
            {
                "id": str(nid),
                "content": f"queued UPDATE {coin} size +${copy.pending_add_notional:.2f}",
                "notificationType": "user_updated_investment",
                "createdAt": created.isoformat(),
                "investmentBaseId": copy.investment_base_id,
                "notifier": {"username": copy.trader} if copy.trader else None,
            }
        )
        return TradeAction(
            action=ActionType.UPDATE,
            notification=notification,
            investment=inv,
            reason="pending_add_drain",
        )

    def _apply_drain_failure(
        self, intent: PendingIntent, action: TradeAction, exc: BaseException
    ) -> bool:
        """Update pending reason. Return True if this was an expected queueable miss."""
        self._note_hl_rate_limit(exc)
        reason = self._queueable_open_reason(action, exc)
        if reason is None:
            return False
        refreshed = self._intent_from_action(action, reason)
        if refreshed is not None:
            refreshed.entry_size = locked_queued_entry_size(
                intent.entry_size, refreshed.entry_size
            )
            self.db.upsert_pending_intent(refreshed)
        else:
            intent.reason = reason
            self.db.upsert_pending_intent(intent)
        log_event_throttled(
            "pending_still_waiting",
            key=f"{intent.investment_base_id}:{reason}",
            base_id=intent.investment_base_id,
            coin=intent.coin,
            reason=reason,
            error=str(exc),
        )
        return True

    def _action_from_pending(
        self, intent: PendingIntent, inv: InvestmentUpdate
    ) -> TradeAction:
        try:
            nid = UUID(intent.notification_id) if intent.notification_id else UUID(int=0)
        except ValueError:
            nid = UUID(int=0)
        try:
            created = datetime.fromisoformat(intent.created_at)
        except ValueError:
            created = datetime.now(UTC)
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        notification = Notification.model_validate(
            {
                "id": str(nid),
                "content": intent.summary or f"queued OPEN {intent.coin}",
                "notificationType": "user_added_investment",
                "createdAt": created.isoformat(),
                "investmentBaseId": intent.investment_base_id,
                "notifier": {"username": intent.trader} if intent.trader else None,
            }
        )
        locked = locked_queued_entry_size(intent.entry_size, inv.entry_size)
        if locked != inv.entry_size:
            inv = inv.model_copy(update={"entry_size": locked})
        return TradeAction(
            action=ActionType.OPEN,
            notification=notification,
            investment=inv,
        )

    def _sleep_interval(self) -> None:
        base = float(self.settings.poll_interval_seconds)
        jitter = max(0.0, float(self.settings.poll_jitter_seconds))
        delay = base + random.uniform(-jitter, jitter) if jitter else base
        time.sleep(max(0.5, delay))

    def _recover_auth(self) -> bool:
        """Try refresh, then IMAP OTP login. Returns True if recovered."""
        try:
            self.client.refresh_tokens()
            console.print("[green]Session refreshed.[/green]")
            logger.info("session refreshed")
            notify_mac(
                "invo-agent",
                "Session refreshed",
                sound=None,
                enabled=self.settings.mac_notify,
                sticky=self.settings.mac_notify_sticky,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]Refresh failed:[/yellow] {exc}")
            logger.warning("refresh failed: %s", exc)

        if not self.settings.imap_configured or not self.settings.invo_email:
            console.print("[red]Cannot auto-relogin — configure IMAP + INVO_EMAIL.[/red]")
            return False

        console.print("[yellow]Attempting IMAP auto-login...[/yellow]")
        try:
            login_with_otp(
                self.client,
                self.session_store,
                self.settings,
                email=self.settings.invo_email,
            )
            console.print("[green]Re-logged in via IMAP.[/green]")
            logger.info("re-logged in via IMAP")
            notify_mac(
                "invo-agent",
                "Re-logged in via IMAP",
                sound=self.settings.mac_notify_sound or None,
                enabled=self.settings.mac_notify,
                sticky=self.settings.mac_notify_sticky,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            console.print(f"[red]Auto-login failed:[/red] {exc}")
            logger.error("auto-login failed: %s", exc)
            notify_mac(
                "invo-agent · ERROR",
                f"Auto-login failed: {exc}",
                sound=self.settings.mac_notify_sound or None,
                enabled=self.settings.mac_notify,
                sticky=self.settings.mac_notify_sticky,
            )
            return False

    def _maybe_reconcile_missed_closes(self) -> None:
        now = time.monotonic()
        forced = self._force_missed_close
        if not forced and self._last_missed_close and now - self._last_missed_close < 300:
            return
        self._force_missed_close = False
        self._last_missed_close = now
        try:
            self.reconcile_missed_closes()
        except Exception as exc:  # noqa: BLE001
            logger.warning("missed-close reconcile failed: %s", exc)

    def _maybe_prune_logs(self) -> None:
        now = time.monotonic()
        if self._last_log_prune and now - self._last_log_prune < 3600:
            return
        self._last_log_prune = now
        try:
            stats = prune_logs(self.settings.log_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("log prune failed: %s", exc)
            return
        if stats.get("deleted") or stats.get("trimmed"):
            logger.info(
                "log prune deleted=%s trimmed=%s",
                stats.get("deleted"),
                stats.get("trimmed"),
            )

    def run_forever(self, *, stop_fn: Callable[[], bool] | None = None) -> None:
        mode = "LIVE" if self.settings.live and not self.settings.dry_run else "DRY-RUN"
        allow = ", ".join(sorted(self.settings.portfolio_ids)[:2]) or "all"
        console.print(
            f"[bold]invo-agent[/bold] polling every "
            f"{self.settings.poll_interval_seconds}s ([cyan]{mode}[/cyan]) "
            f"portfolios={allow}"
        )
        logger.info("polling start mode=%s interval=%s", mode, self.settings.poll_interval_seconds)
        log_event(
            "poller_start",
            mode=mode,
            interval=self.settings.poll_interval_seconds,
            position_pct=self.settings.position_pct,
            live=self.settings.live,
            dry_run=self.settings.dry_run,
            portfolios=sorted(self.settings.portfolio_ids),
        )
        notify_mac(
            "invo-agent",
            f"Started ({mode}) — watching portfolios",
            sound=None,
            enabled=self.settings.mac_notify,
            sticky=self.settings.mac_notify_sticky,
        )
        self.ensure_catchup()
        self.reconcile_missed_closes()
        self._last_missed_close = time.monotonic()

        while True:
            if stop_fn and stop_fn():
                break
            try:
                actions = self.poll_once()
                self._maybe_proactive_dex_renew()
                self._maybe_prune_logs()
                self._maybe_reconcile_missed_closes()
                self._transient_failures = 0
                if actions:
                    self._idle_polls = 0
                    for action in actions:
                        style = {
                            "OPEN": "green",
                            "UPDATE": "yellow",
                            "CLOSE": "red",
                        }.get(action.action.value, "white")
                        console.print(
                            f"[{style}]{action.summary}[/{style}] — "
                            f"{action.notification.content}"
                        )
                        if action.investment:
                            inv = action.investment
                            console.print(
                                f"  entry={inv.entry_price} tp={inv.price_target} "
                                f"sl={inv.stop_loss} lev={inv.leverage} "
                                f"shortId={inv.base_short_id}"
                            )
                        if action.reason:
                            console.print(f"  [dim]{action.reason}[/dim]")
                else:
                    self._idle_polls += 1
                    if self._idle_polls == 1 or self._idle_polls % HEARTBEAT_EVERY == 0:
                        console.print(
                            f"[dim]{datetime.now().strftime('%H:%M:%S')} "
                            f"watching… (no new trades)[/dim]"
                        )
            except InvolioAPIError as exc:
                if self._is_transient_api_error(exc):
                    self._handle_transient(exc, kind="API")
                    self._sleep_interval()
                    continue
                console.print(f"[red]API error:[/red] {exc}")
                logger.error("API error: %s", exc)
                notify_mac(
                    "invo-agent · ERROR",
                    str(exc),
                    subtitle="API",
                    sound=self.settings.mac_notify_sound or None,
                    enabled=self.settings.mac_notify,
                    sticky=self.settings.mac_notify_sticky,
                )
                if exc.status_code in (401, 403):
                    if not self._recover_auth():
                        console.print("[red]Auth recovery failed; sleeping 30s[/red]")
                        time.sleep(30)
                        continue
                else:
                    self._sleep_interval()
                    continue
            except httpx.HTTPError as exc:
                self._handle_transient(exc, kind="Poller")
                self._sleep_interval()
                continue
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]poll error:[/red] {exc}")
                logger.exception("poll error")
                notify_mac(
                    "invo-agent · ERROR",
                    str(exc),
                    subtitle="Poller",
                    sound=self.settings.mac_notify_sound or None,
                    enabled=self.settings.mac_notify,
                    sticky=self.settings.mac_notify_sticky,
                )
            self._sleep_interval()

    @staticmethod
    def _is_transient_api_error(exc: InvolioAPIError) -> bool:
        if exc.status_code in (408, 425, 429, 500, 502, 503, 504):
            return True
        msg = str(exc).lower()
        return any(
            needle in msg
            for needle in (
                "disconnected",
                "timeout",
                "temporarily",
                "connection reset",
                "connect error",
            )
        )

    def _handle_transient(self, exc: BaseException, *, kind: str) -> None:
        self._transient_failures += 1
        n = self._transient_failures
        console.print(f"[yellow]transient {kind.lower()} blip ({n}):[/yellow] {exc}")
        logger.warning("transient %s (%s): %s", kind, n, exc)
        if n >= TRANSIENT_NOTIFY_AFTER and n % TRANSIENT_NOTIFY_AFTER == 0:
            notify_mac(
                "invo-agent · WARN",
                f"{n}x network blips: {exc}",
                subtitle=kind,
                sound=None,
                enabled=self.settings.mac_notify,
                sticky=False,
            )
