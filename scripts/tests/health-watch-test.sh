#!/usr/bin/env bash
# Unit tests for health-watch.sh's log-reading, which is the part with the
# interesting failure: the agent trims its own logs in place, and a trim looks
# like a rotation if you only watch the size. Getting that wrong replays weeks
# of history as new errors.
#
# Sources the helpers out of the script rather than running the watcher, so no
# HTTP endpoint or live agent is needed. Run: bash scripts/tests/health-watch-test.sh
set -uo pipefail

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$HERE/../health-watch.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# Everything from size_of() down to the trap is pure helpers.
start="$(grep -n '^size_of()' "$TARGET" | cut -d: -f1)"
end="$(grep -n '^trap ' "$TARGET" | cut -d: -f1)"
sed -n "${start},$((end - 1))p" "$TARGET" > "$TMP/funcs.sh"
# shellcheck disable=SC1090
. "$TMP/funcs.sh"
type read_new since_armed_events >/dev/null 2>&1 || {
    echo "FATAL: helpers did not load from $TARGET" >&2; exit 99; }

STARTED_TS="2026-09-18T15:55:12"
STARTED_LOG="2026-09-18 15:55:12"
EVENT_ERROR_KINDS='live_error|hl_order_rejected'

pass=0; fail=0
check() {
    if [[ "$2" == "$3" ]]; then
        echo "  PASS $1"; pass=$((pass + 1))
    else
        echo "  FAIL $1: got [$2] want [$3]"; fail=$((fail + 1))
    fi
}
scan() { grep -E "\"($EVENT_ERROR_KINDS)\"" <<<"$FRESH" | since_armed_events | wc -l | tr -d ' '; }

F="$TMP/events.jsonl"
{ echo '{"ts": "2026-08-31T22:56:35+00:00", "kind": "live_error", "summary": "old 1"}'
  echo '{"ts": "2026-09-01T21:18:33+00:00", "kind": "live_error", "summary": "old 2"}'
  echo '{"ts": "2026-09-13T19:50:05+00:00", "kind": "hl_order_rejected", "coin": "PUMP"}'
  echo '{"ts": "2026-09-18T16:00:00+00:00", "kind": "open_fill_persisted", "note": "routine"}'; } > "$F"

echo "a file that is nothing but history"
read_new "$F" 0 ""; size="$OFFSET"; marker="$LAST_LINE"
check "first read pages nobody" "$(scan)" "0"

echo "the prune trims the head; the file shrinks but stays old"
sed -i '1d' "$F"
read_new "$F" "$size" "$marker"; size="$OFFSET"; marker="$LAST_LINE"
check "nothing is replayed" "$(scan)" "0"
check "nothing is even read" "${FRESH:-empty}" "empty"

echo "a real error arrives after the trim"
echo '{"ts": "2026-09-18T17:30:00+00:00", "kind": "live_error", "summary": "REAL"}' >> "$F"
read_new "$F" "$size" "$marker"; size="$OFFSET"; marker="$LAST_LINE"
check "it is still caught" "$(scan)" "1"

echo "a true rotation leaves a new file"
: > "$F"
echo '{"ts": "2026-09-18T17:45:00+00:00", "kind": "live_error", "summary": "AFTER ROTATE"}' > "$F"
read_new "$F" "$size" "$marker"; size="$OFFSET"; marker="$LAST_LINE"
check "read from zero, error caught" "$(scan)" "1"

echo "a row stamped before we armed"
echo '{"ts": "2026-09-01T00:00:00+00:00", "kind": "live_error", "summary": "ancient"}' >> "$F"
read_new "$F" "$size" "$marker"
check "cannot page" "$(scan)" "0"

echo
echo "passed=$pass failed=$fail"
[[ "$fail" -eq 0 ]]
