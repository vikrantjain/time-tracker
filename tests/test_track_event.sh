#!/bin/bash
# Tests for hooks/scripts/track-event.sh — monthly rotation and heartbeat
# throttling.
#
# Self-contained: sets up a temp store, drives the hook with synthetic
# payloads, and asserts on the monthly events file. Time passage is simulated
# by seeding events with back-dated `ts` values (the hook itself always stamps
# real time). Exits non-zero on any failure.

set -u
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/.." && pwd)"
export CLAUDE_PLUGIN_ROOT="$root"
track="$root/hooks/scripts/track-event.sh"

fail=0
pass=0
check() { # check <desc> <expected> <actual>
  if [ "$2" = "$3" ]; then
    pass=$((pass + 1))
  else
    fail=$((fail + 1))
    printf 'FAIL: %s\n  expected: %q\n  actual:   %q\n' "$1" "$2" "$3"
  fi
}

new_store() {
  export TIME_TRACKER_DIR="$(mktemp -d)"
  efile="$TIME_TRACKER_DIR/events-$(date +%Y-%m).jsonl"
}
fire() { # fire <event> <session_id> — drive the hook with a synthetic payload
  printf '{"session_id":"%s","cwd":"/proj/demo","prompt":"hello world"}' "$2" \
    | bash "$track" "$1"
}
seed() { # seed <event> <session_id> <ts-offset-seconds> — back-dated line
  printf '{"ts":%s,"event":"%s","session_id":"%s","project":"/proj/demo"}\n' \
    "$(( $(date +%s) + $3 ))" "$1" "$2" >> "$efile"
}
lines() { # line count; empty string when the file was never created
  [ -f "$efile" ] && wc -l < "$efile" | tr -d '[:space:]' || printf ''
}

# 1. Events land in the current month's file.
new_store
fire session_start s1 >/dev/null
check "monthly file created" "1" "$(lines)"
check "event recorded"       "session_start" "$(jq -r '.event' "$efile" | tail -1)"

# 2. A stop right after a prompt is throttled (heartbeat within 60s).
new_store
fire prompt s1 >/dev/null
fire stop s1 >/dev/null
check "rapid stop throttled" "1" "$(lines)"

# 3. A stop after the 60s window is written.
new_store
seed prompt s1 -120
fire stop s1 >/dev/null
check "stale heartbeat not throttled" "2" "$(lines)"

# 4. Tool heartbeats throttle on a 300s window...
new_store
seed prompt s1 -120
fire tool s1 >/dev/null
check "tool within 300s throttled" "1" "$(lines)"

# 5. ...and are written once the window passes.
new_store
seed tool s1 -400
fire tool s1 >/dev/null
check "tool after 300s written" "2" "$(lines)"

# 6. Sessions never throttle each other.
new_store
seed prompt s1 -10
fire prompt s2 >/dev/null
check "other session not throttled" "2" "$(lines)"

# 7. A prompt while paused is NEVER throttled (it is what auto-resumes).
new_store
seed prompt s1 -10
seed pause s1 -5
fire prompt s1 >/dev/null
check "paused prompt always written" "3" "$(lines)"

# 8. Session boundaries are never throttled.
new_store
seed prompt s1 -10
fire session_end s1 >/dev/null
check "session_end never throttled" "2" "$(lines)"

# 9. The tt sentinel still blocks before capture: no line, block response.
new_store
out="$(printf '{"session_id":"s1","cwd":"/proj/demo","prompt":"tt help"}' | bash "$track" prompt)"
check "sentinel emits block" "block" "$(printf '%s' "$out" | jq -r '.decision')"
check "sentinel not recorded" "" "$(lines)"

# 10. Metadata only: the prompt text never reaches the store.
new_store
seed session_start s1 -120
fire prompt s1 >/dev/null
check "prompt recorded"            "2" "$(lines)"
check "prompt text never stored"   "" "$(grep 'hello world' "$efile" || true)"

# 11. An escaped \tt prompt is not a sentinel: no block, recorded as a prompt.
new_store
out="$(printf '{"session_id":"s1","cwd":"/proj/demo","prompt":"\\\\tt is the word"}' | bash "$track" prompt)"
check "escaped tt not blocked" "" "$out"
check "escaped tt recorded"    "prompt" "$(jq -r '.event' "$efile" 2>/dev/null | tail -1)"

# 12. The /time-tracker:tt slash command is intercepted; another plugin's
#     `tt` and other commands pass through untouched.
expand="$root/hooks/scripts/track-expansion.sh"
new_store
out="$(printf '{"session_id":"s1","cwd":"/proj/demo","command_name":"time-tracker:tt","command_args":"help"}' | bash "$expand")"
check "slash tt blocks"          "block" "$(printf '%s' "$out" | jq -r '.decision')"
out="$(printf '{"session_id":"s1","cwd":"/proj/demo","command_name":"other-plugin:tt","command_args":"help"}' | bash "$expand")"
check "other plugin's tt passes" "" "$out"
out="$(printf '{"session_id":"s1","cwd":"/proj/demo","command_name":"commit","command_args":""}' | bash "$expand")"
check "other command passes"     "" "$out"
check "expansion records nothing" "" "$(lines)"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
