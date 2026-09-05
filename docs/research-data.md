# Research database

The collector stores data in **SQLite**, by default `~/.invo-agent/research.db`.
It is separate from `agent.db` and from the seven-day operational log retention.
Research records are never automatically pruned. SQLite WAL mode allows readers
while the collector appends observations. No additional dependency is required.

## Historical backfill

Use the existing social login on the machine that holds it:

```sh
invo-agent research backfill --followed
invo-agent research status
```

Or select specific portfolios, including portfolios you do not currently follow
if your account can access their history:

```sh
invo-agent research backfill --portfolio PORTFOLIO_UUID --portfolio ANOTHER_UUID
```

The command fetches **closed and open ticker investments**, then each investment's
available history posts, including updates and closing posts. It never invokes
the trading executor, opens `agent.db`, changes the copy allowlist, or starts a
poller. It uses the existing social session and its normal token refresh; it does
not initiate OTP or wallet authorization. Selecting `--followed` records the
current selection and portfolio metadata without modifying trading state.

Options:

- `--database PATH`: override the research database location.
- `--delay 1.0`: minimum pause before each request (minimum allowed: 0.25 seconds).
- `--max-pages 10000`: limit per paginated list. Hitting this limit is reported as
  incomplete, never as a successful full import.
- `--no-revisions`: collect current investment records only. The manifest records
  this restriction; use the default to collect available history posts too.

Requests are sequential. Transient failures retry up to three times with backoff;
`Retry-After` is respected. Persistent rate limits and authentication/permission
errors stop the run. Other failed lists are recorded as incomplete, and the CLI
returns a nonzero status. Repeated pages or malformed responses are failures,
not empty history. Pagination continues until an empty page so a server page-size
cap does not silently truncate the import.

Each record commits independently. Interrupted imports can be safely rerun: the
collector starts at page one, deduplicates unchanged records, and retains changed
versions. It deliberately rescans rather than trusting offset checkpoints because
new and recently closed trades can shift page boundaries. Run again periodically
to discover newly closed trades and corrections. There is no automatic backfill
scheduler in this change.

## Live capture

The `run` and `dry-run` CLI paths attach the archive to the existing poller. The
default is `RESEARCH_ENABLED=true`; `RESEARCH_DB_PATH` overrides the location.
This takes effect when the updated code is next started through those paths. A
research archive that cannot be opened or validated disables capture with a
warning; it never prevents the trader from starting.
The `once` debugging command and direct library use do not attach the collector.

Captured datasets:

| Kind | Contents |
|---|---|
| `notification` | Original notification fields, captured before model parsing/filtering |
| `post`, `latest_post` | Original responses for posts the existing agent requests |
| `followed_portfolios` | Responses from the existing followed-portfolio refresh |
| `decision_inputs` | Enriched signal, selected portfolios, mode and configured position limits |
| `notification_disposition` | Processed/ignored/startup-skipped/queued disposition and summary |
| `action_result` | Existing action-log result, notification ID, dry-run flag and summary |
| `capture_started`, `capture_gap` | Collection scope and known dropped/failed record counts |

Live capture adds no API calls. It observes only data fetched by the current
poller, so it cannot recover notifications missed between polls, enrich every
ignored signal, or recover pre-observation post versions. It does not yet record
a continuous market feed, all exchange fills, account snapshots, or the complete
order lifecycle. Action results are agent reports, not independent fill evidence.

A bounded queue and dedicated writer keep database I/O off the trading thread.
Input copying/serialization still happens on the calling thread. Disk failures
and queue saturation do not fail orders; they increment warning counters and write
a gap record when storage recovers. Shutdown allows five seconds to drain.
Unflushed records can be lost on a crash, shutdown timeout, or sustained storage
failure. Monitor archive growth and collector warnings; archival storage is finite.

## Database layout and queries

`observations` is an append-only application journal with indexed `kind`,
`entity_id`, `portfolio_id`, and `observed_at`, plus `origin`, `source_at`,
`run_id`, `schema_version`, `redacted_keys`, `last_seen_at`, `seen_count`, and
sanitized JSON `payload`. Unknown source fields are retained.

Credential fields are removed recursively, matched on the whole normalized key
name rather than as a substring, so instrument and market fields such as
`tokenAddress`, `tokenSymbol` or `sessionHigh` are preserved while `accessToken`
or `x-api-key` are not. Every removed key path is listed in `redacted_keys`, so
"absent at source" stays distinguishable from "deleted here". Authentication
responses, request headers, and wallet sessions are never passed to the collector.

Deduplication includes dataset, origin, identity, portfolio and payload content.
Identical source content is stored once, but a repeat observation advances that
row's `last_seen_at` and `seen_count`. That matters for training: a row with
`observed_at` far below `last_seen_at` is evidence the value was *still* in place
at the later time, not merely that we once saw it. Repeated action decisions are
separate records. This is application-level append-only behavior, not a database
permission boundary.

`entity_id` is the investment **base** id for `investment`, `history_post`,
`post` and `latest_post`, so live and backfilled records join on it directly. For
`decision_inputs`, `notification_disposition` and `action_result` it is the
notification id. The schema is versioned in `PRAGMA user_version`; opening an
older archive adds missing columns in place and rebuilds the views.

`trade_observations` exposes investment fields as SQL columns while retaining the
full JSON: identity (`base_id`, `source_base_id`, `investment_id`, `ticker`,
`instrument_name`, `owner_id`, `owner_username`, `portfolio_title`), position
(`entry_price`, `closing_price`, `current_price`, `liquidation_price`,
`leverage`, `position_size`, `entry_size`, `entry_sim`), risk (`price_target`,
`stop_loss`), outcome (`is_open`, `active`, `closed_at`,
`reported_percent_change`), source-reported revision deltas (`changes`), and
provenance (`origin`, `observed_at`, `last_seen_at`, `seen_count`, `source_at`,
`redacted_keys`, `source_verified_trade`).

`base_id` falls back to the investment id when the source omitted `baseId`;
`source_base_id` is NULL in exactly that case, and the backfill reports such a
run as incomplete, because those records cannot be grouped into revisions.

It contains **versions**, not one row per completed trade. To reconstruct what
was knowable at decision time — the query to use when building features — bound
it by `observed_at` and never look past it:

```sql
-- State of each trade as of :as_of, using nothing observed later.
WITH ranked AS (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY portfolio_id, base_id
    ORDER BY observed_at DESC, observation_id DESC
  ) AS version_rank
  FROM trade_observations
  WHERE observed_at <= :as_of
)
SELECT portfolio_id, base_id, ticker, direction_long, entry_price,
       price_target, stop_loss, leverage, is_open
FROM ranked
WHERE version_rank = 1;
```

The query below instead returns the **latest** version per trade. It is useful
for inspecting the archive, and is not safe as a feature source: closing prices,
revised stop-losses and final outcomes are all lookahead relative to the entry.

```sql
WITH ranked AS (
  SELECT *, ROW_NUMBER() OVER (
    PARTITION BY portfolio_id, base_id
    ORDER BY observed_at DESC, observation_id DESC
  ) AS version_rank
  FROM trade_observations
)
SELECT portfolio_id, base_id, ticker, direction_long, entry_price,
       closing_price, leverage, is_open, closed_at
FROM ranked
WHERE version_rank = 1;
```

The original per-trade owner and portfolio objects remain in `payload` and can be
queried with SQLite `json_extract`. `history_post.entity_id` links to investment
base IDs; notifications and decisions link through notification IDs and the base
IDs contained in their payloads. Run manifests record selection, scope and
completion. `research status` reports dataset counts and the latest run state.

Use SQLite's backup API or an SQLite-aware backup tool for a running database;
copying only the `.db` file while WAL writes are active can omit recent commits.

## Interpretation for training

- `observed_at` is when this collector received/recorded the data. `source_at` is
  the source's reported `createdAt`, where present. Missing fields stay unknown.
- `origin=backfill` means retrospective data. It does **not** establish when your
  agent could first have seen the signal. `customEntryDate` may be user-selected;
  it is never substituted for signal publication or receipt time.
- Available history posts can already contain edits. Final prices, updated TP/SL,
  and eventual outcomes must not be used as entry-time features without evidence
  that they were available then.
- `verifiedTrade` and `percentChange` are preserved as source claims. The importer
  does not infer actual exchange execution, net profit, fees, or funding from them.
- A completed run means the requested lists reached their exposed end. It cannot
  prove that Involio retained deleted trades or every historic revision. Offset
  pagination is not an atomic snapshot; periodic rescans help with moving lists.
- Selecting today's followed portfolios introduces selection bias. Save broader
  accessible cohorts and selection dates for evaluation; do not treat current
  winners as an unbiased historical universe.

## API evidence and verification

Request shapes were read from the public
[`main.dart.js`](https://app.invoapp.com/main.dart.js) on 2026-09-05:

- `POST /v1_0/investments/get_investments`: `portfolioId`, `isOpen`,
  `params: {page, size}`; response `investmentsTicker` plus other asset lists.
- `POST /v1_0/posts/get_investment_history`: `investmentBaseId`,
  `params: {page, size, shadowInvestmentsOnly: false}`; response `items`.

The archive also retains complete sanitized page responses, including other asset
lists, but per-investment indexing and revision expansion target ticker trades.
The public app demonstrates page parameters; deeper pagination is defended by
repeated-page checks. These are private application APIs and may change.

Tests use synthetic records, mocked HTTP transports, and temporary databases. No
authenticated production backfill or production service restart was performed
during development.

Continuous market-data collection and model training are separate follow-on work,
and the market series belongs in its own store rather than in `observations`:
that table holds thousands of JSON observations, while bar data is orders of
magnitude larger and columnar. A Parquet or DuckDB dataset joined to this archive
on `observed_at`/`source_at` keeps both in the shape that suits them. Until that
exists, questions about regime dependence or the cost of copy delay cannot be
answered from this archive alone — it records when a signal was published and
when we received it, but not what price was executable at either moment.
