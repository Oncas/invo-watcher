from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from uuid import UUID

import httpx
import typer

from invo_agent.api.client import InvolioAPIError, InvolioClient
from invo_agent.auth.session import SessionStore
from invo_agent.config import get_settings
from invo_agent.research import ResearchArchive, validate_archive_path
from invo_agent.research_backfill import HistoryBackfill, HistoryError

research_app = typer.Typer(no_args_is_help=True, help="Collect and inspect research data.")


@research_app.command("backfill")
def backfill(
    portfolio: list[str] | None = typer.Option(None, "--portfolio", "-p",
                                              help="Portfolio UUID; repeatable"),
    followed: bool = typer.Option(False, "--followed", help="Import currently followed portfolios"),
    database: Path | None = typer.Option(None, "--database", help="Separate research SQLite file"),
    delay: float = typer.Option(1.0, min=0.25, help="Minimum seconds between API requests"),
    max_pages: int = typer.Option(10000, min=1, help="Per-list limit; reaching it is incomplete"),
    revisions: bool = typer.Option(True, help="Also retrieve each investment's history posts"),
) -> None:
    """Archive open/closed ticker investments and available revisions; never execute trades."""
    if bool(portfolio) == followed:
        raise typer.BadParameter("Use either --portfolio (repeatable) or --followed")
    try:
        ids = [str(UUID(pid)) for pid in portfolio or []]
    except ValueError as exc:
        raise typer.BadParameter("Each --portfolio must be a UUID") from exc
    try:
        settings = get_settings()
        path = database or settings.research_db_path
        validate_archive_path(path, settings.db_path, settings.log_dir)
        session = SessionStore(settings.session_path)
        login = session.load()
        if login is None:
            raise typer.BadParameter("An existing Involio social login is required")
        with ResearchArchive(path) as archive, InvolioClient(settings, session) as client:
            importer = HistoryBackfill(client, archive, delay=delay, max_pages=max_pages,
                                       revisions=revisions)
            archive.append("backfill_requested", {
                "selection": "currently_followed" if followed else "explicit",
                "portfolio_ids": ids,
            }, origin="backfill", run_id=importer.run_id)
            if followed:
                ids = importer.followed_portfolios(str(login.user_id))
            selection = "currently_followed" if followed else "explicit"
            result = importer.run(ids, selection=selection)
    except (
        HistoryError, InvolioAPIError, httpx.HTTPError, sqlite3.Error, OSError, ValueError,
    ) as exc:
        # Never let rich exception locals print settings, login tokens or response bodies.
        detail = str(exc) if isinstance(exc, HistoryError) else type(exc).__name__
        if isinstance(exc, InvolioAPIError):
            detail += f" (HTTP {exc.status_code})"
        typer.echo(f"Backfill stopped: {detail}. Saved records remain; rerun after resolving it.",
                   err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(result, indent=2))
    if result["status"] != "complete":
        raise typer.Exit(1)


@research_app.command("status")
def status(database: Path | None = typer.Option(None, "--database")) -> None:
    """Read archive counts and the latest backfill result without accessing any account."""
    path = (database or get_settings().research_db_path).expanduser().resolve()
    if not path.is_file():
        typer.echo(f"No research database at {path}")
        raise typer.Exit(1)
    # sqlite3's connection context manager scopes a transaction, not the handle.
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as conn:
        counts = conn.execute("""SELECT kind, origin, COUNT(*), MIN(observed_at),
                                         MAX(observed_at), MAX(last_seen_at), SUM(seen_count)
                                  FROM observations GROUP BY kind, origin""").fetchall()
        latest = conn.execute("""SELECT kind, payload FROM observations
            WHERE kind IN ('backfill_finished', 'backfill_interrupted', 'backfill_started',
                           'backfill_requested')
            ORDER BY id DESC LIMIT 1""").fetchone()
    typer.echo(json.dumps({"database": str(path), "datasets": [
        dict(zip(("kind", "origin", "records", "first_observed", "last_observed",
                  "last_seen", "observations_including_repeats"), row,
                 strict=True)) for row in counts
    ], "latest_backfill": {"kind": latest[0], "detail": json.loads(latest[1])}
        if latest else None}, indent=2))
