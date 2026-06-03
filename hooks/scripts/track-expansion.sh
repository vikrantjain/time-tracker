#!/bin/bash
# Time Tracker — UserPromptExpansion handler for the /time-tracker:tt command.
#
# UserPromptSubmit does NOT fire for slash commands; UserPromptExpansion does.
# This hook fires when a user types `/time-tracker:tt ...`, reads the hook JSON
# from stdin, and (if it's our command) delegates to the shared tt-dispatch.sh,
# which blocks the expansion so it never reaches the model and is never recorded
# as activity. Any other command is allowed through untouched.
#
# It ALWAYS exits 0 so it can never break command expansion.

payload="$(cat 2>/dev/null || true)"

command_name="$(printf '%s' "$payload" | jq -r '.command_name // ""' 2>/dev/null || true)"
command_args="$(printf '%s' "$payload" | jq -r '.command_args // ""' 2>/dev/null || true)"

# Only handle our command; tolerate bare and plugin-namespaced forms.
case "$command_name" in
  tt | time-tracker:tt | *:tt) ;;
  *) exit 0 ;;   # not ours — allow the expansion to proceed normally
esac

session_id="$(printf '%s' "$payload" | jq -r '.session_id // ""' 2>/dev/null || true)"
project="$(printf '%s' "$payload" | jq -r '.cwd // ""' 2>/dev/null || true)"

export TT_SESSION_ID="$session_id" TT_PROJECT="$project" TT_SOURCE="" TT_REASON=""
exec bash "${CLAUDE_PLUGIN_ROOT}/scripts/tt-dispatch.sh" "$command_args"
