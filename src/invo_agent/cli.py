from __future__ import annotations

from datetime import UTC, datetime

import typer
from rich.console import Console
from rich.table import Table

from invo_agent import service as launchd_service
from invo_agent.api.client import InvolioClient
from invo_agent.auth.email_otp import LoginError, login_with_otp
from invo_agent.auth.session import SessionStore
from invo_agent.config import Settings, get_settings
from invo_agent.db import AgentDB
from invo_agent.dex.executor import DexExecutor
from invo_agent.dex.hl_trader import HlTrader
from invo_agent.dex.session import (
    DexSessionError,
    DexSessionStore,
    establish_dex_session,
    load_signer,
    refresh_dex_session,
    session_ttl_remaining_seconds,
)
from invo_agent.hud import format_age, pending_kind, pending_last_tried_age
from invo_agent.logging_setup import setup_logging
from invo_agent.notify import ensure_alerter, find_alerter, notify_mac
from invo_agent.poller import CATCHUP_META, NotificationPoller

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Involio copy-trade agent")
console = Console()


def _bootstrap(*, with_logging: bool = False) -> tuple[Settings, SessionStore, AgentDB]:
    settings = get_settings()
    if with_logging:
        log_path = setup_logging(settings.log_dir, level=settings.log_level)
        console.print(f"[dim]log: {log_path}[/dim]")
    session = SessionStore(settings.session_path)
    db = AgentDB(settings.db_path)
    return settings, session, db


@app.command()
def login(
    email: str | None = typer.Option(None, help="Involio account email"),
    code: str | None = typer.Option(None, help="OTP from email (skips IMAP/prompt wait)"),
) -> None:
    """Start email OTP login and persist session tokens."""
    settings, session, db = _bootstrap()
    db.close()
    address = email or settings.invo_email
    if not address:
        raise typer.BadParameter("Pass --email or set INVO_EMAIL in .env")

    if settings.imap_configured and not code:
        console.print(
            f"[dim]IMAP enabled for {settings.imap_user or address} "
            f"via {settings.imap_host}[/dim]"
        )
    elif not code:
        console.print(
            "[dim]IMAP not configured — you'll paste the OTP manually. "
            "See README Gmail App Password steps.[/dim]"
        )

    with InvolioClient(settings, session) as client:
        try:
            data = login_with_otp(
                client,
                session,
                settings,
                email=address,
                code=code,
                code_prompt=lambda: typer.prompt("Enter email OTP code"),
            )
        except LoginError as exc:
            console.print(f"[red]Login failed:[/red] {exc}")
            raise typer.Exit(1) from exc

    console.print(
        f"[green]Logged in[/green] as [bold]{data.username or data.email}[/bold] "
        f"(userId={data.user_id})"
    )
    console.print(f"Session: {settings.session_path}")
    notify_mac(
        "invo-agent",
        f"Logged in as {data.username or data.email}",
        subtitle="Login",
        sound=settings.mac_notify_sound or None,
        enabled=settings.mac_notify,
        sticky=settings.mac_notify_sticky,
    )


@app.command("imap-check")
def imap_check_cmd() -> None:
    """Verify Gmail IMAP credentials from .env (no Involio login)."""
    from invo_agent.auth.inbox import InboxError, check_imap

    settings = get_settings()
    try:
        msg = check_imap(settings)
    except InboxError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]{msg}[/green]")


@app.command("status")
def status_cmd() -> None:
    """Show session, poll metadata, and tracked open copies."""
    settings, session, db = _bootstrap()
    data = session.load()

    table = Table(title="invo-agent status")
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("session_path", str(settings.session_path))
    table.add_row("db_path", str(settings.db_path))
    table.add_row("log_dir", str(settings.log_dir))
    table.add_row("dry_run", str(settings.dry_run))
    table.add_row("live", str(settings.live))
    table.add_row("mac_notify", str(settings.mac_notify))
    table.add_row("mac_notify_sticky", str(settings.mac_notify_sticky))
    table.add_row("hud_token", "set" if settings.hud_token else "(disabled)")
    table.add_row("hud_sync_url", settings.hud_sync_url or "(local trader)")
    table.add_row("alerter", find_alerter() or "(missing — will auto-download)")
    table.add_row("poll_interval_seconds", str(settings.poll_interval_seconds))
    table.add_row("position_pct", str(settings.position_pct))
    allow = ", ".join(sorted(settings.portfolio_ids)) or "(all trade notifs)"
    table.add_row("portfolio_allowlist", allow)
    table.add_row("catchup_completed_at", db.get_meta(CATCHUP_META) or "(not yet)")
    table.add_row(
        "launchd",
        "loaded" if launchd_service.launchd_running() else "not loaded",
    )

    if data is None:
        table.add_row("logged_in", "no")
    else:
        table.add_row("logged_in", "yes")
        table.add_row("email", data.email)
        table.add_row("username", data.username or "")
        table.add_row("user_id", str(data.user_id or ""))
        table.add_row("session_updated_at", data.updated_at.isoformat())

    table.add_row("last_poll_at", db.get_meta("last_poll_at") or "-")
    table.add_row("last_unseen_count", db.get_meta("last_unseen_count") or "-")
    table.add_row("processed_notifications", str(db.processed_count()))
    console.print(table)

    copies = db.list_open_copies()
    if copies:
        ct = Table(title="Open copies (tracked)")
        ct.add_column("Ticker")
        ct.add_column("Trader")
        ct.add_column("Side")
        ct.add_column("Entry")
        ct.add_column("Recovery")
        ct.add_column("Base short id")
        ct.add_column("Opened at")
        for c in copies:
            recovery = "-"
            if c.recovery_pending:
                recovery = f"TP={c.recovery_tp} SL={c.recovery_sl}"
            ct.add_row(
                c.ticker or "?",
                f"@{c.trader}" if c.trader else "?",
                c.side or "?",
                f"{c.our_entry_px:g}" if c.our_entry_px is not None else "?",
                recovery,
                c.base_short_id or "?",
                c.opened_at,
            )
        console.print(ct)
    else:
        console.print("[dim]No open copies tracked yet.[/dim]")

    pending = db.list_pending_intents()
    if pending:
        now = datetime.now(UTC)
        tried_ages = []
        for intent in pending:
            if not intent.last_tried_at:
                continue
            try:
                last = datetime.fromisoformat(intent.last_tried_at)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=UTC)
                tried_ages.append((now - last.astimezone(UTC)).total_seconds())
            except ValueError:
                continue
        title = "Queued (entry-or-better / margin / coin)"
        if tried_ages:
            title += f" · tried {format_age(min(tried_ages))} ago"
        pt = Table(title=title)
        pt.add_column("Kind")
        pt.add_column("Coin")
        pt.add_column("Trader")
        pt.add_column("Side")
        pt.add_column("Trader entry")
        pt.add_column("Reason")
        pt.add_column("Queued")
        pt.add_column("Summary")
        for intent in pending:
            try:
                created = datetime.fromisoformat(intent.created_at)
                if created.tzinfo is None:
                    created = created.replace(tzinfo=UTC)
                age = format_age((now - created.astimezone(UTC)).total_seconds())
            except ValueError:
                age = "?"
            pt.add_row(
                "OPEN",
                intent.coin,
                f"@{intent.trader}" if intent.trader else "?",
                intent.side,
                f"{intent.trader_entry:g}" if intent.trader_entry is not None else "?",
                intent.reason,
                age,
                (intent.summary or "")[:48],
            )
        console.print(pt)

    recent = db.recent_actions(10)
    if recent:
        lt = Table(title="Recent actions")
        lt.add_column("When")
        lt.add_column("Action")
        lt.add_column("Summary")
        for row in recent:
            lt.add_row(row.created_at, row.action, row.summary or "")
        console.print(lt)

    db.close()


@app.command("dex-login")
def dex_login_cmd(
    code: str | None = typer.Option(None, help="DEX trading OTP from email"),
) -> None:
    """Create a Turnkey trading session via Involio DEX email OTP (IMAP if configured)."""
    settings, session, db = _bootstrap()
    db.close()
    if session.load() is None:
        console.print("[red]Not logged in to Involio.[/red] Run: invo-agent login")
        raise typer.Exit(1)

    dex_store = DexSessionStore(settings.dex_session_path)
    with InvolioClient(settings, session) as client:
        try:
            data = establish_dex_session(
                client,
                settings,
                dex_store,
                code=code,
                code_prompt=lambda: typer.prompt("Enter DEX trading OTP code"),
            )
        except (DexSessionError, Exception) as exc:
            console.print(f"[red]DEX login failed:[/red] {exc}")
            body = getattr(exc, "body", None)
            if body is not None:
                console.print(f"[dim]{body}[/dim]")
            raise typer.Exit(1) from exc

    console.print(
        f"[green]DEX session ready[/green] address={data.master_address} "
        f"org={data.organization_id}"
    )
    console.print(f"Saved: {settings.dex_session_path}")
    notify_mac(
        "invo-agent",
        f"DEX ready · {data.master_address[:10]}…",
        subtitle="DEX login",
        sound=settings.mac_notify_sound or None,
        enabled=settings.mac_notify,
        sticky=settings.mac_notify_sticky,
    )


@app.command("dex-refresh")
def dex_refresh_cmd() -> None:
    """Mint a new Turnkey API key with the current key (no email OTP)."""
    settings, session, db = _bootstrap()
    db.close()
    if session.load() is None:
        console.print("[red]Not logged in to Involio.[/red] Run: invo-agent login")
        raise typer.Exit(1)

    dex_store = DexSessionStore(settings.dex_session_path)
    if dex_store.load() is None:
        console.print("[yellow]No DEX session.[/yellow] Run: invo-agent dex-login")
        raise typer.Exit(1)

    with InvolioClient(settings, session) as client:
        try:
            data = refresh_dex_session(dex_store, client)
        except (DexSessionError, Exception) as exc:
            console.print(f"[red]DEX refresh failed:[/red] {exc}")
            console.print("[dim]If the key is already dead, run: invo-agent dex-login[/dim]")
            raise typer.Exit(1) from exc

    remaining = session_ttl_remaining_seconds(data)
    remaining_txt = (
        f"{remaining:.0f}s" if remaining is not None else "unknown"
    )
    console.print(
        f"[green]DEX session refreshed[/green] api_key_id={data.api_key_id} "
        f"remaining={remaining_txt}"
    )


@app.command("dex-status")
def dex_status_cmd() -> None:
    """Show DEX/Turnkey session and Hyperliquid equity."""
    settings, _session, db = _bootstrap()
    db.close()
    dex_store = DexSessionStore(settings.dex_session_path)
    data = dex_store.load()
    if data is None:
        console.print("[yellow]No DEX session.[/yellow] Run: invo-agent dex-login")
        raise typer.Exit(1)

    table = Table(title="DEX session")
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("path", str(settings.dex_session_path))
    table.add_row("master_address", data.master_address)
    table.add_row("organization_id", data.organization_id)
    table.add_row("api_key_id", data.api_key_id or "")
    table.add_row("expires_at_ms", str(data.expires_at_ms or ""))
    if data.expires_at_ms:
        from datetime import UTC, datetime

        exp = datetime.fromtimestamp(data.expires_at_ms / 1000, tz=UTC)
        remaining_h = (exp - datetime.now(UTC)).total_seconds() / 3600
        table.add_row("expires_at", exp.isoformat())
        table.add_row(
            "expires_in",
            f"{remaining_h:.1f}h" if remaining_h > 0 else "EXPIRED — run dex-login",
        )
    table.add_row("updated_at", data.updated_at.isoformat())
    console.print(table)

    try:
        _data, signer = load_signer(dex_store)
        trader = HlTrader(signer, data.master_address)
        equity = trader.account_value()
        console.print(f"[green]Hyperliquid equity:[/green] ${equity:.4f}")
        trader.close()
        signer.close()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Could not read HL equity:[/yellow] {exc}")
        if "expired" in str(exc).lower() or "API_KEY" in str(exc):
            console.print("[red]Run:[/red] invo-agent dex-login")


@app.command("reset-state")
def reset_state_cmd(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
) -> None:
    """Clear processed notifications / open copies and re-run catch-up next start."""
    settings, _session, db = _bootstrap()
    if not yes:
        confirm = typer.confirm(
            f"Reset local state at {settings.db_path}? (session login is kept)"
        )
        if not confirm:
            raise typer.Abort()
    db.reset_runtime_state()
    db.close()
    console.print(
        f"[green]Cleared[/green] {settings.db_path}. "
        "Next dry-run/once will catch up without replaying history."
    )


def _run_poll_loop(*, force_dry_run: bool) -> None:
    settings, session, db = _bootstrap(with_logging=True)
    if force_dry_run:
        settings = settings.model_copy(update={"dry_run": True, "live": False})

    if session.load() is None:
        console.print("[red]Not logged in.[/red] Run: invo-agent login")
        db.close()
        raise typer.Exit(1)

    with InvolioClient(settings, session) as client:
        try:
            client.refresh_tokens()
            console.print("[dim]session refreshed[/dim]")
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]refresh failed, will try IMAP if needed:[/yellow] {exc}")
            if settings.imap_configured and settings.invo_email:
                try:
                    login_with_otp(client, session, settings, email=settings.invo_email)
                except Exception as login_exc:  # noqa: BLE001
                    console.print(f"[red]login failed:[/red] {login_exc}")
                    db.close()
                    raise typer.Exit(1) from login_exc

        executor = DexExecutor(settings, client=client, db=db)
        if settings.live and not settings.dry_run:
            dex_store = DexSessionStore(settings.dex_session_path)
            if dex_store.load() is None:
                console.print("[red]LIVE mode needs DEX session.[/red] Run: invo-agent dex-login")
                db.close()
                raise typer.Exit(1)
            console.print(
                f"[bold red]LIVE TRADING ENABLED[/bold red] "
                f"position_pct={settings.position_pct}%"
            )

        if settings.hud_token:
            from invo_agent.server import start_hud_server

            start_hud_server(settings)
            console.print(
                f"[dim]hud API {settings.hud_bind}:{settings.hud_port}[/dim]"
            )

        poller = NotificationPoller(client, db, settings, executor, session_store=session)
        try:
            poller.run_forever()
        except KeyboardInterrupt:
            console.print("\n[dim]stopped[/dim]")
        finally:
            db.close()


@app.command("dry-run")
def dry_run_cmd() -> None:
    """Poll notifications and log intended OPEN/UPDATE/CLOSE actions (no trades)."""
    _run_poll_loop(force_dry_run=True)


@app.command()
def run() -> None:
    """Run the agent using DRY_RUN/LIVE from .env."""
    settings = get_settings()
    if settings.live and not settings.dry_run:
        console.print(
            "[yellow]Warning:[/yellow] LIVE=true will place real Hyperliquid orders."
        )
    _run_poll_loop(force_dry_run=False)


@app.command("once")
def once_cmd(
    skip_catchup: bool = typer.Option(
        False,
        "--skip-catchup",
        help="Debug: process inbox items without startup catch-up (can replay old trades)",
    ),
) -> None:
    """Single dry-run poll cycle (useful for testing)."""
    settings, session, db = _bootstrap(with_logging=True)
    settings = settings.model_copy(update={"dry_run": True, "live": False})
    if session.load() is None:
        console.print("[red]Not logged in.[/red] Run: invo-agent login")
        db.close()
        raise typer.Exit(1)

    with InvolioClient(settings, session) as client:
        poller = NotificationPoller(
            client,
            db,
            settings,
            DexExecutor(settings, client=client, db=db),
            session_store=session,
        )
        if skip_catchup:
            poller._watch_started_at = datetime.min.replace(tzinfo=UTC)  # noqa: SLF001
        else:
            poller.ensure_catchup(force=True)
        poller.reconcile_missed_closes()
        actions = poller.poll_once()
        if not actions:
            console.print("[dim]No new trade notifications.[/dim]")
        for action in actions:
            console.print(f"[bold]{action.summary}[/bold] — {action.notification.content}")
            if action.investment:
                inv = action.investment
                console.print(
                    f"  entry={inv.entry_price} tp={inv.price_target} sl={inv.stop_loss} "
                    f"lev={inv.leverage} shortId={inv.base_short_id}"
                )
    db.close()


@app.command("notify-test")
def notify_test_cmd(
    message: str = typer.Option("Hello from invo-agent", "--message", "-m"),
) -> None:
    """Fire a sample sticky macOS alert (stays until Dismiss)."""
    settings = get_settings()
    path = ensure_alerter(download=True)
    if path:
        console.print(f"[dim]alerter: {path}[/dim]")
    else:
        console.print(
            "[yellow]alerter missing[/yellow] — banner may auto-dismiss. "
            "Install: brew install vjeantet/tap/alerter"
        )
    notify_mac(
        "invo-agent",
        message,
        subtitle="notify-test",
        sound=settings.mac_notify_sound or None,
        enabled=True,
        sticky=settings.mac_notify_sticky,
    )
    console.print(
        "[green]Notification sent.[/green] "
        "Sticky alerts stay until you click Dismiss."
    )


@app.command("install-service")
def install_service_cmd(
    mode: str = typer.Option(
        "dry-run",
        "--mode",
        help="launchd command: dry-run (safe) or run (honors LIVE/DRY_RUN in .env)",
    ),
) -> None:
    """Install a launchd agent that keeps invo-agent running in the background."""
    if mode not in ("dry-run", "run"):
        raise typer.BadParameter("mode must be dry-run or run")
    settings = get_settings()
    try:
        path = launchd_service.install_launchd(mode=mode, log_dir=settings.log_dir)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]install failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]Installed[/green] {path}")
    console.print(f"Mode: {mode} · logs: {settings.log_dir}")
    console.print("Unload anytime: invo-agent uninstall-service")
    notify_mac(
        "invo-agent",
        f"launchd installed ({mode})",
        subtitle="Service",
        sound=settings.mac_notify_sound or None,
        enabled=settings.mac_notify,
        sticky=settings.mac_notify_sticky,
    )


@app.command("hud-sync")
def hud_sync_cmd() -> None:
    """Poll the remote HUD API and write local hud.json + log tails (Mac sidecar)."""
    from invo_agent.hud_sync import run_forever

    settings = get_settings()
    if not settings.hud_sync_url or not settings.hud_token:
        console.print("[red]Set HUD_SYNC_URL and HUD_TOKEN in .env[/red]")
        raise typer.Exit(1)
    console.print(f"[dim]hud-sync → {settings.hud_sync_url}[/dim]")
    try:
        run_forever(settings)
    except KeyboardInterrupt:
        console.print("\n[dim]hud-sync stopped[/dim]")


@app.command("install-hud-sync")
def install_hud_sync_cmd() -> None:
    """Install a launchd agent that polls the VPS HUD API every 1s."""
    settings = get_settings()
    if not settings.hud_sync_url or not settings.hud_token:
        console.print("[red]Set HUD_SYNC_URL and HUD_TOKEN in .env first.[/red]")
        raise typer.Exit(1)
    try:
        path = launchd_service.install_hud_launchd(log_dir=settings.log_dir)
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]install failed:[/red] {exc}")
        raise typer.Exit(1) from exc
    console.print(f"[green]Installed[/green] {path}")
    console.print(f"Polling {settings.hud_sync_url} · logs: {settings.log_dir}")
    console.print("Unload anytime: invo-agent uninstall-hud-sync")
    notify_mac(
        "invo-agent",
        "hud-sync launchd installed",
        subtitle="Service",
        sound=settings.mac_notify_sound or None,
        enabled=settings.mac_notify,
        sticky=settings.mac_notify_sticky,
    )


@app.command("uninstall-hud-sync")
def uninstall_hud_sync_cmd() -> None:
    """Unload and remove the hud-sync launchd agent."""
    settings = get_settings()
    path = launchd_service.uninstall_hud_launchd()
    if path:
        console.print(f"[green]Removed[/green] {path}")
    else:
        console.print("[dim]No hud-sync plist found (launchctl remove attempted).[/dim]")
    notify_mac(
        "invo-agent",
        "hud-sync uninstalled",
        subtitle="Service",
        sound=None,
        enabled=settings.mac_notify,
        sticky=settings.mac_notify_sticky,
    )


@app.command("uninstall-service")
def uninstall_service_cmd() -> None:
    """Unload and remove the launchd agent."""
    settings = get_settings()
    path = launchd_service.uninstall_launchd()
    if path:
        console.print(f"[green]Removed[/green] {path}")
    else:
        console.print("[dim]No plist found (launchctl remove attempted).[/dim]")
    notify_mac(
        "invo-agent",
        "launchd uninstalled",
        subtitle="Service",
        sound=None,
        enabled=settings.mac_notify,
        sticky=settings.mac_notify_sticky,
    )


@app.command("watch")
def watch_cmd(
    interval: float = typer.Option(2.0, "--interval", "-i", help="Refresh seconds"),
) -> None:
    """Live terminal HUD — easiest way to see background agent health (no menu-bar app)."""
    import time as _time

    from invo_agent.hud import (
        collect_hud,
        format_pnl,
        format_pnl_24h,
        format_roe,
        hud_file_age_seconds,
        load_hud_file,
        write_hud_file,
    )

    settings = get_settings()
    remote = bool(settings.hud_sync_url)
    console.print(
        "[dim]Watching invo-agent (Ctrl+C to stop). "
        + (
            "Reading local hud.json from hud-sync."
            if remote
            else "PnL refreshes at most every 5s."
        )
        + "[/dim]\n"
    )
    try:
        while True:
            if remote:
                snap = load_hud_file(settings)
                if snap is None:
                    console.clear()
                    console.print("[red]No hud.json yet — is hud-sync running?[/red]")
                    _time.sleep(interval)
                    continue
            else:
                snap = collect_hud(settings, with_pnl=True)
                write_hud_file(settings, snap)
            console.clear()
            sidecar_age = hud_file_age_seconds(settings) if remote else None
            sidecar_ok = sidecar_age is not None and sidecar_age < 5
            ok = snap.healthy and (sidecar_ok if remote else True)
            health = "[green]ok[/green]" if ok else "[red]problem[/red]"
            console.print(f"[bold]invo-agent[/bold]  {snap.mode}  {health}")
            age = (
                f"{snap.poll_age_seconds:.0f}s ago"
                if snap.poll_age_seconds is not None
                else "never"
            )
            eq = ""
            if snap.equity is not None:
                eq = f"  equity=${snap.equity:.2f}"
                if snap.pnl_24h is not None:
                    day = format_pnl_24h(snap.pnl_24h, snap.pnl_24h_pct)
                    style = "green" if snap.pnl_24h >= 0 else "red"
                    eq += f"  [{style}]{day}[/{style}]"
            console.print(f"poll {age}{eq}")

            if snap.positions:
                pt = Table(title="Open trades")
                pt.add_column("Coin")
                pt.add_column("Side")
                pt.add_column("Lev")
                pt.add_column("uPnL")
                pt.add_column("ROE")
                pt.add_column("Open for")
                for p in snap.positions:
                    style = "green" if p.unrealized_pnl >= 0 else "red"
                    pt.add_row(
                        p.coin,
                        p.side,
                        f"{p.leverage}x",
                        f"[{style}]{format_pnl(p.unrealized_pnl)}[/{style}]",
                        f"[{style}]{format_roe(p.return_on_equity)}[/{style}]",
                        format_age(getattr(p, "open_age_s", None)),
                    )
                console.print(pt)
            else:
                console.print("[dim]No open HL positions[/dim]")

            pending = getattr(snap, "pending", None) or []
            if pending:
                tried = pending_last_tried_age(pending)
                q_title = "Queued"
                if tried is not None:
                    q_title += f" · tried {format_age(tried)} ago"
                qt = Table(title=q_title)
                qt.add_column("Kind")
                qt.add_column("Coin")
                qt.add_column("Trader")
                qt.add_column("Reason")
                qt.add_column("Queued")
                for item in pending:
                    qt.add_row(
                        pending_kind(kind=item.get("kind")),
                        str(item.get("coin") or "?"),
                        f"@{item['trader']}" if item.get("trader") else "?",
                        str(item.get("reason") or "?"),
                        format_age(item.get("age_s")),
                    )
                console.print(qt)

            if snap.recent:
                table = Table(title="Recent actions")
                table.add_column("When")
                table.add_column("Action")
                table.add_column("Summary")
                for r in snap.recent:
                    table.add_row(
                        r["at"][11:19] if len(r["at"]) > 19 else r["at"],
                        r["action"],
                        r["summary"][:70],
                    )
                console.print(table)

            console.print(
                f"\n[dim]refresh {interval}s · PnL cache ≤5s · Ctrl+C quit[/dim]"
            )
            _time.sleep(interval)
    except KeyboardInterrupt:
        console.print("\n[dim]watch stopped[/dim]")


@app.command("logs")
def logs_cmd(
    lines: int = typer.Option(40, "--lines", "-n", help="Tail this many agent.log lines"),
    all_events: bool = typer.Option(
        False, "--all-events", help="Print full events.jsonl history"
    ),
) -> None:
    """Show log paths and recent history (files append; nothing is wiped on new trades)."""
    from datetime import datetime

    settings = get_settings()
    log_dir = settings.log_dir.expanduser()
    history_dir = log_dir / "history"
    day = datetime.now().strftime("%Y-%m-%d")
    agent_log = log_dir / "agent.log"
    events = log_dir / "events.jsonl"
    daily = history_dir / f"{day}.log"
    daily_events = history_dir / f"{day}.events.jsonl"

    table = Table(title="Overnight logs (append-only history)")
    table.add_column("File")
    table.add_column("Path")
    table.add_column("Lines")
    for label, path in [
        ("agent.log", agent_log),
        ("events.jsonl", events),
        (f"history/{day}.log", daily),
        (f"history/{day}.events.jsonl", daily_events),
        ("launchd.stdout", log_dir / "launchd.stdout.log"),
        ("db", settings.db_path),
    ]:
        n = ""
        if path.exists() and path.is_file():
            try:
                n = str(sum(1 for _ in path.open("rb")))
            except Exception:
                n = "?"
        table.add_row(label, str(path), n or "(missing)")
    console.print(table)
    console.print(
        "[dim]Prefer agent.log + events.jsonl (or history/). "
        "launchd.stdout was truncated on restarts with the old plist — "
        "trade history is in agent.log / events.jsonl.[/dim]"
    )

    if agent_log.exists():
        console.print(f"\n[bold]Last {lines} lines of agent.log[/bold]")
        text = agent_log.read_text(encoding="utf-8", errors="replace").splitlines()
        for line in text[-lines:]:
            console.print(line)
    else:
        console.print("[dim]agent.log not created yet.[/dim]")

    if events.exists():
        ev = events.read_text(encoding="utf-8", errors="replace").splitlines()
        show = ev if all_events else ev[-min(lines, 30) :]
        console.print(
            f"\n[bold]{'All' if all_events else f'Last {len(show)}'} events.jsonl "
            f"({len(ev)} total)[/bold]"
        )
        for line in show:
            console.print(line)
    console.print(
        f"\n[dim]Full history:\n"
        f"  less {agent_log}\n"
        f"  less {events}\n"
        f"  jq . {events}[/dim]"
    )


@app.command("service-status")
def service_status_cmd() -> None:
    """Show whether the launchd agent is loaded."""
    settings = get_settings()
    running = launchd_service.launchd_running()
    path = launchd_service.plist_path()
    hud_running = launchd_service.hud_launchd_running()
    hud_path = launchd_service.hud_plist_path()
    table = Table(title="launchd")
    table.add_column("Key")
    table.add_column("Value")
    table.add_row("label", launchd_service.LABEL)
    table.add_row("plist", str(path))
    table.add_row("exists", str(path.exists()))
    table.add_row("loaded", str(running))
    table.add_row("hud_label", launchd_service.HUD_LABEL)
    table.add_row("hud_plist", str(hud_path))
    table.add_row("hud_exists", str(hud_path.exists()))
    table.add_row("hud_loaded", str(hud_running))
    table.add_row("log_dir", str(settings.log_dir))
    console.print(table)


if __name__ == "__main__":
    app()
