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

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
