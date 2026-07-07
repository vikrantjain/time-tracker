#!/bin/bash
# Tests for scripts/tt-dispatch.sh — focuses on `tt add` target resolution
# (default to current project vs explicit --to override).
#
# Self-contained: sets up a temp store, drives the dispatcher, and asserts on
# the manual.jsonl rows and block messages. Exits non-zero on any failure.

set -u
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/.." && pwd)"
export CLAUDE_PLUGIN_ROOT="$root"
dispatch="$root/scripts/tt-dispatch.sh"

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
contains() { # contains <desc> <needle> <haystack>
  case "$3" in
    *"$2"*) pass=$((pass + 1)) ;;
    *) fail=$((fail + 1)); printf 'FAIL: %s\n  expected substring: %q\n  in:                %q\n' "$1" "$2" "$3" ;;
  esac
}

# Fresh store + a current project for each scenario.
new_store() { export TIME_TRACKER_DIR="$(mktemp -d)"; }
last_manual() { jq -c '.' "$TIME_TRACKER_DIR/manual.jsonl" 2>/dev/null | tail -1; }

# 1. Default target = current project; multi-word note needs no quotes.
new_store
export TT_PROJECT="/proj/acme-api"
bash "$dispatch" "add 2h fixed login bug" >/dev/null
row="$(last_manual)"
check "default project"      "/proj/acme-api"  "$(printf '%s' "$row" | jq -r '.project')"
check "default note joined"  "fixed login bug" "$(printf '%s' "$row" | jq -r '.note')"
check "default duration"     "2h"              "$(printf '%s' "$row" | jq -r '.duration')"

# 2. --to override sets an explicit target (customer name); note follows.
new_store
export TT_PROJECT="/proj/beta"
bash "$dispatch" 'add 30m --to "Acme Corp" kickoff call' >/dev/null
row="$(last_manual)"
check "--to target"   "Acme Corp"    "$(printf '%s' "$row" | jq -r '.project')"
check "--to note"     "kickoff call" "$(printf '%s' "$row" | jq -r '.note')"

# 3. Duration only -> current project, empty note.
new_store
export TT_PROJECT="/proj/gamma"
bash "$dispatch" "add 1h" >/dev/null
row="$(last_manual)"
check "dur-only project" "/proj/gamma" "$(printf '%s' "$row" | jq -r '.project')"
check "dur-only note"    ""            "$(printf '%s' "$row" | jq -r '.note')"

# 4. Negative correction keeps the sign and defaults the project.
new_store
export TT_PROJECT="/proj/acme-api"
bash "$dispatch" "add -30m over-counted" >/dev/null
row="$(last_manual)"
check "correction duration" "-30m" "$(printf '%s' "$row" | jq -r '.duration')"
check "correction project"  "/proj/acme-api" "$(printf '%s' "$row" | jq -r '.project')"

# 5. No duration -> usage message, nothing written.
new_store
export TT_PROJECT="/proj/acme-api"
out="$(bash "$dispatch" "add" | jq -r '.reason')"
contains "missing duration usage" "Usage: tt add" "$out"
check    "missing duration no-write" "" "$(last_manual)"

# 6. No current project and no --to -> actionable error, nothing written.
new_store
export TT_PROJECT=""
out="$(bash "$dispatch" "add 2h some note" | jq -r '.reason')"
contains "no project error" "no current project" "$out"
check    "no project no-write" "" "$(last_manual)"

# 7. --to with no value -> error.
new_store
export TT_PROJECT="/proj/acme-api"
out="$(bash "$dispatch" "add 2h --to" | jq -r '.reason')"
contains "--to needs value" "--to needs a value" "$out"

# 8. Invalid duration -> rejected up front, nothing written (no silent loss).
new_store
export TT_PROJECT="/proj/acme-api"
out="$(bash "$dispatch" "add bogus the bug" | jq -r '.reason')"
contains "invalid duration rejected" "isn't a valid duration" "$out"
check    "invalid duration no-write" "" "$(last_manual)"

# 9. Successful add echoes back the verbatim saved JSON record.
new_store
export TT_PROJECT="/proj/acme-api"
out="$(bash "$dispatch" "add 2h fixed login bug" | jq -r '.reason')"
contains "echo saved label"    "saved:"             "$out"
contains "echo saved duration" "\"duration\":\"2h\"" "$out"
contains "echo saved note"     "fixed login bug"     "$out"

# 10. Apostrophe in a free-text note keeps every token (xargs regression:
#     the old tokenizer dropped everything after the unmatched quote).
new_store
export TT_PROJECT="/proj/acme-api"
bash "$dispatch" "add 2h don't forget the fix" >/dev/null
row="$(last_manual)"
check "apostrophe note kept"    "don't forget the fix" "$(printf '%s' "$row" | jq -r '.note')"
check "apostrophe duration"     "2h"                   "$(printf '%s' "$row" | jq -r '.duration')"

# 11. Apostrophe inside a double-quoted --to value still parses as one token.
new_store
export TT_PROJECT="/proj/beta"
argline="add 30m --to \"O'Brien Ltd\" sync call"
bash "$dispatch" "$argline" >/dev/null
row="$(last_manual)"
check "--to with apostrophe" "O'Brien Ltd" "$(printf '%s' "$row" | jq -r '.project')"
check "note after quoted --to" "sync call" "$(printf '%s' "$row" | jq -r '.note')"

# 12. Quoted report filters tokenize and reach the engine (model-free path).
new_store
out="$(bash "$dispatch" 'report --customer "Acme Corp"' | jq -r '.reason')"
check "quoted report filter runs" "No activity recorded." "$out"

# 13b. Period shorthand passes through to the engine.
new_store
out="$(bash "$dispatch" "report today" | jq -r '.reason')"
contains "period shorthand runs" "No activity in the selected period" "$out"

# 13. pause/resume markers land in the current MONTH's events file.
new_store
export TT_SESSION_ID="sess1" TT_PROJECT="/proj/acme-api"
bash "$dispatch" "pause" >/dev/null
month_file="$TIME_TRACKER_DIR/events-$(date +%Y-%m).jsonl"
check "pause marker in monthly file" "pause" "$(jq -r '.event' "$month_file" 2>/dev/null | tail -1)"

# 16. tt map creates the mapping and confirms it.
new_store
export TT_PROJECT="/proj/acme-api"
out="$(bash "$dispatch" 'map "Acme Corp" --name "Acme API"' | jq -r '.reason')"
contains "map confirms"      "Mapped /proj/acme-api" "$out"
toml="$(cat "$TIME_TRACKER_DIR/projects.toml")"
contains "map writes customer" 'customer = "Acme Corp"' "$toml"
contains "map writes name"     'name = "Acme API"'      "$toml"

# 17. Hand-written comments survive later map calls.
printf '# hand-written billing note\n' >> "$TIME_TRACKER_DIR/projects.toml"
export TT_PROJECT="/proj/beta"
bash "$dispatch" 'map "Beta LLC"' >/dev/null
toml="$(cat "$TIME_TRACKER_DIR/projects.toml")"
contains "comment preserved"    "hand-written billing note" "$toml"
contains "old mapping preserved" 'customer = "Acme Corp"'    "$toml"
contains "new mapping added"     'customer = "Beta LLC"'     "$toml"

# 18. Remapping an existing project rewrites its table in place.
export TT_PROJECT="/proj/beta"
bash "$dispatch" 'map "Gamma Inc"' >/dev/null
toml="$(cat "$TIME_TRACKER_DIR/projects.toml")"
contains "remap took effect" 'customer = "Gamma Inc"' "$toml"
check "old customer gone" "" "$(grep 'Beta LLC' "$TIME_TRACKER_DIR/projects.toml" || true)"
check "single table for project" "1" "$(grep -c '\["/proj/beta"\]' "$TIME_TRACKER_DIR/projects.toml")"

# 19. Bare tt map lists mappings; no project context errors helpfully.
out="$(bash "$dispatch" 'map' | jq -r '.reason')"
contains "map lists" "Acme Corp" "$out"
export TT_PROJECT=""
out="$(bash "$dispatch" 'map "Acme Corp"' | jq -r '.reason')"
contains "map without project" "no current project" "$out"

# 20. An unmapped project in a report comes with the tt map hint.
new_store
export TT_SESSION_ID="sess9" TT_PROJECT="/proj/unmapped"
month_file="$TIME_TRACKER_DIR/events-$(date +%Y-%m).jsonl"
now="$(date +%s)"
printf '{"ts":%s,"event":"session_start","session_id":"sess9","project":"/proj/unmapped"}\n{"ts":%s,"event":"session_end","session_id":"sess9","project":"/proj/unmapped"}\n' \
  "$(( now - 600 ))" "$now" >> "$month_file"
out="$(bash "$dispatch" 'report' | jq -r '.reason')"
contains "unmapped hint in report" "tt map" "$out"

# 14. tt status reports tracking state and the status header.
new_store
export TT_SESSION_ID="sess1" TT_PROJECT="/proj/acme-api"
month_file="$TIME_TRACKER_DIR/events-$(date +%Y-%m).jsonl"
printf '{"ts":%s,"event":"session_start","session_id":"sess1","project":"/proj/acme-api"}\n' \
  "$(( $(date +%s) - 600 ))" >> "$month_file"
out="$(bash "$dispatch" "status" | jq -r '.reason')"
contains "status header"        "time-tracker status" "$out"
contains "status shows tracking" "session: tracking"  "$out"

# 15. tt status while paused says so.
bash "$dispatch" "pause" >/dev/null
out="$(bash "$dispatch" "status" | jq -r '.reason')"
contains "status shows paused" "paused" "$out"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
