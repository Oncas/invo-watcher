#!/usr/bin/env bash
# Unattended health watch for invo-agent.
#
# Stays SILENT while the agent is healthy and exits non-zero the moment it is
# not, printing everything needed to diagnose the fault. It runs quietly in
# the background; a non-zero exit signals that attention is needed.
#
# Faults:
#   1. /v1/health returns 503        -- the agent itself says it is unhealthy
#      ("poll_stale" or "dex_key_expired"; see server.py::_health)
#   2. unreachable N times in a row  -- process died, port closed, host gone
#   3. new ERROR/Traceback in agent.log
#   4. new error-class row in events.jsonl
#
# A single failed probe is never enough: a restart or a slow poll must not page
# anyone. Only --fails consecutive misses count as down.
set -uo pipefail

URL="${INVO_HUD_URL:-http://127.0.0.1:8787}"
TOKEN="${HUD_TOKEN:-}"
LOG_DIR_ARG=""
INTERVAL=15
FAILS=3

usage() {
    cat <<'EOF'
Usage: health-watch.sh [options]

  --url URL         HUD API base (default $INVO_HUD_URL or http://127.0.0.1:8787)
  --token TOKEN     Bearer token (default $HUD_TOKEN, else read from .env)
  --log-dir DIR     agent.log / events.jsonl location
                    (default $LOG_DIR, else ~/.invo-agent/logs)
  --interval SEC    Seconds between probes (default 15)
  --fails N         Consecutive unreachable probes before it trips (default 3)
  -h, --help        This text

Exit codes: 0 interrupted, 2 unhealthy, 3 unreachable, 4 log error,
            5 event error, 64 bad usage.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --url) URL="${2:-}"; shift 2 ;;
        --token) TOKEN="${2:-}"; shift 2 ;;
        --log-dir) LOG_DIR_ARG="${2:-}"; shift 2 ;;
        --interval) INTERVAL="${2:-}"; shift 2 ;;
        --fails) FAILS="${2:-}"; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 64 ;;
    esac
done

for pair in "INTERVAL:$INTERVAL" "FAILS:$FAILS"; do
    if ! [[ "${pair#*:}" =~ ^[0-9]+$ ]] || [[ "${pair#*:}" -lt 1 ]]; then
        echo "${pair%%:*} must be a positive integer, got: ${pair#*:}" >&2
        exit 64
    fi
done

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"

# The token lives in .env next to the code; don't make the caller repeat it.
if [[ -z "$TOKEN" && -f "$REPO_ROOT/.env" ]]; then
    TOKEN="$(sed -n 's/^HUD_TOKEN=//p' "$REPO_ROOT/.env" | tail -n1 | tr -d '"'"'"'')"
fi

LOG_DIR="$LOG_DIR_ARG"
if [[ -z "$LOG_DIR" && -f "$REPO_ROOT/.env" ]]; then
    LOG_DIR="$(sed -n 's/^LOG_DIR=//p' "$REPO_ROOT/.env" | tail -n1 | tr -d '"'"'"'')"
fi
LOG_DIR="${LOG_DIR:-$HOME/.invo-agent/logs}"
LOG_DIR="${LOG_DIR/#\~/$HOME}"
AGENT_LOG="$LOG_DIR/agent.log"
EVENTS_LOG="$LOG_DIR/events.jsonl"

HEALTH_URL="${URL%/}/v1/health"

# Event kinds that mean something went wrong rather than something happened.
# Taken from the kinds actually present in a live events.jsonl, not from
# grepping the source: "live_error" is a trade that failed to execute and
# "hl_order_rejected" is an order Hyperliquid refused -- both cost money.
EVENT_ERROR_KINDS='live_error|hl_order_rejected|allowlist_unavailable|allowlist_sync_empty'
# Matches logging_setup.py's format: "%Y-%m-%d %H:%M:%S LEVEL [logger] message".
# Tracebacks are continuation lines with no prefix, so they need their own arm.
LOG_ERROR_RE='^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} (ERROR|CRITICAL) |Traceback \(most recent call last\)'

probe() { curl -sS -m 10 -w '\n%{http_code}' -H "Authorization: Bearer $TOKEN" "$HEALTH_URL" 2>/dev/null; }

tail_log() {
    [[ -f "$AGENT_LOG" ]] && { echo; echo "--- last 20 lines of $AGENT_LOG ---"; tail -n 20 "$AGENT_LOG"; }
}

# Byte offsets, so an append costs one tail instead of a re-scan. Two different
# things make the size drop, and they need opposite handling:
#
#   logrotate  -- a genuinely new file; everything in it is new
#   log prune  -- logging_setup.py::_trim keeps the last cap_bytes of the SAME
#                 file, so what survives is content already read
#
# Treating a trim as a rotation replays the whole retained tail: one routine
# "log prune trimmed=1" then reports every historical live_error as fresh.
# The last line consumed tells them apart -- still there means a trim, so
# resume just after it; gone means a new file, so read from the start.
size_of() { [[ -f "$1" ]] && wc -c <"$1" || echo 0; }

# Reads bytes appended since offset $2 into $FRESH, using $3 as the resume
# marker, and leaves the new offset in $OFFSET and the new marker in $LAST_LINE.
FRESH=""
OFFSET=0
LAST_LINE=""
read_new() {
    local path="$1" prev="$2" marker="$3" hit
    OFFSET="$(size_of "$path")"
    if [[ "$OFFSET" -lt "$prev" ]]; then
        prev=0
        if [[ -n "$marker" ]]; then
            hit="$(grep -Fxb -- "$marker" "$path" 2>/dev/null | tail -n1 | cut -d: -f1)"
            if [[ -n "$hit" ]]; then
                prev=$(( hit + $(printf '%s\n' "$marker" | wc -c) ))
                [[ "$prev" -gt "$OFFSET" ]] && prev="$OFFSET"
            fi
        fi
    fi
    if [[ "$OFFSET" -gt "$prev" ]]; then
        FRESH="$(tail -c "+$((prev + 1))" "$path")"
        LAST_LINE="$(tail -n1 <<<"$FRESH")"
    else
        FRESH=""
    fi
}

# Second line of defence: nothing stamped before we armed is news, whatever the
# offsets say. Both files timestamp in UTC with fixed-width fields, so a string
# compare is an ordering compare. Lines carrying no timestamp (traceback
# continuations) are kept rather than silently dropped.
since_armed_events() {
    awk -v cut="$STARTED_TS" '
        match($0, /"ts": "[^"]+"/) {
            ts = substr($0, RSTART + 7, RLENGTH - 8)
            if (ts > cut) print
            next
        }
        { print }
    '
}
since_armed_log() {
    awk -v cut="$STARTED_LOG" '
        /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9][0-9]:[0-9][0-9]:[0-9][0-9] / {
            if (substr($0, 1, 19) > cut) print
            next
        }
        { print }
    '
}

trap 'echo "health-watch: stopped"; exit 0' INT TERM

log_size="$(size_of "$AGENT_LOG")"
log_marker=""
events_size="$(size_of "$EVENTS_LOG")"
events_marker=""
misses=0
started="$(date -Is)"
STARTED_TS="$(date -u +%Y-%m-%dT%H:%M:%S)"
STARTED_LOG="$(date -u '+%Y-%m-%d %H:%M:%S')"

echo "health-watch: armed at $started -> $HEALTH_URL (every ${INTERVAL}s, trip after ${FAILS} misses)" >&2
echo "health-watch: logs $LOG_DIR" >&2

while true; do
    response="$(probe)"
    code="${response##*$'\n'}"
    body="${response%$'\n'*}"

    case "$code" in
        200)
            misses=0
            ;;
        503)
            echo "UNHEALTHY: the agent reports it is not well (HTTP 503)"
            echo "watch armed at $started, tripped at $(date -Is)"
            echo "probe: $HEALTH_URL"
            echo "$body"
            tail_log
            exit 2
            ;;
        401|403)
            echo "UNREACHABLE: HUD API rejected the token (HTTP $code)"
            echo "probe: $HEALTH_URL -- check HUD_TOKEN matches the agent's"
            exit 3
            ;;
        *)
            misses=$((misses + 1))
            if [[ "$misses" -ge "$FAILS" ]]; then
                echo "UNREACHABLE: no answer from the HUD API on $misses consecutive probes"
                echo "watch armed at $started, tripped at $(date -Is)"
                echo "probe: $HEALTH_URL (HTTP code: ${code:-none})"
                echo "The agent process is likely dead, or the port/host is unreachable."
                tail_log
                exit 3
            fi
            ;;
    esac

    if [[ -f "$AGENT_LOG" ]]; then
        read_new "$AGENT_LOG" "$log_size" "$log_marker"
        log_size="$OFFSET"; log_marker="$LAST_LINE"
        if hits="$(grep -E "$LOG_ERROR_RE" <<<"$FRESH" | since_armed_log)" && [[ -n "$hits" ]]; then
            echo "LOG ERROR: new error lines in $AGENT_LOG"
            echo "watch armed at $started, tripped at $(date -Is)"
            echo "$hits" | head -n 40
            tail_log
            exit 4
        fi
    fi

    if [[ -f "$EVENTS_LOG" ]]; then
        read_new "$EVENTS_LOG" "$events_size" "$events_marker"
        events_size="$OFFSET"; events_marker="$LAST_LINE"
        if hits="$(grep -E "\"($EVENT_ERROR_KINDS)\"" <<<"$FRESH" | since_armed_events)" && [[ -n "$hits" ]]; then
            echo "EVENT ERROR: the agent recorded a failure event"
            echo "watch armed at $started, tripped at $(date -Is)"
            echo "$hits" | head -n 20
            tail_log
            exit 5
        fi
    fi

    sleep "$INTERVAL"
done
