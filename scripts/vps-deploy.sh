#!/bin/bash
# Pull origin/main, reinstall the venv, restart systemd, wait for /v1/health.
# Secrets live outside the repo (EnvironmentFile + /var/lib/invo-agent). Never wipe those.
set -euo pipefail

ROOT="${INVO_AGENT_ROOT:-/opt/invo-agent}"
ENV_FILE="${INVO_AGENT_ENV_FILE:-/etc/invo-agent.env}"
HEALTH_TRIES="${INVO_AGENT_HEALTH_TRIES:-30}"
HEALTH_SLEEP="${INVO_AGENT_HEALTH_SLEEP:-2}"

if [[ "$(id -u)" -eq 0 ]]; then
  echo "Run as the invo user (not root) so git/.venv stay owned by invo." >&2
  exit 1
fi

cd "$ROOT"

echo "==> fetch origin/main"
git fetch origin
git reset --hard origin/main

if [[ ! -d .venv ]]; then
  echo "==> create venv"
  python3 -m venv .venv
fi

echo "==> pip install -e ."
.venv/bin/pip install -U pip
.venv/bin/pip install -e .

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

echo "==> migrate local state"
.venv/bin/invo-agent migrate

# invo sudoers allow restart only (not stop/start). Drop the 5s PnL
# cache so the recycled process cannot serve a pre-restart baseline.
rm -f /var/lib/invo-agent/pnl-cache.json

echo "==> restart invo-agent"
sudo systemctl restart invo-agent

TOKEN="${HUD_TOKEN:-}"
PORT="${HUD_PORT:-8787}"
if [[ -z "$TOKEN" ]]; then
  echo "WARN: HUD_TOKEN unset — skipping health wait" >&2
  exit 0
fi

echo "==> wait for GET /v1/health"
ok=0
for _ in $(seq 1 "$HEALTH_TRIES"); do
  if curl -sf -H "Authorization: Bearer ${TOKEN}" \
    "http://127.0.0.1:${PORT}/v1/health" >/dev/null; then
    ok=1
    break
  fi
  sleep "$HEALTH_SLEEP"
done

if [[ "$ok" -ne 1 ]]; then
  echo "ERROR: /v1/health did not become OK. Unit left running/failed — check journalctl -u invo-agent" >&2
  sudo systemctl --no-pager --full status invo-agent || true
  exit 1
fi

echo "==> healthy"
curl -sS -H "Authorization: Bearer ${TOKEN}" "http://127.0.0.1:${PORT}/v1/health"
echo

echo "==> check HUD 24h PnL vs Involio rule"
.venv/bin/invo-agent check-hud-pnl
