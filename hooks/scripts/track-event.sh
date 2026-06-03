#!/bin/bash
# Activity Tracker — capture hook + sentinel interpreter.
#
# Usage: track-event.sh <event>
#   event ∈ { session_start | prompt | stop | session_end }
#
# Reads the hook JSON payload from stdin. For most events it appends ONE
# metadata-only JSON line to events.jsonl (never prompt/response text).
#
# On the `prompt` (UserPromptSubmit) path it first checks for a `tt ` SENTINEL
# command. A sentinel is handled entirely in-plugin and then BLOCKED, so it
# never reaches the model and is never recorded as activity:
#   - prints {"decision":"block","reason":<output>,"suppressOriginalPrompt":true}
#   - "block" prevents the prompt from being processed and erases it; "reason"
#     is shown to the USER only (not added to the model context).
# A non-sentinel prompt falls through and is recorded as a normal heartbeat.
#
# The store lives OUTSIDE the plugin dir (wiped on update/uninstall):
#   ${ACTIVITY_TRACKER_DIR:-$HOME/activity-tracker}/events.jsonl
#
# This hook must never break the user's session: it always exits 0 and swallows
# its own errors (a missing line is preferable to a blocked prompt).

event="${1:-}"
payload="$(cat 2>/dev/null || true)"

store_dir="${ACTIVITY_TRACKER_DIR:-$HOME/activity-tracker}"
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

# Emit a UserPromptSubmit block response (no model turn, user-facing message)
# and exit. Skips recording so the sentinel is never counted as activity.
emit_block() {
  jq -n -c --arg reason "$1" \
    '{decision: "block", reason: $reason, suppressOriginalPrompt: true}' \
    2>/dev/null || printf '{"decision":"block","reason":"(report unavailable)","suppressOriginalPrompt":true}'
  exit 0
}

# ---- sentinel handling (UserPromptSubmit only) --------------------------- #
if [ "$event" = "prompt" ]; then
  prompt="$(printf '%s' "$payload" | jq -r '.prompt // ""' 2>/dev/null || true)"

  # Escaped: a prompt that legitimately starts with `\tt ` is NOT a sentinel —
  # it falls through to normal recording and reaches the model.
  if [ "${prompt:0:4}" = "\\tt " ]; then
    : # not a sentinel; fall through
  elif [ "${prompt:0:3}" = "tt " ]; then
    sentinel="${prompt:3}"                 # everything after 'tt '
    action="${sentinel%% *}"               # first word
    rest="${sentinel#"$action"}"           # remaining filter string

    case "$action" in
      report)
        # Tokenize the filter string honoring quotes WITHOUT invoking a shell
        # (xargs parses quotes/escapes but never runs the tokens as a command).
        mapfile -t rargs < <(printf '%s' "$rest" | xargs -n1 printf '%s\n' 2>/dev/null || true)
        out="$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" --dir "$store_dir" "${rargs[@]}" 2>&1)"
        [ -z "$out" ] && out="(no output)"
        emit_block "$out"
        ;;
      add)
        # tt add <duration> <project-or-customer> "<note>"
        # Tokenized honoring quotes; duration kept as a string for the engine
        # to parse (bare number = hours; suffix s/m/h; negative = correction).
        mapfile -t aargs < <(printf '%s' "$rest" | xargs -n1 printf '%s\n' 2>/dev/null || true)
        dur="${aargs[0]:-}"
        target="${aargs[1]:-}"
        note="${aargs[2]:-}"
        if [ -z "$dur" ] || [ -z "$target" ]; then
          emit_block "Usage: tt add <duration> <project-or-customer> \"<note>\"  (e.g. tt add 2h \"Acme Corp\" \"phone call\")"
        fi
        manual_file="${store_dir}/manual.jsonl"
        jq -n -c \
          --argjson ts "${now_ts:-0}" \
          --arg iso "$now_iso" \
          --arg project "$target" \
          --arg date "$(date +%F)" \
          --arg duration "$dur" \
          --arg note "$note" \
          '{ts: $ts, iso: $iso, source: "manual", project: $project, date: $date, duration: $duration, note: $note}' \
          >> "$manual_file" 2>/dev/null || true
        emit_block "✎ Recorded ${dur} to '${target}'${note:+ — ${note}} (manual, billable; excluded from active-engagement)."
        ;;
      pause)
        # Record a pause MARKER (not a heartbeat) and block. The marker carries
        # session_id/project/ts; the engine treats the span until the next
        # resume / real prompt / session end as suppressed.
        append_event "pause"
        emit_block "⏸ Tracking paused for this session. Resume with 'tt resume' or just send your next prompt."
        ;;
      resume)
        append_event "resume"
        emit_block "▶ Tracking resumed."
        ;;
      *)
        emit_block "Unknown tt command: '${action}'. Available: report, pause, resume, add"
        ;;
    esac
  fi
fi

# ---- normal capture ------------------------------------------------------ #
append_event "$event"
exit 0
