from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import httpx
import typer

from invo_agent.config import get_settings
from invo_agent.market import INTERVAL_MS, MarketStore, validate_market_path
from invo_agent.market_fetch import (
    HyperliquidCandles,
    MarketBackfill,
    MarketFetchError,
    coins_from_archive,
    tradable_coins,
)
from invo_agent.market_join import DEFAULT_DELAYS_MS, coverage_gaps, signal_rows

market_app = typer.Typer(no_args_is_help=True, help="Collect and join market bars.")

DEFAULT_INTERVALS = "1m,1h,4h"


def _resolve_coins(coin: list[str] | None, research_db: Path) -> list[str]:
    if coin:
        return sorted({c.upper() for c in coin})
    if not research_db.expanduser().resolve().is_file():
        raise typer.BadParameter(
            "No research archive to take coins from; pass --coin, or run a research backfill"
        )
    found = coins_from_archive(str(research_db))
    if not found:
        raise typer.BadParameter("The research archive has no observed tickers yet")
    return found


def _intervals(raw: str) -> list[str]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    unknown = [p for p in parts if p not in INTERVAL_MS]
    if unknown:
        raise typer.BadParameter(f"Unsupported interval(s): {', '.join(unknown)}")
    return parts


@market_app.command("backfill")
def backfill(
    coin: list[str] | None = typer.Option(None, "--coin", "-c",
                                          help="Coin symbol; repeatable. Default: from archive"),
    intervals: str = typer.Option(DEFAULT_INTERVALS, help="Comma-separated bar intervals"),
    database: Path | None = typer.Option(None, "--database", help="Market SQLite file"),
    delay: float = typer.Option(1.0, min=0.1, help="Minimum seconds between requests"),
    update_only: bool = typer.Option(False, "--update", help="Only fetch since the newest bar"),
) -> None:
    """Import bars from the public Hyperliquid endpoint. Never trades or logs in."""
    try:
        settings = get_settings()
        path = database or settings.market_db_path
        validate_market_path(path, settings.db_path, settings.log_dir)
        coins = _resolve_coins(coin, settings.research_db_path)
        wanted = _intervals(intervals)
        results = []
        with MarketStore(path) as store, HyperliquidCandles() as source:
            # The archive names coins this venue never listed; asking for one
            # answers HTTP 500, which would otherwise end the whole run.
            coins, unlisted = tradable_coins(coins, source)
            if not coins:
                raise typer.BadParameter(
                    "None of the requested coins are listed on the venue")
            importer = MarketBackfill(source, store, delay=delay)
            for symbol in coins:
                for interval in wanted:
                    if update_only:
                        results.append(importer.update(symbol, interval))
                    else:
                        results.append(importer.backfill(symbol, interval))
    except (MarketFetchError, httpx.HTTPError, sqlite3.Error, OSError, ValueError) as exc:
        detail = str(exc) if isinstance(exc, MarketFetchError) else type(exc).__name__
        typer.echo(f"Market import stopped: {detail}. Stored bars remain.", err=True)
        raise typer.Exit(1) from None
    incomplete = [r for r in results if r["status"] != "complete"]
    typer.echo(json.dumps({
        "stored_bars": importer.stored, "scopes": len(results),
        "skipped_unlisted": unlisted,
        "incomplete": incomplete, "results": results,
    }, indent=2))
    if incomplete:
        raise typer.Exit(1)


@market_app.command("status")
def status(database: Path | None = typer.Option(None, "--database")) -> None:
    """Report bar coverage per coin and interval without any network access."""
    path = (database or get_settings().market_db_path).expanduser().resolve()
    if not path.is_file():
        typer.echo(f"No market database at {path}")
        raise typer.Exit(1)
    with MarketStore(path) as store:
        rows = store.coverage()
        empty = store.conn.execute(
            "SELECT COUNT(*) FROM fetch_runs WHERE outcome = 'empty'"
        ).fetchone()[0]
    typer.echo(json.dumps({"database": str(path), "coverage": rows,
                           "empty_windows": empty}, indent=2))


@market_app.command("join")
def join(
    database: Path | None = typer.Option(None, "--database", help="Market SQLite file"),
    research: Path | None = typer.Option(None, "--research", help="Research archive"),
    price_interval: str = typer.Option("1m", help="Interval used for executable prices"),
    vol_interval: str = typer.Option("1h", help="Interval used for the volatility window"),
    vol_lookback: int = typer.Option(24, min=2, help="Closed bars in the volatility window"),
    out: Path | None = typer.Option(None, "--out", help="Write rows as JSON lines here"),
    summary_only: bool = typer.Option(False, "--summary", help="Print coverage only"),
) -> None:
    """Join archived signals to entry-time market conditions. Read-only throughout."""
    settings = get_settings()
    market_path = (database or settings.market_db_path).expanduser().resolve()
    research_path = (research or settings.research_db_path).expanduser().resolve()
    for label, p in (("market", market_path), ("research", research_path)):
        if not p.is_file():
            typer.echo(f"No {label} database at {p}", err=True)
            raise typer.Exit(1)
    try:
        with MarketStore(market_path) as store:
            rows = list(signal_rows(
                research_path, store, price_interval=price_interval,
                vol_interval=vol_interval, vol_lookback=vol_lookback))
    except (sqlite3.Error, OSError, ValueError) as exc:
        typer.echo(f"Join stopped: {type(exc).__name__}", err=True)
        raise typer.Exit(1) from None
    gaps = coverage_gaps(rows)
    if out is not None and not summary_only:
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row) + "\n")
        gaps["written_to"] = str(out)
    typer.echo(json.dumps({
        "coverage": gaps,
        "delays_priced_seconds": [d // 1000 for d in DEFAULT_DELAYS_MS],
        "rows": None if (summary_only or out is not None) else rows,
    }, indent=2))
