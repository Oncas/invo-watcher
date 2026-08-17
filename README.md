# invo-agent

Watches Involio portfolio trade notifications and copies **OPEN / UPDATE / CLOSE** on Hyperliquid (via Involio’s DEX / Turnkey wallet).

**Recommended layout:** the trader runs 24/7 on a cheap Tailscale VPS. Your Mac is a thin client — SwiftBar + sticky `alerter` notifications — that polls **your** snapshot API every 1s. The Mac never calls Involio or Hyperliquid.

**Risk:** Involio’s Terms ban bots/scrapers. This uses their private web API with your own account. Ban risk is real. Default mode is **dry-run only** (no real orders). Do not run two LIVE pollers against the same wallet.

---

## Architecture

```
Involio / Hyperliquid  →  VPS poller + agent.db + logs
                              ↓  GET /v1/hud|events|logs  (Tailscale, HUD_TOKEN)
Mac hud-sync (1s)  →  ~/.invo-agent/hud.json + log tails  →  SwiftBar / alerter
```

| Where | What |
|-------|------|
| **VPS** | Involio poll (~4s), DEX/Turnkey, IMAP OTP, `agent.db`, HL PnL snapshot, `GET /v1/*` |
| **Mac** | `invo-agent hud-sync` LaunchAgent, SwiftBar, sticky notifications |

`healthy` on the server means the poller loop is current. Green in SwiftBar also requires `hud.json` newer than ~5s (sidecar is alive).

---

## Requirements (Mac)

- macOS (Notification Center + SwiftBar + `launchd` sidecar)
- Python **3.12+** (`python3 --version`)
- Tailscale on the same tailnet as the VPS
- An Involio account with email OTP login (used on the **VPS**)
- Gmail (or IMAP) on the **VPS** for OTP auto-login
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
| `MAX_POSITION_PCT` | Cap each OPEN to this % of equity as margin (default 50) |
| `RECOVERY_ENABLED` | On startup, reconcile open copies vs Involio closes (default true) |
| `RECOVERY_SL_PCT` | Adverse % from mid for parked SL while waiting recovery TP (default 3) |
| `MIN_EQUITY_USD` | Skip trades if HL equity is below this |
| `PORTFOLIO_ALLOWLIST` | Only act on these followed portfolios |
| `MAC_NOTIFY` | macOS notifications on actions/errors (Mac sidecar; set `false` on the VPS) |
| `MAC_NOTIFY_STICKY` | Stay until **Dismiss** (Slack-style via `alerter`) |
| `HUD_TOKEN` | Shared bearer token. Set on the VPS to enable `GET /v1/*`; same value on the Mac |
| `HUD_BIND` / `HUD_PORT` | Snapshot API bind (VPS: `0.0.0.0:8787`, firewall only on `tailscale0`) |
| `HUD_SYNC_URL` | Mac sidecar base URL, e.g. `http://invo-vps:8787` |

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
```

### 6c. Run

```bash
invo-agent run
```

**Sizing:** each OPEN copies the source trader’s `entrySize` as percent of *your* HL equity (Involio `0.8` = 0.8%, `1` = 1%, `12` = 12% — not a 0–1 fraction). If that would make order notional (`margin × leverage`) below `HL_MIN_ORDER_USD` ($10), it bids the % up to clear that floor. `POSITION_PCT` is only used when `entrySize` is missing.

`size ≈ (margin × leverage) / mid_price`

(UPDATE does not add size in v1; CLOSE flattens your full HL position for that coin.)

Flow: size → set leverage → IoC market order on HL (Turnkey-signed) → record on Involio.

To go back to safe mode:

```env
DRY_RUN=true
LIVE=false
```

---

## 7. Mac menu bar (after the VPS is up)

On the Mac `.env` (no Turnkey keys, no `LIVE=true`):

```env
HUD_SYNC_URL=http://<vps-magicdns>:8787
HUD_TOKEN=<same as /etc/invo-agent.env on the VPS>
MAC_NOTIFY=true
MAC_NOTIFY_STICKY=true
```

```bash
invo-agent install-hud-sync
invo-agent service-status
```

That writes `~/Library/LaunchAgents/com.invo-agent.hud.plist`. Every 1s it:

- writes `~/.invo-agent/hud.json`
- appends log tails into `~/.invo-agent/logs/`
- fires `notify_mac()` for new OPEN / UPDATE / CLOSE / ERROR_* / SKIP_* / RECOVERY_*

SwiftBar only reads `hud.json` (no live HL/Turnkey). Copy `scripts/swiftbar/invo-agent.5s.sh` into your SwiftBar plugins folder.

Unload the old Mac trader if it is still loaded:

```bash
invo-agent uninstall-service
```

---

## 8. VPS (24/7 trader)

1 vCPU / 1GB RAM / 10GB Ubuntu 24.04 is enough (no Docker). On a fresh box, as root:

```bash
# after cloning this repo onto the box, or curl the script
sudo bash /opt/invo-agent/scripts/vps-bootstrap.sh
sudo tailscale up
```

Bootstrap: 1GB swap, `invo` user, venv, systemd, logrotate (~14 days), journald cap, ufw on `tailscale0:22` + `tailscale0:8787` only, generates `HUD_TOKEN` in `/etc/invo-agent.env`.

Copy state from the Mac **before** going LIVE (secrets stay outside git):

```bash
# on the Mac
scp ~/.invo-agent/session.json ~/.invo-agent/dex-session.json ~/.invo-agent/agent.db \
  invo@<vps-magicdns>:/tmp/
# on the VPS
sudo install -o invo -g invo -m 600 /tmp/session.json /var/lib/invo-agent/session.json
sudo install -o invo -g invo -m 600 /tmp/dex-session.json /var/lib/invo-agent/dex-session.json
sudo install -o invo -g invo -m 600 /tmp/agent.db /var/lib/invo-agent/agent.db
sudo -e /etc/invo-agent.env   # IMAP + INVO_EMAIL + PORTFOLIO_ALLOWLIST; keep MAC_NOTIFY=false
```

Start dry-run first:

```bash
sudo systemctl start invo-agent
curl -sS -H "Authorization: Bearer $HUD_TOKEN" http://127.0.0.1:8787/v1/health
```

### Auto-deploy on push to `main`

Repo secrets: `TAILSCALE_AUTHKEY` (ephemeral, tagged `tag:ci`), `VPS_HOST` (MagicDNS), optional `VPS_SSH_KEY` / `VPS_USER` (default `invo`).

The Action joins the tailnet and SSHes to run `scripts/vps-deploy.sh`: fetch `origin/main`, `pip install -e .`, `systemctl restart invo-agent`, wait until `GET /v1/health` is OK. A failed health check leaves the unit as-is (check `journalctl -u invo-agent`) — no silent rollback. Deploy never overwrites `/etc/invo-agent.env` or `/var/lib/invo-agent/`.

Mac `hud-sync` / SwiftBar are **not** auto-deployed.

### Cutover (no dual traders)

1. Ship API + `hud-sync` while the Mac still runs the trader (API unused).
2. Point SwiftBar at local `hud.json` from `hud-sync` against the VPS in **dry-run**.
3. `invo-agent uninstall-service` on the Mac, set `LIVE=true` / `DRY_RUN=false` on the VPS, `systemctl restart invo-agent`, `invo-agent install-hud-sync` on the Mac.
4. Confirm one `last_poll_at` advancing and no second process calling Involio.

Do not put Turnkey keys on the Mac after cutover. IMAP on the VPS is required so OTP auto-login still works when keys expire.

---

## Local Mac trader (optional)

Foreground is fine for testing without a VPS:

```bash
# Safe: forces dry-run even if .env has LIVE=true
invo-agent install-service --mode dry-run

invo-agent service-status
tail -f ~/.invo-agent/logs/agent.log
```

After you trust live (Mac-only — skip if the VPS is the trader):

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
invo-agent install-service       # launchd Mac trader (optional)
invo-agent uninstall-service
invo-agent hud-sync              # Mac sidecar (foreground)
invo-agent install-hud-sync      # launchd sidecar
invo-agent uninstall-hud-sync
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
cp "$PWD/scripts/swiftbar/invo-agent.5s.sh" \
  "$HOME/Library/Application Support/SwiftBar/Plugins/invo-agent.5s.sh"
chmod +x scripts/swiftbar/invo-agent.5s.sh
# Remove old 15s plugin if present (SwiftBar schedule is in the filename).
rm -f "$HOME/Library/Application Support/SwiftBar/Plugins/invo-agent.15s.sh"
# Open SwiftBar once; pick …/SwiftBar/Plugins if asked
```

Menu bar: green/red `●`. Green = server poller current **and** `hud.json` newer than 5s. Click for open-trade PnL, recent actions, Status / Watch / Logs (local cache).

`hud-sync` writes `~/.invo-agent/hud.json` every 1s. SwiftBar does not call Hyperliquid.

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
  server.py      # Tailscale snapshot API (GET /v1/hud|events|logs|health)
  hud_sync.py    # Mac sidecar
  notify.py      # macOS Notification Center
  service.py     # launchd install/unload (trader + hud-sync)
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
