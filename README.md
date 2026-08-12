# invo-agent

Mac-local agent that watches Involio portfolio trade notifications and auto-mimics **OPEN / UPDATE / CLOSE** on Hyperliquid (via Involio’s DEX / Turnkey wallet).

**Risk:** Involio’s Terms ban bots/scrapers. This uses their private web API with your own account. Ban risk is real. Default mode is **dry-run only** (no real orders).

---

## Requirements (Mac)

- macOS (Notification Center + optional `launchd`)
- Python **3.12+** (`python3 --version`)
- An Involio account with email OTP login
- Gmail (or IMAP) for auto-reading OTP codes
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

Keep the venv activated in any terminal you use for `invo-agent`:

```bash
source .venv/bin/activate
```

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

# Only copy these portfolios (comma-separated UUIDs)
PORTFOLIO_ALLOWLIST=<portfolio-id-1>,<portfolio-id-2>

MAC_NOTIFY=true
MAC_NOTIFY_STICKY=true
MAC_NOTIFY_SOUND=Glass
```

| Setting | Meaning |
|---------|---------|
| `DRY_RUN` / `LIVE` | Keep `DRY_RUN=true` and `LIVE=false` until you’re ready |
| `POSITION_PCT` | Fallback margin % if the source trade has no `entrySize` |
| `HL_MIN_ORDER_USD` | Floor order notional (margin×leverage); default $10 for Hyperliquid |
| `MAX_POSITION_PCT` | Cap on margin % of our equity (safety) |
| `RECOVERY_ENABLED` | On startup, reconcile open copies vs Involio closes (default true) |
| `RECOVERY_SL_PCT` | Adverse % from mid for parked SL while waiting recovery TP (default 3) |
| `MIN_EQUITY_USD` | Skip trades if HL equity is below this |
| `MIN_ORDER_USD` | Skip if sized margin is below this |
| `PORTFOLIO_ALLOWLIST` | Only act on these followed portfolios |
| `MAC_NOTIFY` | macOS notifications on actions/errors |
| `MAC_NOTIFY_STICKY` | Stay until **Dismiss** (Slack-style via `alerter`) |

Sessions and state are stored under `~/.invo-agent/` (not in the repo):

| Path | Contents |
|------|----------|
| `~/.invo-agent/session.json` | Involio social login JWT |
| `~/.invo-agent/dex-session.json` | Turnkey trading keys (after `dex-login`) |
| `~/.invo-agent/agent.db` | Processed notifications + open copies |
| `~/.invo-agent/logs/agent.log` | Rotating file log |

Do not commit `.env`, `captures/`, or anything under `~/.invo-agent/`.

---

## 3. Gmail App Password (IMAP)

Used so the agent can read OTP emails without you pasting codes every time.

1. Turn on [2-Step Verification](https://myaccount.google.com/signinoptions/two-step-verification).
2. Create an [App Password](https://myaccount.google.com/apppasswords) → app **Mail**, device **Mac**.
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
```

IMAP should pick up the Involio 4-digit code. Then:

```bash
invo-agent status
invo-agent notify-test    # sticky alert until Dismiss
```

Allow notifications for **Terminal** if macOS asks (System Settings → Notifications). Sticky alerts use [`alerter`](https://github.com/vjeantet/alerter) (auto-downloaded to `~/.invo-agent/bin/alerter` on first notify). They stay on screen until you click **Dismiss**, like Slack.

```bash
invo-agent notify-test
```

Optional Homebrew install instead: `brew install vjeantet/tap/alerter`. Set `MAC_NOTIFY_STICKY=false` for classic auto-dismiss banners.

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

After catch-up, **missed-close recovery** checks local open copies against Involio. If the trader already closed while the agent was offline but Hyperliquid still holds the coin, it either market-closes or parks reduce-only TP/SL (`RECOVERY_ENABLED`, `RECOVERY_SL_PCT`).

You’ll get:

- Terminal output
- Sticky macOS alerts (until Dismiss)
- Lines in `~/.invo-agent/logs/agent.log`

Useful:

```bash
invo-agent status           # last poll, open copies, recent actions
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

### 6b. Enable live in `.env`

```env
DRY_RUN=false
LIVE=true
POSITION_PCT=5
MIN_EQUITY_USD=5
MIN_ORDER_USD=1
```

### 6c. Run

```bash
invo-agent run
```

**Sizing:** each OPEN copies the source trader’s `entrySize` (portfolio %) as margin % of *your* HL equity. If that would make order notional (`margin × leverage`) below `HL_MIN_ORDER_USD` ($10), it bids the % up to clear that floor. `POSITION_PCT` is only used when `entrySize` is missing.

`size ≈ (margin × leverage) / mid_price`

(UPDATE does not add size in v1; CLOSE flattens your full HL position for that coin.)

Flow: size → set leverage → IoC market order on HL (Turnkey-signed) → record on Involio.

To go back to safe mode:

```env
DRY_RUN=true
LIVE=false
```

---

## 7. Keep it running on Mac (`launchd`)

Foreground is fine for testing. For always-on:

```bash
# Safe: forces dry-run even if .env has LIVE=true
invo-agent install-service --mode dry-run

invo-agent service-status
tail -f ~/.invo-agent/logs/agent.log
```

After you trust live:

```bash
invo-agent install-service --mode run   # honors DRY_RUN / LIVE from .env
```

Stop / remove:

```bash
invo-agent uninstall-service
```

This writes `~/Library/LaunchAgents/com.invo-agent.plist` (`RunAtLoad` + `KeepAlive`). Working directory is the repo so `.env` is loaded. Your Mac must be awake (or allow background) for polling to continue.

---

## CLI cheat sheet

```bash
invo-agent login                 # Involio account (Involio email OTP)
invo-agent imap-check            # test Gmail App Password
invo-agent status                # session, poll meta, copies, launchd
invo-agent once                  # single dry-run poll
invo-agent dry-run               # loop, no trades
invo-agent run                   # loop, honors .env LIVE/DRY_RUN
invo-agent dex-login             # Turnkey / wallet OTP
invo-agent dex-status            # HL equity
invo-agent notify-test           # sticky alert until Dismiss
invo-agent install-service       # launchd background
invo-agent uninstall-service
invo-agent service-status
invo-agent reset-state -y        # clear local DB (not login)
```

---

## Seeing how it’s doing (while backgrounded)

You don’t need a Docker-style menu-bar app. Easiest options:

### 1. Terminal HUD (recommended, zero install)

```bash
source .venv/bin/activate
invo-agent watch          # refreshes every 2s — health, last poll, recent trades, log tail
invo-agent status         # one-shot
invo-agent logs -n 80
```

Leave `watch` in a small Terminal/iTerm window.

### 2. Menu bar like Docker (optional — SwiftBar)

```bash
brew install --cask swiftbar
mkdir -p "$HOME/Library/Application Support/SwiftBar"
cp "$PWD/scripts/swiftbar/invo-agent.15s.sh" \
  "$HOME/Library/Application Support/SwiftBar/Plugins/invo-agent.15s.sh"
chmod +x scripts/swiftbar/invo-agent.15s.sh
# Open SwiftBar once; pick …/SwiftBar/Plugins if asked
```

Menu bar: green/red `●`. Click for open-trade PnL (cached ≤60s), recent actions, Status / Watch / Logs.

Also written every refresh: `~/.invo-agent/hud.json`.

---

## Overnight live run (Mac)

**Before bed checklist**

1. Lid can close only if you prevent sleep (service uses `caffeinate`), or leave Mac awake/plugged in.
2. Social login already works (`invo-agent status` → logged in).
3. DEX/Turnkey session required for real orders. Prefer a fresh `dex-login` before bed. If the Turnkey key expires overnight, the agent **auto re-logins via IMAP** (Turnkey OTP email) and retries the trade; on failure it cools down 10 minutes then tries again.

```bash
source .venv/bin/activate
invo-agent dex-login
invo-agent dex-status          # confirm HL equity + expiry looks right
```

4. `.env` for live:

```env
DRY_RUN=false
LIVE=true
POSITION_PCT=5
```

5. Start background agent (keeps Mac awake via `caffeinate -dims`):

```bash
invo-agent install-service --mode run
invo-agent service-status
invo-agent notify-test
```

Or foreground in a Terminal window that stays open:

```bash
caffeinate -dims invo-agent run
```

**In the cluster — debug what happened**

```bash
invo-agent status
invo-agent logs -n 80
invo-agent dex-status

# Full history
tail -n 200 ~/.invo-agent/logs/agent.log
cat ~/.invo-agent/logs/events.jsonl          # one JSON object per trade/step
```

| File | What it has |
|------|-------------|
| `~/.invo-agent/logs/agent.log` | Rotating text log (~100MB retained) |
| `~/.invo-agent/logs/events.jsonl` | Structured OPEN/UPDATE/CLOSE sizing, HL responses, errors |
| `~/.invo-agent/logs/launchd.*.log` | launchd stdout/stderr if using the service |
| `~/.invo-agent/agent.db` | Processed notifs + action_log + open copies |

Stop live anytime:

```bash
invo-agent uninstall-service
# and/or set DRY_RUN=true LIVE=false in .env
```

---

## Typical Mac workflow

```bash
cd ~/code/personal/invo-agent
source .venv/bin/activate

invo-agent imap-check
invo-agent login
invo-agent once                  # catch-up + one poll
invo-agent dry-run               # watch for a while / overnight in Terminal

# later, when ready for real copies:
invo-agent dex-login
# set LIVE=true DRY_RUN=false in .env
invo-agent run
# or: invo-agent install-service --mode run
```

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
  notify.py      # macOS Notification Center
  service.py     # launchd install/unload
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
| No macOS alerts | `invo-agent notify-test`; allow Terminal notifications; check `~/.invo-agent/bin/alerter` |
| launchd not running | `invo-agent service-status`; check `~/.invo-agent/logs/launchd.*.log` |
| Want to ignore old history | `invo-agent reset-state -y` then `once` / `dry-run` |
