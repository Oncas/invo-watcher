#!/bin/bash
# One-time Ubuntu 24.04 setup for invo-agent on a 1GB VPS.
# Run as root. Does not start LIVE trading and does not overwrite existing secrets.
set -euo pipefail

REPO_URL="${INVO_AGENT_REPO:-git@github.com:Once4thewin/invo-agent-public.git}"
ROOT=/opt/invo-agent
DATA=/var/lib/invo-agent
ENV_FILE=/etc/invo-agent.env

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip git curl ca-certificates ufw logrotate

# 1GB swap so pip install during deploy does not OOM.
if ! swapon --show | grep -q .; then
  if [[ ! -f /swapfile ]]; then
    echo "==> create 1G swap"
    fallocate -l 1G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=1024
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile || true
  if ! grep -q '^/swapfile ' /etc/fstab; then
    echo '/swapfile none swap sw 0 0' >> /etc/fstab
  fi
fi

if ! id invo >/dev/null 2>&1; then
  useradd --system --create-home --home-dir /home/invo --shell /bin/bash invo
fi

mkdir -p "$DATA/logs/history" "$ROOT"
chown -R invo:invo "$DATA"

if [[ ! -d "$ROOT/.git" ]]; then
  echo "==> clone $REPO_URL"
  git clone "$REPO_URL" "$ROOT"
fi
chown -R invo:invo "$ROOT"

if [[ ! -d "$ROOT/.venv" ]]; then
  sudo -u invo python3 -m venv "$ROOT/.venv"
  sudo -u invo "$ROOT/.venv/bin/pip" install -U pip
  sudo -u invo "$ROOT/.venv/bin/pip" install -e "$ROOT"
fi

install -m 644 "$ROOT/deploy/invo-agent.service" /etc/systemd/system/invo-agent.service
install -m 644 "$ROOT/deploy/invo-agent.logrotate" /etc/logrotate.d/invo-agent
mkdir -p /etc/systemd/journald.conf.d
cat >/etc/systemd/journald.conf.d/invo-agent.conf <<'EOF'
[Journal]
SystemMaxUse=200M
MaxRetentionSec=14day
EOF
systemctl restart systemd-journald || true
systemctl daemon-reload
systemctl enable invo-agent

cat >/etc/sudoers.d/invo-agent <<'EOF'
invo ALL=(root) NOPASSWD: /bin/systemctl restart invo-agent, /bin/systemctl stop invo-agent, /bin/systemctl start invo-agent, /bin/systemctl --no-pager --full status invo-agent
EOF
chmod 440 /etc/sudoers.d/invo-agent

if [[ ! -f "$ENV_FILE" ]]; then
  HUD_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
  cat >"$ENV_FILE" <<EOF
# Runtime secrets — never commit. Paths stay outside the git checkout.
DRY_RUN=true
LIVE=false
MAC_NOTIFY=false
LOG_LEVEL=INFO

SESSION_PATH=${DATA}/session.json
DEX_SESSION_PATH=${DATA}/dex-session.json
DB_PATH=${DATA}/agent.db
LOG_DIR=${DATA}/logs

HUD_TOKEN=${HUD_TOKEN}
HUD_BIND=0.0.0.0
HUD_PORT=8787

# Fill these before going LIVE:
# INVO_EMAIL=
# IMAP_HOST=imap.gmail.com
# IMAP_PORT=993
# IMAP_USER=
# IMAP_PASSWORD=
# PORTFOLIO_ALLOWLIST=
EOF
  chown root:invo "$ENV_FILE"
  chmod 640 "$ENV_FILE"
  echo "==> wrote $ENV_FILE (DRY_RUN). Copy session/dex/db + IMAP/LIVE from the Mac."
else
  echo "==> keep existing $ENV_FILE"
fi

if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

ufw default deny incoming
ufw default allow outgoing
# Public SSH + HUD API. HUD is authenticated with HUD_TOKEN (Mac IPs change).
ufw allow 22/tcp
ufw allow 8787/tcp comment 'invo-agent HUD (HUD_TOKEN auth)'
ufw allow in on tailscale0 to any port 22 proto tcp || true
ufw allow in on tailscale0 to any port 8787 proto tcp || true
ufw --force enable

echo
echo "Next:"
echo "  1. tailscale up   (login this box to your tailnet)"
echo "  2. Copy from Mac: session.json dex-session.json agent.db  →  $DATA/"
echo "     chown invo:invo $DATA/*"
echo "  3. Edit $ENV_FILE (IMAP + HUD_TOKEN already generated; set LIVE only after Mac trader is stopped)"
echo "  4. systemctl start invo-agent     # keep DRY_RUN until cutover"
echo "  5. Do NOT run two LIVE pollers against the same wallet"
