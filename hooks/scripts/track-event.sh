#!/bin/bash
# Activity Tracker — capture hook.
#
# Usage: track-event.sh <event>
#   event ∈ { session_start | prompt | stop | session_end }
#
# Reads the hook JSON payload from stdin and appends ONE metadata-only JSON line
# to events.jsonl. It never stores prompt or response text — only metadata.
#
# The store lives OUTSIDE the plugin dir (which is wiped on update/uninstall):
#   ${ACTIVITY_TRACKER_DIR:-$HOME/activity-tracker}/events.jsonl
#
# This hook must never break the user's session, so it always exits 0 and
# swallows its own errors (a missing line is preferable to a blocked prompt).
#
# NOTE: the `prompt` (UserPromptSubmit) path will later gain `tt ` sentinel
# handling (pause/resume/add/report) ahead of recording — see Story 6. Keep the
# dispatch below easy to branch on.

event="${1:-}"

# Read the full payload once; everything downstream is best-effort.
payload="$(cat 2>/dev/null || true)"

store_dir="${ACTIVITY_TRACKER_DIR:-$HOME/activity-tracker}"
events_file="${store_dir}/events.jsonl"

# Extract metadata only (no prompt/response text is ever read into a field).
session_id="$(printf '%s' "$payload" | jq -r '.session_id // ""' 2>/dev/null || true)"
project="$(printf '%s' "$payload" | jq -r '.cwd // ""' 2>/dev/null || true)"
source="$(printf '%s' "$payload" | jq -r '.source // ""' 2>/dev/null || true)"
reason="$(printf '%s' "$payload" | jq -r '.reason // ""' 2>/dev/null || true)"

ts="$(date +%s)"
iso="$(date -Iseconds)"

mkdir -p "$store_dir" 2>/dev/null || true

jq -n -c \
  --argjson ts "${ts:-0}" \
  --arg iso "$iso" \
  --arg event "$event" \
  --arg session_id "$session_id" \
  --arg project "$project" \
  --arg source "$source" \
  --arg reason "$reason" \
  '{ts: $ts, iso: $iso, event: $event, session_id: $session_id, project: $project}
   + (if $source != "" then {source: $source} else {} end)
   + (if $reason != "" then {reason: $reason} else {} end)' \
  >> "$events_file" 2>/dev/null || true

exit 0
