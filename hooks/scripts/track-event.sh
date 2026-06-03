#!/bin/bash
# Time Tracker — capture hook + sentinel interpreter.
#
# Usage: track-event.sh <event>
#   event ∈ { session_start | prompt | stop | session_end }
#
# Reads the hook JSON payload from stdin. For most events it appends ONE
# metadata-only JSON line to events.jsonl (never prompt/response text).
#
# On the `prompt` (UserPromptSubmit) path it first checks for a `tt ` SENTINEL
# command and, if found, hands the arg line to scripts/tt-dispatch.sh (shared
# with the /time-tracker:tt slash command). The dispatcher runs the action
# in-plugin and BLOCKS, so it never reaches the model and is never recorded as
# activity. A non-sentinel prompt falls through and is recorded as a heartbeat.
#
# The store lives OUTSIDE the plugin dir (wiped on update/uninstall):
#   ${TIME_TRACKER_DIR:-$HOME/time-tracker}/events.jsonl
#
# This hook must never break the user's session: it always exits 0 and swallows
# its own errors (a missing line is preferable to a blocked prompt).

event="${1:-}"
payload="$(cat 2>/dev/null || true)"

store_dir="${TIME_TRACKER_DIR:-$HOME/time-tracker}"
events_file="${store_dir}/events.jsonl"

# Metadata only (no prompt/response text is ever read into a stored field).
session_id="$(printf '%s' "$payload" | jq -r '.session_id // ""' 2>/dev/null || true)"
project="$(printf '%s' "$payload" | jq -r '.cwd // ""' 2>/dev/null || true)"
source="$(printf '%s' "$payload" | jq -r '.source // ""' 2>/dev/null || true)"
reason="$(printf '%s' "$payload" | jq -r '.reason // ""' 2>/dev/null || true)"

now_ts="$(date +%s)"
now_iso="$(date -Iseconds)"

mkdir -p "$store_dir" 2>/dev/null || true

# Append one metadata-only event line. Args: event, [extra jq object].
append_event() {
  local ev="$1"
  jq -n -c \
    --argjson ts "${now_ts:-0}" \
    --arg iso "$now_iso" \
    --arg event "$ev" \
    --arg session_id "$session_id" \
    --arg project "$project" \
    --arg source "$source" \
    --arg reason "$reason" \
    '{ts: $ts, iso: $iso, event: $event, session_id: $session_id, project: $project}
     + (if $source != "" then {source: $source} else {} end)
     + (if $reason != "" then {reason: $reason} else {} end)' \
    >> "$events_file" 2>/dev/null || true
}

# ---- sentinel handling (UserPromptSubmit only) --------------------------- #
if [ "$event" = "prompt" ]; then
  prompt="$(printf '%s' "$payload" | jq -r '.prompt // ""' 2>/dev/null || true)"

  # Escaped: a prompt that legitimately starts with `\tt ` is NOT a sentinel —
  # it falls through to normal recording and reaches the model.
  if [ "${prompt:0:4}" = "\\tt " ]; then
    : # not a sentinel; fall through
  elif [ "$prompt" = "tt" ] || [ "${prompt:0:3}" = "tt " ]; then
    # Hand the arg line (everything after 'tt ') to the shared dispatcher; it
    # prints the block response and exits, so we never reach normal capture.
    export TT_SESSION_ID="$session_id" TT_PROJECT="$project" TT_SOURCE="$source" TT_REASON="$reason"
    exec bash "${CLAUDE_PLUGIN_ROOT}/scripts/tt-dispatch.sh" "${prompt:3}"
  fi
fi

# ---- normal capture ------------------------------------------------------ #
append_event "$event"
exit 0
