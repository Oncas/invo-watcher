"""Read-only market bar import. Public endpoint only; no session, no executor.

The source serves a rolling window of roughly ``MAX_BARS_PER_REQUEST`` bars per
interval. It honours ``startTime`` and truncates the tail, so history is walked
forwards in bounded chunks. Past the retention edge it returns an empty list
rather than an error, which is indistinguishable from "this coin did not trade"
unless the request that produced it is recorded alongside — so it is.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import httpx

from invo_agent.market import MAX_BARS_PER_REQUEST, MarketStore, interval_ms

MAINNET_INFO_URL = "https://api.hyperliquid.xyz/info"


class MarketFetchError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def now_ms() -> int:
    return int(time.time() * 1000)


class CandleSource(Protocol):
    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int
                ) -> list[dict[str, Any]]: ...


class HyperliquidCandles:
    """Public ``candleSnapshot``. Sends no account data and needs no login."""

    name = "hyperliquid"

    def __init__(self, url: str = MAINNET_INFO_URL, *, timeout: float = 30.0):
        self.url = url
        self._client = httpx.Client(timeout=timeout, headers={"Content-Type": "application/json"})

    def candles(self, coin: str, interval: str, start_ms: int, end_ms: int
                ) -> list[dict[str, Any]]:
        response = self._client.post(self.url, json={
            "type": "candleSnapshot",
            "req": {"coin": coin, "interval": interval,
                    "startTime": int(start_ms), "endTime": int(end_ms)},
        })
        if response.status_code != 200:
            raise MarketFetchError(f"candleSnapshot failed (HTTP {response.status_code})")
        body = response.json()
        if body is None:
            return []
        if not isinstance(body, list):
            raise MarketFetchError("candleSnapshot did not return a list")
        for bar in body:
            if not isinstance(bar, dict) or not {"t", "T", "o", "h", "l", "c"} <= bar.keys():
                raise MarketFetchError("candleSnapshot returned a malformed bar")
        return body

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HyperliquidCandles:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class MarketBackfill:
    def __init__(
        self, source: CandleSource, store: MarketStore, *, delay: float = 1.0,
        max_requests: int = 2000, sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], int] = now_ms,
    ):
        if delay < 0.1 or max_requests < 1:
            raise ValueError("delay must be >= 0.1 seconds and max_requests must be positive")
        self.source, self.store = source, store
        self.delay, self.max_requests, self.sleep = delay, max_requests, sleep
        self.clock = clock
        self.run_id = str(uuid4())
        self.stored = 0

    def _request(self, coin: str, interval: str, start: int, end: int
                 ) -> list[dict[str, Any]]:
        wait = self.delay
        for attempt in range(4):
            self.sleep(wait)
            try:
                return self.source.candles(coin, interval, start, end)
            except (MarketFetchError, httpx.TransportError):
                if attempt == 3:
                    raise
                wait = max(self.delay, 2 ** (attempt + 1))
        raise AssertionError("unreachable")

    def retention_start(self, interval: str) -> int:
        """Earliest moment the source still serves for this interval."""
        return self.clock() - MAX_BARS_PER_REQUEST * interval_ms(interval)

    def backfill(
        self, coin: str, interval: str, *, since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> dict[str, Any]:
        step = interval_ms(interval)
        chunk = MAX_BARS_PER_REQUEST * step
        edge = self.retention_start(interval)
        cursor = edge if since_ms is None else max(since_ms, edge)
        until = self.clock() if until_ms is None else until_ms
        requests = empties = 0
        first_stored = last_stored = None

        while cursor <= until and requests < self.max_requests:
            window_end = min(cursor + chunk, until)
            bars = self._request(coin, interval, cursor, window_end)
            fetched_at = utc_now()
            stored = self.store.upsert_bars(coin, interval, bars, source=
                                            getattr(self.source, "name", "unknown"),
                                            fetched_at=fetched_at, now_ms=self.clock())
            self.stored += stored
            requests += 1
            if bars:
                first, last = int(bars[0]["t"]), int(bars[-1]["t"])
                if first_stored is None:
                    first_stored = first
                last_stored = last
                self.store.record_fetch(
                    run_id=self.run_id, coin=coin, interval=interval,
                    requested_start=cursor, requested_end=window_end, bars=bars,
                    stored=stored, fetched_at=fetched_at, outcome="ok")
                advanced = last + step
                if advanced <= cursor:
                    # The window did not move: paging further would loop.
                    raise MarketFetchError(
                        f"{coin} {interval}: source returned no progress past {cursor}")
                cursor = advanced
            else:
                empties += 1
                self.store.record_fetch(
                    run_id=self.run_id, coin=coin, interval=interval,
                    requested_start=cursor, requested_end=window_end, bars=[],
                    stored=0, fetched_at=fetched_at, outcome="empty")
                cursor = window_end + step

        return {
            "coin": coin, "interval": interval, "run_id": self.run_id,
            "requests": requests, "empty_windows": empties,
            "first_open": first_stored, "last_open": last_stored,
            "retention_edge": edge,
            # Reaching the request ceiling means the walk stopped early; the
            # window between last_open and until was never asked for.
            "status": "incomplete" if requests >= self.max_requests else "complete",
        }

    def update(self, coin: str, interval: str) -> dict[str, Any]:
        """Fetch only what is missing since the newest stored bar."""
        row = self.store.conn.execute(
            "SELECT MAX(open_time) FROM bars WHERE coin = ? AND interval = ?",
            (coin, interval),
        ).fetchone()
        since = None if row is None or row[0] is None else int(row[0]) + interval_ms(interval)
        return self.backfill(coin, interval, since_ms=since)


def coins_from_archive(research_db_path: str) -> list[str]:
    """Tickers the archive has actually observed, which are the coins worth storing."""
    import sqlite3
    from contextlib import closing
    from pathlib import Path

    uri = Path(research_db_path).expanduser().resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM trade_observations "
            "WHERE ticker IS NOT NULL AND ticker <> ''"
        ).fetchall()
    return sorted({str(r[0]).upper() for r in rows})
