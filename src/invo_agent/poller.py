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
from invo_agent.dex.executor import DexExecutor, RecoveryResult
from invo_agent.dex.hl_trader import (
    InsufficientMarginError,
    IocUnfilledError,
    NotOpenCopyError,
    PriceNotBetterError,
    SameCoinOpenError,
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
from invo_agent.events import log_event
from invo_agent.labels import (
    format_action_summary,
    merge_update_tags,
    tags_from_summary,
    trader_from_action,
)
from invo_agent.notify import notify_mac

console = Console()
logger = logging.getLogger(__name__)

CATCHUP_META = "catchup_completed_at"
HEARTBEAT_EVERY = 8  # polls between "still alive" lines when idle
# Don't hammer Turnkey OTP if auto dex-login keeps failing.
DEX_RECOVER_COOLDOWN_SECONDS = 10 * 60
# Transient network / 5xx: log quietly until this many in a row.
TRANSIENT_NOTIFY_AFTER = 5


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

    @property
    def is_dry_run(self) -> bool:
        return bool(self.settings.dry_run) or not bool(self.settings.live)

    def needs_catchup(self) -> bool:
        return self.db.get_meta(CATCHUP_META) is None

    def catch_up(self, *, pages: int = 3) -> int:
        """Mark notifications already in the feed as seen — do not execute them.

        Called on every process start so downtime gaps are not replayed as live
        trades (e.g. a 6‑minute‑old OPEN sitting in the inbox).
        """
        marked = 0
        for page in range(1, pages + 1):
            resp = self.client.get_notifications(page=page)
            if not resp.items:
                break
            for notification in resp.items:
                nid = str(notification.id)
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
                            sound="Basso",
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
                        sound="Basso",
                        enabled=self.settings.mac_notify,
                        sticky=self.settings.mac_notify_sticky,
                    )
                    continue
            if result is not None:
                results.append(result)

        closed_n = sum(1 for r in results if r.status in {"closed", "already_flat"})
        parked_n = sum(1 for r in results if r.status == "parked")
        log_event(
            "missed_close_reconcile_done",
            checked=len(open_copies),
            results=len(results),
            closed=closed_n,
            parked=parked_n,
        )
        if results:
            summary = "; ".join(r.message for r in results[:5])
            if len(results) > 5:
                summary += f" (+{len(results) - 5} more)"
            notify_mac(
                "invo-agent · RECOVERY",
                summary,
                sound=self.settings.mac_notify_sound or None,
                enabled=self.settings.mac_notify,
                sticky=self.settings.mac_notify_sticky,
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

    def _logged_summary(self, action: TradeAction) -> str:
        summary = format_action_summary(action)
        if action.action is not ActionType.UPDATE or tags_from_summary(summary):
            return summary
        extra = merge_update_tags(getattr(self.executor, "last_update_tags", None))
        if not extra:
            return summary
        return f"{summary} · {' · '.join(extra)}"

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
        body = action.summary
        if action.notification.content:
            body = f"{action.summary} — {action.notification.content}"
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
        items = sorted(resp.items, key=lambda n: n.created_at)
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
                if created <= self._watch_started_at:
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

            action = self.engine.enrich(notification)
            if not self.is_dry_run:
                self._ensure_dex_session()

            if action.action is ActionType.CLOSE:
                self._cancel_pending_for_action(action, reason="trader_close")
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
            self._expire_pending()
            self._drain_pending()
            self._expire_pending_adds()
            self._drain_pending_adds()
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
        self._record_executor_error(nid, action, exc)
        return True

    def _queueable_open_reason(self, action: TradeAction, exc: BaseException) -> str | None:
        if action.action is not ActionType.OPEN:
            return None
        if isinstance(exc, PriceNotBetterError) or isinstance(exc, IocUnfilledError):
            return "price"
        if isinstance(exc, SameCoinOpenError):
            return "same_coin"
        if self._is_insufficient_margin_error(exc):
            return "margin"
        return None

    def _queue_open_from_error(
        self, nid: str, action: TradeAction, exc: BaseException
    ) -> bool:
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

    def _record_executor_error(
        self, nid: str, action: TradeAction, exc: BaseException
    ) -> None:
        if self._is_insufficient_margin_error(exc):
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
        notify_mac(
            "invo-agent · ERROR",
            f"{action.summary}: {exc}",
            subtitle=action.action.value,
            sound="Basso",
            enabled=self.settings.mac_notify,
            sticky=self.settings.mac_notify_sticky,
        )

    @staticmethod
    def _is_insufficient_margin_error(exc: BaseException) -> bool:
        if isinstance(exc, InsufficientMarginError):
            return True
        msg = str(exc).lower()
        return (
            "insufficient margin" in msg
            or "minimum value of $10" in msg
            or "free margin too small" in msg
        )

    def _notify_dex_login_needed(self, exc: BaseException) -> None:
        if self._dex_dead_notified:
            return
        self._dex_dead_notified = True
        notify_mac(
            "invo-agent · DEX LOGIN NEEDED",
            "Turnkey session invalid and auto dex-login failed. "
            f"Check IMAP / run: invo-agent dex-login ({exc})",
            subtitle="Trading paused",
            sound="Basso",
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
        self.db.upsert_pending_intent(refreshed)
        log_event(
            "pending_refresh",
            base_id=refreshed.investment_base_id,
            trader_entry=refreshed.trader_entry,
            entry_size=refreshed.entry_size,
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
            self.db.delete_pending_intent(intent.investment_base_id)
            logger.warning(
                "expired pending %s age=%.0fs",
                intent.summary or intent.investment_base_id,
                age,
            )
            log_event(
                "pending_expire",
                base_id=intent.investment_base_id,
                coin=intent.coin,
                age_seconds=age,
            )
            notify_mac(
                "invo-agent · queue",
                f"Dropped stale pending {intent.coin} after {age / 3600:.1f}h",
                sound=None,
                enabled=self.settings.mac_notify,
                sticky=False,
            )

    def _drain_pending(self) -> None:
        intents = self.db.list_pending_intents()
        if not intents:
            return
        for intent in intents:
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
                self.db.delete_pending_intent(intent.investment_base_id)
                log_event(
                    "pending_cancel",
                    base_id=intent.investment_base_id,
                    coin=intent.coin,
                    reason="trader_closed_on_drain",
                )
                continue
            action = self._action_from_pending(intent, inv)
            try:
                self.executor.handle(action)
            except Exception as exc:  # noqa: BLE001
                if self._is_dex_session_error(exc):
                    self.executor.invalidate_trader()
                    if self._recover_dex_session():
                        try:
                            self.executor.handle(action)
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
            logger.info("drained pending OPEN %s", action.summary)
            log_event(
                "pending_fill",
                base_id=intent.investment_base_id,
                coin=intent.coin,
                trader_entry=inv.entry_price,
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
        max_age = float(self.settings.pending_max_age_seconds)
        for copy in self.db.list_open_copies():
            if float(copy.pending_add_notional or 0) <= 0:
                continue
            age = self._pending_add_age_seconds(copy)
            if age < max_age:
                continue
            self.db.set_pending_add_notional(copy.investment_base_id, 0.0)
            coin = (copy.ticker or "?").upper()
            logger.warning(
                "expired pending size-add %s %s age=%.0fs",
                coin,
                copy.base_short_id or copy.investment_base_id,
                age,
            )
            log_event(
                "pending_add_expire",
                base_id=copy.investment_base_id,
                coin=coin,
                age_seconds=age,
            )
            notify_mac(
                "invo-agent · queue",
                f"Dropped stale size-add {coin} after {age / 3600:.1f}h",
                sound=None,
                enabled=self.settings.mac_notify,
                sticky=False,
            )

    def _drain_pending_adds(self) -> None:
        waiting = [
            copy
            for copy in self.db.list_open_copies()
            if float(copy.pending_add_notional or 0) > 0
        ]
        if not waiting:
            return
        for copy in waiting:
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
                if self._is_dex_session_error(exc):
                    self.executor.invalidate_trader()
                    if self._recover_dex_session():
                        try:
                            self.executor.handle(action)
                        except Exception as retry_exc:  # noqa: BLE001
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
        )

    def _apply_drain_failure(
        self, intent: PendingIntent, action: TradeAction, exc: BaseException
    ) -> bool:
        """Update pending reason. Return True if this was an expected queueable miss."""
        reason = self._queueable_open_reason(action, exc)
        if reason is None:
            return False
        refreshed = self._intent_from_action(action, reason)
        if refreshed is not None:
            self.db.upsert_pending_intent(refreshed)
        else:
            intent.reason = reason
            self.db.upsert_pending_intent(intent)
        log_event(
            "pending_still_waiting",
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
                sound="Basso",
                enabled=self.settings.mac_notify,
                sticky=self.settings.mac_notify_sticky,
            )
            return False

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

        while True:
            if stop_fn and stop_fn():
                break
            try:
                actions = self.poll_once()
                self._maybe_proactive_dex_renew()
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
                    sound="Basso",
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
                    sound="Basso",
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
