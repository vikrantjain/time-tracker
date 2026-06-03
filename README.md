# Time Tracker

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
/plugin   # enable time-tracker for this project (writes .claude/settings.local.json, which is gitignored)
```

Project/local-scoped plugins load hooks **only** for that project — there is no cross-project coupling.

## Where data lives

A single **visible** central directory, so billing data outlives plugin updates/uninstalls (the plugin dir is wiped on both):

- Default: `~/time-tracker/`
- Override: set `TIME_TRACKER_DIR` to an absolute path.

Files:

- `events.jsonl` — observed session events (auto-created on first event).
- `manual.jsonl` — user-asserted time written by `tt add` (separate from observed events).
- `projects.toml` — hand-edited absolute-path → customer mapping (see below).

Each `events.jsonl` line is metadata only: `ts` (epoch), `iso`, `event`, `session_id`, `project` (absolute cwd), plus `source` (on session start) or `reason` (on session end).

### Mapping projects to customers

Create `projects.toml` in the store directory. Each table key is the **absolute project path** (the `cwd` recorded at session start); `customer` is required, `name` is an optional display label:

```toml
["/home/vikrant/work/acme-website"]
customer = "Acme Corp"
name = "Acme Website"          # optional; defaults to the path

["/home/vikrant/work/acme-api"]
customer = "Acme Corp"         # multiple projects roll up under one customer

["/home/vikrant/work/beta-app"]
customer = "Beta LLC"
```

The file is read-only to the tool (you hand-edit it). A project that appears in the log but is **not** in `projects.toml` is shown flagged as `⚠ unmapped` — never silently dropped — so you always notice unbilled work. A missing `projects.toml` simply means every project is unmapped.

## Reporting

`scripts/report.py` (also reachable via the `tt report` sentinel) accepts:

| Flag | Effect |
| --- | --- |
| `--month YYYY-MM` | Restrict to a whole calendar month (local days). |
| `--from YYYY-MM-DD` / `--to YYYY-MM-DD` | Restrict to an explicit inclusive date range. |
| `--customer "<name>"` | Restrict to one customer's projects. |
| `--idle-threshold <dur>` | Idle gap above which time is dropped from active-engagement (e.g. `30m`, `900s`, `1h`; bare number = minutes). |
| `--csv` | Emit CSV (one row per customer/project) instead of the Markdown table. |
| `--dir <path>` | Override the store directory (defaults to `$TIME_TRACKER_DIR` or `~/time-tracker`). |

Filters compose, e.g. a customer's invoice for one month as CSV:

```
python3 scripts/report.py --month 2026-05 --customer "Acme Corp" --csv
```

(`--month` cannot be combined with `--from`/`--to`.)

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
- `tt pause` / `tt resume` — exclude a deliberate idle span (e.g. lunch) from a session you leave open. `tt pause` drops the clock; it auto-resumes on your next normal prompt, or sooner if you type `tt resume` (useful when you're back and reading before typing). The paused span is removed from **both** wall-clock and active-engagement. Markers are appended to the log (the log is never mutated) — they are not counted as activity.
- `tt add <duration> [--to <project-or-customer>] [note]` — record billable time the hooks can't see (work outside Claude Code, or before the plugin was enabled). `<duration>` accepts `2h`, `90m`, or a bare number (= hours); a **negative** duration (`-30m`) records a correction. By default the time is attributed to the **current project** (the `cwd` you run it from), mapped to a customer at report time just like a session — so `tt add 2h fixed the deploy script` logs to wherever you are. Use **`--to <project-or-customer>`** to attribute it elsewhere — a project path, or a customer name directly (e.g. a meeting not tied to one repo): `tt add 30m --to "Acme Corp" kickoff call`. The note is everything else (quoting optional). Manual time is written to a separate `manual.jsonl` and appears in the report as a distinct `✎ manual` line under its customer — added to **wall-clock** but **excluded from active-engagement** (which is observed-only). A negative entry shows as its own adjustment, never netted into observed hours.
- `tt help` (or a bare `tt`, or `/time-tracker:tt` with no argument) — print this list of commands. Static text only; nothing is logged.

> **Escape:** to send a real prompt that legitimately begins with `tt `, prefix it with a backslash — e.g. `\tt is the abbreviation I mean`. The plugin will not intercept it and it reaches the model normally.

## Status

Under construction — see `docs/IMPLEMENTATION_PLAN.md`. This README is filled in story by story.
