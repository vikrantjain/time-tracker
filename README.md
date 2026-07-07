# Time Tracker

A Claude Code plugin that records how much **wall-clock time** you spend working *with Claude Code* in each project — so you can bill customers per project and analyse your own productivity.

Capture is passive (hooks). Reporting and corrections are on demand and cost **no model turn**.

## Requirements

- **Claude Code** with plugin support (the plugin is loaded via `/plugin`).
- **`python3`** (3.11+, for `tomllib`) on `PATH` — the report engine and helpers are stdlib-only, no `pip install`.
- **`jq`** on `PATH` — the capture hook and dispatcher use it to read/write the JSONL store.

> Both `python3` and `jq` must be installed **before** you enable the plugin. The hooks **fail open**: if `jq` is missing they exit quietly rather than erroring, so tracking would silently record nothing. Verify with `python3 --version` and `jq --version`, then run `tt status` after enabling to confirm capture is live.

## Installation

The repo is its own plugin marketplace (it ships a `.claude-plugin/marketplace.json`), so add it and install in one flow:

```
/plugin marketplace add vikrantjain/time-tracker    # or the full URL: https://github.com/vikrantjain/time-tracker.git
/plugin install time-tracker
```

`/plugin marketplace add` registers the marketplace; `/plugin install` fetches the plugin. To update later, `/plugin marketplace update time-tracker` then reinstall. Installing makes the plugin *available*; you still opt in per project — see [Enabling it](#enabling-it).

## Quick start

1. **Enable** the plugin for the current project (local scope — see [Enabling it](#enabling-it)):
   ```
   /plugin
   ```
2. **Work normally.** Every session, prompt, and tool call is captured passively — nothing to run, no tokens spent.
3. **Check it's tracking** at any time:
   ```
   tt status
   ```
   → `today: 1h 30m this project · 1h 30m all projects (engaged 1h 05m)`
4. **Map the project to a customer** (once per project), so reports roll up for billing:
   ```
   tt map "Acme Corp"
   ```
5. **Pull a report** whenever you need one — instant, no model turn:
   ```
   tt report today
   tt report --month 2026-07 --customer "Acme Corp" --csv
   ```

That's the whole loop. Everything below is detail on how it works and the full command set.

## How it works

Three layers:

1. **Capture hook** (`hooks/scripts/track-event.sh`) — fires on `SessionStart`, `UserPromptSubmit`, `PostToolUse`, `Stop`, and `SessionEnd`, appending one **metadata-only** JSON line per event. No prompt or response text is ever stored. The `PostToolUse` (`tool`) heartbeat exists so a long autonomous turn — where minutes pass between your prompt and Claude's stop — still counts as active engagement.
2. **JSONL store** — an append-only log keyed by absolute project path, **rotated monthly** so no single file grows forever. Heartbeats are **throttled** (at most one per session per 60s for prompt/stop, per 300s for tool activity) — the idle threshold works at minutes scale, so sub-minute precision would be pure log bloat. Session boundaries and pause/resume markers are never throttled.
3. **Report engine** (`scripts/report.py`, stdlib-only Python 3) — derives wall-clock and active-engagement at report time, loading only the monthly files that overlap the requested date range.

### The two metrics

- **Wall-clock** (the billable number) — total session time, unioned across overlapping concurrent sessions (never summed).
- **Active-engagement** — wall-clock minus idle gaps longer than a threshold (default **15 min**, override with `--idle-threshold`, e.g. `--idle-threshold 30m`). Idle subtraction touches active-engagement only — wall-clock is never reduced by it.

## Enabling it

Enable per project at **local** scope so the opt-in never lands in the customer's repo:

```
/plugin   # enable time-tracker for this project (writes .claude/settings.local.json, which is gitignored)
```

Project/local-scoped plugins load hooks **only** for that project — there is no cross-project coupling.

## Where data lives

A single **visible** central directory, so billing data outlives plugin updates/uninstalls (the plugin dir is wiped on both):

- Default: `~/time-tracker/`
- Override: set `TIME_TRACKER_DIR` to an absolute path.

Files:

- `events-YYYY-MM.jsonl` — observed session events, one file per calendar month (auto-created on first event). Reports only load the months overlapping the requested date range (±1 month, so sessions spanning a month boundary are reassembled).
- `events.jsonl` — legacy pre-rotation log; if present it is still read by every report. No migration needed — new events simply go to the monthly files.
- `manual.jsonl` — user-asserted time written by `tt add` (separate from observed events).
- `projects.toml` — hand-edited absolute-path → customer mapping (see below).

Each event line is metadata only: `ts` (epoch seconds, UTC instant), `event`, `session_id`, `project` (absolute cwd), plus `source` (on session start) or `reason` (on session end). Local time and calendar day are derived from `ts` at report time using the machine's timezone — no human-readable timestamp is stored.

> **Note on timezone:** day bucketing uses the machine's timezone *at report time*, so historical day boundaries shift if you change timezones between working and reporting.

### Mapping projects to customers

The easy way: run **`tt map "Acme Corp"`** inside the project (optionally `--name "Acme Website"` for a display label). It writes the mapping for the current project into `projects.toml`; `tt map` with no arguments lists the existing mappings. New mappings are appended and remappings rewrite only that project's table, so any hand-written comments in the file survive.

The file itself stays hand-editable. Each table key is the **absolute project path** (the `cwd` recorded at session start); `customer` is required, `name` is an optional display label:

```toml
["/home/vikrant/work/acme-website"]
customer = "Acme Corp"
name = "Acme Website"          # optional; defaults to the path

["/home/vikrant/work/acme-api"]
customer = "Acme Corp"         # multiple projects roll up under one customer

["/home/vikrant/work/beta-app"]
customer = "Beta LLC"
```

A project that appears in the log but is **not** in `projects.toml` is shown flagged as `⚠ unmapped` — never silently dropped — so you always notice unbilled work, and the report ends with a hint telling you to `tt map` it. A missing `projects.toml` simply means every project is unmapped.

## Reporting

`scripts/report.py` (also reachable via the `tt report` sentinel) accepts:

| Flag | Effect |
| --- | --- |
| `today` / `yesterday` / `week` / `last-week` / `month` / `last-month` | Period shorthand (positional, e.g. `tt report today`). Weeks run Mon–Sun. |
| `--month YYYY-MM` | Restrict to a whole calendar month (local days). |
| `--from YYYY-MM-DD` / `--to YYYY-MM-DD` | Restrict to an explicit inclusive date range. |
| `--customer "<name>"` | Restrict to one customer's projects. |
| `--idle-threshold <dur>` | Idle gap above which time is dropped from active-engagement (e.g. `30m`, `900s`, `1h`; bare number = minutes). |
| `--csv` | Emit CSV (one row per customer/project) instead of the Markdown table. |
| `--out <path>` | Write the report to a file and confirm the path (handy with `--csv` for invoices — from the `tt` sentinel the CSV would otherwise arrive as a message to copy-paste). |
| `--dir <path>` | Override the store directory (defaults to `$TIME_TRACKER_DIR` or `~/time-tracker`). |

The Markdown table shows humanized durations (`2h 45m`); the total row also carries decimal hours (the number an invoice wants), and CSV output always uses decimal hours. Every table starts with a header line stating the period, customer filter, and idle threshold in effect. A filter that matches nothing tells you the date span the store actually covers, so a mistyped month is obvious.

Filters compose, e.g. a customer's invoice for one month as CSV:

```
python3 scripts/report.py --month 2026-05 --customer "Acme Corp" --csv
```

(`--month`, the period shorthands, and `--from`/`--to` are mutually exclusive ways to pick the period.)

### Two ways to get a report

- **`tt report [filters]`** (sentinel) — instant, no model turn, no token cost. The result is shown only to you. This is the everyday path.
- **`/time-tracker:timesheet [filters]`** (slash command) — the optional **model-driven** path. Use it when you want Claude to format the table, explain the numbers, flag unmapped projects, or walk you through corrections conversationally. This one *does* cost a model turn.

Both call the same `report.py` engine and accept the same filters.

## tt commands (no model turn)

These bookkeeping commands run entirely in-plugin and never reach the model — no model turn, no token cost, and they are **not** recorded as activity. There are two equivalent ways to invoke them:

- **Typed sentinel** — a prompt beginning with **`tt `** (e.g. `tt pause`), intercepted on `UserPromptSubmit`. Terse; nothing to discover in the `/` palette.
- **Slash command** — **`/time-tracker:tt <cmd>`** (e.g. `/time-tracker:tt pause`), intercepted on `UserPromptExpansion`. Discoverable via `/`; running `/time-tracker:tt` with no argument prints the help.

Both forms share one dispatcher (`scripts/tt-dispatch.sh`), so behavior is identical. The verbs:

- `tt report [filters]` — print a timesheet (accepts the reporting flags above, e.g. `tt report --month 2026-05 --customer "Acme Corp"`). The result is shown to you directly; Claude never sees it.
- `tt map [<customer>] [--name <label>]` — map the **current project** to a customer in `projects.toml` (bare `tt map` lists all mappings). Hand-edits and comments in the file are preserved.
- `tt status` — one glance at the tracker: the current project (with its customer mapping, or a hint to map it), whether this session is tracked or paused, and today's time — this project, all projects, and engaged. The everyday "am I being tracked right now?" answer.
- `tt pause [<duration>] [reason]` / `tt resume` — exclude a deliberate idle span (e.g. lunch) from a session you leave open. `tt pause` drops the clock; it auto-resumes on your next normal prompt, or sooner if you type `tt resume` (useful when you're back and reading before typing). A **timed pause** — `tt pause 45m lunch` (bare number = minutes) — additionally expires on its own after the duration, so a forgotten `tt resume` can't eat the afternoon; the reason is kept in the log for later context. Pausing while already paused, or resuming when nothing is paused, says so instead of silently stacking markers. The paused span is removed from **both** wall-clock and active-engagement. Markers are appended to the log (the log is never mutated) — they are not counted as activity.
- `tt add <duration> [--to <project-or-customer>] [--on <YYYY-MM-DD>] [note]` — record billable time the hooks can't see (work outside Claude Code, or before the plugin was enabled). `<duration>` accepts `2h`, `90m`, or a bare number (= hours); a **negative** duration (`-30m`) records a correction. By default the time is attributed to the **current project** (the `cwd` you run it from), mapped to a customer at report time just like a session — so `tt add 2h fixed the deploy script` logs to wherever you are. Use **`--to <project-or-customer>`** to attribute it elsewhere — a project path, or a customer name directly (e.g. a meeting not tied to one repo): `tt add 30m --to "Acme Corp" kickoff call`. Use **`--on 2026-07-03`** to backfill work done on a past day (entries default to today). The note is everything else (quoting optional; apostrophes are fine — `tt add 2h don't forget the fix` keeps the whole note). Manual time is written to a separate `manual.jsonl` and appears in the report as a distinct `✎ manual` line under its customer — added to **wall-clock** but **excluded from active-engagement** (which is observed-only). A negative entry shows as its own adjustment, never netted into observed hours.
- `tt undo` — strike the last `tt add` entry (mistyped duration, wrong project…). Repeat it to strike earlier entries. Append-only like everything else: it records a strike marker in `manual.jsonl` rather than deleting the line, and it never touches observed session events.
- `tt help` (or a bare `tt`, or `/time-tracker:tt` with no argument) — print this list of commands. Static text only; nothing is logged.

> **Escape:** to send a real prompt that legitimately begins with `tt `, prefix it with a backslash — e.g. `\tt is the abbreviation I mean`. The plugin will not intercept it and it reaches the model normally.

## Statusline (optional)

To see today's tracked time passively instead of asking, wire the bundled segment into Claude Code's statusline in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "bash /path/to/plugins/time-tracker/scripts/statusline.sh"
  }
}
```

It prints `⏱ 3h 12m today` (or `⏸ paused until 13:05 · 2h 41m today` while paused) using the same engine as `tt status --brief`. Point the command at the installed plugin path — `statusLine` settings can't use `${CLAUDE_PLUGIN_ROOT}`. It honors `TIME_TRACKER_DIR`, always exits 0, and always prints something, so it can't blank your statusline. To combine it with an existing statusline command, append it to your script and join the outputs however you like.

## Status

Feature-complete: passive capture (with heartbeat throttling and monthly log rotation), wall-clock + active-engagement reporting (period shorthands, humanized tables, CSV/file export), `tt status`, customer mapping via `tt map`, pause/resume with timed pauses, manual time entry with backfill and undo, an optional statusline segment, and the model-free `tt` command set with typo suggestions. Tests live in `tests/` (report engine, dispatcher, and capture hook).
