"""Pull the VPS research buffer into the laptop's long-term archive.

The VPS writes locally and answers export queries; the laptop holds every
watermark and is the only side that decides what has been received. Nothing
here writes to the remote archive — pruning is a separate, explicit command.
"""
from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path
from typing import Any

from invo_agent.research import ResearchArchive, utc_now

# Bumped when the wire format changes shape. The puller refuses a stream whose
# protocol it does not recognise rather than guessing at the columns.
PROTOCOL = 1

BATCH_ROWS = 1000


class SyncProtocolError(RuntimeError):
    """The stream is not something this build knows how to merge."""

# `id` rides along so the puller can order phase 2 and advance its watermark.
# It is the *remote* id and is never written locally.
_COLUMNS = (
    "id", "schema_version", "kind", "origin", "entity_id", "portfolio_id",
    "observed_at", "source_at", "run_id", "dedupe_key", "payload",
    "last_seen_at", "seen_count", "redacted_keys",
)
_SELECT = ", ".join(_COLUMNS)

_UPDATES_SQL = f"""
    SELECT {_SELECT} FROM observations
     WHERE id <= ? AND last_seen_at > ?
"""
_NEW_SQL = f"""
    SELECT {_SELECT} FROM observations
     WHERE id > ?
     ORDER BY id ASC
"""


_MERGE_COLUMNS = tuple(c for c in _COLUMNS if c != "id")
_INSERT_SQL = f"""
    INSERT OR IGNORE INTO observations ({", ".join(_MERGE_COLUMNS)})
    VALUES ({", ".join("?" * len(_MERGE_COLUMNS))})
"""
# Never `seen_count + 1`: the collector increments because it genuinely saw the
# value again, and a merge has seen nothing. Taking the max also makes a replay
# of the same rows a no-op, which is the property the whole design rests on.
_UPDATE_SQL = """
    UPDATE observations
       SET seen_count = MAX(COALESCE(seen_count, 0), COALESCE(?, 0)),
           last_seen_at = CASE
               WHEN last_seen_at IS NULL THEN ?
               WHEN ? IS NULL THEN last_seen_at
               ELSE MAX(last_seen_at, ?)
           END
     WHERE dedupe_key = ?
"""


def _as_dict(cursor: sqlite3.Cursor, row: tuple, phase: str) -> dict[str, Any]:
    record = dict(zip((c[0] for c in cursor.description), row, strict=True))
    record["type"] = "row"
    record["phase"] = phase
    return record


def export_stream(
    archive: ResearchArchive, *, after_id: int, since: str
) -> Iterator[dict[str, Any]]:
    """Yield header, rows, then a completion marker.

    ``remote_now`` is stamped before the snapshot is established, never after.
    Stamped after, a row committed in between would carry a ``last_seen_at``
    below the boundary while being absent from the stream, and the next pull —
    which starts its window at that boundary — would never look for it again.
    Stamped before, such a row is simply re-scanned next time.
    """
    remote_now = utc_now()
    yield {
        "type": "header",
        "protocol": PROTOCOL,
        "archive_uuid": archive.archive_uuid,
        "after_id": after_id,
        "since": since,
        "remote_now": remote_now,
    }
    conn = archive.conn
    counts = {"update": 0, "new": 0}
    conn.execute("BEGIN")
    try:
        updates = conn.execute(_UPDATES_SQL, (after_id, since))
        for row in updates:
            counts["update"] += 1
            yield _as_dict(updates, row, "update")
        fresh = conn.execute(_NEW_SQL, (after_id,))
        for row in fresh:
            counts["new"] += 1
            yield _as_dict(fresh, row, "new")
        max_id = conn.execute("SELECT COALESCE(MAX(id), 0) FROM observations").fetchone()[0]
    finally:
        # Read-only throughout; rolling back just releases the snapshot.
        conn.rollback()
    yield {
        "type": "complete",
        "protocol": PROTOCOL,
        "archive_uuid": archive.archive_uuid,
        "max_id": max_id,
        "update_boundary": remote_now,
        "counts": counts,
    }


@dataclass
class MergeResult:
    """What a merge learned about the source, and how far it got."""

    archive_uuid: str | None = None
    last_id: int = 0
    max_id: int = 0
    update_boundary: str | None = None
    complete: bool = False
    inserted: int = 0
    counts: dict[str, int] = field(default_factory=lambda: {"update": 0, "new": 0})


def _merge_row(conn: sqlite3.Connection, row: dict[str, Any]) -> bool:
    values = [row.get(name) for name in _MERGE_COLUMNS]
    cursor = conn.execute(_INSERT_SQL, values)
    if cursor.rowcount:
        return True
    seen_at = row.get("last_seen_at")
    conn.execute(
        _UPDATE_SQL,
        (row.get("seen_count"), seen_at, seen_at, seen_at, row.get("dedupe_key")),
    )
    return False


def merge_stream(
    archive: ResearchArchive,
    lines: Iterable[dict[str, Any]],
    *,
    last_id: int = 0,
    batch_rows: int = BATCH_ROWS,
    on_batch: Callable[[MergeResult], None] | None = None,
) -> MergeResult:
    """Merge an export into ``archive``, committing in batches.

    The watermark only ever advances past rows the transaction actually
    committed, so an interrupted merge resumes from the last committed batch
    rather than skipping the remainder of the one it was in.
    """
    result = MergeResult(last_id=last_id)
    seen_header = False
    batch: list[dict[str, Any]] = []
    pending_id = last_id

    def flush() -> None:
        nonlocal batch, pending_id
        if not batch:
            return
        conn = archive.conn
        conn.execute("BEGIN")
        try:
            for row in batch:
                if _merge_row(conn, row):
                    result.inserted += 1
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        # Only now is it true that these rows are on disk.
        result.last_id = pending_id
        batch = []
        if on_batch is not None:
            on_batch(result)

    for line in lines:
        kind = line.get("type")
        if kind == "header":
            if line.get("protocol") != PROTOCOL:
                raise SyncProtocolError(
                    f"Export speaks protocol {line.get('protocol')!r}, this build speaks {PROTOCOL}"
                )
            incoming = line.get("archive_uuid")
            if seen_header and incoming != result.archive_uuid:
                raise SyncProtocolError(
                    f"Stream mixes archives {result.archive_uuid!r} and {incoming!r}"
                )
            result.archive_uuid = incoming
            seen_header = True
        elif kind == "row":
            if not seen_header:
                raise SyncProtocolError("Export began with a row before its header")
            phase = line.get("phase")
            if phase not in ("update", "new"):
                raise SyncProtocolError(f"Unknown row phase {phase!r}")
            result.counts[phase] += 1
            batch.append(line)
            if phase == "new":
                # Phase 2 is ordered by id, so the last one in the batch is the
                # high-water mark. Phase 1 rows sit below it and never move it.
                pending_id = max(pending_id, int(line.get("id") or 0))
            if len(batch) >= batch_rows:
                flush()
        elif kind == "complete":
            if not seen_header:
                raise SyncProtocolError("Export completed without a header")
            flush()
            result.max_id = int(line.get("max_id") or 0)
            result.update_boundary = line.get("update_boundary")
            result.complete = True
        else:
            raise SyncProtocolError(f"Unknown export line type {kind!r}")

    # A truncated stream still keeps whatever it managed to commit; it just
    # never advances last_pull_at, so the next run re-scans the same window.
    flush()
    return result


# --- laptop-side state ------------------------------------------------------

STATE_FILE = "sync-state.json"
LOCK_FILE = "pull.lock"
DEFAULT_RESCAN_DAYS = 14


class PullBusyError(RuntimeError):
    """Another pull already holds the destination lock."""


class PruneRefused(RuntimeError):
    """The acknowledgement does not authorise deleting anything here."""


@dataclass
class SourceState:
    """What the laptop knows about one remote archive."""

    archive_uuid: str | None = None
    last_id: int = 0
    last_max_id: int = 0
    rescan_days: int = DEFAULT_RESCAN_DAYS
    last_pull_at: str | None = None
    last_pull_status: str = "never"

    def to_json(self) -> dict[str, Any]:
        return {
            "archive_uuid": self.archive_uuid,
            "last_id": self.last_id,
            "last_max_id": self.last_max_id,
            "rescan_days": self.rescan_days,
            "last_pull_at": self.last_pull_at,
            "last_pull_status": self.last_pull_status,
        }

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> SourceState:
        return cls(
            archive_uuid=raw.get("archive_uuid"),
            last_id=int(raw.get("last_id") or 0),
            last_max_id=int(raw.get("last_max_id") or 0),
            rescan_days=int(raw.get("rescan_days") or DEFAULT_RESCAN_DAYS),
            last_pull_at=raw.get("last_pull_at"),
            last_pull_status=str(raw.get("last_pull_status") or "never"),
        )


class SyncState:
    """``sync-state.json``: every watermark the laptop owns."""

    def __init__(self, sources: dict[str, SourceState] | None = None):
        self.sources: dict[str, SourceState] = sources or {}

    def source(self, name: str) -> SourceState:
        return self.sources.setdefault(name, SourceState())

    @classmethod
    def load(cls, dest: Path) -> SyncState:
        path = Path(dest).expanduser() / STATE_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return cls()
        return cls({
            name: SourceState.from_json(body)
            for name, body in (raw.get("sources") or {}).items()
        })

    def save(self, dest: Path) -> None:
        """Write atomically: a crash mid-write must not lose the watermark.

        A truncated state file is worse than a stale one — it would restart the
        next pull from zero and re-transfer the whole archive.
        """
        dest = Path(dest).expanduser()
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / STATE_FILE
        body = json.dumps(
            {"sources": {n: s.to_json() for n, s in self.sources.items()}}, indent=2,
        ) + "\n"
        tmp = dest / f".{STATE_FILE}.tmp"
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.replace(tmp, path)
        except OSError:
            tmp.unlink(missing_ok=True)
            raise


@contextmanager
def pull_lock(dest: Path) -> Iterator[None]:
    """Serialize pulls against one destination.

    The SQLite write lock is not enough on its own: two pulls would interleave
    their batches and then race to write ``sync-state.json``, and the loser
    could roll the watermark backwards.
    """
    dest = Path(dest).expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    handle = (dest / LOCK_FILE).open("w", encoding="utf-8")
    try:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PullBusyError(
                f"Another pull is already running against {dest}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


# --- prune (runs on the VPS) ------------------------------------------------


def _parse_ts(value: str | None, label: str) -> datetime:
    if not value:
        raise PruneRefused(f"Acknowledgement has no {label}; refusing to delete anything")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PruneRefused(f"Acknowledgement {label} is not a timestamp: {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def prune_archive(
    archive: ResearchArchive,
    ack: SourceState,
    *,
    keep_days: int = DEFAULT_RESCAN_DAYS,
    apply: bool = False,
) -> dict[str, Any]:
    """Delete rows the laptop has provably received and stopped caring about.

    Two independent conditions must both hold: the row is at or below a
    watermark the laptop confirmed, *and* it has not been touched inside
    ``keep_days`` of that confirmation. The second is what protects a row whose
    id is old but whose content was re-observed recently.
    """
    if ack.archive_uuid != archive.archive_uuid:
        raise PruneRefused(
            f"Acknowledgement is for archive {ack.archive_uuid!r}, this is "
            f"{archive.archive_uuid!r}. Refusing to prune a database it never saw."
        )
    top = archive.conn.execute("SELECT COALESCE(MAX(id), 0) FROM observations").fetchone()[0]
    if ack.last_max_id > top:
        raise PruneRefused(
            f"Acknowledgement claims id {ack.last_max_id}, archive stops at {top}. "
            "The watermark came from a different database."
        )
    pulled_at = _parse_ts(ack.last_pull_at, "last_pull_at")
    now = datetime.now(UTC)
    if pulled_at > now:
        raise PruneRefused(f"Acknowledgement is dated {ack.last_pull_at}, which is in the future")

    cutoff = (pulled_at - timedelta(days=max(1, int(keep_days)))).isoformat()
    predicate = """
        WHERE id <= ? AND COALESCE(last_seen_at, observed_at) < ?
    """
    before = archive.conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    doomed = archive.conn.execute(
        f"SELECT COUNT(*) FROM observations {predicate}", (ack.last_max_id, cutoff)
    ).fetchone()[0]
    if apply and doomed:
        with archive.conn:
            archive.conn.execute(
                f"DELETE FROM observations {predicate}", (ack.last_max_id, cutoff)
            )
    return {
        "before": before,
        "deleted": doomed,
        "after": before - doomed if apply else before,
        "cutoff": cutoff,
        "watermark": ack.last_max_id,
        # Stale is reported, never fatal: the cutoff is anchored to the ack, so
        # an old one simply spares more rows than a fresh one would.
        "stale_days": round((now - pulled_at).total_seconds() / 86400, 2),
        "applied": bool(apply),
    }


# --- pull (runs on the laptop) ----------------------------------------------

EPOCH = "1970-01-01T00:00:00+00:00"


class ArchiveMismatch(RuntimeError):
    """The remote is not the archive this source was bound to."""


def _window_start(state: SourceState, rescan_days: int, now: datetime) -> str:
    """The older of the last confirmed boundary and the re-scan floor.

    Taking the minimum is what makes ``rescan_days`` pure margin: however
    narrow it is, the window still reaches back to the last completed pull, so
    no update since then can slip past.
    """
    floor = now - timedelta(days=max(1, int(rescan_days)))
    if not state.last_pull_at:
        return floor.isoformat()
    try:
        previous = datetime.fromisoformat(state.last_pull_at)
    except ValueError:
        return floor.isoformat()
    if previous.tzinfo is None:
        previous = previous.replace(tzinfo=UTC)
    return state.last_pull_at if previous <= floor else floor.isoformat()


def pull_source(
    archive: ResearchArchive,
    dest: Path,
    *,
    open_stream: Callable[[int, str], Iterable[dict[str, Any]]],
    source: str = "vps",
    rescan_days: int = DEFAULT_RESCAN_DAYS,
    batch_rows: int = BATCH_ROWS,
    now: datetime | None = None,
) -> MergeResult:
    """Merge one remote export into the local archive and persist the watermark."""
    dest = Path(dest).expanduser()
    state = SyncState.load(dest)
    src = state.source(source)
    src.rescan_days = rescan_days
    since = _window_start(src, rescan_days, now or datetime.now(UTC))

    stream = iter(open_stream(src.last_id, since))
    first = next(stream, None)
    if first is not None and first.get("type") == "header":
        remote = first.get("archive_uuid")
        if src.archive_uuid and remote != src.archive_uuid:
            raise ArchiveMismatch(
                f"Source {source!r} is bound to archive {src.archive_uuid!r} but the "
                f"remote reports {remote!r}. If the remote was rebuilt, clear that "
                f"source's archive_uuid in {dest / STATE_FILE} to adopt the new one."
            )
    lines = chain([first], stream) if first is not None else iter(())

    def persist(result: MergeResult) -> None:
        src.last_id = result.last_id
        state.save(dest)

    result = merge_stream(
        archive, lines, last_id=src.last_id, batch_rows=batch_rows, on_batch=persist,
    )
    src.last_id = result.last_id
    if result.archive_uuid and not src.archive_uuid:
        src.archive_uuid = result.archive_uuid
    if result.complete:
        # The VPS clock, out of the marker — never this machine's.
        src.last_pull_at = result.update_boundary
        src.last_max_id = result.max_id
        src.last_pull_status = "ok"
    else:
        src.last_pull_status = "partial"
    state.save(dest)
    return result


def merge_archive_file(
    archive: ResearchArchive, path: Path, *, batch_rows: int = BATCH_ROWS
) -> MergeResult:
    """Merge a whole backfill database, touching no watermark.

    A backfill is its own archive with its own identity, so its ids mean
    nothing to the VPS source and must not advance it. Replay safety comes
    entirely from the rows' stable ``dedupe_key``s.
    """
    with ResearchArchive(Path(path).expanduser()) as source:
        return merge_stream(
            archive,
            export_stream(source, after_id=0, since=EPOCH),
            batch_rows=batch_rows,
        )
