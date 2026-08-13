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
    our_entry_px: float | None = None
    side: str | None = None
    size: float | None = None
    leverage: float | None = None
    recovery_pending: bool = False
    recovery_tp: float | None = None
    recovery_sl: float | None = None
    trader_closing_px: float | None = None
    pending_add_notional: float = 0.0
    trader: str | None = None


@dataclass
class PendingIntent:
    investment_base_id: str
    coin: str
    side: str
    reason: str
    created_at: str
    updated_at: str
    base_short_id: str | None = None
    notification_id: str | None = None
    investment_id: str | None = None
    leverage: float | None = None
    trader_entry: float | None = None
    tp: float | None = None
    sl: float | None = None
    entry_size: float | None = None
    portfolio_id: str | None = None
    summary: str | None = None
    trader: str | None = None


@dataclass
class ActionLogRow:
    id: int
    created_at: str
    action: str
    summary: str | None
    notification_id: str | None
    dry_run: bool


_OPEN_COPY_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("our_entry_px", "REAL"),
    ("side", "TEXT"),
    ("size", "REAL"),
    ("leverage", "REAL"),
    ("recovery_pending", "INTEGER NOT NULL DEFAULT 0"),
    ("recovery_tp", "REAL"),
    ("recovery_sl", "REAL"),
    ("trader_closing_px", "REAL"),
    ("pending_add_notional", "REAL NOT NULL DEFAULT 0"),
    ("trader", "TEXT"),
)

_PENDING_EXTRA_COLUMNS: tuple[tuple[str, str], ...] = (
    ("trader", "TEXT"),
)


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

            CREATE TABLE IF NOT EXISTS pending_intents (
                investment_base_id TEXT PRIMARY KEY,
                coin TEXT NOT NULL,
                side TEXT NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                base_short_id TEXT,
                notification_id TEXT,
                investment_id TEXT,
                leverage REAL,
                trader_entry REAL,
                tp REAL,
                sl REAL,
                entry_size REAL,
                portfolio_id TEXT,
                summary TEXT,
                trader TEXT
            );
            """
        )
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(open_copies)").fetchall()
        }
        for name, col_type in _OPEN_COPY_EXTRA_COLUMNS:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE open_copies ADD COLUMN {name} {col_type}")
        pending_existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(pending_intents)").fetchall()
        }
        for name, col_type in _PENDING_EXTRA_COLUMNS:
            if name not in pending_existing:
                self._conn.execute(
                    f"ALTER TABLE pending_intents ADD COLUMN {name} {col_type}"
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

    def unmark_processed(self, notification_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM processed_notifications WHERE notification_id = ?",
            (notification_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def unmark_processed_matching(self, action_prefix: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT notification_id FROM processed_notifications WHERE action LIKE ?",
            (f"{action_prefix}%",),
        ).fetchall()
        ids = [str(row["notification_id"]) for row in rows]
        if ids:
            self._conn.executemany(
                "DELETE FROM processed_notifications WHERE notification_id = ?",
                [(nid,) for nid in ids],
            )
            self._conn.commit()
        return ids

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

    def actions_after(self, after_id: int, *, limit: int = 50) -> list[ActionLogRow]:
        rows = self._conn.execute(
            """
            SELECT id, created_at, action, summary, notification_id, dry_run
            FROM action_log
            WHERE id > ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (after_id, limit),
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
        our_entry_px: float | None = None,
        side: str | None = None,
        size: float | None = None,
        leverage: float | None = None,
        trader: str | None = None,
    ) -> None:
        existing = self._conn.execute(
            "SELECT * FROM open_copies WHERE investment_base_id = ?",
            (investment_base_id,),
        ).fetchone()
        opened_at = existing["opened_at"] if existing else datetime.now(UTC).isoformat()

        def _keep(new: float | str | None, col: str) -> float | str | None:
            if new is not None:
                return new
            if existing is None:
                return None
            return existing[col]

        self._conn.execute(
            """
            INSERT OR REPLACE INTO open_copies (
                investment_base_id, base_short_id, ticker, portfolio_id,
                opened_at, last_notification_id,
                our_entry_px, side, size, leverage,
                recovery_pending, recovery_tp, recovery_sl, trader_closing_px,
                pending_add_notional, trader
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                investment_base_id,
                base_short_id if base_short_id is not None else (
                    existing["base_short_id"] if existing else None
                ),
                ticker if ticker is not None else (existing["ticker"] if existing else None),
                portfolio_id if portfolio_id is not None else (
                    existing["portfolio_id"] if existing else None
                ),
                opened_at,
                notification_id,
                _keep(our_entry_px, "our_entry_px"),
                _keep(side, "side"),
                _keep(size, "size"),
                _keep(leverage, "leverage"),
                int(existing["recovery_pending"] or 0) if existing else 0,
                existing["recovery_tp"] if existing else None,
                existing["recovery_sl"] if existing else None,
                existing["trader_closing_px"] if existing else None,
                float(existing["pending_add_notional"] or 0) if existing else 0.0,
                _keep(trader, "trader"),
            ),
        )
        self._conn.commit()

    def set_open_copy_fill(
        self,
        investment_base_id: str,
        *,
        our_entry_px: float,
        side: str,
        size: float,
        leverage: float | None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE open_copies
            SET our_entry_px = ?, side = ?, size = ?, leverage = ?
            WHERE investment_base_id = ?
            """,
            (our_entry_px, side, size, leverage, investment_base_id),
        )
        self._conn.commit()

    def set_recovery_state(
        self,
        investment_base_id: str,
        *,
        pending: bool,
        recovery_tp: float | None = None,
        recovery_sl: float | None = None,
        trader_closing_px: float | None = None,
    ) -> None:
        self._conn.execute(
            """
            UPDATE open_copies
            SET recovery_pending = ?,
                recovery_tp = ?,
                recovery_sl = ?,
                trader_closing_px = ?
            WHERE investment_base_id = ?
            """,
            (
                1 if pending else 0,
                recovery_tp,
                recovery_sl,
                trader_closing_px,
                investment_base_id,
            ),
        )
        self._conn.commit()

    def close_copy(self, investment_base_id: str) -> None:
        self._conn.execute(
            "DELETE FROM open_copies WHERE investment_base_id = ?",
            (investment_base_id,),
        )
        self._conn.commit()

    def get_open_copy(self, investment_base_id: str) -> OpenCopy | None:
        row = self._conn.execute(
            "SELECT * FROM open_copies WHERE investment_base_id = ?",
            (investment_base_id,),
        ).fetchone()
        return None if row is None else self._row_to_open_copy(row)

    def get_open_copy_by_short_id(self, base_short_id: str) -> OpenCopy | None:
        row = self._conn.execute(
            "SELECT * FROM open_copies WHERE base_short_id = ?",
            (base_short_id,),
        ).fetchone()
        return None if row is None else self._row_to_open_copy(row)

    def list_open_copies(self) -> list[OpenCopy]:
        rows = self._conn.execute(
            "SELECT * FROM open_copies ORDER BY opened_at DESC"
        ).fetchall()
        return [self._row_to_open_copy(row) for row in rows]

    @staticmethod
    def _row_to_open_copy(row: sqlite3.Row) -> OpenCopy:
        keys = set(row.keys())
        return OpenCopy(
            investment_base_id=row["investment_base_id"],
            base_short_id=row["base_short_id"],
            ticker=row["ticker"],
            portfolio_id=row["portfolio_id"],
            opened_at=row["opened_at"],
            last_notification_id=row["last_notification_id"],
            our_entry_px=row["our_entry_px"] if "our_entry_px" in keys else None,
            side=row["side"] if "side" in keys else None,
            size=row["size"] if "size" in keys else None,
            leverage=row["leverage"] if "leverage" in keys else None,
            recovery_pending=bool(row["recovery_pending"])
            if "recovery_pending" in keys and row["recovery_pending"] is not None
            else False,
            recovery_tp=row["recovery_tp"] if "recovery_tp" in keys else None,
            recovery_sl=row["recovery_sl"] if "recovery_sl" in keys else None,
            trader_closing_px=row["trader_closing_px"]
            if "trader_closing_px" in keys
            else None,
            pending_add_notional=float(row["pending_add_notional"] or 0)
            if "pending_add_notional" in keys
            else 0.0,
            trader=row["trader"] if "trader" in keys else None,
        )

    def set_open_copy_trader(self, investment_base_id: str, trader: str) -> None:
        self._conn.execute(
            "UPDATE open_copies SET trader = ? WHERE investment_base_id = ?",
            (trader, investment_base_id),
        )
        self._conn.commit()

    def lookup_trader_from_logs(
        self, *, ticker: str | None = None, short_id: str | None = None
    ) -> str | None:
        from invo_agent.labels import trader_from_summary

        rows = self._conn.execute(
            """
            SELECT summary FROM action_log
            WHERE summary LIKE '%@%'
            ORDER BY id DESC
            LIMIT 300
            """
        ).fetchall()
        ticker_hit: str | None = None
        ticker_u = ticker.upper() if ticker else None
        for row in rows:
            summary = row["summary"] or ""
            name = trader_from_summary(summary)
            if not name:
                continue
            if short_id and short_id in summary:
                return name
            if ticker_u and ticker_u in summary.upper() and ticker_hit is None:
                ticker_hit = name
        return ticker_hit

    def set_pending_add_notional(self, investment_base_id: str, notional: float) -> None:
        self._conn.execute(
            """
            UPDATE open_copies
            SET pending_add_notional = ?
            WHERE investment_base_id = ?
            """,
            (max(0.0, float(notional)), investment_base_id),
        )
        self._conn.commit()

    def upsert_pending_intent(self, intent: PendingIntent) -> None:
        existing = self.get_pending_intent(intent.investment_base_id)
        created_at = existing.created_at if existing else intent.created_at
        now = datetime.now(UTC).isoformat()
        self._conn.execute(
            """
            INSERT OR REPLACE INTO pending_intents (
                investment_base_id, coin, side, reason, created_at, updated_at,
                base_short_id, notification_id, investment_id, leverage,
                trader_entry, tp, sl, entry_size, portfolio_id, summary, trader
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                intent.investment_base_id,
                intent.coin,
                intent.side,
                intent.reason,
                created_at,
                now,
                intent.base_short_id,
                intent.notification_id,
                intent.investment_id,
                intent.leverage,
                intent.trader_entry,
                intent.tp,
                intent.sl,
                intent.entry_size,
                intent.portfolio_id,
                intent.summary,
                intent.trader or (existing.trader if existing else None),
            ),
        )
        self._conn.commit()

    def get_pending_intent(self, investment_base_id: str) -> PendingIntent | None:
        row = self._conn.execute(
            "SELECT * FROM pending_intents WHERE investment_base_id = ?",
            (investment_base_id,),
        ).fetchone()
        return None if row is None else self._row_to_pending(row)

    def get_pending_for_coin(
        self, coin: str, *, exclude_base_id: str | None = None
    ) -> PendingIntent | None:
        rows = self._conn.execute(
            """
            SELECT * FROM pending_intents
            WHERE upper(coin) = upper(?)
            ORDER BY created_at ASC
            """,
            (coin,),
        ).fetchall()
        for row in rows:
            intent = self._row_to_pending(row)
            if exclude_base_id and intent.investment_base_id == exclude_base_id:
                continue
            return intent
        return None

    def list_pending_intents(self) -> list[PendingIntent]:
        rows = self._conn.execute(
            "SELECT * FROM pending_intents ORDER BY created_at ASC"
        ).fetchall()
        return [self._row_to_pending(row) for row in rows]

    def delete_pending_intent(self, investment_base_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM pending_intents WHERE investment_base_id = ?",
            (investment_base_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def delete_pending_for_coin(
        self, coin: str, *, exclude_base_id: str | None = None
    ) -> list[str]:
        removed: list[str] = []
        for intent in self.list_pending_intents():
            if intent.coin.upper() != coin.upper():
                continue
            if exclude_base_id and intent.investment_base_id == exclude_base_id:
                continue
            self.delete_pending_intent(intent.investment_base_id)
            removed.append(intent.investment_base_id)
        return removed

    @staticmethod
    def _row_to_pending(row: sqlite3.Row) -> PendingIntent:
        return PendingIntent(
            investment_base_id=row["investment_base_id"],
            coin=row["coin"],
            side=row["side"],
            reason=row["reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            base_short_id=row["base_short_id"],
            notification_id=row["notification_id"],
            investment_id=row["investment_id"],
            leverage=row["leverage"],
            trader_entry=row["trader_entry"],
            tp=row["tp"],
            sl=row["sl"],
            entry_size=row["entry_size"],
            portfolio_id=row["portfolio_id"],
            summary=row["summary"],
            trader=row["trader"] if "trader" in row.keys() else None,
        )

    def processed_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM processed_notifications").fetchone()
        return int(row["c"])

    def reset_runtime_state(self) -> None:
        """Clear processed/open/action state so catch-up can re-run. Keeps nothing."""
        self._conn.executescript(
            """
            DELETE FROM processed_notifications;
            DELETE FROM open_copies;
            DELETE FROM pending_intents;
            DELETE FROM action_log;
            DELETE FROM meta;
            """
        )
        self._conn.commit()
