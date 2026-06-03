# Activity Tracker

A Claude Code plugin that records how much **wall-clock time** you spend working *with Claude Code* in each project — so you can bill customers per project and analyse your own productivity.

Capture is passive (hooks). Reporting and corrections are on demand and cost **no model turn**.

## How it works

Three layers:

1. **Capture hook** (`hooks/scripts/track-event.sh`) — fires on `SessionStart`, `UserPromptSubmit`, `Stop`, and `SessionEnd`, appending one **metadata-only** JSON line per event. No prompt or response text is ever stored.
2. **JSONL store** — an append-only log keyed by absolute project path.
3. **Report engine** (`scripts/report.py`, stdlib-only Python 3) — derives wall-clock and active-engagement at report time.

### The two metrics

- **Wall-clock** (the billable number) — total session time, unioned across overlapping concurrent sessions (never summed).
- **Active-engagement** — wall-clock minus idle gaps longer than a threshold (default **15 min**, override with `--idle-threshold`, e.g. `--idle-threshold 30m`). Idle subtraction touches active-engagement only — wall-clock is never reduced by it.

## Enabling it

Enable per project at **local** scope so the opt-in never lands in the customer's repo:

```
/plugin   # enable activity-tracker for this project (writes .claude/settings.local.json, which is gitignored)
```

Project/local-scoped plugins load hooks **only** for that project — there is no cross-project coupling.

## Where data lives

A single **visible** central directory, so billing data outlives plugin updates/uninstalls (the plugin dir is wiped on both):

- Default: `~/activity-tracker/`
- Override: set `ACTIVITY_TRACKER_DIR` to an absolute path.

Files:

- `events.jsonl` — observed session events (auto-created on first event).
- `manual.jsonl` — user-asserted time (see `tt add`).            <!-- detailed in a later story -->
- `projects.toml` — hand-edited absolute-path → customer mapping. <!-- detailed in a later story -->

Each `events.jsonl` line is metadata only: `ts` (epoch), `iso`, `event`, `session_id`, `project` (absolute cwd), plus `source` (on session start) or `reason` (on session end).

## Sentinel commands (no model turn)

Typed prompts beginning with the sentinel token **`tt `** are intercepted by the plugin, executed locally, and never reach the model:

- `tt report [filters]` — print a timesheet.    <!-- Story 6 -->
- `tt pause` / `tt resume` — exclude a deliberate idle span.  <!-- Story 7 -->
- `tt add <duration> <project-or-customer> "<note>"` — record out-of-session time.  <!-- Story 8 -->

> If you ever need to send a real prompt that legitimately starts with `tt `, escape it (documented alongside the sentinel implementation).

## Status

Under construction — see `docs/IMPLEMENTATION_PLAN.md`. This README is filled in story by story.
