# Market data

Bars live in their own **SQLite** file, by default `~/.invo-agent/market.db`,
separate from both `agent.db` and the research archive. Nothing here logs in,
signs, or touches an executor: it reads the public Hyperliquid `candleSnapshot`
endpoint and writes bars.

Hyperliquid is the venue we actually execute on, so its bars are the closest
available stand-in for prices we could have transacted at. They are still not
fills: no spread, no slippage, no funding, no fees.

## The retention limit — read this first

The source serves a rolling window of roughly **5000 bars per interval**. It
honours `startTime` and truncates the tail, and past its retention edge it
returns an empty list rather than an error. Measured on 2026-09-05:

| interval | oldest bar available | reach |
|---|---|---|
| `1m` | 6 days | days |
| `5m` | 20 days | weeks |
| `15m` | 55 days | ~2 months |
| `1h` | 211 days | ~7 months |
| `4h` | 836 days | ~2 years |
| `1d` | listing date | full |

Two consequences shape everything below.

**Minute history is use-it-or-lose-it.** Every day that passes, another day of
`1m` bars falls off the far end and cannot be recovered from this source at any
price. Questions that need minute resolution — above all *"does copying after a
delay eliminate the edge?"* — are answerable only **going forward**, from bars
captured before they expire. Backfilling them later is not an option.

**Regime questions are answerable retrospectively.** `1h` reaches back months
and `4h` back years, which is ample for realized-volatility context. *"Does this
trader's BTC signal perform better during low volatility?"* can be asked of
history that already exists.

So run the update command on a schedule now, even before the analysis exists:

```sh
invo-agent market backfill --intervals 1m --update
```

Anything not captured today is gone in a week.

## Commands

```sh
invo-agent market backfill              # coins taken from the research archive
invo-agent market backfill --coin BTC --coin ETH --intervals 1m,1h,4h
invo-agent market backfill --update     # only what is missing since the newest bar
invo-agent market status
invo-agent market join --out signals.jsonl
```

`backfill` walks forward from the retention edge in bounded chunks. With no
`--coin`, it takes the coin list from tickers the research archive has actually
observed, so it collects what the followed traders trade and nothing else.
Reaching the request ceiling is reported as `incomplete` and exits non-zero;
it is never reported as a full import. Requests are sequential and paced.

## What is stored

`bars` is keyed on `(coin, interval, open_time)` with open/high/low/close,
volume, trade count, source, and `fetched_at`. Re-fetching is safe: a stored bar
is replaced only by a later fetch of the same slot, which is how the source's own
corrections land.

**A bar still forming is never stored.** Its high, low and close are not final,
and a partial bar written as final is indistinguishable from a real one later.

`fetch_runs` records what each request *asked* for against what came back —
first and last bar, count, and outcome. Because an empty response means both
"past retention" and "this coin never traded here", a gap is only interpretable
next to the request that produced it.

## Reading without lookahead

Two accessors, both of which refuse rather than guess.

`executable_price_at(coin, t)` returns the **open of the next bar to start** at
or after `t`. It deliberately does not return the bar containing `t`: by the
time `t` arrives that bar is part-formed, its open is a price from the past and
its close is a price from the future. Using either as "the price at `t`" leaks
hindsight into an entry-time feature. When no bar covers the moment it returns
`None` — unknown stays unknown, never interpolated or nearest-matched.

`realized_vol_asof(coin, t)` uses only bars whose close is at or before `t`, and
returns `None` when the lookback window is not fully covered. A partial window
would report a thin patch of history as a calm regime.

## Joining to signals

```sh
invo-agent market join --out signals.jsonl
```

One row per signal, taken from the **earliest** stored version of each trade —
the entry-time view. Later versions carry revised stops and final prices and are
deliberately not read.

Each row carries when the source says the trade was published, when our
collector received it, the gap between them, the executable price at
publication, the volatility regime at publication, and the price at a series of
delays after receipt. `reported_entry_price` is the trader's own claim and is
never substituted for a measured price.

Rows from `origin=backfill` are flagged `receipt_is_evidence_of_latency: false`.
A retrospective import's receipt time is when we ran the importer, not when the
agent could first have acted, and treating it as latency would invent a delay
that never happened.

The `coverage` block reports how much of the join is actually priced. A low
ratio is usually the retention edge rather than a fetch bug — signals older than
the interval's reach cannot be priced at that interval, ever.

## What this still does not give you

- **Fills.** Bar opens are not executions. No spread, slippage, fees, funding,
  or partial fills. Estimated copy returns computed from these are upper bounds.
- **Order book.** No depth, so size-dependent impact is invisible. A clip large
  relative to the book would have moved it.
- **Pre-retention minutes.** Gone, permanently.
- **Non-Hyperliquid venues.** A trader's reported entry may be from a different
  venue at a different price. `reported_entry_price` is kept apart for exactly
  this reason.

## Why SQLite and not Parquet or DuckDB

Bars are a tight numeric table, and at the volumes in play — a few thousand bars
per coin per interval, plus roughly 1440 minute bars per coin per day of forward
capture — SQLite handles it comfortably while adding no dependency to a process
that runs a live trader. DuckDB can read this file directly if the analysis side
ever wants columnar scans, so nothing here forecloses that.
