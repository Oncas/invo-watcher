# Laptop sync timers

Four systemd **user** units. No root, nothing installed automatically.

The VPS holds only the buffer since the last confirmed pull; this machine is
the archive of record. See `docs/specs/2026-09-19-research-sync-design.md`.

## Configure

```sh
mkdir -p ~/.config/invo-agent
cat > ~/.config/invo-agent/sync.env <<'EOF'
INVO_BIN=/path/to/invo-agent/.venv/bin/invo-agent
INVO_REMOTE=invo@192.0.2.10
INVO_DB_DIR=$HOME/Work/invo-db
EOF
mkdir -p ~/Work/invo-db/logs
```

SSH must be non-interactive (key in the agent, or an `IdentityFile` entry in
`~/.ssh/config`); a timer has no TTY to type a passphrase into.

## Install

```sh
mkdir -p ~/.config/systemd/user
cp deploy/user-timers/* ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now \
  invo-research-pull.timer invo-market-1m.timer \
  invo-market-slow.timer invo-logs-pull.timer
```

Let the timers keep running when you are logged out:

```sh
sudo loginctl enable-linger "$USER"
```

## Ordering

The first `research pull` must precede the first `market` run: with no
`--coin`, the coin list is read from the research archive and the command
fails if it is empty.

```sh
systemctl --user start invo-research-pull.service
systemctl --user start invo-market-1m.service
```

## Check

```sh
systemctl --user list-timers 'invo-*'
journalctl --user -u invo-research-pull.service -n 50
cat ~/Work/invo-db/sync-state.json
```

## Pruning the VPS

Never on a timer — the buffer only shrinks when you say so, and only for rows
this machine has confirmed receiving.

```sh
scp ~/Work/invo-db/sync-state.json invo@HOST:/tmp/ack.json
ssh invo@HOST '/opt/invo-agent/.venv/bin/invo-agent research prune --ack /tmp/ack.json'
# ...read the dry-run count, then:
ssh invo@HOST '/opt/invo-agent/.venv/bin/invo-agent research prune --ack /tmp/ack.json --yes'
```

`--vacuum` reclaims the disk but takes an exclusive lock: stop `invo-agent`
first. Without it, pruning is safe against the running trader.

## If the VPS archive is ever rebuilt

A new `research.db` gets a new `archive_uuid`, and every pull will refuse
rather than merge into a watermark that no longer means anything. To adopt the
replacement, clear that one field and pull again:

```sh
python3 - <<'EOF'
import json, pathlib
p = pathlib.Path.home() / "Work/invo-db/sync-state.json"
s = json.loads(p.read_text())
s["sources"]["vps"] = {"archive_uuid": None, "last_id": 0, "last_max_id": 0,
                       "rescan_days": 14, "last_pull_at": None,
                       "last_pull_status": "never"}
p.write_text(json.dumps(s, indent=2))
EOF
```

The next pull re-transfers the whole remote buffer. Nothing already in the
local archive is lost — the merge is idempotent.
