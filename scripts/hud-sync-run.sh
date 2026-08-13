#!/bin/bash
# launchd entrypoint for the Mac HUD sidecar (no caffeinate — Mac may sleep).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${INVO_AGENT_LOG_DIR:-$HOME/.invo-agent/logs}"
mkdir -p "$LOG_DIR/history"
DAY="$(date +%Y-%m-%d)"
exec >>"$LOG_DIR/hud-sync.stdout.log" 2>>"$LOG_DIR/hud-sync.stderr.log"
echo "---- hud-sync start $(date -u +%Y-%m-%dT%H:%M:%SZ) ----" \
  | tee -a "$LOG_DIR/history/${DAY}.hud-sync.log"
cd "$ROOT"
exec "$ROOT/.venv/bin/invo-agent" hud-sync
