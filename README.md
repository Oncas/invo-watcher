# invo-watcher

A personal portfolio watcher for Involio, with local monitoring, research archives,
and optional Hyperliquid trade execution through an Involio / Turnkey wallet.

The application reads trade notifications from the portfolios you follow, records
what happened, and exposes a terminal dashboard. With live execution enabled, it
can copy OPEN, UPDATE, and CLOSE signals to your own wallet.

The repository is named `invo-watcher`; the installed command remains `invo-agent`.

## Authorized use

This is an independent integration using authenticated Involio web endpoints.
It is not an official Involio integration or an endorsed application.

[Involio's Terms of Service, §5.2](https://www.invoapp.com/terms-condition)
restrict automated monitoring and data collection, subject to the exceptions
stated there, including written permission. Use connected features only with the
required authorization. Account access alone does not establish permission to
automate access. Dry-run prevents trade execution but still contacts Involio.

## Modes

| Mode | Behavior |
| --- | --- |
| Dry-run, the default | Read notifications and record intended actions without placing Hyperliquid orders |
| Live execution, optional | Submit wallet-signed orders, apply position limits, and reconcile tracked copies |

Keep `DRY_RUN=true` and `LIVE=false` while configuring the application. Live
execution places real orders. Run only one live poller against a wallet.

## Requirements

- Python 3.12 or newer.
- An Involio account and authorization for the connected features you use.
- Access to the account's email verification codes, manually or through IMAP.
- An Involio / Turnkey wallet with Hyperliquid funds if enabling live execution.

## Install

```bash
git clone https://github.com/Once4thewin/invo-watcher.git
cd invo-watcher
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

Keep the virtual environment active when running `invo-agent` commands.

## Configure

Edit `.env` with your own account details. A minimal monitoring configuration is:

```env
INVO_EMAIL=you@example.com
DRY_RUN=true
LIVE=false
POLL_INTERVAL_SECONDS=4

PORTFOLIO_SYNC=true
PORTFOLIO_SYNC_INTERVAL_SECONDS=900
PORTFOLIO_ALLOWLIST=
```

With `PORTFOLIO_SYNC=true`, the selected portfolios are refreshed from your
account's follows. A successful sync replaces `PORTFOLIO_ALLOWLIST`, including a
hand-picked subset. If sync fails and no fallback list exists, copying pauses.
To restrict selection to a fixed list, set `PORTFOLIO_SYNC=false` and supply the
portfolio UUIDs in `PORTFOLIO_ALLOWLIST`, separated by commas.

For automatic email-code retrieval, add IMAP settings:

```env
IMAP_HOST=imap.gmail.com
IMAP_PORT=993
IMAP_USER=you@example.com
IMAP_PASSWORD=your-app-password
IMAP_MAILBOX=INBOX
```

For Gmail, enable [2-Step Verification](https://myaccount.google.com/signinoptions/two-step-verification)
and create an [App Password](https://myaccount.google.com/apppasswords). Use that
password for IMAP. If automatic retrieval is unavailable, login prompts for a
verification code. Account and wallet verification emails are handled separately.

| Setting | Purpose |
| --- | --- |
| `POSITION_PCT` | Fallback margin allocation when a source trade has no `entrySize`; default 5% |
| `MAX_POSITION_PCT` | Margin cap for each copy; default 10% of equity |
| `MAX_BOOK_MARGIN_PCT` | Combined margin cap for tracked copies; default 40% |
| `HL_MIN_ORDER_USD` | Minimum order notional used by the executor; default $10 |
| `MIN_EQUITY_USD` | Minimum account equity for execution; default $5 |
| `RECOVERY_ENABLED` | Reconcile tracked copies after missed closes; default true |
| `RECOVERY_SL_PCT` | Recovery stop distance as a percentage of position margin; default 3% |
| `HL_BUDGET_RESERVE` | Request slots reserved for closes and protective orders; default 50 |
| `LOG_DIR` | Log directory; default `~/.invo-agent/logs` |
| `HUD_TOKEN` | Snapshot API bearer token; an empty value disables the server |
| `HUD_BIND` / `HUD_PORT` | Snapshot API address; default `127.0.0.1:8787` |

See `.env.example` for the remaining settings. Keep `.env`, email credentials,
session files, wallet keys, captures, and account databases out of Git.

## Start monitoring

After confirming authorization and configuring your account:

```bash
invo-agent imap-check        # optional IMAP connectivity check
invo-agent login            # account login
invo-agent status           # session and local state
invo-agent once             # one dry-run polling cycle
invo-agent dry-run          # continuous monitoring; Ctrl+C to stop
```

Startup catch-up marks existing notifications as handled before processing new
signals. This prevents old notifications from being replayed as new actions.

Inspect a running instance from another terminal:

```bash
invo-agent watch
invo-agent status
invo-agent logs -n 80
invo-agent portfolios
```

The terminal dashboard shows health, tracked copies, and recent events.
`events.jsonl` records decisions and execution outcomes; `live_error` and
`hl_order_rejected` identify failed execution attempts.

## Optional live execution

Live mode copies followed portfolio signals to your wallet. Review portfolio
selection, position limits, and observed dry-run decisions before enabling it.

Create the separate wallet session:

```bash
invo-agent dex-login
invo-agent dex-status
```

Then update `.env`:

```env
DRY_RUN=false
LIVE=true
```

Run `invo-agent run`. Restore `DRY_RUN=true` and `LIVE=false` before restarting
in monitoring mode. The `invo-agent dry-run` command always forces dry-run mode.

| Signal | Execution behavior |
| --- | --- |
| OPEN | Size from the source allocation, apply margin caps, and submit an entry-or-better order |
| UPDATE | Adjust size or protection when the source trade changes, subject to execution limits |
| CLOSE | Reduce a tracked copy; shared-coin copies are limited to their stored slice |

Source `entrySize` values are percentages: `0.8` means 0.8% of your Hyperliquid
equity. `POSITION_PCT` is the fallback when allocation is missing. Size increases
scale your existing copy by the source trade's relative increase; per-copy and
combined margin caps still apply. When a tracked copy is the only copy on a coin,
CLOSE can flatten the full live position on that coin.

Queued opens use a resting order at the source entry price. The executor maintains
a local Hyperliquid request budget, reserves capacity for closes and protective
orders, and paces retries after request-limit responses. The dashboard labels
blocked work with its current reason, such as `ratelimit`.

Missed-close recovery compares tracked copies with Involio state at startup and
periodically while running. Depending on timing and recovery state, it closes the
position or maintains reduce-only protection. Expired Turnkey sessions can be
renewed through email verification when IMAP is configured.

## Architecture and local data

```text
Involio notifications → poller → decision engine → optional Hyperliquid execution
                                   ↓
                         SQLite state + logs + events
```

| Component | Responsibility |
| --- | --- |
| `api/`, `auth/` | Authenticated requests, account sessions, and email verification |
| `poller.py` | Notification polling, catch-up, and reconciliation |
| `engine.py` | OPEN / UPDATE / CLOSE decisions |
| `dex/` | Turnkey signing, order execution, request budgets, and recovery |
| `db.py` | Processed notifications and tracked-copy state |
| `server.py` | Optional read-only health, snapshot, event, and log endpoints |

Default state lives under `~/.invo-agent/`: account and wallet session files,
`agent.db`, `research.db`, `market.db`, and `logs/`. `healthy` indicates a current
polling loop and, during live execution, a usable signing session.

Additional documentation:

- [Research data collection](docs/research-data.md): portfolio history, signal capture, and archive queries.
- [Market data](docs/market-data.md): candle capture and joining market conditions to signals.
- [Scheduled collection](deploy/user-timers/README.md): user timers for archive and market capture.
- [Research sync rollout](docs/rollout-research-sync.md): deployment order and verification.

The VPS bootstrap script defaults to this repository and accepts an
`INVO_AGENT_REPO` override. Runtime directories and service names remain
`invo-agent`. The deployment workflow uses the `PROD` environment's VPS secrets;
when configured, a push to `main` deploys the code and restarts the service.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| IMAP login fails | Mailbox settings, Gmail App Password, and account 2-Step Verification |
| Login is waiting | Verification email delivery; enter the code manually if needed |
| Wallet login is rate-limited | Wait before retrying the verification request |
| Health endpoint returns 503 | Poller freshness and wallet-session expiry |
| An open remains queued | Request budget, available equity, entry price, and order minimum |

Run `invo-agent --help` for the complete command list, including service management
and remote monitoring.
