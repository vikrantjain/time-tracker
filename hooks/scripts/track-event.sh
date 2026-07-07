#!/bin/bash
# Time Tracker — capture hook + sentinel interpreter.
#
# Usage: track-event.sh <event>
#   event ∈ { session_start | prompt | stop | tool | session_end }
#
# Reads the hook JSON payload from stdin. For most events it appends ONE
# metadata-only JSON line to the current month's events file (never
# prompt/response text). `tool` fires on PostToolUse and exists so that long
# autonomous turns still produce heartbeats for the active-engagement metric.
#
# On the `prompt` (UserPromptSubmit) path it first checks for a `tt ` SENTINEL
# command and, if found, hands the arg line to scripts/tt-dispatch.sh (shared
# with the /time-tracker:tt slash command). The dispatcher runs the action
# in-plugin and BLOCKS, so it never reaches the model and is never recorded as
# activity. A non-sentinel prompt falls through and is recorded as a heartbeat.
#
# THROTTLING: prompt/stop/tool events are pure heartbeats consumed at
# minutes-scale granularity (the idle threshold defaults to 15 min), so a new
# heartbeat is skipped when this session already logged one within the last
# 60s (prompt/stop) or 300s (tool). session_start/session_end and the
# pause/resume markers are never throttled, and neither is a prompt while the
# session is paused — that prompt is what auto-resumes the pause.
#
# The store lives OUTSIDE the plugin dir (wiped on update/uninstall) and is
# rotated monthly so no single file grows forever:
#   ${TIME_TRACKER_DIR:-$HOME/.time-tracker}/events-YYYY-MM.jsonl
# (report.py also reads the legacy pre-rotation events.jsonl when present.)
#
# Concurrent sessions append to the same file. Each line is one small
# O_APPEND write far below PIPE_BUF, which Linux keeps atomic, so lines from
# parallel sessions never interleave.
#
# This hook must never break the user's session: it always exits 0 and swallows
# its own errors (a missing line is preferable to a blocked prompt).

event="${1:-}"
payload="$(cat 2>/dev/null || true)"

store_dir="${TIME_TRACKER_DIR:-$HOME/.time-tracker}"
events_file="${store_dir}/events-$(date +%Y-%m).jsonl"

# Metadata only (no prompt/response text is ever read into a stored field).
session_id="$(printf '%s' "$payload" | jq -r '.session_id // ""' 2>/dev/null || true)"
project="$(printf '%s' "$payload" | jq -r '.cwd // ""' 2>/dev/null || true)"
source="$(printf '%s' "$payload" | jq -r '.source // ""' 2>/dev/null || true)"
reason="$(printf '%s' "$payload" | jq -r '.reason // ""' 2>/dev/null || true)"

now_ts="$(date +%s)"

mkdir -p "$store_dir" 2>/dev/null || true

# Append one metadata-only event line. Args: event, [extra jq object].
append_event() {
  local ev="$1"
  jq -n -c \
    --argjson ts "${now_ts:-0}" \
    --arg event "$ev" \
    --arg session_id "$session_id" \
    --arg project "$project" \
    --arg source "$source" \
    --arg reason "$reason" \
    '{ts: $ts, event: $event, session_id: $session_id, project: $project}
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

# ---- heartbeat throttling ------------------------------------------------ #
# Returns 0 (throttle: skip the write) only when this session has a heartbeat
# newer than the event's threshold in the CURRENT month's file. Fails open:
# any doubt (no file, month just rolled over, unparsable tail) means "write".
should_throttle() {
  local threshold
  case "$event" in
    prompt|stop) threshold=60 ;;
    tool)        threshold=300 ;;
    *)           return 1 ;;
  esac
  [ -n "$session_id" ] || return 1
  [ -f "$events_file" ] || return 1
  local recent last_hb=0 pause_state="" ts ev
  recent="$(tail -n 100 "$events_file" 2>/dev/null \
    | jq -rR --arg sid "$session_id" \
        'fromjson? // empty | select(.session_id == $sid) | "\(.ts) \(.event)"' \
    2>/dev/null || true)"
  [ -n "$recent" ] || return 1
  while read -r ts ev; do
    case "$ev" in
      session_start|prompt|stop|tool)
        [ "$ts" -gt "$last_hb" ] 2>/dev/null && last_hb="$ts" ;;
    esac
    case "$ev" in
      pause) pause_state="paused" ;;
      resume|prompt|session_end) pause_state="" ;;
    esac
  done <<< "$recent"
  # A paused session's next prompt closes the pause — it must be recorded.
  if [ "$event" = "prompt" ] && [ "$pause_state" = "paused" ]; then
    return 1
  fi
  [ "$last_hb" -gt 0 ] 2>/dev/null || return 1
  [ $(( now_ts - last_hb )) -lt "$threshold" ]
}

# ---- normal capture ------------------------------------------------------ #
if should_throttle; then
  exit 0
fi
append_event "$event"
exit 0
