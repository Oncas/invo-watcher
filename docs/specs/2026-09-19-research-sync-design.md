# Research sync: VPS buffers, the laptop archives

## Why

The research archive lives on the VPS at `/home/invo/.invo-agent/research.db`,
growing as new observations arrive. It is where analysis happens, and it
is on a box that also runs a live trader. Two things follow.

Analysis wants the data local. Working over SSH against a file that a live
process is writing is awkward and slow, and the machine that should hold the
long-term record is the one with a backup story and a keyboard.

The VPS should hold only what the agent there is currently producing. Anything
older has no operational purpose: the trader reads `agent.db`, never the
archive. A buffer that only spans the gap since the last successful pull is
enough.

The archive cannot simply be relocated. The collector writes to it continuously
from the trading thread's queue and must never block a trade, so it cannot
depend on a laptop being awake or on a network filesystem. The workable shape is
a pull: the VPS writes locally as it does now, the laptop pulls incrementally,
and `~/Work/invo-db` becomes the archive of record.

Market bars are a separate case and need no sync at all. They come from the
public Hyperliquid `candleSnapshot` endpoint — no session, no account — so the
laptop fetches them directly. This also fixes a standing gap: nothing has ever
scheduled market capture, no `market.db` exists on the VPS, and `1m` bars expire
from the source within about six days.

## Goals

- `~/Work/invo-db/research.db` is the archive of record, accumulating forever.
- The VPS holds only observations since the laptop's last confirmed pull.
- Pulls are incremental, resumable, and safe to interrupt.
- Pruning the VPS can never delete something the laptop did not receive.
- Market bars are captured locally, on a schedule, starting immediately.
- Historical backfills pass through the VPS without accumulating there.
- No change to trading behaviour.

## Non-goals

- Syncing `agent.db`. Live trading state stays on the VPS.
- Syncing operational logs into the research database. Logs are files and are
  pulled as files.
- Two-way sync. The laptop never writes back to the VPS archive.
- A second Involio session on the laptop. See "Backfill passthrough".
- Exact `seen_count` across a prune-and-re-observe cycle. See "The merge rule".

## Architecture

```
VPS                                          Laptop
───                                          ──────
agent (poller) ──> research.db  ──ssh──>  research pull ──> ~/Work/invo-db/research.db
                   (rolling buffer)                          (archive of record)

research backfill --database /tmp/bf.db ──> pull --from-file ──> merged, remote deleted

logs/history/*.{log,events.jsonl} ──scp──> ~/Work/invo-db/logs/

                                 Hyperliquid (public) ──> ~/Work/invo-db/market.db
```

The laptop holds all sync state. The VPS is stateless with respect to the sync:
it answers export queries and, when explicitly told, deletes rows below a
watermark it is handed. It never tracks what has been pulled.

## Local archive layout

```
~/Work/invo-db/
  research.db                          same schema as the VPS archive
  market.db                            bars, fetched locally
  logs/YYYY-MM-DD.log
  logs/YYYY-MM-DD.events.jsonl
  sync-state.json
```

`sync-state.json`:

```json
{
  "sources": {
    "vps": {
      "archive_uuid": "00000000-0000-4000-8000-000000000013",
      "last_id": 1000,
      "last_max_id": 1000,
      "rescan_days": 14,
      "last_pull_at": "2026-09-19T14:03:11+00:00",
      "last_pull_status": "ok"
    }
  }
}
```

`archive_uuid` binds this entry to one remote archive; a pull whose header
carries a different UUID is refused rather than merged into the wrong
watermark. `last_id` is the highest `observations.id` committed locally from
that source. `last_max_id` is the `max_id` from the last completed export — the
value prune is handed. `rescan_days` records the window this source was last
pulled with, so prune can see it without being told. `last_pull_at` is a **VPS**
clock value taken from the export marker's `update_boundary`, and advances only
when a pull completes in full; an interrupted pull leaves it alone so the next
run re-scans the same window.

## The merge rule

Idempotency requires a unique identity on every row. `dedupe_key` today is
nullable and `append()` defaults to `deduplicate=False` (`research.py:173`), so
a re-run of the same export duplicates rows the collector produced without a
content hash — backfill markers, capture gaps, selection metadata. Two schema
changes make the merge sound:

- The archive stores its own `archive_uuid` in a one-row `meta` table,
  generated on first open. It rides on every export line and every marker.
- Every row carries a non-NULL `dedupe_key` — enforced in application code, not
  by the schema; see *Migration mechanics* below — populated three ways:
  - **Content-deduplicated rows** (`deduplicate=True`) keep today's
    deterministic content hash, unchanged.
  - **Intentionally distinct rows** (`deduplicate=False`) get a
    source-generated unique key namespaced by archive: `<archive_uuid>:<uuid4>`.
    These rows are distinct by intent, so determinism buys nothing —
    uniqueness and stability-once-written are the only requirements.
  - **Existing NULL keys** migrate deterministically to
    `<archive_uuid>:<observations.id>`. Determinism matters here: the migration
    must be re-runnable, producing the same key on a retry or against a copy of
    the archive *taken after its `archive_uuid` was assigned*. Two copies of a
    pre-migration database each generate their own UUID on first open and would
    diverge — so assigning archive identity is the first migration step, before
    any key is written.

**Migration mechanics.** SQLite cannot `ALTER COLUMN`. `dedupe_key TEXT UNIQUE`
(`research.py:52`) could only become `NOT NULL` by rebuilding the table — create
new, copy the archive, drop, rename, recreate indexes and views — needing roughly
double the disk and an exclusive write window against a live collector. Not
worth it. The declaration stays nullable and the invariant lives in application
code: `append()` always supplies a key, a one-time migration backfills existing
NULLs, and an open-time check fails loudly if any NULL key survives. A table
rebuild during a planned maintenance stop stays available if DB-level
enforcement is ever wanted.

Export emits `dedupe_key` verbatim, and the puller never recomputes it. A key
recomputed on the laptop would not match the remote's, and every replay would
insert a duplicate.

Merging reuses the collector's insert (`INSERT OR IGNORE INTO observations ...`,
`research.py:198`). With every row carrying a non-NULL key and the column's
existing `UNIQUE` index doing the work, a row pulled twice inserts once. Local
`id`s are assigned locally and never collide with remote ones, which is what
makes it safe to merge a live pull and a backfill file into one archive.

The update half is deliberately **not** the collector's. The collector runs
`seen_count = seen_count + 1` because it genuinely observed the value again. A
merge observes nothing; incrementing would fabricate sightings, and pulling
twice would inflate the count. The merge takes the maximum, NULL-safe:

```sql
UPDATE observations
   SET seen_count   = MAX(COALESCE(seen_count, 0),
                          COALESCE(:remote_seen_count, 0)),
       last_seen_at = CASE
                        WHEN last_seen_at IS NULL THEN :remote_last_seen_at
                        WHEN :remote_last_seen_at IS NULL THEN last_seen_at
                        ELSE MAX(last_seen_at, :remote_last_seen_at)
                      END
 WHERE dedupe_key = :dedupe_key;
```

Pulling the same rows twice leaves the archive **logically identical**: same
rows, same `dedupe_key`s, same `seen_count`s and `last_seen_at`s. It is not
byte-identical — SQLite reuses freed pages and updates the WAL — and no test
should assert that.

**`seen_count` is approximate across a prune boundary.** If a
content-deduplicated row is pruned from the VPS and its content is later
observed again, the collector inserts a fresh row with `seen_count = 1`.
Merging that against a local row already holding, say, 12 takes the max and
keeps 12, dropping the new sighting. This is accepted rather than fixed:
`seen_count` is analytical, `last_seen_at` still advances correctly, and the
alternatives — counter tombstones on the VPS, or per-source counter tracking on
the laptop — cost more than the precision is worth today. If exact sighting
cadence later matters analytically, per-source counter tracking is the way in;
it needs no schema change on the VPS.

## Export

`invo-agent research export --after-id N --since ISO8601 [--database PATH]`

Runs on the VPS. Opens the archive read-only, writes NDJSON to stdout, writes
nothing. Every line is a versioned envelope; the puller rejects unknown `type`
values so the protocol can evolve without silent divergence.

First line — the header. `remote_now` is read **immediately before** the first
query that establishes the transaction's snapshot, never after. Stamping it
afterwards would let a commit land between the snapshot and the timestamp: that
row's `last_seen_at` would fall below the claimed boundary while being absent
from the export, and the next pull — starting its window at that boundary —
would skip it forever. Stamping before can only over-deliver: rows committed
between the stamp and the snapshot are included but not claimed, and are
re-scanned harmlessly on the next pull.

```json
{"type": "header", "protocol": 1, "archive_uuid": "…", "after_id": N,
 "since": "…", "remote_now": "2026-09-19T14:03:11+00:00"}
```

Then rows, in two phases so that batched watermark advance is sound:

1. **Updates** — rows where `id <= N AND last_seen_at > :since`, any order.
   These carry ids at or below the watermark and never advance it.
2. **New** — rows where `id > N`, `ORDER BY id ASC`.

Each row line:

```json
{"type": "row", "phase": "update" | "new", …columns…}
```

Final line:

```json
{"type": "complete", "protocol": 1, "archive_uuid": "…",
 "max_id": M, "update_boundary": "<remote_now>", "counts": {…}}
```

Absence of a `type: "complete"` line means the stream was truncated, and the
puller treats it as a partial pull. `update_boundary` equals the header's
`remote_now`, i.e. the VPS-clock point up to which updates are guaranteed
included. The puller uses that value — never `time.now()` on the laptop — for
anything the prune predicate later depends on.

## Pull

`invo-agent research pull --remote invo@HOST --dest ~/Work/invo-db
[--rescan-days 14] [--remote-bin /opt/invo-agent/.venv/bin/invo-agent]`

Runs on the laptop.

0. Take an exclusive `flock` on `<dest>/pull.lock` for the whole run. A second
   pull exits immediately with a clear message. This is what makes the state
   file safe against concurrent invocations; a SQLite write lock alone does not
   serialize the `sync-state.json` read-modify-write.
1. Read `sync-state.json` for `last_id` and `last_pull_at` (a VPS timestamp
   from the previous marker, not a laptop clock).
2. `since = min(last_pull_at, now - rescan_days)`. `now` here is the laptop
   clock and only picks the window's outer bound; the boundary the prune
   predicate later relies on comes from the remote header.
3. `ssh -C <remote> <remote-bin> research export --after-id <last_id> --since <since>`.
   `-C` turns on SSH's own zlib compression; there is no separate `gzip`
   process. The first pull moves the full archive as JSON, compressed on the wire.
4. Read the header. Capture `archive_uuid` and `remote_now`. Reject if
   `archive_uuid` differs from the one already recorded for this source
   (unless this is the source's first pull, in which case record it).
5. Merge in batches of 1,000 rows, one transaction per batch. Only
   `phase: "new"` rows advance `last_id`; `phase: "update"` rows are merged
   with the same watermark.
6. After each committed batch, write `sync-state.json` atomically: write to
   `sync-state.json.tmp`, `flush`, `fsync`, `os.replace` into place.
7. On the `type: "complete"` line, set `last_pull_at = update_boundary`
   (VPS clock, from the header), `last_max_id = max_id`,
   `last_pull_status = "ok"`, and atomic-write again.

Interrupting at any point leaves a consistent archive and a watermark pointing
at the last committed batch. Re-running resumes from there. Because
`last_pull_at` did not advance, the re-scan window is repeated, which is
harmless — the merge is idempotent.

**Why 14 days.** The rescan window covers delayed updates to previously
observed records. Validate the window against your own collection activity.
An id watermark assumes IDs advance monotonically with observation time.

`--from-file PATH` merges a local NDJSON dump or SQLite file instead of SSHing,
using the same merge rule. This is the backfill path, and it has **separate
state semantics**: a backfill database is its own archive with its own
`archive_uuid`, so it can neither be validated against the `vps` entry nor
advance its watermark. Its ids are meaningless to that source. Concretely,
`--from-file`:

- Takes the destination `flock`, like any other merge.
- Merges the file in full — there is no watermark to resume from.
- Validates that every row carries one consistent `archive_uuid`, and rejects a
  file mixing sources.
- Neither reads nor writes the `vps` entry in `sync-state.json`.
- Relies on stable `dedupe_key`s for whole-file replay idempotency: importing
  the same backfill twice is a no-op.

## Prune

`invo-agent research prune --ack PATH [--keep-days 14] [--vacuum] [--yes]`

Runs on the VPS. Dry-run unless `--yes`. `--ack` points at the laptop's
`sync-state.json` for this archive (or a JSON extract of it), copied over.
Prune refuses unless **all** hold:

- `ack.archive_uuid` equals this archive's `meta.archive_uuid`.
- `ack.last_max_id <= max(observations.id)`.
- `ack.last_pull_at` parses and is not in the future.

A *stale* ack is a warning, not a refusal. The delete predicate is anchored to
`ack.last_pull_at` rather than to the current clock, so an old ack simply
prunes less; prune prints the ack's age and proceeds.

There is deliberately **no** `keep_days >= rescan_days` requirement.
Correctness does not depend on the two windows' relative size:
`since = min(last_pull_at, now - rescan_days)` means every pull re-scans the
whole interval since the last completed pull, so an update to an old row is
caught by the next successful pull whatever `rescan_days` is. `rescan_days`
only buys margin beyond that guarantee.

Deletes rows where **all** hold:

- `id <= ack.last_max_id`.
- `COALESCE(last_seen_at, observed_at) < ack.last_pull_at - keep_days`, using
  the VPS clock throughout. Nothing inside the pull-plus-rescan window is
  removed even if its id is below the watermark.

It reports counts before and after.

Deletion in SQLite does not shrink the file; freed pages are reused. `--vacuum`
runs `VACUUM` to reclaim disk, which needs an exclusive lock and will contend
with the collector. Prune without `--vacuum` is safe against the running agent
(the collector's bounded queue absorbs brief write contention). Prune with
`--vacuum` should be run with the service stopped.

There is no timer. Pruning is always an explicit action.

## Backfill passthrough

`research backfill` already takes `--database`, so no new code is needed:

```sh
# on the VPS
invo-agent research backfill --portfolio ... --database /tmp/bf-$(date +%s).db --no-revisions
# on the laptop
invo-agent research pull --from-file <scp'd file>
# then delete the remote file
```

The backfill runs where the Involio session already is. This deliberately avoids
a second session on the laptop: `client.refresh_tokens()` rotates the refresh
token on every refresh and saves the new one, so two clients on one account can
invalidate each other. `device_id` probably scopes sessions per device, but the
downside of being wrong is the live trader logged out while holding positions,
and the workaround costs nothing.

## Logs

`invo-agent logs pull --remote invo@HOST --dest ~/Work/invo-db/logs`

Copies `history/YYYY-MM-DD.log` and `history/YYYY-MM-DD.events.jsonl` that are
not already present locally. The live `agent.log` and `events.jsonl` are not
copied.

Two current-code bugs must be fixed before this is safe:

- `setup_logging` binds today's date once at startup (`logging_setup.py:41`),
  so a process running across midnight keeps writing to yesterday's dated file
  — "already present" would then skip a file the VPS is still appending to.
  `TimedRotatingFileHandler` does **not** fix this: it holds `baseFilename`
  fixed and, on rollover, renames the *old* file to `<base>.YYYY-MM-DD` and
  reopens the same base name. That inverts the naming this spec depends on.
  Instead add a small date-aware handler that, on each `emit`, compares today's
  date against the open file's date and — on a change — closes the old handle
  and opens `history/<today>.log`. The dated file is then closed exactly at the
  rollover and never touched again, which is what makes "already present" a
  sufficient skip condition for anything but the current day.
- The events sink needs the same behaviour but not the same code: it is not a
  logging handler. `events.configure_events` stashes a module-global
  `_daily_path` once (`events.py:17-32`) and appends to it directly. Give it
  the same date check at write time, resolving `history/<today>.events.jsonl`
  per append.
- `LOG_RETAIN_DAYS` is a module constant, read only as `prune_logs()`'s default
  kwarg (`logging_setup.py:12,112`). Nothing plumbs the env var through, so
  `LOG_RETAIN_DAYS=…` in `/etc/invo-agent.env` does nothing today. Add
  `Settings.log_retain_days` (env-bound), and pass it explicitly to
  `prune_logs()` from the agent's periodic housekeeping.

Once both are in place, set `LOG_RETAIN_DAYS=14` in `/etc/invo-agent.env`
(bounded by the longest expected laptop-offline window).

## Market bars, locally

```sh
invo-agent market backfill --database ~/Work/invo-db/market.db \
  --intervals 1m --update
```

No VPS involvement. One ordering constraint: with no `--coin`, `_resolve_coins`
takes the coin list from the research archive (`market_cli.py:24`) and raises if
there is none, so the first research pull must precede the first market run, or
`RESEARCH_DB_PATH` must point at `~/Work/invo-db/research.db`.

Intervals: `1m` on the frequent timer because it expires within days; `1h,4h`
daily, since they reach back months and years and can be fetched at leisure.

## Scheduling

Four systemd **user** units on the laptop, no root:

| Unit | Schedule | Command |
|---|---|---|
| `invo-research-pull.timer` | hourly | `research pull` |
| `invo-market-1m.timer` | every 5 min | `market backfill --intervals 1m --update` |
| `invo-market-slow.timer` | daily | `market backfill --intervals 1h,4h --update` |
| `invo-logs-pull.timer` | daily | `logs pull` |

All with `Persistent=true` so a missed run after a suspend fires on resume.
Shipped in `deploy/user-timers/` with an install note; not installed
automatically.

## Failure modes

| Situation | Behaviour |
|---|---|
| Laptop offline for weeks | VPS buffer grows (~25 MB/day). No loss. Next pull catches up. |
| Pull interrupted mid-stream | Archive consistent at the last committed batch; `last_pull_at` unchanged; re-run resumes and repeats the re-scan window. |
| Truncated stream, no `type: "complete"` | Marked partial; `last_pull_at` not advanced. |
| Prune with a foreign ack | Refused — `archive_uuid` mismatch or `last_max_id` above `max(id)` short-circuits before any row is touched. |
| Prune with a stale ack | Warned, not refused. The predicate is anchored to `ack.last_pull_at`, so an old ack prunes strictly less. |
| Backfill file imported twice | No-op — stable `dedupe_key`s; the `vps` watermark is never consulted or advanced. |
| Prune racing the collector | Chunked deletes; the collector's bounded queue absorbs contention. `--vacuum` requires the service stopped. |
| Two pulls at once | The second exits on the `flock`; `sync-state.json` cannot be interleaved. |
| Log rollover across midnight | The date-aware handler closes yesterday's dated file and opens today's on the first emit after midnight, so `logs pull` never skips a still-growing file. |
| Remote schema newer than local | The archive's `_open` refuses to open when `user_version > SCHEMA_VERSION` (the check moves out of `_initialize`, which today unconditionally rewrites the version — `research.py:170`), so both the agent and `pull --from-file` refuse to run against a newer archive. |
| Disk full on the laptop | Batch transaction fails, watermark does not advance, pull exits non-zero. |

## Testing

- Merge idempotency: pull the same export twice; row count stable, `seen_count`
  and `last_seen_at` unchanged on the second pass.
- Idempotency holds for rows the collector produces with `deduplicate=False`
  (backfill markers, capture gaps): synthesized `dedupe_key`s prevent
  duplicates on replay.
- The NULL-key migration is deterministic: running it twice, or against a copy
  taken after `archive_uuid` was assigned, produces identical keys.
- The migration assigns `archive_uuid` before writing any key, so a re-run
  cannot renumber rows under a fresh UUID.
- Content-deduplicated rows keep their existing content hash across the
  migration — the key does not change for rows that already had one.
- The puller stores `dedupe_key` verbatim; a row exported and re-imported keeps
  byte-equal keys on both sides.
- `seen_count` undercounts after a prune-and-re-observe cycle — asserted as the
  documented behaviour, so a future fix has a test to flip.
- `seen_count` merge takes the max, never increments.
- Merge is NULL-safe: rows with NULL `seen_count` or `last_seen_at` on either
  side merge without producing NULLs.
- Watermark does not advance when a batch raises mid-transaction.
- `sync-state.json` writes are atomic: killing the process during write does
  not leave a truncated or empty file.
- Two concurrent pulls: the second exits on the `flock` and does not touch
  state.
- `last_pull_at` does not advance without a `type: "complete"` line.
- `last_pull_at` is set from the marker's `update_boundary` (VPS clock),
  never from the laptop clock.
- Export protocol: header, typed rows, and complete marker all validate; an
  unknown `type` fails the pull.
- Export phase 1 returns only `id <= after_id` rows with a moved `last_seen_at`;
  phase 2 only `id > after_id`, ascending.
- Prune refuses without `--ack`, refuses on `archive_uuid` mismatch, refuses on
  `last_max_id` above `max(id)`, and leaves rows inside `keep_days` of
  `ack.last_pull_at` untouched regardless of id.
- A stale ack warns and prunes less; it is not refused.
- `--from-file` does not read or modify the `vps` entry in `sync-state.json`,
  and rejects a file whose rows carry more than one `archive_uuid`.
- Importing the same backfill file twice is a no-op.
- The export header's `remote_now` precedes the snapshot: a row committed
  between the stamp and the snapshot appears in the export and is re-scanned
  next pull, never skipped.
- Newer-schema archive refuses to open with old code (`user_version` check
  moves out of `_initialize`).
- Backfill file merges into an archive already holding live rows without
  duplicating shared records.
- Log rollover writes to today's dated file after midnight without a process
  restart.
- `LOG_RETAIN_DAYS` from the environment reaches `prune_logs()`.

Tests use temporary databases and synthetic rows, per the existing convention in
`tests/test_research.py`. No production database and no authenticated call.

## Net changes

| File | Change | ~Lines |
|---|---|---|
| `src/invo_agent/research.py` | `meta.archive_uuid`, synthesized keys, NULL-key migration, non-null + `user_version` checks on open | +50 |
| `src/invo_agent/research_sync.py` | new — versioned export, NDJSON, batched merge, `flock`, atomic state | 220 |
| `src/invo_agent/research_cli.py` | +`export`, +`pull`, +`prune` | +90 |
| `src/invo_agent/logs_cli.py` | new — `logs pull` | 60 |
| `src/invo_agent/logging_setup.py` | date-aware daily handler; retain-days becomes a parameter | +30 |
| `src/invo_agent/events.py` | resolve the dated path per append instead of once | +10 |
| `src/invo_agent/config.py` | +`log_retain_days`, env-bound | +3 |
| `src/invo_agent/cli.py` | register `logs_app` | +2 |
| `tests/test_research_sync.py` | new | 260 |
| `deploy/user-timers/` | new — 4 timer/service pairs | 40 |

Unchanged: `poller.py`, `engine.py`, `executor.py`, `db.py`, `hud.py`,
`server.py`. `research.py` gains a `meta` table, a key-synthesis path, a
one-time NULL-key migration and two open-time checks; no column is redeclared,
so there is no table rebuild. No trading logic, risk setting, allowlist, or
`agent.db` behaviour is touched.

Config: `LOG_RETAIN_DAYS=14` on the VPS; `RESEARCH_DB_PATH` and `MARKET_DB_PATH`
pointed at `~/Work/invo-db` on the laptop.

Operational: the VPS needs the new code deployed before `research export`
exists there. The first pull transfers the full archive (compressed), then only new and
updated observations.
