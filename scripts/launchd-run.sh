#!/bin/bash
# launchd entrypoint: append console output (launchd StandardOutPath truncates
# the file on every restart — we avoid that by redirecting ourselves).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${INVO_AGENT_LOG_DIR:-$HOME/.invo-agent/logs}"
mkdir -p "$LOG_DIR/history"
DAY="$(date +%Y-%m-%d)"
# Append-only console mirrors
exec >>"$LOG_DIR/launchd.stdout.log" 2>>"$LOG_DIR/launchd.stderr.log"
echo "---- launchd start $(date -u +%Y-%m-%dT%H:%M:%SZ) mode=${1:-run} ----" \
  | tee -a "$LOG_DIR/history/${DAY}.launchd.log"
cd "$ROOT"
exec /usr/bin/caffeinate -dims -- "$ROOT/.venv/bin/invo-agent" "${1:-run}"
