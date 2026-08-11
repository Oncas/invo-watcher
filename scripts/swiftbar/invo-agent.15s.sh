#!/bin/bash
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.alwaysVisible>true</swiftbar.alwaysVisible>
# Green/red dot = agent health. Dropdown = open PnL (cached ~60s) + recent trades.

set -uo pipefail

ROOT="${INVO_AGENT_ROOT:-$HOME/Projects/invo-agent}"
PY="$ROOT/.venv/bin/python"
CLI="$ROOT/.venv/bin/invo-agent"

if [[ ! -x "$PY" ]]; then
  echo "● | color=red size=18"
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
try:
    from invo_agent.config import get_settings
    from invo_agent.hud import (
        collect_hud,
        format_pnl,
        format_roe,
        write_hud_file,
    )

    s = get_settings()
    snap = collect_hud(s, with_pnl=True)
    write_hud_file(s, snap)
    ok = bool(snap.launchd_loaded and snap.healthy)
    color = "#30D158" if ok else "#FF453A"
    print(f"● | color={color} size=18")
    print("---")

    # Compact status
    age = (
        f"{snap.poll_age_seconds:.0f}s ago"
        if snap.poll_age_seconds is not None
        else "never"
    )
    print(f"{snap.mode} · {'ok' if ok else 'problem'} · poll {age}")
    if snap.equity is not None:
        print(f"Equity ${snap.equity:.2f}")
    print("---")

    # Open trades + PnL
    print("Open trades")
    if not snap.positions:
        print("None")
    else:
        for p in snap.positions:
            pnl_color = "#30D158" if p.unrealized_pnl >= 0 else "#FF453A"
            line = (
                f"{p.coin} {p.side} {p.leverage}x  "
                f"{format_pnl(p.unrealized_pnl)} ({format_roe(p.return_on_equity)})"
            )
            print(f"{line} | color={pnl_color}")
    if snap.pnl_updated_at:
        # show clock only HH:MM:SS Z
        ts = snap.pnl_updated_at[11:19] if len(snap.pnl_updated_at) > 19 else snap.pnl_updated_at
        print(f"PnL as of {ts} UTC (≤60s cache)")
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
    print(f"Log folder | bash=/usr/bin/open param1={Path.home()}/.invo-agent/logs terminal=false")
except Exception as exc:
    print("● | color=#FF453A size=18")
    print("---")
    print("invo-agent error")
    print(str(exc)[:120].replace("|", "/"))
PY
