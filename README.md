# invo-agent

Watches Involio portfolio trade notifications and copies **OPEN / UPDATE / CLOSE** on Hyperliquid (via Involio’s DEX / Turnkey wallet).

**Risk:** Involio’s Terms ban bots/scrapers. This uses their private web API with your own account. Ban risk is real. Default mode is **dry-run only** (no real orders). Do not run two LIVE pollers against the same wallet.

---

## How it works

One process polls Involio for new trade notifications from the portfolios you follow, decides what each one means for your book, and executes on Hyperliquid with Turnkey-signed orders.

```
Involio notifications  →  poller  →  engine (OPEN/UPDATE/CLOSE)  →  Hyperliquid
                                          ↓
                            agent.db + agent.log + events.jsonl
```

| Piece | Responsibility |
|-------|----------------|
| `poller` | Polls Involio (~4s), startup catch-up, missed-close reconcile |
| `engine` | Turns a notification into an OPEN / UPDATE / CLOSE decision |
| `dex` | Turnkey signing, Hyperliquid execution, recovery |
| `db` | Processed notification ids, open copies, entry + recovery state |
| `server` | Optional read-only snapshot API (`GET /v1/hud|events|logs|health`) |

`healthy` means the poller loop is current — it has polled within `4 ×` the poll interval (min 30s) — and, on a LIVE run, that the signing key has not expired.

Portfolio history and ongoing signal/decision capture are stored in a separate SQLite research database. See [Research data collection](docs/research-data.md) for backfill commands, SQL queries, coverage, and training-data limitations. Market bars for joining those signals to conditions at the time live in a third SQLite file. See [Market data](docs/market-data.md) — note that minute-level history expires within days, so forward capture has to start before analysis.

---

## Requirements

- Python **3.12+** (`python3 --version`)
- An Involio account with email OTP login
- Gmail (or any IMAP mailbox) for OTP auto-login
- Hyperliquid equity in the Involio wallet (for live trading later)

---

## 1. Install

```bash
cd invo-agent
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
```

Keep the venv activated in any terminal you use for `invo-agent`.

---

## 2. Configure `.env`

Minimum for dry-run:

```env
INVO_EMAIL=you@example.com

# Gmail App Password (see next section)
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USER=you@example.com
IMAP_PASSWORD=xxxx xxxx xxxx xxxx
IMAP_MAILBOX=INBOX

DRY_RUN=true
LIVE=false
POLL_INTERVAL_SECONDS=4
POSITION_PCT=5

# Fallback allowlist (comma-separated UUIDs). With PORTFOLIO_SYNC=true this list
# is REPLACED by every portfolio the account follows on the first successful
# sync -- a hand-picked subset here widens to all your follows. Set
# PORTFOLIO_SYNC=false to pin exactly this list.
PORTFOLIO_ALLOWLIST=<portfolio-id-1>,<portfolio-id-2>
PORTFOLIO_SYNC=true
PORTFOLIO_SYNC_INTERVAL_SECONDS=900
```

| Setting | Meaning |
|---------|---------|
| `DRY_RUN` / `LIVE` | Keep `DRY_RUN=true` and `LIVE=false` until you’re ready |
| `POSITION_PCT` | Fallback margin % if the source trade has no `entrySize` |
| `HL_MIN_ORDER_USD` | Floor order notional (margin×leverage); default $10 for Hyperliquid |
| `MAX_POSITION_PCT` | Cap each copy to this % of equity as margin (default 10) |
| `MAX_BOOK_MARGIN_PCT` | Cap the sum of open-copy margins + this order (default 40) |
| `RECOVERY_ENABLED` | On startup, reconcile open copies vs Involio closes (default true) |
| `RECOVERY_SL_PCT` | Parked SL as % of our margin (price move = this / leverage; default 3) |
| `MIN_EQUITY_USD` | Skip trades if HL equity is below this |
| `PORTFOLIO_ALLOWLIST` | Fallback list, used until the first successful follows sync |
| `PORTFOLIO_SYNC` | Replace the allowlist with the account's followed portfolios (default true; **widens** a hand-picked `PORTFOLIO_ALLOWLIST` to all follows) |
| `PORTFOLIO_SYNC_INTERVAL_SECONDS` | How often to re-read follows while running (default 900) |
| | If `PORTFOLIO_SYNC=true` and the sync fails with no `PORTFOLIO_ALLOWLIST` fallback, copying **pauses** rather than copying every trader |
| `LOG_DIR` | Where `agent.log` and `events.jsonl` are written (default `~/.invo-agent/logs`) |
| `HUD_TOKEN` | Bearer token for the snapshot API. Empty disables the HTTP server entirely |
| `HUD_BIND` / `HUD_PORT` | Snapshot API bind address and port (default `127.0.0.1:8787`) |

Sessions and state are stored under `~/.invo-agent/` (not in the repo):

| Path | Contents |
|------|----------|
| `~/.invo-agent/session.json` | Involio social login JWT |
| `~/.invo-agent/dex-session.json` | Turnkey trading keys (after `dex-login`) |
| `~/.invo-agent/agent.db` | Processed notifications + open copies |
| `~/.invo-agent/logs/agent.log` | Rotating file log |
| `~/.invo-agent/logs/events.jsonl` | Structured OPEN/UPDATE/CLOSE sizing, HL responses, errors |

Do not commit `.env`, `captures/`, or anything under `~/.invo-agent/`.

---

## 3. Gmail App Password (IMAP)

Used so the agent can read OTP emails without you pasting codes every time.

1. Turn on [2-Step Verification](https://myaccount.google.com/signinoptions/two-step-verification).
2. Create an [App Password](https://myaccount.google.com/apppasswords) → app **Mail**.
3. Paste the 16-character password into `IMAP_PASSWORD` (spaces are fine).
4. Test:

```bash
invo-agent imap-check
```

You should see `OK — logged in as …`.

### Which OTP email is which

| Command | Email from | Looks like |
|---------|------------|------------|
| `invo-agent login` | `no-reply@mail.involio.com` | Subject **Verification Code**, **4-digit** PIN |
| `invo-agent dex-login` | `no-reply@mail.turnkey.com` | Subject **`NNNNNN` is your sign in code for Invo Wallet**, **6-digit** PIN |

The agent filters by sender so a wallet email is never used for account login (and vice versa). If IMAP misses, the CLI asks you to paste the code.

---

## 4. First-time login

```bash
invo-agent login
invo-agent status
```

IMAP should pick up the Involio 4-digit code.

---

## 5. Dry-run (recommended first)

Watches notifications and logs what it *would* trade — **no Hyperliquid orders**.

```bash
# One poll (first run catch-up marks old notifs as seen — no history spam)
invo-agent once

# Continuous watcher (Ctrl+C to stop)
invo-agent dry-run
```

On every start (or after `reset-state`), **catch-up** marks existing notifications as already handled, then only new trader signals fire OPEN / UPDATE / CLOSE.

After catch-up, **missed-close recovery** checks local open copies against Involio. If the trader already closed while the agent was offline but Hyperliquid still holds the coin, it either market-closes or parks reduce-only TP/SL (`RECOVERY_ENABLED`, `RECOVERY_SL_PCT`). It also runs every 5 minutes, and immediately after a CLOSE we could not execute.

How a CLOSE is handled depends on how late we are:

| Age of the CLOSE signal | Behaviour |
| --- | --- |
| Fresh | Market-close our slice |
| Failed on a blip (cap / network), < 2 min old | Retried at market on the next poll |
| Older than 10 min, or still failing | Recovery prices it: flatten while green or less red than the trader, otherwise park TP/SL. A parked copy is never market-dumped later. |

Hyperliquid’s address request cap is `10,000 + 1 request per USDC traded` (lifetime, no daily reset). The agent tracks it locally from `userFees` volume plus every action it posts, and re-anchors from HL’s rejection string. Closes and protective TP/SL always go through; new opens and size-adds wait (HUD tag `ratelimit`) while remaining slots are at or below `HL_BUDGET_RESERVE` (default 50). While over the cap, actions are spaced 11s apart so we use HL’s one-per-10s allowance on the trade instead of a leverage write.

A fresh OPEN is still an IoC at entry-or-better. Queued opens place **one** resting GTC at the trader’s entry and wait; they are not re-probed every poll. An unchanged UPDATE writes nothing. TP/SL replaces only the leg that moved.

When Hyperliquid answers “too many requests”, opportunistic queue work also pauses for 2 minutes as a backstop.

Useful:

```bash
invo-agent status           # last poll, open copies, recent actions
invo-agent portfolios       # re-read follows: manager, portfolio name, id
invo-agent reset-state -y   # clear DB + re-catch-up (keeps login)
```

---

## 6. Live trading (optional)

Only after dry-run looks correct on real signals.

### 6a. DEX / wallet session

Separate OTP from account login (Turnkey / Invo Wallet):

```bash
invo-agent dex-login
invo-agent dex-status      # should show Hyperliquid equity
```

If you hit rate limits on `/dex/auth/otp`, wait a few minutes and retry (or paste the 6-digit code with `--code`).

If the Turnkey key expires while running, the agent **auto re-logins via IMAP** (Turnkey OTP email) and retries the trade; on failure it cools down 10 minutes then tries again.

### 6b. Enable live in `.env`

```env
DRY_RUN=false
LIVE=true
POSITION_PCT=5
MIN_EQUITY_USD=5
```

### 6c. Run

```bash
invo-agent run
```

**Sizing:** each OPEN copies the source trader’s `entrySize` as percent of *your* HL equity (Involio `0.8` = 0.8%, `1` = 1%, `12` = 12% — not a 0–1 fraction). If that would make order notional (`margin × leverage`) below `HL_MIN_ORDER_USD` ($10), it bids the % up to clear that floor. `POSITION_PCT` is only used when `entrySize` is missing. Size-ups grow **your existing clip** by `simDifference / previous entrySim` (the same percent they added to dollars already in the trade) — they do not retarget the trader’s new `entrySize` onto your whole account. Per-copy and book caps still apply.

`size ≈ (margin × leverage) / mid_price`

(UPDATE does not add size in v1; CLOSE flattens your full HL position for that coin.)

Flow: size → set leverage → IoC market order on HL (Turnkey-signed) → record on Involio.

To go back to safe mode:

```env
DRY_RUN=true
LIVE=false
```

---

## CLI cheat sheet

```bash
invo-agent login                 # Involio account (Involio email OTP)
invo-agent imap-check            # test Gmail App Password
invo-agent status                # session, poll meta, open copies
invo-agent once                  # single dry-run poll
invo-agent dry-run               # loop, no trades
invo-agent run                   # loop, honors .env LIVE/DRY_RUN
invo-agent dex-login             # Turnkey / wallet OTP
invo-agent dex-status            # HL equity
invo-agent watch                 # live terminal HUD, refreshes every 2s
invo-agent logs -n 80            # recent log lines
invo-agent portfolios            # re-read follows
invo-agent reset-state -y        # clear local DB (not login)
```

Run `invo-agent --help` for the full list, including background-service and
remote-monitoring commands not covered here.

---

## Watching a run

```bash
invo-agent watch          # health, last poll, recent trades, log tail
invo-agent status         # one-shot
invo-agent logs -n 80
```

Leave `watch` in a small terminal window. For anything scripted, read
`events.jsonl` — one JSON object per trade/step, with `live_error` and
`hl_order_rejected` marking actions that failed to execute.

---

## Layout

```
src/invo_agent/
  api/           # Involio HTTP client + models
  auth/          # OTP login, IMAP (account vs wallet), session
  dex/           # Turnkey signing + Hyperliquid execution (+ missed-close recovery)
  poller.py      # notification loop + startup catch-up / missed-close reconcile
  engine.py      # notification → OPEN/UPDATE/CLOSE
  db.py          # processed ids + open copies (entry + recovery state)
  server.py      # read-only snapshot API (GET /v1/hud|events|logs|health)
  logging_setup.py
  cli.py
```

---

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| `imap-check` fails | Use a Gmail **App Password**, not your normal password; 2FA must be on |
| `login` waits forever | Check spam for `mail.involio.com`; paste code manually when prompted |
| `dex-login` 429 / rate limit | Wait several minutes; don’t spam OTP requests |
| Wrong OTP used | Fixed by sender filter — ensure you’re on latest code (`pip install -e .`) |
| `health` returns 503 | `poll_stale` = poller stuck; `dex_key_expired` = run `invo-agent dex-login` |
| Opens queue but never fill | Check the HL request cap (HUD tag `ratelimit`) and `MIN_EQUITY_USD` |
| Want to ignore old history | `invo-agent reset-state -y` then `once` / `dry-run` |
