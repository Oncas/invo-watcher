"""Append-only research observations, independent of the agent's operational DB."""
from __future__ import annotations

import hashlib
import json
import logging
import queue
import re
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)
SCHEMA_VERSION = 2


class SchemaTooNewError(RuntimeError):
    """The archive was written by a newer build than this one understands."""


class ArchiveNotMigratedError(RuntimeError):
    """Opened read-only, but the archive still needs a write to be usable."""

# Credential keys are matched on the whole normalized name, never as a bare
# substring: "tokenAddress" and "sessionHigh" are instrument/market fields and
# must survive, while "accessToken" and "x-api-key" must not.
_REDACT_EXACT = frozenset({
    "auth", "authorization", "bearer", "cookie", "credential", "credentials",
    "jwt", "mnemonic", "passwd", "password", "privatekey", "secret", "seed",
    "session", "sessionid", "signature", "token",
})
_REDACT_SUFFIX = (
    "apikey", "authorization", "cookie", "credential", "credentials",
    "mnemonic", "password", "privatekey", "secret", "seedphrase", "token",
)
# Fragments that never occur inside a legitimate market or trade field name.
_REDACT_SUBSTRING = ("authorization", "mnemonic", "password", "privatekey")

# Added after SCHEMA_VERSION 1; applied to archives created by the older code.
_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("last_seen_at", "TEXT"),
    ("seen_count", "INTEGER NOT NULL DEFAULT 1"),
    ("redacted_keys", "TEXT"),
)

_BASE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY,
        schema_version INTEGER NOT NULL,
        kind TEXT NOT NULL,
        origin TEXT NOT NULL,
        entity_id TEXT,
        portfolio_id TEXT,
        observed_at TEXT NOT NULL,
        source_at TEXT,
        run_id TEXT,
        dedupe_key TEXT UNIQUE,
        payload TEXT NOT NULL,
        last_seen_at TEXT,
        seen_count INTEGER NOT NULL DEFAULT 1,
        redacted_keys TEXT
    );
    CREATE INDEX IF NOT EXISTS observations_entity
        ON observations(kind, entity_id, observed_at);
    CREATE INDEX IF NOT EXISTS observations_portfolio
        ON observations(portfolio_id, kind, observed_at);
    CREATE TABLE IF NOT EXISTS archive_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
"""

# Views carry no data, so they are rebuilt rather than migrated.
_VIEW_SCHEMA = """
    DROP VIEW IF EXISTS trade_observations;
    CREATE VIEW trade_observations AS
        SELECT id AS observation_id, entity_id AS base_id, portfolio_id,
            origin, observed_at, last_seen_at, seen_count, source_at, run_id,
            json_extract(payload, '$.id') AS investment_id,
            -- NULL where the source omitted baseId; base_id then falls back to
            -- the investment id and cannot be used to group revisions.
            json_extract(payload, '$.baseId') AS source_base_id,
            json_extract(payload, '$.baseShortId') AS base_short_id,
            json_extract(payload, '$.ticker') AS ticker,
            json_extract(payload, '$.name') AS instrument_name,
            json_extract(payload, '$.directionLong') AS direction_long,
            json_extract(payload, '$.owner.id') AS owner_id,
            json_extract(payload, '$.owner.username') AS owner_username,
            json_extract(payload, '$.portfolio.title') AS portfolio_title,
            json_extract(payload, '$.entryPrice') AS entry_price,
            json_extract(payload, '$.closingPrice') AS closing_price,
            json_extract(payload, '$.currentPrice') AS current_price,
            json_extract(payload, '$.liquidationPrice') AS liquidation_price,
            json_extract(payload, '$.leverage') AS leverage,
            json_extract(payload, '$.positionSize') AS position_size,
            json_extract(payload, '$.entrySize') AS entry_size,
            json_extract(payload, '$.entrySim') AS entry_sim,
            json_extract(payload, '$.priceTarget') AS price_target,
            json_extract(payload, '$.stopLoss') AS stop_loss,
            json_extract(payload, '$.isOpen') AS is_open,
            json_extract(payload, '$.active') AS active,
            json_extract(payload, '$.closedAt') AS closed_at,
            json_extract(payload, '$.percentChange') AS reported_percent_change,
            json_extract(payload, '$.verifiedTrade') AS source_verified_trade,
            json_extract(payload, '$.customEntryDate') AS custom_entry_date,
            json_extract(payload, '$.updatedAt') AS source_updated_at,
            -- Source-reported revision deltas, retained as JSON.
            json_extract(payload, '$.changes') AS changes,
            redacted_keys, payload
        FROM observations WHERE kind = 'investment';
"""


def is_credential_key(key: str) -> bool:
    name = re.sub(r"[^a-z]", "", str(key).lower())
    return (
        name in _REDACT_EXACT
        or name.endswith(_REDACT_SUFFIX)
        or any(fragment in name for fragment in _REDACT_SUBSTRING)
    )


def sanitize(value: Any, *, removed: list[str] | None = None, path: str = "") -> Any:
    """Strip credential fields recursively; callers must only supply trading data.

    Removed key paths are appended to ``removed`` so the archive can record what
    it deleted, keeping "absent at source" distinguishable from "redacted here".
    """
    if isinstance(value, dict):
        clean = {}
        for k, v in value.items():
            key = str(k)
            where = f"{path}.{key}" if path else key
            if is_credential_key(key):
                if removed is not None:
                    removed.append(where)
                continue
            clean[key] = sanitize(v, removed=removed, path=where)
        return clean
    if isinstance(value, (list, tuple)):
        return [sanitize(v, removed=removed, path=f"{path}[]") for v in value]
    return value


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def validate_archive_path(path: Path, operational_db: Path, log_dir: Path) -> None:
    resolved = path.expanduser().resolve()
    if resolved == operational_db.expanduser().resolve():
        raise ValueError("Research archive must be separate from the trading database")
    if resolved.is_relative_to(log_dir.expanduser().resolve()):
        raise ValueError("Research archive must be outside the pruned log directory")


class ResearchArchive:
    def __init__(self, path: Path, *, readonly: bool = False):
        path = path.expanduser()
        self.readonly = readonly
        if readonly:
            # Export runs beside a live collector on the trading box. Opening
            # read-write would take the write lock — and `_initialize` writes
            # on *every* open — so a "read-only" command would contend with
            # the trader and fail outright on an unwritable file.
            if not path.is_file():
                raise FileNotFoundError(f"No research archive at {path}")
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            try:
                self._check_readable()
            except BaseException:
                self.conn.close()
                raise
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=5)
        try:
            self._initialize()
        except BaseException:
            self.conn.close()
            raise

    def _assert_schema_understood(self) -> None:
        """An archive from a newer build may have semantics this code would corrupt."""
        found = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if found > SCHEMA_VERSION:
            raise SchemaTooNewError(
                f"Research archive is at schema {found}, this build understands "
                f"{SCHEMA_VERSION}. Upgrade invo-agent before opening it."
            )

    def _check_readable(self) -> None:
        """Verify a read-only archive is already in a state worth exporting."""
        self._assert_schema_understood()
        try:
            row = self.conn.execute(
                "SELECT value FROM archive_meta WHERE key = 'archive_uuid'"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            raise ArchiveNotMigratedError(
                "Archive has no identity yet. Open it once for writing "
                "(any agent or backfill run) before exporting."
            ) from exc
        if row is None:
            raise ArchiveNotMigratedError(
                "Archive has no archive_uuid yet. Open it once for writing "
                "before exporting."
            )
        self._archive_uuid = str(row[0])
        # Exporting NULL keys would silently defeat the merge: a NULL is not
        # UNIQUE in SQLite, so every replay would insert those rows again.
        unkeyed = self.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE dedupe_key IS NULL"
        ).fetchone()[0]
        if unkeyed:
            raise ArchiveNotMigratedError(
                f"{unkeyed} observations still have no dedupe_key. Open the "
                "archive for writing once to migrate them, then export."
            )

    def _initialize(self) -> None:
        self._assert_schema_understood()
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_BASE_SCHEMA)
        existing = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(observations)").fetchall()
        }
        for name, col_type in _EXTRA_COLUMNS:
            if name not in existing:
                self.conn.execute(f"ALTER TABLE observations ADD COLUMN {name} {col_type}")
        # Identity must exist before any key is derived from it, or a re-run
        # would mint a second UUID and renumber rows it had already migrated.
        self._archive_uuid = self._ensure_archive_uuid()
        self._backfill_null_dedupe_keys()
        self.conn.executescript(_VIEW_SCHEMA)
        self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        self.conn.commit()

    def _ensure_archive_uuid(self) -> str:
        row = self.conn.execute(
            "SELECT value FROM archive_meta WHERE key = 'archive_uuid'"
        ).fetchone()
        if row is not None:
            return str(row[0])
        minted = str(uuid4())
        self.conn.execute(
            "INSERT INTO archive_meta (key, value) VALUES ('archive_uuid', ?)", (minted,)
        )
        return minted

    def _backfill_null_dedupe_keys(self) -> None:
        """Give pre-migration rows a stable identity so replay can dedupe them.

        ``archive_uuid || ':' || id`` is deterministic, so a retry or a copy of
        this archive derives the same keys rather than duplicating on merge.
        """
        self.conn.execute(
            """UPDATE observations
                  SET dedupe_key = ? || ':' || id
                WHERE dedupe_key IS NULL""",
            (self._archive_uuid,),
        )
        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM observations WHERE dedupe_key IS NULL"
        ).fetchone()[0]
        if remaining:
            raise RuntimeError(
                f"{remaining} observations still have no dedupe_key; merge would duplicate them"
            )

    @property
    def archive_uuid(self) -> str:
        return self._archive_uuid

    def append(
        self, kind: str, payload: dict, *, origin: str = "live",
        entity_id: str | None = None, portfolio_id: str | None = None,
        source_at: str | None = None, observed_at: str | None = None,
        run_id: str | None = None, deduplicate: bool = False,
        redacted: list[str] | None = None,
    ) -> bool:
        """Store one observation. Returns True when the content was new.

        A repeat of already-stored content is not inserted again; instead its
        ``last_seen_at``/``seen_count`` advance, so an unchanged record still
        carries evidence of when it was last confirmed unchanged.
        """
        removed: list[str] = []
        encoded = json.dumps(
            sanitize(payload, removed=removed), sort_keys=True, default=str, ensure_ascii=False,
        )
        dropped = sorted(set(removed) | set(redacted or []))
        seen_at = observed_at or utc_now()
        if deduplicate:
            # Content-addressed, deliberately archive-independent: two archives
            # must agree on this key or merging them would duplicate the row.
            identity = json.dumps([kind, origin, entity_id, portfolio_id, encoded])
            key = hashlib.sha256(identity.encode()).hexdigest()
        else:
            # Distinct by intent. Uniqueness is the only requirement, but it
            # must still exist: a NULL key is not UNIQUE in SQLite, so replaying
            # an export would insert these rows again and again.
            key = f"{self._archive_uuid}:{uuid4()}"
        with self.conn:
            cursor = self.conn.execute(
                """INSERT OR IGNORE INTO observations
                (schema_version, kind, origin, entity_id, portfolio_id, observed_at,
                 source_at, run_id, dedupe_key, payload, last_seen_at, seen_count,
                 redacted_keys)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (SCHEMA_VERSION, kind, origin, entity_id, portfolio_id, seen_at,
                 source_at, run_id, key, encoded, seen_at,
                 json.dumps(dropped) if dropped else None),
            )
            if cursor.rowcount == 0 and key is not None:
                self.conn.execute(
                    """UPDATE observations SET last_seen_at = ?, seen_count = seen_count + 1
                       WHERE dedupe_key = ?""",
                    (seen_at, key),
                )
        return cursor.rowcount > 0

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> ResearchArchive:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class BufferedResearch:
    """One writer thread; disk failures or queue saturation never block trading.

    Losses are counted and logged, then persisted on the next successful write
    and again at shutdown. This is best-effort telemetry, not a transactional
    execution ledger.
    """

    def __init__(self, path: Path, *, capacity: int = 4096):
        if capacity < 1:
            raise ValueError("Research queue capacity must be positive")
        self.path = path
        self.run_id = str(uuid4())
        self.dropped = 0
        self.failed = 0
        self._queue: queue.Queue = queue.Queue(maxsize=capacity)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="research-writer", daemon=True)
        self._thread.start()

    def record(self, kind: str, payload: dict, **metadata: Any) -> None:
        if self._stop.is_set():
            return
        observed_at = utc_now()
        try:
            # Redact on the calling thread so credentials never reach the queue,
            # and carry the removed key paths through to the archive.
            removed: list[str] = []
            clean = sanitize(payload, removed=removed)
            # Detach mutable inputs before returning to the trading code.
            snapshot = json.loads(json.dumps(clean, default=str))
            self._queue.put_nowait((kind, snapshot, {
                "observed_at": observed_at, "run_id": self.run_id,
                "redacted": removed, **metadata,
            }))
        except Exception:
            self.dropped += 1
            if self.dropped == 1 or self.dropped % 100 == 0:
                logger.warning("Research capture dropped records: %s", self.dropped)

    def _run(self) -> None:
        archive = None
        reported = (0, 0)
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    kind, payload, metadata = self._queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    if archive is None:
                        archive = ResearchArchive(self.path)
                    counts = (self.dropped, self.failed)
                    if counts != reported:
                        archive.append("capture_gap", {
                            "dropped_total": counts[0], "failed_total": counts[1],
                        }, run_id=self.run_id)
                        reported = counts
                    archive.append(kind, payload, **metadata)
                except Exception:
                    self.failed += 1
                    if self.failed == 1 or self.failed % 100 == 0:
                        logger.warning("Research archive write failed; total=%s", self.failed)
                finally:
                    self._queue.task_done()
        finally:
            if archive is not None:
                archive.close()

    def _flush_final_gap(self) -> None:
        """Persist the closing tally; otherwise late losses only ever reach the log."""
        try:
            with ResearchArchive(self.path) as archive:
                archive.append("capture_gap", {
                    "dropped_total": self.dropped, "failed_total": self.failed,
                    "final": True,
                }, run_id=self.run_id)
        except Exception:
            logger.warning("Research shutdown gap record could not be written")

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        alive = self._thread.is_alive()
        if alive or self.dropped or self.failed:
            logger.warning(
                "Research shutdown: queued=%s dropped=%s failed=%s",
                self._queue.qsize(), self.dropped, self.failed,
            )
        # Only once the writer has released the archive, so there is one writer.
        if not alive and (self.dropped or self.failed):
            self._flush_final_gap()
