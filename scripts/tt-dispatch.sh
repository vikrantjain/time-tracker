#!/bin/bash
# Time Tracker — shared command dispatcher.
#
# Usage: tt-dispatch.sh "<argline>"
#   <argline> is everything after the verb, e.g.
#     'report --month 2026-05'   'add 2h "Acme" "call"'   'pause'   '' (-> help)
#
# Two entry points feed this script with the SAME argline:
#   - the typed `tt ` sentinel on UserPromptSubmit          (track-event.sh)
#   - the `/time-tracker:tt` slash command on UserPromptExpansion (track-expansion.sh)
#
# It runs the requested action entirely in-plugin and prints a hook BLOCK
# response (decision=block) so nothing reaches the model and nothing is
# recorded as activity. It ALWAYS exits 0.
#
# Context comes from the environment (set by the caller from the hook JSON):
#   CLAUDE_PLUGIN_ROOT  TT_SESSION_ID  TT_PROJECT  TT_SOURCE  TT_REASON

argline="${1:-}"
action="${argline%% *}"          # first word ('' for an empty argline)
rest="${argline#"$action"}"      # remainder (report filters / add args)

store_dir="${TIME_TRACKER_DIR:-$HOME/time-tracker}"
events_file="${store_dir}/events.jsonl"
now_ts="$(date +%s)"
now_iso="$(date -Iseconds)"
mkdir -p "$store_dir" 2>/dev/null || true

# Append one metadata-only event line (used by pause/resume markers).
append_event() {
  local ev="$1"
  jq -n -c \
    --argjson ts "${now_ts:-0}" \
    --arg iso "$now_iso" \
    --arg event "$ev" \
    --arg session_id "${TT_SESSION_ID:-}" \
    --arg project "${TT_PROJECT:-}" \
    --arg source "${TT_SOURCE:-}" \
    --arg reason "${TT_REASON:-}" \
    '{ts: $ts, iso: $iso, event: $event, session_id: $session_id, project: $project}
     + (if $source != "" then {source: $source} else {} end)
     + (if $reason != "" then {reason: $reason} else {} end)' \
    >> "$events_file" 2>/dev/null || true
}

# Print a hook block response (no model turn, user-facing message) and exit.
emit_block() {
  jq -n -c --arg reason "$1" \
    '{decision: "block", reason: $reason, suppressOriginalPrompt: true}' \
    2>/dev/null || printf '{"decision":"block","reason":"(report unavailable)","suppressOriginalPrompt":true}'
  exit 0
}

case "$action" in
  report)
    # Tokenize the filter string honoring quotes WITHOUT invoking a shell
    # (xargs parses quotes/escapes but never runs the tokens as a command).
    mapfile -t rargs < <(printf '%s' "$rest" | xargs -r -n1 printf '%s\n' 2>/dev/null || true)
    out="$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" --dir "$store_dir" "${rargs[@]}" 2>&1)"
    [ -z "$out" ] && out="(no output)"
    emit_block "$out"
    ;;
  add)
    # add <duration> <project-or-customer> "<note>"
    # Tokenized honoring quotes; duration kept as a string for the engine to
    # parse (bare number = hours; suffix s/m/h; negative = correction).
    mapfile -t aargs < <(printf '%s' "$rest" | xargs -r -n1 printf '%s\n' 2>/dev/null || true)
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
    # Record a pause MARKER (not a heartbeat). The engine treats the span until
    # the next resume / real prompt / session end as suppressed.
    append_event "pause"
    emit_block "⏸ Tracking paused for this session. Resume with 'tt resume' (or '/time-tracker:tt resume'), or just send your next prompt."
    ;;
  resume)
    append_event "resume"
    emit_block "▶ Tracking resumed."
    ;;
  help|"")
    # Static help. Model-free and not recorded as activity. A bare verb
    # (typed `tt`, or `/time-tracker:tt` with no args) lands here.
    emit_block "$(printf '%s\n' \
      "time-tracker — session time tracking" \
      "Use either form:   tt <cmd>      or      /time-tracker:tt <cmd>" \
      "" \
      "  report [filters]              Wall-clock + active-engagement per project/customer" \
      "  add <dur> <target> \"<note>\"   Record out-of-session time (e.g. tt add 2h \"Acme\" \"call\")" \
      "  pause                         Exclude a deliberate idle span (auto-resumes on next prompt)" \
      "  resume                        Resume tracking now" \
      "  help                          Show this help" \
      "" \
      "Also: /time-tracker:timesheet   Model-formatted timesheet over the tracked data" \
      "" \
      "Tip: prefix the typed form with a backslash (\\tt ...) to send a literal 'tt' line to the model.")"
    ;;
  *)
    emit_block "Unknown tt command: '${action}'. Run 'tt help' (or '/time-tracker:tt') for available commands."
    ;;
esac
