---
name: timesheet
description: "Produce a formatted timesheet from tracked Claude Code activity and optionally walk through billing corrections"
argument-hint: "[optional filters, e.g. --month 2026-05 --customer \"Acme Corp\"]"
---

# Time Tracker — Timesheet

Produce a per-customer / per-project timesheet from the Time Tracker store, then offer to help with corrections.

This is the **optional, model-driven** path. For a fast, zero-cost report that never invokes the model, the user can instead type the sentinel `tt report [filters]` directly. Use this command when the user wants you to *format*, *explain*, or *help correct* the timesheet conversationally.

## Input
Any arguments are passed straight through as report filters: $ARGUMENTS
(Supported flags: `--month YYYY-MM`, `--from`/`--to YYYY-MM-DD`, `--customer "<name>"`, `--idle-threshold <dur>`, `--csv`.)

## Instructions

1. Run the report engine, forwarding the user's arguments verbatim:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/report.py" $ARGUMENTS
   ```

   The engine reads the store at `$TIME_TRACKER_DIR` (default `~/time-tracker/`); do not pass `--dir` unless the user specifies a different location.

2. If the output is `No activity recorded.`, tell the user there's nothing tracked for that filter and suggest checking the date range / customer name, or that the plugin may not have been enabled in the relevant project. Then stop.

3. Otherwise present the table to the user. The wall-clock column is the **billable** number; active-engagement is a productivity sanity-check (wall-clock minus idle gaps). Manual `✎ manual` rows are user-asserted time included in wall-clock but excluded from engagement.

4. Flag anything that needs attention:
   - Projects shown as `⚠ unmapped` — these have no customer in `projects.toml`. Offer to help the user add the mapping (point them at the `projects.toml` format in the README; you may edit it if they ask).
   - Large wall-clock vs. small active-engagement gaps — may indicate sessions left open; the user can trim these with `tt pause`/`tt resume` next time, or correct historically with `tt add -<duration> --to <project-or-customer> ...`.

5. Offer correction guidance, but do **not** silently change data. Manual adjustments are the user's call:
   - To add untracked time to the current project: `tt add <duration> [note]`; to another target: `tt add <duration> --to <project-or-customer> [note]`.
   - To deduct over-counted time: `tt add -<duration> --to <project-or-customer> [note]`.
   Explain that these are typed commands the user enters themselves (they cost no model turn) — you can suggest the exact command, but the user runs it.

Keep the response concise: the table first, then a short bullet list of anything flagged, then the suggested corrections (if any).
