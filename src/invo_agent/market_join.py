"""Join archived signals to market conditions, without leaking hindsight.

Every field here is either something that was knowable at the stated moment, or
is labelled as a later observation. Nothing is interpolated: where the market
store has no bar, the value is ``None`` and stays ``None``.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from invo_agent.market import MarketStore

# Delays to price a signal at, relative to when our collector received it.
DEFAULT_DELAYS_MS: tuple[int, ...] = (0, 30_000, 60_000, 300_000, 900_000, 3_600_000)


def to_ms(value: Any) -> int | None:
    """Parse an archived timestamp. Unparseable stays unknown rather than 0."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None  # a naive stamp cannot be placed on the market clock
    return int(parsed.timestamp() * 1000)


def market_context(
    store: MarketStore, coin: str, at_ms: int, *,
    price_interval: str = "1m", vol_interval: str = "1h", vol_lookback: int = 24,
) -> dict[str, Any]:
    """What the market looked like at ``at_ms``, using only prior information."""
    executable = store.executable_price_at(coin, at_ms, interval=price_interval)
    vol = store.realized_vol_asof(coin, at_ms, interval=vol_interval, lookback=vol_lookback)
    return {
        "executable_price": executable["price"] if executable else None,
        "executable_delay_ms": executable["delay_ms"] if executable else None,
        "price_basis": executable["basis"] if executable else None,
        "realized_vol": vol["stdev_log_return"] if vol else None,
        "vol_bars": vol["bars"] if vol else None,
        "vol_interval": vol_interval if vol else None,
    }


def first_observations(research_db: Path) -> Iterator[dict[str, Any]]:
    """Earliest stored version of each trade, i.e. the entry-time view.

    Later versions carry revised stops and final prices and are deliberately
    not read here.
    """
    uri = Path(research_db).expanduser().resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """WITH ranked AS (
                   SELECT *, ROW_NUMBER() OVER (
                       PARTITION BY portfolio_id, base_id
                       ORDER BY observed_at ASC, observation_id ASC
                   ) AS version_rank
                   FROM trade_observations
               )
               SELECT observation_id, base_id, portfolio_id, ticker, direction_long,
                      entry_price, leverage, observed_at, source_at, origin
               FROM ranked WHERE version_rank = 1 AND ticker IS NOT NULL
               ORDER BY observed_at"""
        ).fetchall()
    for row in rows:
        yield dict(row)


def signal_rows(
    research_db: Path, store: MarketStore, *,
    delays_ms: tuple[int, ...] = DEFAULT_DELAYS_MS,
    price_interval: str = "1m", vol_interval: str = "1h", vol_lookback: int = 24,
) -> Iterator[dict[str, Any]]:
    """One row per signal: when it was published, when we saw it, and what the
    market would have given us at each delay after that.

    ``origin='backfill'`` rows are marked: a retrospective import says nothing
    about when the agent could first have acted, so their receipt time is not
    evidence of latency.
    """
    for obs in first_observations(research_db):
        coin = str(obs["ticker"]).upper()
        published = to_ms(obs["source_at"])
        received = to_ms(obs["observed_at"])
        row: dict[str, Any] = {
            "observation_id": obs["observation_id"],
            "base_id": obs["base_id"],
            "portfolio_id": obs["portfolio_id"],
            "coin": coin,
            "direction_long": obs["direction_long"],
            "leverage": obs["leverage"],
            "origin": obs["origin"],
            "receipt_is_evidence_of_latency": obs["origin"] != "backfill",
            "published_at_ms": published,
            "received_at_ms": received,
            "publication_delay_ms": (
                received - published if published is not None and received is not None else None
            ),
            # The trader's own claim, kept apart from anything we measured.
            "reported_entry_price": obs["entry_price"],
        }
        if published is not None:
            ctx = market_context(store, coin, published, price_interval=price_interval,
                                 vol_interval=vol_interval, vol_lookback=vol_lookback)
            row["price_at_publication"] = ctx["executable_price"]
            row["realized_vol_at_publication"] = ctx["realized_vol"]
            row["vol_bars"] = ctx["vol_bars"]
        else:
            row["price_at_publication"] = None
            row["realized_vol_at_publication"] = None
            row["vol_bars"] = None

        base = received if received is not None else published
        for delay in delays_ms:
            key = f"price_after_{delay // 1000}s"
            if base is None:
                row[key] = None
                continue
            hit = store.executable_price_at(coin, base + delay, interval=price_interval)
            row[key] = hit["price"] if hit else None
        yield row


def coverage_gaps(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """How much of the joined set is actually priced, so a thin join is visible."""
    total = len(rows)
    priced = sum(1 for r in rows if r.get("price_at_publication") is not None)
    with_vol = sum(1 for r in rows if r.get("realized_vol_at_publication") is not None)
    dated = sum(1 for r in rows if r.get("published_at_ms") is not None)
    return {
        "signals": total,
        "with_publication_time": dated,
        "with_executable_price": priced,
        "with_volatility": with_vol,
        "unpriced": total - priced,
        # Bars older than the source's retention edge cannot be recovered, so a
        # low ratio here is a permanent property of the data, not a fetch bug.
        "priced_ratio": round(priced / total, 4) if total else None,
    }
