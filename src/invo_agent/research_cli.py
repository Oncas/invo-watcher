from __future__ import annotations

import json
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from uuid import UUID

import httpx
import typer

from invo_agent import research_sync
from invo_agent.api.client import InvolioAPIError, InvolioClient
from invo_agent.auth.session import SessionStore
from invo_agent.config import get_settings
from invo_agent.research import (
    ArchiveNotMigratedError,
    ResearchArchive,
    SchemaTooNewError,
    validate_archive_path,
)
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


@research_app.command("export")
def export(
    after_id: int = typer.Option(0, "--after-id", min=0, help="Highest id already received"),
    since: str = typer.Option(
        research_sync.EPOCH, "--since", help="Re-send rows re-seen after this ISO timestamp"
    ),
    database: Path | None = typer.Option(None, "--database", help="Archive to read"),
) -> None:
    """Stream the archive as NDJSON. Read-only; writes nothing."""
    path = database or get_settings().research_db_path
    try:
        # Read-only: this runs on the trading box, beside the live collector.
        with ResearchArchive(path, readonly=True) as archive:
            for line in research_sync.export_stream(archive, after_id=after_id, since=since):
                # Flushing per line keeps a truncated pipe truncated at a line
                # boundary, which is what lets the puller detect it cleanly.
                typer.echo(json.dumps(line, default=str, ensure_ascii=False))
    except (
        SchemaTooNewError, ArchiveNotMigratedError, sqlite3.Error, OSError, ValueError,
    ) as exc:
        typer.secho(f"export failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None


def _ssh_stream(remote: str, remote_bin: str, compress: bool):
    def opener(after_id: int, since: str):
        args = ["ssh"]
        if compress:
            # SSH's own zlib on the wire; no separate gzip process.
            args.append("-C")
        args += [remote, remote_bin, "research", "export",
                 "--after-id", str(after_id), "--since", since]
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        assert proc.stdout is not None
        failed = False
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if raw:
                    yield json.loads(raw)
        except BaseException:
            failed = True
            proc.kill()
            raise
        finally:
            proc.stdout.close()
            stderr = proc.stderr.read() if proc.stderr else ""
            if proc.stderr:
                proc.stderr.close()
            code = proc.wait()
            # Without this a dead ssh looks exactly like an empty archive: the
            # stream just ends, and the pull reports a tidy "PARTIAL: 0 new".
            if code and not failed:
                raise RuntimeError(
                    f"ssh exited {code}: {stderr.strip() or 'no output'}"
                )
    return opener


@research_app.command("pull")
def pull(
    remote: str | None = typer.Option(None, "--remote", help="user@host of the VPS"),
    from_file: Path | None = typer.Option(
        None, "--from-file", help="Merge a local backfill database instead of SSHing"
    ),
    dest: Path | None = typer.Option(None, "--dest", help="Local archive directory"),
    database: Path | None = typer.Option(None, "--database", help="Local archive file"),
    rescan_days: int = typer.Option(
        research_sync.DEFAULT_RESCAN_DAYS, "--rescan-days", min=1,
        help="Re-scan at least this far back for updates to old rows",
    ),
    remote_bin: str = typer.Option(
        "/opt/invo-agent/.venv/bin/invo-agent", "--remote-bin", help="CLI path on the VPS"
    ),
    source: str = typer.Option("vps", "--source", help="Name of this source in sync-state.json"),
) -> None:
    """Merge the VPS archive, or a backfill file, into the local archive."""
    if bool(remote) == bool(from_file):
        raise typer.BadParameter("Use either --remote or --from-file")
    target = (dest or Path("~/Work/invo-db")).expanduser()
    local = database or (target / "research.db")
    try:
        with research_sync.pull_lock(target), ResearchArchive(local) as archive:
            if from_file is not None:
                # A backfill is its own archive: merged in full, and deliberately
                # never allowed to move the VPS watermark.
                result = research_sync.merge_archive_file(archive, from_file)
            else:
                result = research_sync.pull_source(
                    archive, target, source=source, rescan_days=rescan_days,
                    open_stream=_ssh_stream(str(remote), remote_bin, compress=True),
                )
    except research_sync.PullBusyError as exc:
        typer.secho(str(exc), fg=typer.colors.YELLOW, err=True)
        raise typer.Exit(code=1) from None
    except (
        research_sync.ArchiveMismatch, research_sync.SyncProtocolError,
        SchemaTooNewError, ArchiveNotMigratedError, sqlite3.Error, OSError,
        ValueError, json.JSONDecodeError,
        # A transport failure (ssh died, remote binary missing) arrives here.
        RuntimeError,
    ) as exc:
        typer.secho(f"pull failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    state = "complete" if result.complete else "PARTIAL"
    typer.echo(
        f"{state}: {result.counts['new']} new, {result.counts['update']} updated, "
        f"{result.inserted} inserted, watermark {result.last_id}"
    )
    if not result.complete:
        raise typer.Exit(code=1)


@research_app.command("prune")
def prune(
    ack: Path | None = typer.Option(
        None, "--ack", help="The laptop's sync-state.json, copied over"
    ),
    database: Path | None = typer.Option(None, "--database", help="Archive to prune"),
    keep_days: int = typer.Option(
        research_sync.DEFAULT_RESCAN_DAYS, "--keep-days", min=1,
        help="Never delete anything touched this recently",
    ),
    source: str = typer.Option("vps", "--source", help="Which source entry to read"),
    vacuum: bool = typer.Option(False, "--vacuum", help="Reclaim disk; stop the service first"),
    yes: bool = typer.Option(False, "--yes", help="Actually delete. Without it, dry-run."),
) -> None:
    """Delete rows the laptop has confirmed receiving. Dry-run unless --yes."""
    if ack is None:
        raise typer.BadParameter("--ack is required; prune never guesses a watermark")
    path = database or get_settings().research_db_path
    try:
        state = research_sync.SyncState.load(ack.parent if ack.is_file() else ack)
        with ResearchArchive(path) as archive:
            report = research_sync.prune_archive(
                archive, state.source(source), keep_days=keep_days, apply=yes
            )
            if yes and vacuum:
                archive.conn.execute("VACUUM")
    except research_sync.PruneRefused as exc:
        typer.secho(f"prune refused: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    except (SchemaTooNewError, sqlite3.Error, OSError, ValueError) as exc:
        typer.secho(f"prune failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from None
    if report["stale_days"] > keep_days:
        typer.secho(
            f"note: acknowledgement is {report['stale_days']} days old, so this "
            "prunes less than a fresh one would",
            fg=typer.colors.YELLOW, err=True,
        )
    verb = "deleted" if report["applied"] else "would delete"
    typer.echo(
        f"{verb} {report['deleted']} of {report['before']} rows "
        f"(id <= {report['watermark']}, untouched since {report['cutoff']})"
    )
