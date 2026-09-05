"""Market bars kept beside the research archive, for as-of joins to observations.

Two rules run through this module, because both are easy to violate by accident
and neither is recoverable once a dataset has been built on top of the mistake:

* A bar that is still forming is never stored as final.
* A feature computed for time ``t`` may only read bars that had already closed
  at ``t``. The bar *containing* ``t`` embeds prices from after ``t``, so it is
  lookahead and this module will not hand it to you as one.
"""
from __future__ import annotations

import math
import sqlite3
from itertools import pairwise
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

# Hyperliquid serves a rolling window of roughly this many bars per interval,
# honouring startTime and truncating the tail. Retention therefore depends on
# the interval: minutes go back days, hours go back months.
MAX_BARS_PER_REQUEST = 5000

INTERVAL_MS: dict[str, int] = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "8h": 28_800_000,
    "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000,
}

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS bars (
        coin TEXT NOT NULL,
        interval TEXT NOT NULL,
        open_time INTEGER NOT NULL,
        close_time INTEGER NOT NULL,
        open REAL NOT NULL,
        high REAL NOT NULL,
        low REAL NOT NULL,
        close REAL NOT NULL,
        volume REAL,
        trades INTEGER,
        source TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        PRIMARY KEY (coin, interval, open_time)
    ) WITHOUT ROWID;

    -- What we asked for versus what came back. The source silently truncates a
    -- window that exceeds its cap and returns nothing at all past its retention
    -- edge, so a gap is only interpretable against the request that produced it.
    CREATE TABLE IF NOT EXISTS fetch_runs (
        id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL,
        coin TEXT NOT NULL,
        interval TEXT NOT NULL,
        requested_start INTEGER NOT NULL,
        requested_end INTEGER NOT NULL,
        returned_first INTEGER,
        returned_last INTEGER,
        returned_count INTEGER NOT NULL,
        stored_count INTEGER NOT NULL,
        fetched_at TEXT NOT NULL,
        outcome TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS fetch_runs_scope
        ON fetch_runs(coin, interval, requested_start);

    CREATE VIEW IF NOT EXISTS bar_coverage AS
        SELECT coin, interval, COUNT(*) AS bars,
            MIN(open_time) AS first_open, MAX(open_time) AS last_open,
            MAX(fetched_at) AS last_fetched
        FROM bars GROUP BY coin, interval;
"""


def interval_ms(interval: str) -> int:
    try:
        return INTERVAL_MS[interval]
    except KeyError:
        raise ValueError(f"Unsupported interval {interval!r}") from None


def validate_market_path(path: Path, operational_db: Path, log_dir: Path) -> None:
    resolved = path.expanduser().resolve()
    if resolved == operational_db.expanduser().resolve():
        raise ValueError("Market store must be separate from the trading database")
    if resolved.is_relative_to(log_dir.expanduser().resolve()):
        raise ValueError("Market store must be outside the pruned log directory")


class MarketStore:
    def __init__(self, path: Path):
        path = path.expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=5)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.executescript(_SCHEMA)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.conn.commit()
        except BaseException:
            self.conn.close()
            raise

    # --- writing ---------------------------------------------------------

    def upsert_bars(
        self, coin: str, interval: str, bars: list[dict[str, Any]], *,
        source: str, fetched_at: str, now_ms: int,
    ) -> int:
        """Store closed bars. A bar still forming at ``now_ms`` is skipped.

        Returns the number of rows written. Re-fetching the same window is
        safe: a bar already stored is replaced only by a later fetch of the
        same slot, which is how the source's own corrections land.
        """
        rows = []
        for bar in bars:
            close_time = int(bar["T"])
            if close_time > now_ms:
                continue  # still forming; its high/low/close are not final yet
            rows.append((
                coin, interval, int(bar["t"]), close_time,
                float(bar["o"]), float(bar["h"]), float(bar["l"]), float(bar["c"]),
                float(bar["v"]) if bar.get("v") is not None else None,
                int(bar["n"]) if bar.get("n") is not None else None,
                source, fetched_at,
            ))
        if not rows:
            return 0
        with self.conn:
            self.conn.executemany(
                """INSERT INTO bars (coin, interval, open_time, close_time, open, high,
                       low, close, volume, trades, source, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(coin, interval, open_time) DO UPDATE SET
                       close_time=excluded.close_time, open=excluded.open,
                       high=excluded.high, low=excluded.low, close=excluded.close,
                       volume=excluded.volume, trades=excluded.trades,
                       source=excluded.source, fetched_at=excluded.fetched_at
                   WHERE excluded.fetched_at >= bars.fetched_at""",
                rows,
            )
        return len(rows)

    def record_fetch(
        self, *, run_id: str, coin: str, interval: str, requested_start: int,
        requested_end: int, bars: list[dict[str, Any]], stored: int,
        fetched_at: str, outcome: str,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT INTO fetch_runs (run_id, coin, interval, requested_start,
                       requested_end, returned_first, returned_last, returned_count,
                       stored_count, fetched_at, outcome)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, coin, interval, requested_start, requested_end,
                 int(bars[0]["t"]) if bars else None,
                 int(bars[-1]["t"]) if bars else None,
                 len(bars), stored, fetched_at, outcome),
            )

    # --- reading, without lookahead --------------------------------------

    def executable_price_at(
        self, coin: str, at_ms: int, *, interval: str = "1m", max_wait_ms: int | None = None,
    ) -> dict[str, Any] | None:
        """Soonest price actually transactable at or after ``at_ms``.

        This is the *open of the next bar to start*, not the bar containing
        ``at_ms``: by the time ``at_ms`` arrives that bar is already part-formed,
        and its open is a price from the past while its close is from the
        future. Returns None when no bar covers the moment — an unknown price
        stays unknown rather than being interpolated.
        """
        limit = max_wait_ms if max_wait_ms is not None else interval_ms(interval) * 2
        row = self.conn.execute(
            """SELECT open_time, open FROM bars
               WHERE coin = ? AND interval = ? AND open_time >= ? AND open_time <= ?
               ORDER BY open_time LIMIT 1""",
            (coin, interval, at_ms, at_ms + limit),
        ).fetchone()
        if row is None:
            return None
        return {"price": row[1], "bar_open_time": row[0], "delay_ms": row[0] - at_ms,
                "interval": interval, "basis": "next_bar_open"}

    def realized_vol_asof(
        self, coin: str, at_ms: int, *, interval: str = "1h", lookback: int = 24,
    ) -> dict[str, Any] | None:
        """Stdev of log returns over bars that had already closed at ``at_ms``.

        Deliberately excludes the bar containing ``at_ms``. Returns None rather
        than a partial figure when the window is not fully covered, so a thin
        patch of history cannot masquerade as a calm regime.
        """
        rows = self.conn.execute(
            """SELECT close FROM bars
               WHERE coin = ? AND interval = ? AND close_time <= ?
               ORDER BY close_time DESC LIMIT ?""",
            (coin, interval, at_ms, lookback + 1),
        ).fetchall()
        if len(rows) < lookback + 1:
            return None
        closes = [r[0] for r in reversed(rows)]
        rets = [math.log(b / a) for a, b in pairwise(closes) if a > 0 and b > 0]
        if len(rets) < 2:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        return {"stdev_log_return": math.sqrt(var), "bars": len(rets),
                "interval": interval, "asof_ms": at_ms}

    def coverage(self) -> list[dict[str, Any]]:
        return [
            dict(zip(("coin", "interval", "bars", "first_open", "last_open",
                      "last_fetched"), row, strict=True))
            for row in self.conn.execute(
                "SELECT * FROM bar_coverage ORDER BY coin, interval"
            ).fetchall()
        ]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> MarketStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
