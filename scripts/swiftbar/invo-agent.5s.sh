#!/bin/bash
# <swiftbar.hideAbout>true</swiftbar.hideAbout>
# <swiftbar.alwaysVisible>true</swiftbar.alwaysVisible>
# Colored circle emoji — SF Symbols render white/template in the menu bar on this Mac.
# Filename schedule (.5s.) = SwiftBar re-runs this every 5s.
# Live refresh is hard-killed after 3s so HL/Turnkey hangs cannot stale the plugin.

set -uo pipefail

ROOT="${INVO_AGENT_ROOT:-$HOME/Projects/invo-agent}"
PY="$ROOT/.venv/bin/python"
CLI="$ROOT/.venv/bin/invo-agent"
BUDGET_SEC=3

if [[ ! -x "$PY" ]]; then
  echo "🔴"
  echo "---"
  echo "invo-agent venv missing"
  exit 0
fi

cd "$ROOT" || exit 0
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

render_py() {
  # $1 = mode: live | cache
  local mode="$1"
  "$PY" - "$CLI" "$mode" <<'PY'
import json
import sys
from dataclasses import fields
from pathlib import Path

cli = sys.argv[1]
mode = sys.argv[2]

from invo_agent.config import get_settings
from invo_agent.hud import (
    HudSnapshot,
    PositionRow,
    collect_hud,
    format_pnl,
    format_pnl_24h,
    format_roe,
    hud_path,
    write_hud_file,
)

def render(snap: HudSnapshot, *, note: str | None = None) -> None:
    ok = bool(snap.launchd_loaded and snap.healthy)
    print("🟢" if ok else "🔴")
    print("---")
    age = (
        f"{snap.poll_age_seconds:.0f}s ago"
        if snap.poll_age_seconds is not None
        else "never"
    )
    print(f"{snap.mode} · {'ok' if ok else 'problem'} · poll {age}")
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
            line = (
                f"{p.coin} {p.side} {p.leverage}x  "
                f"{format_pnl(p.unrealized_pnl)} ({format_roe(p.return_on_equity)})"
            )
            print(f"{line} | color={pnl_color}")
    if snap.pnl_updated_at:
        ts = (
            snap.pnl_updated_at[11:19]
            if len(snap.pnl_updated_at) > 19
            else snap.pnl_updated_at
        )
        print(f"PnL as of {ts} UTC (≤5s cache)")
    if note:
        print(note)
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
        f"Log folder | bash=/usr/bin/open param1={Path.home()}/.invo-agent/logs "
        "terminal=false"
    )


def from_hud_file(settings) -> HudSnapshot | None:
    path = hud_path(settings)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    allowed = {f.name for f in fields(HudSnapshot)}
    payload = {k: v for k, v in data.items() if k in allowed}
    rows = []
    for row in payload.get("positions") or []:
        try:
            rows.append(PositionRow(**row))
        except TypeError:
            continue
    payload["positions"] = rows
    try:
        return HudSnapshot(**payload)
    except TypeError:
        return None


s = get_settings()
try:
    if mode == "live":
        snap = collect_hud(s, with_pnl=True)
        write_hud_file(s, snap)
        render(snap)
    else:
        cached = from_hud_file(s)
        if cached is None:
            # Local-only snapshot (no HL) so the menu bar still shows something.
            snap = collect_hud(s, with_pnl=False)
            render(snap, note="(no cache — live refresh timed out)")
        else:
            render(cached, note="(cached — live refresh timed out)")
except Exception as exc:
    print("🔴")
    print("---")
    print("invo-agent error")
    print(str(exc)[:120].replace("|", "/"))
    raise SystemExit(1)
PY
}

OUT="$(mktemp -t invo-agent-swiftbar)"
render_py live >"$OUT" 2>/dev/null &
pid=$!
(
  sleep "$BUDGET_SEC"
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    sleep 0.2
    kill -KILL "$pid" 2>/dev/null || true
  fi
) &
killer=$!

if wait "$pid" 2>/dev/null; then
  kill "$killer" 2>/dev/null || true
  wait "$killer" 2>/dev/null || true
  cat "$OUT"
  rm -f "$OUT"
  exit 0
fi

kill "$killer" 2>/dev/null || true
wait "$killer" 2>/dev/null || true
rm -f "$OUT"

# Timed out or failed — serve last good HUD quickly (no network).
render_py cache
exit 0
