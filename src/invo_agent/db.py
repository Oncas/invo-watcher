from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


@dataclass
class OpenCopy:
    investment_base_id: str
    base_short_id: str | None
    ticker: str | None
    portfolio_id: str | None
    opened_at: str
    last_notification_id: str | None


@dataclass
class ActionLogRow:
    id: int
    created_at: str
    action: str
    summary: str | None
    notification_id: str | None
    dry_run: bool


class AgentDB:
    def __init__(self, path: Path):
        self.path = path.expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS processed_notifications (
                notification_id TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                summary TEXT
            );

            CREATE TABLE IF NOT EXISTS open_copies (
                investment_base_id TEXT PRIMARY KEY,
                base_short_id TEXT,
                ticker TEXT,
                portfolio_id TEXT,
                opened_at TEXT NOT NULL,
                last_notification_id TEXT
            );

            CREATE TABLE IF NOT EXISTS action_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                action TEXT NOT NULL,
                summary TEXT,
                notification_id TEXT,
                dry_run INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def is_processed(self, notification_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM processed_notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        return row is not None

    def mark_processed(self, notification_id: str, action: str, summary: str | None = None) -> None:
        self._conn.execute(
            """
            INSERT OR REPLACE INTO processed_notifications
            (notification_id, action, processed_at, summary)
            VALUES (?, ?, ?, ?)
            """,
            (notification_id, action, datetime.now(UTC).isoformat(), summary),
        )
        self._conn.commit()

    def log_action(
        self,
        *,
        action: str,
        summary: str | None,
        notification_id: str | None,
        dry_run: bool,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO action_log (created_at, action, summary, notification_id, dry_run)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                datetime.now(UTC).isoformat(),
                action,
                summary,
                notification_id,
                1 if dry_run else 0,
            ),
        )
        self._conn.commit()

    def recent_actions(self, limit: int = 20) -> list[ActionLogRow]:
        rows = self._conn.execute(
            """
            SELECT id, created_at, action, summary, notification_id, dry_run
            FROM action_log
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            ActionLogRow(
                id=row["id"],
                created_at=row["created_at"],
                action=row["action"],
                summary=row["summary"],
                notification_id=row["notification_id"],
                dry_run=bool(row["dry_run"]),
            )
            for row in rows
        ]

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value),
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def upsert_open_copy(
        self,
        *,
        investment_base_id: str,
        base_short_id: str | None,
        ticker: str | None,
        portfolio_id: str | None,
        notification_id: str | None,
    ) -> None:
        existing = self._conn.execute(
            "SELECT opened_at FROM open_copies WHERE investment_base_id = ?",
            (investment_base_id,),
        ).fetchone()
        opened_at = existing["opened_at"] if existing else datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO open_copies
            (investment_base_id, base_short_id, ticker, portfolio_id, opened_at, last_notification_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                investment_base_id,
                base_short_id,
                ticker,
                portfolio_id,
                opened_at,
                notification_id,
            ),
        )
        self._conn.commit()

    def close_copy(self, investment_base_id: str) -> None:
        self._conn.execute(
            "DELETE FROM open_copies WHERE investment_base_id = ?",
            (investment_base_id,),
        )
        self._conn.commit()

    def list_open_copies(self) -> list[OpenCopy]:
        rows = self._conn.execute(
            "SELECT * FROM open_copies ORDER BY opened_at DESC"
        ).fetchall()
        return [
            OpenCopy(
                investment_base_id=row["investment_base_id"],
                base_short_id=row["base_short_id"],
                ticker=row["ticker"],
                portfolio_id=row["portfolio_id"],
                opened_at=row["opened_at"],
                last_notification_id=row["last_notification_id"],
            )
            for row in rows
        ]

    def processed_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM processed_notifications").fetchone()
        return int(row["c"])

    def reset_runtime_state(self) -> None:
        """Clear processed/open/action state so catch-up can re-run. Keeps nothing."""
        self._conn.executescript(
            """
            DELETE FROM processed_notifications;
            DELETE FROM open_copies;
            DELETE FROM action_log;
            DELETE FROM meta;
            """
        )
        self._conn.commit()
