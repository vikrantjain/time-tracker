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
mkdir -p "$store_dir" 2>/dev/null || true

# Append one metadata-only event line (used by pause/resume markers).
append_event() {
  local ev="$1"
  jq -n -c \
    --argjson ts "${now_ts:-0}" \
    --arg event "$ev" \
    --arg session_id "${TT_SESSION_ID:-}" \
    --arg project "${TT_PROJECT:-}" \
    --arg source "${TT_SOURCE:-}" \
    --arg reason "${TT_REASON:-}" \
    '{ts: $ts, event: $event, session_id: $session_id, project: $project}
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
    # add <duration> [--to <project-or-customer>] [note...]
    #   duration : bare number = hours; suffix s/m/h; negative = correction.
    #   --to     : explicit target (project path or customer name). When
    #              omitted, the time is attributed to the CURRENT project (the
    #              cwd the command was invoked from), exactly like a session.
    #   note     : everything else (quoting optional); joined with spaces.
    # Tokenized honoring quotes WITHOUT invoking a shell.
    mapfile -t aargs < <(printf '%s' "$rest" | xargs -r -n1 printf '%s\n' 2>/dev/null || true)
    dur="${aargs[0]:-}"
    if [ -z "$dur" ]; then
      emit_block "Usage: tt add <duration> [--to <project-or-customer>] [note]  (e.g. tt add 2h \"fixed login bug\"  |  tt add 30m --to \"Acme Corp\" kickoff call)"
    fi
    # Validate the duration BEFORE writing. Otherwise junk (e.g. `tt add fix
    # the bug`) is written with duration="fix", confirmed as "Recorded", then
    # silently dropped at report time (report.py's parse_duration raises and the
    # row vanishes). Grammar mirrors parse_duration in report.py: optional '-',
    # a number, optional s/m/h suffix (bare number = hours).
    dur_re='^-?([0-9]+\.?[0-9]*|\.[0-9]+)[smh]?$'
    if ! [[ "$dur" =~ $dur_re ]]; then
      emit_block "tt add: '$dur' isn't a valid duration. Use 2h, 90m, 900s, or a bare number (= hours); prefix with - for a correction (e.g. -30m)."
    fi

    # Split the remaining args into an optional --to <value> and the note words.
    target=""
    target_explicit=0
    pos=()
    i=1
    n=${#aargs[@]}
    while [ "$i" -lt "$n" ]; do
      if [ "${aargs[$i]}" = "--to" ]; then
        target="${aargs[$((i+1))]:-}"
        target_explicit=1
        i=$((i + 2))
      else
        pos+=("${aargs[$i]}")
        i=$((i + 1))
      fi
    done
    note="${pos[*]}"

    defaulted=0
    if [ "$target_explicit" -eq 1 ]; then
      if [ -z "$target" ]; then
        emit_block "tt add: --to needs a value (a project path or customer name)."
      fi
    else
      # Default to the current project (cwd), mapped to a customer at report time.
      target="${TT_PROJECT:-}"
      defaulted=1
      if [ -z "$target" ]; then
        emit_block "tt add: no current project to attribute to — pass --to <project-or-customer>  (e.g. tt add 2h --to \"Acme Corp\" \"call\")."
      fi
    fi

    manual_file="${store_dir}/manual.jsonl"
    # Build the row first so we can echo back EXACTLY what landed in the file.
    record="$(jq -n -c \
      --argjson ts "${now_ts:-0}" \
      --arg project "$target" \
      --arg date "$(date +%F)" \
      --arg duration "$dur" \
      --arg note "$note" \
      '{ts: $ts, source: "manual", project: $project, date: $date, duration: $duration, note: $note}' \
      2>/dev/null)"
    [ -n "$record" ] && printf '%s\n' "$record" >> "$manual_file" 2>/dev/null || true
    where="$target"
    [ "$defaulted" -eq 1 ] && where="$target (current project)"
    msg="✎ Recorded ${dur} to '${where}'${note:+ — ${note}} (manual, billable; excluded from active-engagement)."
    # Show the verbatim stored line so the user can confirm the entry is right.
    [ -n "$record" ] && msg="${msg}
  saved: ${record}"
    emit_block "$msg"
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
      "  add <dur> [--to <tgt>] [note] Log off-session time (default target = current project)" \
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
