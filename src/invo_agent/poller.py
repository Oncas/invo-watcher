from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime

from rich.console import Console

from invo_agent.api.client import InvolioAPIError, InvolioClient
from invo_agent.api.models import ActionType, TradeAction
from invo_agent.auth.email_otp import login_with_otp
from invo_agent.auth.session import SessionStore
from invo_agent.config import Settings
from invo_agent.db import AgentDB
from invo_agent.dex.executor import DexExecutor
from invo_agent.engine import CopyEngine
from invo_agent.events import log_event
from invo_agent.notify import notify_mac

console = Console()
logger = logging.getLogger(__name__)

CATCHUP_META = "catchup_completed_at"
HEARTBEAT_EVERY = 8  # polls between "still alive" lines when idle


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
        self.executor = executor or DexExecutor(settings, client=client)
        self.session_store = session_store or client.session
        self._idle_polls = 0
        # Only act on notifications created after this timestamp (set on catch-up).
        self._watch_started_at: datetime | None = None

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
            try:
                self.executor.handle(action)
            except Exception as exc:  # noqa: BLE001
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

        while True:
            if stop_fn and stop_fn():
                break
            try:
                actions = self.poll_once()
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
                    time.sleep(self.settings.poll_interval_seconds)
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
            time.sleep(self.settings.poll_interval_seconds)
