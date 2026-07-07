#!/bin/bash
# Time Tracker — optional Claude Code statusline segment.
#
# Prints one short line — "⏱ 3h 12m today", or "⏸ paused until 13:05 · 2h 41m
# today" while paused — from the same engine `tt status` uses
# (report.py --status --brief). Claude Code runs the statusline command after
# each update and pipes a JSON payload (session_id, workspace.current_dir, …)
# on stdin.
#
# Wire it up in ~/.claude/settings.json (statusLine cannot use
# ${CLAUDE_PLUGIN_ROOT}, so point it at the installed plugin path):
#
#   "statusLine": {
#     "type": "command",
#     "command": "bash /path/to/plugins/time-tracker/scripts/statusline.sh"
#   }
#
# Honors TIME_TRACKER_DIR like the rest of the plugin. Always exits 0 and
# always prints something, so a broken store can never blank the statusline.

payload="$(cat 2>/dev/null || true)"
here="$(cd "$(dirname "$0")" && pwd)"

sid="$(printf '%s' "$payload" | jq -r '.session_id // ""' 2>/dev/null || true)"
cwd="$(printf '%s' "$payload" | jq -r '.workspace.current_dir // .cwd // ""' 2>/dev/null || true)"

out="$(python3 "$here/report.py" --status --brief --session "$sid" --project "$cwd" 2>/dev/null || true)"
[ -n "$out" ] || out="⏱ time-tracker"
printf '%s\n' "$out"
exit 0
