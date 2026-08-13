#!/bin/bash
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.alwaysVisible>true</swiftbar.alwaysVisible>
# Colored circle emoji — SF Symbols render white/template in the menu bar on this Mac.
# Filename schedule (.5s.) = SwiftBar re-runs this every 5s.
# Renders ~/.invo-agent/hud.json only (written by hud-sync). No HL/Turnkey calls.

set -uo pipefail

ROOT="${INVO_AGENT_ROOT:-$HOME/Projects/invo-agent}"
PY="$ROOT/.venv/bin/python"
CLI="$ROOT/.venv/bin/invo-agent"

if [[ ! -x "$PY" ]]; then
  echo "🔴"
  echo "---"
  echo "invo-agent venv missing"
  exit 0
fi

cd "$ROOT" || exit 0
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

"$PY" - "$CLI" <<'PY'
import sys
from pathlib import Path

cli = sys.argv[1]

from invo_agent.config import get_settings
from invo_agent.hud import (
    format_age,
    format_pnl,
    format_pnl_24h,
    format_roe,
    hud_file_age_seconds,
    hud_path,
    load_hud_file,
    pending_last_tried_age,
)

FRESH_SECONDS = 5.0

s = get_settings()
snap = load_hud_file(s)
age = hud_file_age_seconds(s)
fresh = age is not None and age < FRESH_SECONDS
ok = bool(snap and snap.healthy and fresh)

print("🟢" if ok else "🔴")
print("---")

if snap is None:
    print("problem · no hud.json")
    print("Start: invo-agent install-hud-sync")
    print("---")
    print(f"Status | bash={cli} param1=status terminal=true")
    print(f"Logs | bash={cli} param1=logs terminal=true")
    print(
        f"Log folder | bash=/usr/bin/open param1={Path.home()}/.invo-agent/logs "
        "terminal=false"
    )
    raise SystemExit(0)

age_poll = (
    f"{snap.poll_age_seconds:.0f}s ago"
    if snap.poll_age_seconds is not None
    else "never"
)
fresh_note = f"sidecar {age:.1f}s" if age is not None else "sidecar missing"
q = len(getattr(snap, "pending", None) or [])
q_note = f" · Q:{q}" if q else ""
print(f"{snap.mode} · {'ok' if ok else 'problem'} · poll {age_poll} · {fresh_note}{q_note}")
if snap.equity is not None:
    eq_line = f"Equity ${snap.equity:.2f}"
    if snap.pnl_24h is not None:
        day = format_pnl_24h(snap.pnl_24h, snap.pnl_24h_pct)
        color = "#30D158" if snap.pnl_24h >= 0 else "#FF453A"
        print(f"{eq_line}  {day} | color={color}")
    else:
        print(eq_line)
print("---")
print("Open trades")
if not snap.positions:
    print("None")
else:
    for p in snap.positions:
        pnl_color = "#30D158" if p.unrealized_pnl >= 0 else "#FF453A"
        trader = getattr(p, "trader", None)
        trader_s = f"  @{trader}" if trader else "  (untracked)"
        line = (
            f"{p.coin} {p.side} {p.leverage}x{trader_s}  "
            f"{format_pnl(p.unrealized_pnl)} ({format_roe(p.return_on_equity)})"
        )
        print(f"{line} | color={pnl_color}")
print("---")
pending = getattr(snap, "pending", None) or []
tried = pending_last_tried_age(pending)
q_header = "Queued opens"
if tried is not None:
    q_header += f" · tried {format_age(tried)} ago"
print(q_header)
if not pending:
    print("None")
else:
    for item in pending:
        coin = item.get("coin") or "?"
        side = (item.get("side") or "").upper()
        reason = item.get("reason") or "?"
        trader = item.get("trader")
        lev = item.get("leverage")
        age_s = format_age(item.get("age_s"))
        lev_s = f" {int(lev)}x" if isinstance(lev, (int, float)) else ""
        trader_s = f"  @{trader}" if trader else ""
        print(f"{coin} {side}{lev_s}{trader_s}  · {reason} · queued {age_s}")
if snap.pnl_updated_at:
    ts = (
        snap.pnl_updated_at[11:19]
        if len(snap.pnl_updated_at) > 19
        else snap.pnl_updated_at
    )
    print(f"PnL as of {ts} UTC (≤5s cache)")
if not fresh:
    print("(stale hud.json — is hud-sync running?)")
print("---")
print("Recent")
if not snap.recent:
    print("None yet")
else:
    for r in snap.recent[:4]:
        summary = (r.get("summary") or r.get("action") or "")[:56]
        print(f"{r['action']}: {summary}".replace("|", "/"))
print("---")
print(f"Status | bash={cli} param1=status terminal=true")
print(f"Watch | bash={cli} param1=watch terminal=true")
print(f"Logs | bash={cli} param1=logs terminal=true")
print(
    f"Log folder | bash=/usr/bin/open param1={hud_path(s).parent / 'logs'} "
    "terminal=false"
)
PY
