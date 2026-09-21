# Rollout: research sync

Bringing `feat/research-sync` into production. Design note:
`docs/specs/2026-09-19-research-sync-design.md`.

Set these once per shell so the commands below paste cleanly. Real values live
in `~/.config/invo-agent/sync.env` and never in this repo:

```sh
export INVO_REMOTE=USER@VPS_HOST          # ssh target for the VPS
export INVO_KEY=~/.ssh/YOUR_DEPLOY_KEY    # key that reaches it
export INVO_DB_DIR="$HOME/Work/invo-db"   # local archive of record
alias vps='ssh -i "$INVO_KEY" -o BatchMode=yes "$INVO_REMOTE"'
```

---

## Before you start

**Merging to `main` deploys and restarts the live trader.** `.github/workflows/deploy.yml`
fires on push to `main`, tars the tree to the box, and `scripts/vps-deploy.sh`
reinstalls the venv, runs `invo-agent migrate`, then
`sudo systemctl restart invo-agent`. There is no manual gate between merge and
restart.

That restart drops in-flight order state on a box trading real money. Do it
when you can watch it, and not while a position is opening or closing.

```sh
vps 'systemctl is-active invo-agent'
cd /opt/invo-agent && make status      # on the box: open copies, last poll
```

---

## 1. Merge and let it deploy

```sh
git switch main && git pull
git merge --no-ff feat/research-sync
git push
```

Watch the run. `vps-deploy.sh` polls `/v1/health` for up to 60s and exits
non-zero if it never comes back; a red run means the unit is down, not that the
deploy was cosmetic.

```sh
gh run watch
vps 'systemctl is-active invo-agent'
```

Git on the box tells you nothing about what is deployed — CI pushes files and
never touches refs. To confirm the new code actually landed, look for a symbol
that only exists after this change:

```sh
vps 'grep -c archive_uuid /opt/invo-agent/src/invo_agent/research.py'
```

## 2. Let the agent migrate the archive

The restart in step 1 does this: the agent opens `research.db` for writing,
which mints its `archive_uuid` and backfills rows that have no
`dedupe_key`. Migration happens inside startup, so confirm the agent came back.

**`research export` cannot do this for you.** It opens read-only by design —
so it never contends with the live collector — and will refuse an archive that
has not been migrated yet. If you see `ArchiveNotMigratedError`, the agent has
not restarted since the deploy.

```sh
vps 'sqlite3 ~/.invo-agent/research.db \
  "SELECT (SELECT COUNT(*) FROM archive_meta), \
          (SELECT COUNT(*) FROM observations WHERE dedupe_key IS NULL)"'
```

Expect `1|0`. Anything else, stop here.

## 3. Prove the export works on the box

Read-only and safe against the running trader. Check the shape before moving
the archive over the wire:

```sh
vps '/opt/invo-agent/.venv/bin/invo-agent research export --after-id 0 | head -1'
vps '/opt/invo-agent/.venv/bin/invo-agent research export --after-id 0 | wc -l'
```

First line should be a `"type": "header"` envelope carrying an `archive_uuid`.
Line count should be rows + 2.

## 4. Laptop setup

`~/Work/invo-db` already exists; the config directory does not.

```sh
mkdir -p ~/.config/invo-agent "$INVO_DB_DIR/logs"
cat > ~/.config/invo-agent/sync.env <<EOF
INVO_BIN=$PWD/.venv-linux/bin/invo-agent
INVO_REMOTE=$INVO_REMOTE
INVO_DB_DIR=$INVO_DB_DIR
EOF
chmod 600 ~/.config/invo-agent/sync.env
```

SSH must be non-interactive — a timer has no TTY for a passphrase. Either load
the key into the agent, or give it an `IdentityFile` entry in `~/.ssh/config`
so the bare `ssh USER@HOST` the code runs picks it up without `-i`.

```sh
ssh -o BatchMode=yes "$INVO_REMOTE" true && echo "non-interactive ssh OK"
```

## 5. First pull, by hand

Do not install a timer until one manual run has worked. The first pull moves
the full archive as JSON, compressed on the wire by `ssh -C`.

```sh
.venv-linux/bin/invo-agent research pull \
  --remote "$INVO_REMOTE" --dest "$INVO_DB_DIR" \
  --remote-bin /opt/invo-agent/.venv/bin/invo-agent
```

Expect `complete: N new, 0 updated, N inserted, watermark N`. Then read the
state file — this is the thing every later step trusts:

```sh
cat "$INVO_DB_DIR/sync-state.json"
```

`archive_uuid` set, `last_pull_status: "ok"`, and `last_pull_at` a **VPS**
timestamp taken from the export marker. A `PARTIAL` result exits non-zero and
leaves `last_pull_at` alone; re-running resumes from the last committed batch.

Run it a second time. It should report `0 new` and change nothing — that is the
idempotency the whole design rests on.

## 6. Market bars

Ordering matters: with no `--coin`, the coin list comes from the research
archive, so this fails until step 5 has landed.

```sh
.venv-linux/bin/invo-agent market backfill \
  --database "$INVO_DB_DIR/market.db" --intervals 1m --update
.venv-linux/bin/invo-agent market status --database "$INVO_DB_DIR/market.db"
```

## 7. Timers

Only after 5 and 6 have worked by hand. See
`deploy/user-timers/README.md` for the units and what each one runs.

```sh
mkdir -p ~/.config/systemd/user
cp deploy/user-timers/*.service deploy/user-timers/*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now \
  invo-research-pull.timer invo-market-1m.timer \
  invo-market-slow.timer invo-logs-pull.timer
sudo loginctl enable-linger "$USER"     # keep firing while logged out
```

```sh
systemctl --user list-timers 'invo-*'
journalctl --user -u invo-research-pull.service -n 50
```

---

## Deliberately later

Neither of these is urgent, and both are irreversible in the direction that
loses data. Leave them until several pulls have landed and you trust the
watermarks.

**Log retention.** `LOG_RETAIN_DAYS` is unset on the box, so it runs on the
code default of 7. Only lower it once `logs pull` has been collecting for
longer than the new value — the VPS deletes on its own schedule regardless of
whether anything has copied the files.

**Pruning.** Never on a timer.

```sh
scp "$INVO_DB_DIR/sync-state.json" "$INVO_REMOTE:/tmp/ack.json"
vps '/opt/invo-agent/.venv/bin/invo-agent research prune --ack /tmp/ack.json'
# read the dry-run count, then repeat with --yes
```

Prune refuses a foreign `archive_uuid` or a watermark above `max(id)`. A stale
ack only warns: the cutoff is anchored to the ack's own timestamp, so an old
one prunes strictly less. `--vacuum` needs the service stopped; without it,
pruning is safe against the running trader.

---

## What is not proven

Worth knowing before you trust the first unattended run:

- Everything was verified against a **copy** of the archive on the laptop. The
  real file has a live collector writing to it; no test here reproduces that
  contention.
- The pull has never run over real ssh against the real box. The transport is
  covered by a fake `ssh` on `PATH`, not the genuine article.
- `seen_count` undercounts if a row is pruned remotely and then observed again.
  Accepted: it is analytical, and `last_seen_at` stays correct.

## If the VPS archive is ever rebuilt

A fresh `research.db` gets a new `archive_uuid` and every pull refuses rather
than merge into a watermark that no longer means anything. Recovery is a manual
edit — see the last section of `deploy/user-timers/README.md`. Nothing already
in the local archive is lost; the merge is idempotent.
