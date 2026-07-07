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
events_file="${store_dir}/events-$(date +%Y-%m).jsonl"
now_ts="$(date +%s)"
mkdir -p "$store_dir" 2>/dev/null || true

# Tokenize a string into NUL-terminated tokens honoring shell-style quotes,
# WITHOUT invoking a shell on it. Unbalanced quotes — an apostrophe in a free-
# text note like `tt add 2h don't forget` — fall back to plain whitespace
# splitting instead of silently dropping every token after the quote (which is
# what the previous xargs-based tokenizer did). python3 is already a hard
# dependency via report.py.
tokenize() {
  python3 -c '
import shlex, sys
s = sys.argv[1] if len(sys.argv) > 1 else ""
try:
    toks = shlex.split(s)
except ValueError:
    toks = s.split()
sys.stdout.write("".join(t + "\0" for t in toks))
' "$1" 2>/dev/null || true
}

# Append one metadata-only event line (used by pause/resume markers).
# An optional second arg is a JSON object merged into the line (e.g. a timed
# pause's {"until": ...}).
append_event() {
  local ev="$1"
  local extra="${2:-null}"
  jq -n -c \
    --argjson ts "${now_ts:-0}" \
    --arg event "$ev" \
    --arg session_id "${TT_SESSION_ID:-}" \
    --arg project "${TT_PROJECT:-}" \
    --arg source "${TT_SOURCE:-}" \
    --arg reason "${TT_REASON:-}" \
    --argjson extra "$extra" \
    '{ts: $ts, event: $event, session_id: $session_id, project: $project}
     + (if $source != "" then {source: $source} else {} end)
     + (if $reason != "" then {reason: $reason} else {} end)
     + ($extra // {})' \
    >> "$events_file" 2>/dev/null || true
}

# Echo "<ts> <until|->" for this session's open pause, or nothing when not
# paused. Reads only the current month's tail — a miss fails open (the caller
# behaves as if not paused, which at worst records a harmless extra marker).
pause_state() {
  local recent ts ev until state=""
  [ -n "${TT_SESSION_ID:-}" ] || return 0
  [ -f "$events_file" ] || return 0
  recent="$(tail -n 200 "$events_file" 2>/dev/null \
    | jq -rR --arg sid "$TT_SESSION_ID" \
        'fromjson? // empty | select(.session_id == $sid)
         | "\(.ts) \(.event) \(.until // "-")"' \
    2>/dev/null || true)"
  [ -n "$recent" ] || return 0
  while read -r ts ev until; do
    case "$ev" in
      pause) [ -z "$state" ] && state="$ts $until" ;;
      resume|prompt|session_end) state="" ;;
    esac
  done <<< "$recent"
  if [ -n "$state" ]; then
    set -- $state
    # A timed pause expires on its own once `until` passes.
    if [ "$2" != "-" ] && [ "$now_ts" -ge "$2" ] 2>/dev/null; then
      state=""
    fi
  fi
  printf '%s' "$state"
}

hhmm() { date -d "@$1" +%H:%M 2>/dev/null || printf '?'; }

# Print a hook block response (no model turn, user-facing message) and exit.
emit_block() {
  jq -n -c --arg reason "$1" \
    '{decision: "block", reason: $reason, suppressOriginalPrompt: true}' \
    2>/dev/null || printf '{"decision":"block","reason":"(report unavailable)","suppressOriginalPrompt":true}'
  exit 0
}

case "$action" in
  report)
    mapfile -d '' -t rargs < <(tokenize "$rest")
    out="$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" --dir "$store_dir" "${rargs[@]}" 2>&1)"
    [ -z "$out" ] && out="(no output)"
    emit_block "$out"
    ;;
  add)
    # add <duration> [--to <project-or-customer>] [--on <YYYY-MM-DD>] [note...]
    #   duration : bare number = hours; suffix s/m/h; negative = correction.
    #   --to     : explicit target (project path or customer name). When
    #              omitted, the time is attributed to the CURRENT project (the
    #              cwd the command was invoked from), exactly like a session.
    #   --on     : the local date the work happened (backfill); default today.
    #   note     : everything else (quoting optional); joined with spaces.
    mapfile -d '' -t aargs < <(tokenize "$rest")
    dur="${aargs[0]:-}"
    if [ -z "$dur" ]; then
      emit_block "Usage: tt add <duration> [--to <project-or-customer>] [--on <YYYY-MM-DD>] [note]  (e.g. tt add 2h \"fixed login bug\"  |  tt add 30m --to \"Acme Corp\" --on 2026-07-03 kickoff call)"
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

    # Split the remaining args into optional --to/--on values and the note words.
    target=""
    target_explicit=0
    on_date=""
    on_explicit=0
    pos=()
    i=1
    n=${#aargs[@]}
    while [ "$i" -lt "$n" ]; do
      if [ "${aargs[$i]}" = "--to" ]; then
        target="${aargs[$((i+1))]:-}"
        target_explicit=1
        i=$((i + 2))
      elif [ "${aargs[$i]}" = "--on" ]; then
        on_date="${aargs[$((i+1))]:-}"
        on_explicit=1
        i=$((i + 2))
      else
        pos+=("${aargs[$i]}")
        i=$((i + 1))
      fi
    done
    note="${pos[*]}"

    if [ "$on_explicit" -eq 1 ]; then
      if [ -z "$on_date" ]; then
        emit_block "tt add: --on needs a date (YYYY-MM-DD)."
      fi
      if ! [[ "$on_date" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
         || [ "$(date -d "$on_date" +%F 2>/dev/null)" != "$on_date" ]; then
        emit_block "tt add: '--on $on_date' isn't a valid date. Use YYYY-MM-DD (e.g. --on 2026-07-03)."
      fi
    fi

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
      --arg date "${on_date:-$(date +%F)}" \
      --arg duration "$dur" \
      --arg note "$note" \
      '{ts: $ts, source: "manual", project: $project, date: $date, duration: $duration, note: $note}' \
      2>/dev/null)"
    [ -n "$record" ] && printf '%s\n' "$record" >> "$manual_file" 2>/dev/null || true
    where="$target"
    [ "$defaulted" -eq 1 ] && where="$target (current project)"
    msg="✎ Recorded ${dur} to '${where}'${on_date:+ on ${on_date}}${note:+ — ${note}} (manual, billable; excluded from active-engagement)."
    # Show the verbatim stored line so the user can confirm the entry is right.
    [ -n "$record" ] && msg="${msg}
  saved: ${record}"
    emit_block "$msg"
    ;;
  map)
    # map [<customer>] [--name <label>] [--list] — map the CURRENT project to
    # a customer in projects.toml (hand-edits are preserved); bare form lists.
    mapfile -d '' -t margs < <(tokenize "$rest")
    out="$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/tt-map.py" --dir "$store_dir" \
      --project "${TT_PROJECT:-}" "${margs[@]}" 2>&1)"
    [ -z "$out" ] && out="(map unavailable)"
    emit_block "$out"
    ;;
  status)
    out="$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" --dir "$store_dir" \
      --status --session "${TT_SESSION_ID:-}" --project "${TT_PROJECT:-}" 2>&1)"
    [ -z "$out" ] && out="(status unavailable)"
    emit_block "$out"
    ;;
  undo)
    # Strike the LAST surviving manual entry by appending a manual_undo
    # tombstone — the log itself is never rewritten. Repeat to strike earlier
    # entries. Only touches tt add entries, never observed session events.
    manual_file="${store_dir}/manual.jsonl"
    last="$(python3 -c '
import json, os, sys
sys.path.insert(0, os.path.join(os.environ.get("CLAUDE_PLUGIN_ROOT", ""), "scripts"))
import report
entries = report.load_manual(sys.argv[1])
print(json.dumps(entries[-1]) if entries else "")
' "$manual_file" 2>/dev/null || true)"
    if [ -z "$last" ]; then
      emit_block "tt undo: no manual entries left to undo (only 'tt add' entries can be undone)."
    fi
    tts="$(printf '%s' "$last" | jq -r '.ts // empty' 2>/dev/null || true)"
    if [ -z "$tts" ]; then
      emit_block "tt undo: couldn't identify the last manual entry — check ${manual_file} by hand."
    fi
    jq -n -c --argjson ts "$now_ts" --argjson target "$tts" \
      '{ts: $ts, source: "manual_undo", target_ts: $target}' \
      >> "$manual_file" 2>/dev/null || true
    emit_block "↩ Undid the last manual entry:
  ${last}
(Append-only: a strike marker was recorded, nothing was deleted. Repeat 'tt undo' to strike earlier entries.)"
    ;;
  pause)
    # pause [<duration>] [reason...] — record a pause MARKER (not a heartbeat).
    # The engine suppresses the span until resume / next prompt / session end;
    # a duration (bare number = minutes) additionally caps it at now+duration,
    # so a forgotten 'tt resume' can't eat the afternoon.
    cur="$(pause_state)"
    if [ -n "$cur" ]; then
      set -- $cur
      tail_msg=""
      [ "$2" != "-" ] && tail_msg=", auto-resumes $(hhmm "$2")"
      emit_block "⏸ Already paused (since $(hhmm "$1")${tail_msg}). Resume with 'tt resume' or just send a prompt."
    fi
    mapfile -d '' -t pargs < <(tokenize "$rest")
    until_ts=""
    reason_txt=""
    pause_dur_re='^([0-9]+\.?[0-9]*|\.[0-9]+)[smh]?$'
    if [ "${#pargs[@]}" -gt 0 ] && [[ "${pargs[0]}" =~ $pause_dur_re ]]; then
      secs="$(python3 -c '
import sys
s = sys.argv[1].lower()
mult = {"s": 1, "m": 60, "h": 3600}.get(s[-1], 60)
num = s[:-1] if s[-1] in "smh" else s
print(int(float(num) * mult))
' "${pargs[0]}" 2>/dev/null || true)"
      [ -n "$secs" ] && until_ts=$((now_ts + secs))
      reason_txt="${pargs[*]:1}"
    else
      reason_txt="${pargs[*]}"
    fi
    extra="$(jq -n -c \
      --argjson until "${until_ts:-null}" \
      --arg reason "$reason_txt" \
      '(if $until != null then {until: $until} else {} end)
       + (if $reason != "" then {reason: $reason} else {} end)' 2>/dev/null || printf 'null')"
    append_event "pause" "$extra"
    if [ -n "$until_ts" ]; then
      emit_block "⏸ Paused until $(hhmm "$until_ts")${reason_txt:+ — ${reason_txt}}. Auto-resumes then, on 'tt resume', or on your next prompt."
    fi
    emit_block "⏸ Tracking paused for this session${reason_txt:+ — ${reason_txt}}. Resume with 'tt resume' (or '/time-tracker:tt resume'), or just send your next prompt."
    ;;
  resume)
    cur="$(pause_state)"
    # The marker is recorded either way: if the pause predates this month's
    # log tail the state check can miss it, and a stray resume is harmless.
    append_event "resume"
    if [ -z "$cur" ]; then
      emit_block "▶ Wasn't paused — tracking was already running."
    fi
    emit_block "▶ Tracking resumed."
    ;;
  help|"")
    # Static help. Model-free and not recorded as activity. A bare verb
    # (typed `tt`, or `/time-tracker:tt` with no args) lands here.
    emit_block "$(printf '%s\n' \
      "time-tracker — session time tracking" \
      "Use either form:   tt <cmd>      or      /time-tracker:tt <cmd>" \
      "" \
      "  report [period] [filters]     Wall-clock + active-engagement per project/customer" \
      "                                (period: today, yesterday, week, last-week, month, last-month)" \
      "  status                        Tracking state, paused?, time today (this project + all)" \
      "  map [<customer>] [--name <n>] Map the current project to a customer (bare form lists)" \
      "  add <dur> [--to <tgt>] [--on <date>] [note]" \
      "                                Log off-session time (default: current project, today)" \
      "  undo                          Strike the last 'tt add' entry (repeatable)" \
      "  pause [<dur>] [reason]        Pause tracking (dur caps it, bare number = minutes; auto-resumes on next prompt)" \
      "  resume                        Resume tracking now" \
      "  help                          Show this help" \
      "" \
      "Also: /time-tracker:timesheet   Model-formatted timesheet over the tracked data" \
      "" \
      "Tip: prefix the typed form with a backslash (\\tt ...) to send a literal 'tt' line to the model.")"
    ;;
  *)
    sug="$(python3 -c '
import difflib, sys
verbs = ["report", "status", "map", "add", "undo", "pause", "resume", "help"]
m = difflib.get_close_matches(sys.argv[1], verbs, 1, 0.6)
print(m[0] if m else "")
' "$action" 2>/dev/null || true)"
    hint=""
    [ -n "$sug" ] && hint=" Did you mean 'tt ${sug}'?"
    emit_block "Unknown tt command: '${action}'.${hint} Run 'tt help' (or '/time-tracker:tt') for available commands."
    ;;
esac
