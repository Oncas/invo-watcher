from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
from rich.console import Console

from invo_agent.api.client import InvolioAPIError, InvolioClient
from invo_agent.api.models import ActionType, InvestmentUpdate, TradeAction
from invo_agent.auth.email_otp import login_with_otp
from invo_agent.auth.session import SessionStore
from invo_agent.config import Settings
from invo_agent.db import AgentDB, OpenCopy
from invo_agent.dex.executor import DexExecutor, RecoveryResult
from invo_agent.dex.hl_trader import InsufficientMarginError, SameCoinOpenError
from invo_agent.dex.session import (
    DexSessionError,
    DexSessionStore,
    establish_dex_session,
    session_is_expired,
)
from invo_agent.engine import CopyEngine
from invo_agent.events import log_event
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
            try:
                self.executor.handle(action)
            except Exception as exc:  # noqa: BLE001
                if self._handle_live_failure(nid, action, exc):
                    continue
            self.engine.remember(action)
            self.db.log_action(
                action=action.action.value,
                summary=action.summary,
                notification_id=nid,
                dry_run=self.is_dry_run,
            )
            self._notify_action(action)
            logger.info("%s %s", action.action.value, action.summary)
            actions.append(action)
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
                    self._record_executor_error(nid, action, retry_exc)
                    return True
                return False  # retry succeeded — fall through to remember
            logger.warning(
                "DEX session error for %s (will retry): %s", action.summary, exc
            )
            self._notify_dex_login_needed(exc)
            return True
        self._record_executor_error(nid, action, exc)
        return True

    def _record_executor_error(
        self, nid: str, action: TradeAction, exc: BaseException
    ) -> None:
        # No free margin: skip permanently. A later retry after other closes
        # can be a stale/expired move — only act on fresh signals.
        if self._is_insufficient_margin_error(exc):
            self.db.mark_processed(
                nid, f"SKIP_NO_MARGIN_{action.action.value}", str(exc)[:500]
            )
            self.db.log_action(
                action=f"SKIP_NO_MARGIN_{action.action.value}",
                summary=f"{action.summary}: {exc}"[:500],
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

        # Already mimicking this coin via another trader — one HL position per coin.
        if isinstance(exc, SameCoinOpenError):
            self.db.mark_processed(
                nid, f"SKIP_SAME_COIN_{action.action.value}", str(exc)[:500]
            )
            self.db.log_action(
                action=f"SKIP_SAME_COIN_{action.action.value}",
                summary=f"{action.summary}: {exc}"[:500],
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

        # Mark processed so we don't retry the same trade every poll.
        self.db.mark_processed(nid, f"ERROR_{action.action.value}", str(exc)[:500])
        self.db.log_action(
            action=f"ERROR_{action.action.value}",
            summary=f"{action.summary}: {exc}"[:500],
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
        """Proactively refresh Turnkey session if local expiry is known."""
        store = self.executor.dex_store
        data = store.load()
        if data is None or not session_is_expired(data):
            return
        console.print("[yellow]DEX session expired — auto dex-login...[/yellow]")
        logger.warning("DEX session expired locally; attempting auto dex-login")
        self._recover_dex_session()

    def _recover_dex_session(self) -> bool:
        """IMAP Turnkey OTP → new DEX API key. Rate-limited."""
        now = time.monotonic()
        if now < self._dex_recover_after:
            logger.info(
                "DEX auto-login cooling down (%.0fs left)",
                self._dex_recover_after - now,
            )
            return False

        if not self.settings.imap_configured:
            console.print("[red]Cannot auto dex-login — configure IMAP.[/red]")
            self._dex_recover_after = now + DEX_RECOVER_COOLDOWN_SECONDS
            return False

        self.executor.invalidate_trader()
        # Drop the dead key so we never keep signing with a revoked pubkey.
        DexSessionStore(self.settings.dex_session_path).clear()
        console.print("[yellow]Auto dex-login via Turnkey OTP email...[/yellow]")
        logger.info("attempting auto dex-login via IMAP")
        try:
            establish_dex_session(
                self.client,
                self.settings,
                DexSessionStore(self.settings.dex_session_path),
            )
            self._dex_recover_after = 0.0
            self._dex_dead_notified = False
            console.print("[green]DEX session renewed.[/green]")
            logger.info("auto dex-login succeeded")
            log_event("dex_auto_login", ok=True)
            notify_mac(
                "invo-agent",
                "DEX session renewed (auto dex-login)",
                sound=self.settings.mac_notify_sound or None,
                enabled=self.settings.mac_notify,
                sticky=self.settings.mac_notify_sticky,
            )
            return True
        except Exception as exc:  # noqa: BLE001
            self._dex_recover_after = now + DEX_RECOVER_COOLDOWN_SECONDS
            console.print(f"[red]Auto dex-login failed:[/red] {exc}")
            logger.error("auto dex-login failed: %s", exc)
            log_event("dex_auto_login", ok=False, error=str(exc))
            return False

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
