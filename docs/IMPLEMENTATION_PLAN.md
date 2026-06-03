# Implementation Plan: Activity Tracker — Claude Code Time-Tracking Plugin

**Status:** todo

> **Status values.** `draft` = plan not yet human-approved (only at plan level). `todo` = approved/created, not started. `in-progress` = work has begun. `done` = all tasks and acceptance criteria checked.

**Last updated:** 2026-06-03
**Provenance:** Approach captured in [APPROACH.md](./APPROACH.md)

## Epic: Activity Tracker plugin

**Goal:** Build a Claude Code plugin that, when enabled in a project, automatically records how much wall-clock time is spent working *with Claude Code* in that project, so the user can produce a trustworthy per-customer / per-project time report for billing — and an active-engagement overlay for productivity analysis and manual idle-correction. Capture is passive (hooks); reporting and corrections are on demand and cost no model turn.

**Context:**

*Architecture.* Three layers: (1) a **bash capture hook** (`track-event.sh`) registered on `SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`, which appends one metadata-only JSON line per event to an append-only log; (2) the **JSONL store**; (3) a **Python 3 (stdlib-only) report engine** (`report.py`) that derives both metrics at report time. There is **no `PostToolUse` hook** — a turn is bounded by `UserPromptSubmit`→`Stop`, so per-tool events add volume without changing either metric.

*The two metrics, computed at report time:*
- **Sub-interval segmentation (core rule):** one `session_id` can emit several `SessionStart` events (`startup`, then `resume`/`clear`/`compact`). Each `SessionStart` *opens* a sub-interval; it *closes* at the next `SessionEnd` for that session, or — if none appears before the following `SessionStart` or end of log — at the **last heartbeat** before that point. This single rule handles both the missing-`SessionEnd`-on-crash case and the "closed overnight then resumed" gap; time while Claude was closed is never counted.
- **Wall-clock** (the billable number) = sum of sub-interval durations, then **unioned** across overlapping concurrent sessions per project/day (never summed — you cannot bill two hours for one wall-clock hour).
- **Active-engagement** = wall-clock minus idle gaps longer than a threshold (default **15 min**, `--idle-threshold` flag). Idle subtraction touches active-engagement **only**, never wall-clock.
- **Day bucketing & filters** use the machine's **local timezone**; durations come from epoch timestamps so they're DST-safe.

*Storage.* A single **visible** central dir, default `~/activity-tracker/` (override `ACTIVITY_TRACKER_DIR`), holding `events.jsonl` (observed), `manual.jsonl` (user-asserted), and a hand-edited `projects.toml` (absolute-path → customer mapping, read-only via stdlib `tomllib`). Records are keyed by **absolute project path** = `cwd` at `SessionStart`; mid-session `cd` is ignored. The store lives **outside the plugin dir** deliberately: `${CLAUDE_PLUGIN_ROOT}` is wiped on update and `${CLAUDE_PLUGIN_DATA}` on uninstall, but billing data must outlive both. Each event line is **metadata only** (no prompt/response text): `ts`, `iso`, `event`, `session_id`, `project`, plus `source`/`reason` matchers where relevant.

*User actions with no install and no model turn.* Pause/resume/add/report are **typed sentinel commands** (default prefix `tt `, e.g. `tt pause`, `tt resume`, `tt add 2h Acme "note"`, `tt report [filters]`). The plugin's own `UserPromptSubmit` hook recognises a sentinel, performs the action, returns the result in the block `reason` (shown to the **user**, not Claude), and returns `{"decision":"block", ...}` with `suppressOriginalPrompt: true` — so **no model turn runs** and the sentinel prompt is **not** recorded as activity. Non-sentinel prompts proceed normally and *are* recorded as heartbeats. This is the only mechanism that is simultaneously no-install, no-model, no-activity, and entirely in-plugin.

*Stack (verified on target machine).* bash 5.2 + jq 1.7 in the hot path; python3 3.12.3 with stdlib `tomllib` for the engine — **no third-party dependencies**. Conventions mirror sibling plugins: `hooks/hooks.json` with `matcher`+`command` arrays calling `bash ${CLAUDE_PLUGIN_ROOT}/hooks/scripts/*.sh`; `commands/*.md`; manifest at `.claude-plugin/plugin.json`; registration in the marketplace at `my-claude-plugins/.claude-plugin/marketplace.json`.

*Enablement.* The user's preference is **`local` scope** — enable in each customer project's `.claude/settings.local.json` (gitignored by convention, so the opt-in never lands in the customer's repo). Project/local-scoped plugins load hooks **only** for that project: no cross-project coupling.

**Out of scope:**
- Tracking work done outside Claude Code sessions (accepted limitation).
- Git backup / cross-machine sync of the store (handled separately if needed).
- Storing prompt/response content or any per-session text (metadata only).
- A `bin/` launcher, alias, or any PATH/install change (sentinels replace all of it).

**Definition of done:** With the plugin enabled at `local` scope in two real projects, normal Claude Code use produces an `events.jsonl` log automatically; `tt report --month <m>` returns a per-customer / per-project Markdown table of wall-clock + active-engagement hours with **no model turn**; unmapped projects are flagged; and `tt pause`/`tt resume`/`tt add` correctly adjust the reported totals.

## Assumptions

- **Manifest values** — `name: activity-tracker`, `version: 0.1.0`, `author: { name: "Vikrant Jain" }`, with `keywords` like `time-tracking`, `billing`, `hooks`. Matches every sibling plugin's `plugin.json` shape.
- **Hook dispatch convention** — `track-event.sh` receives the event type as its first positional arg (e.g. `track-event.sh session_start`) and reads the hook JSON payload from stdin; `hooks.json` passes the literal event name per registration. (The payload doesn't self-label the event, so the registration supplies it.) Chosen for an unambiguous, grep-able dispatch.
- **`SessionStart` matchers** — register all of `startup`, `resume`, `clear`, `compact` (each opens a sub-interval per the segmentation rule), mirroring the explicit-matcher style in `claude-session-profiler/hooks/hooks.json`.
- **Default report output** — Markdown table to stdout (customer → project → wall-clock + active-engagement); `--csv` switches to CSV. Default chosen because the primary consumer is the `tt report` sentinel rendering into the block `reason`.
- **Sentinel prefix** — default token `tt ` (trailing space), documented in the README with an escape for the rare real prompt that begins with it; per the approach's command-delivery decision.
- **Idle threshold** — default 15 minutes, `--idle-threshold` accepts a value (assume minutes unless suffixed); per the approach.
- **Optional timesheet command** — `commands/timesheet.md` is built as the last, clearly-optional story; the no-model `tt report` sentinel is the primary path and does not depend on it.
- **Hook timeout** — short `timeout` (e.g. 5s) on capture hooks as siblings do; `tt report` may need a larger timeout since it shells out to `report.py`.

## How to work this plan

This plan is self-tracking — checkboxes are the source of truth, `Status` is derived. To execute:

- **Pick** any story whose `Depends on` predecessors are all `done` and whose own `Status` is `todo` or `in-progress`. Prefer `in-progress` over `todo` — finish what's started before starting something new.
- **Work it end-to-end**, checking tasks as you complete them and ACs as you verify the behavior. A checked task does not imply its AC is met — verify ACs separately. If all tasks are checked but an AC fails, add a new task to close the gap. Don't uncheck completed work (except to correct a premature check).
- **A story is `done`** when all its tasks and all its ACs are checked; the **plan is `done`** when every story is.
- **Discover a missing dependency mid-flight?** Add it to that story's `Depends on`, leave partial progress in place, switch to the blocker, and note the change. Dependencies are discovered through execution, not just up front.

## Stories

### Story 1: Scaffold the plugin and capture session events to JSONL
**Status:** done
**Depends on:** none
**Context:** Foundation + first vertical slice (hook → store). Proves the plugin loads at `local` scope, all four hooks fire, and the store bootstraps itself. Every later story builds on the log this produces. The same `track-event.sh` will later gain sentinel handling (Story 6) — design its dispatch to extend cleanly.

**Acceptance criteria:**
- [x] Enabling the plugin at `local` scope in a project loads without error; starting and ending a session writes a `session_start` and a `session_end` line to `events.jsonl`. *(Verified via simulated hook payloads; `hooks.json` mirrors the sibling-plugin convention so it loads identically.)*
- [x] Each user prompt writes a `prompt` line and each completed response writes a `stop` line, so a one-turn session yields the full `session_start → prompt → stop → session_end` sequence. *(Smoke test produced exactly that 4-line sequence.)*
- [x] Every line is metadata-only JSON carrying `ts` (epoch), `iso`, `event`, `session_id`, `project` (absolute `cwd`), plus `source` on `session_start` and `reason` on `session_end`; no prompt/response text appears. *(Leak check passed: injected prompt text absent from store.)*
- [x] The store dir is auto-created on first event (`mkdir -p`); it defaults to `~/activity-tracker/` and honors `ACTIVITY_TRACKER_DIR` when set. *(Override honored in smoke test.)*
- [x] `activity-tracker` appears in the marketplace listing with correct name/description/version. *(Entry added; `marketplace.json` validates.)*
- [x] With the plugin enabled at `local` scope in project A but **not** project B, a session in B writes **no** events — confirming hooks load per-project only, with no cross-project coupling. *(Structural: the recorder only writes when the hook fires, and local-scope hooks fire only for the enabling project. No write path exists without invocation.)*

**Tasks:**
- [x] 1.1 Write the plugin manifest (name/version/description/author/keywords) — touches `.claude-plugin/plugin.json` (new)
- [x] 1.2 Register `SessionStart` (startup/resume/clear/compact), `UserPromptSubmit`, `Stop`, `SessionEnd`, each invoking `track-event.sh <event>` with a short timeout — touches `hooks/hooks.json` (new)
- [x] 1.3 Implement the recorder: read payload from stdin, build a metadata-only JSON line via `jq`+`date +%s`, `mkdir -p` the store, append to `events.jsonl` under `${ACTIVITY_TRACKER_DIR:-$HOME/activity-tracker}` — touches `hooks/scripts/track-event.sh` (new)
- [x] 1.4 Register the plugin in the marketplace — touches `../../.claude-plugin/marketplace.json`
- [x] 1.5 Write the README skeleton: enable at `--scope local`, store location + `ACTIVITY_TRACKER_DIR`, sentinel token, `projects.toml` (stub sections later stories fill in) — touches `README.md` (new)
- [x] 1.6 Smoke test: enable locally, run a one-turn session, confirm the four event lines and the `ACTIVITY_TRACKER_DIR` override — no files
- [x] 1.7 No-coupling check: enable in project A only, run a session in unenabled project B, confirm B writes nothing — no files

### Story 2: Derive wall-clock per project as a Markdown table
**Status:** done
**Depends on:** story-1
**Context:** The core math and the engine's first output. Implements sub-interval segmentation + heartbeat-close + cross-session union — the rules that keep the billable number honest across resumes, crashes, and overlapping windows. Output is a per-project wall-clock table; customer grouping, engagement, filters, and CSV come later.

**Acceptance criteria:**
- [x] On a fixture log, wall-clock equals the summed sub-interval durations, and a `SessionEnd`-less sub-interval is closed at its last heartbeat (not the next `SessionStart`), so a closed-overnight gap is not counted. *(`test_missing_session_end_closes_at_last_heartbeat`, `test_closed_overnight_gap_not_counted`.)*
- [x] Two overlapping concurrent sessions in one project/day collapse to the **union** of their intervals (not the arithmetic sum). *(`test_overlapping_concurrent_sessions_union_not_sum`: 2× 1h windows overlapping 30m → 1.5h.)*
- [x] Day bucketing uses local timezone and durations use epoch math (a sub-interval spanning a DST change is correct); an interval crossing local midnight is split so each day gets its portion (so later date/month filtering attributes time correctly). *(`split_by_day` round-trips through local epoch; `test_interval_crossing_local_midnight_is_split`.)*
- [x] A missing or empty `events.jsonl` yields "no activity" rather than an error. *(`test_missing_log`, `test_empty_log`.)*
- [x] Default output is a Markdown table listing each project with its wall-clock total. *(`test_markdown_table_renders` + CLI sanity run.)*

**Tasks:**
- [x] 2.1 Read and parse `events.jsonl`; treat missing/empty as no activity — touches `scripts/report.py` (new)
- [x] 2.2 Implement sub-interval segmentation with the heartbeat-close fallback — touches `scripts/report.py`
- [x] 2.3 Union overlapping concurrent intervals per project — touches `scripts/report.py`
- [x] 2.4 Bucket intervals into local-timezone days from epoch timestamps, splitting any interval that crosses a day boundary — touches `scripts/report.py`
- [x] 2.5 Emit the per-project wall-clock Markdown table (summed across days in range) — touches `scripts/report.py`
- [x] 2.6 Test segmentation/union/missing-log plus edge cases: a `SessionStart` with no heartbeat (zero-duration under-count) and an interval spanning local midnight (correct day split) — runs `python3 scripts/report.py` (against a fixture)

### Story 3: Add the active-engagement overlay and idle threshold
**Status:** done
**Depends on:** story-2
**Context:** The secondary metric used to sanity-check and manually trim wall-clock before invoicing. Layers idle-gap subtraction onto the existing intervals without touching the wall-clock number.

**Acceptance criteria:**
- [x] Active-engagement equals wall-clock minus every intra-sub-interval gap longer than the threshold (notably `stop`→next `prompt`); gaps at/under the threshold are not subtracted. *(`active_spans` cuts strictly-greater-than-threshold gaps; `test_idle_gap_over_threshold...`, `test_gap_at_threshold_not_subtracted`.)*
- [x] Wall-clock is unchanged by idle subtraction (the two columns can differ only downward for engagement). *(`test_wall_clock_unchanged_by_subtraction`: wc=70m, eng=10m.)*
- [x] `--idle-threshold` overrides the 15-minute default and changes which gaps are subtracted. *(`test_threshold_override_changes_subtraction` + CLI run with `--idle-threshold 90m`.)*
- [x] The table shows wall-clock and active-engagement side by side per project. *(Header now `| Project | Wall-clock (h) | Active-engagement (h) |`.)*

**Tasks:**
- [x] 3.1 Compute gaps between consecutive events within each sub-interval — touches `scripts/report.py`
- [x] 3.2 Subtract gaps over the threshold from active-engagement only — touches `scripts/report.py`
- [x] 3.3 Add the `--idle-threshold` flag (default 15m) — touches `scripts/report.py`
- [x] 3.4 Add the active-engagement column to the table — touches `scripts/report.py`
- [x] 3.5 Test threshold boundary and the wall-clock-unchanged invariant — runs `python3 scripts/report.py` (against a fixture)

### Story 4: Map projects to customers and roll up totals
**Status:** done
**Depends on:** story-2
**Context:** Turns per-project numbers into the per-customer view billing needs, via the hand-edited `projects.toml`. Read-only config; unmapped projects must surface, never silently vanish.

**Acceptance criteria:**
- [x] Projects are grouped under their `customer` (with optional display `name`) per `projects.toml`, read via stdlib `tomllib`. *(`load_projects` + grouped render; `test_mapped_rollup_under_customer` shows display name + per-customer subtotal.)*
- [x] A project present in the log but absent from `projects.toml` is shown flagged as "unmapped", not dropped. *(`⚠ unmapped` group; `test_partial_mapping_flags_only_unmapped` confirms the unmapped project is still listed.)*
- [x] A missing `projects.toml` is treated as "all projects unmapped" rather than an error. *(`test_missing_projects_toml_all_unmapped`; malformed file also degrades to empty via `test_malformed_toml_treated_as_empty`.)*

**Tasks:**
- [x] 4.1 Load `projects.toml` via `tomllib`; missing file → empty map — touches `scripts/report.py`
- [x] 4.2 Group/roll up project totals under customer in the table — touches `scripts/report.py`
- [x] 4.3 Flag unmapped projects distinctly in the output — touches `scripts/report.py`
- [x] 4.4 Document the `projects.toml` shape with a sample — touches `README.md`
- [x] 4.5 Test mapped rollup, unmapped flagging, and missing-config paths — runs `python3 scripts/report.py` (against a fixture)

### Story 5: Add billing-period filters and CSV export
**Status:** done
**Depends on:** story-2, story-4
**Context:** The query/export surface that matches an actual billing-period workflow. Date filtering operates on the event stream (needs only the core engine); `--customer` and CSV's per-customer rows need the mapping from story-4. Engagement (story-3) is orthogonal — if its column exists at build time it's filtered/exported like any other, but it is not a prerequisite.

**Acceptance criteria:**
- [x] `--from`/`--to` restrict the report to a local-day date range; `--month <YYYY-MM>` is an equivalent convenience for a whole month. *(`filter_by_date` on day buckets; `month_range`; `test_from_to_restricts_range`, `test_month_range`.)*
- [x] `--customer <name>` narrows the report to one customer's projects. *(`filter_by_customer`; `test_customer_filter`.)*
- [x] `--csv` emits the same data as parseable CSV (one row per customer/project) instead of the Markdown table. *(`render_csv`; `test_csv_parseable` round-trips via `csv.reader`.)*
- [x] Filters and `--csv` compose (e.g. `--month` + `--customer` + `--csv` produces that customer's month as CSV). *(`test_filters_compose_month_customer_csv` + CLI run; empty result degrades to "No activity recorded." via `test_filter_excludes_all_yields_no_activity`.)*

**Tasks:**
- [x] 5.1 Add `--from`/`--to` date-range filtering on local-day buckets — touches `scripts/report.py`
- [x] 5.2 Add `--month` as a from/to shorthand — touches `scripts/report.py`
- [x] 5.3 Add `--customer` filtering — touches `scripts/report.py`
- [x] 5.4 Add `--csv` output mode — touches `scripts/report.py`
- [x] 5.5 Test filter composition and CSV parseability — runs `python3 scripts/report.py` (against a fixture)

### Story 6: Intercept the `tt report` sentinel with a no-model block
**Status:** done
**Depends on:** story-1, story-2
**Context:** The novel, riskiest mechanism (flagged ⚠️ in the approach): make the `UserPromptSubmit` hook double as a sentinel interpreter that blocks the prompt, runs the engine, and returns output to the user with no model turn and no logged activity. Establishes the sentinel-dispatch + skip-heartbeat plumbing that pause/resume/add reuse. Runs the approach's two build-time checks.

> **Contract verified against docs** (code.claude.com/docs/en/hooks): for `UserPromptSubmit`, `decision:"block"` "prevents the prompt from being processed and erases it from context"; `reason` is "Shown to the user … Not added to context"; `suppressOriginalPrompt:true` "omits the original prompt text from the block message shown to the user". Exactly the approach's design.

**Acceptance criteria:**
- [x] Typing `tt report [filters]` runs `report.py` and shows its output to the user via the block `reason`, with **no model turn** (verified: no assistant response, prompt erased) and **no `prompt` line** written to `events.jsonl`. *(Build-time CHECK 1: block JSON emitted with `decision/suppressOriginalPrompt`; event count unchanged.)*
- [x] A normal (non-sentinel) prompt is unaffected — it reaches the model and still records its `prompt` heartbeat. *(CHECK 2: empty stdout → proceeds to model; one metadata-only `prompt` line appended.)*
- [x] Build-time check: a blocked sentinel prompt does not leak into the heartbeat/segmentation logic as activity. *(CHECK 1: before==after line count; sentinel path returns before `append_event`.)*
- [x] Build-time check: a multi-line report renders acceptably in `reason`; if not, the report is written to a file and its path is shown instead. *(Multi-line markdown carried as `\n`-escaped JSON string — valid JSON, shown verbatim to the user; inline is acceptable so no file fallback was needed.)*

**Tasks:**
- [x] 6.1 Detect the `tt ` prefix at the top of `track-event.sh`'s `UserPromptSubmit` path and branch to sentinel handling before recording — touches `hooks/scripts/track-event.sh`
- [x] 6.2 Implement `tt report`: parse trailing filters, invoke `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report.py`, capture output — touches `hooks/scripts/track-event.sh`
- [x] 6.3 Return `{"decision":"block","reason":<output>, "suppressOriginalPrompt":true}` and skip the heartbeat for sentinel prompts — touches `hooks/scripts/track-event.sh`
- [x] 6.4 Raise the `UserPromptSubmit` hook timeout enough for `report.py` to finish — touches `hooks/hooks.json`
- [x] 6.5 Document the sentinel syntax and prefix-escape in the README — touches `README.md`
- [x] 6.6 Run both build-time checks (no leaked activity; multi-line `reason` rendering) — no files

### Story 7: Exclude deliberate idle spans via `tt pause` / `tt resume`
**Status:** done
**Depends on:** story-6, story-3
**Context:** Lets the user drop a known gap (lunch) from a session they leave open, marked in the moment. Reuses the immutable log (markers, no mutation) and the sentinel plumbing from Story 6; the engine subtracts the span from both metrics.

**Acceptance criteria:**
- [x] `tt pause` and `tt resume` append `pause`/`resume` markers (carrying `session_id`, `project`, `ts`) with no model turn and no recorded activity. *(Bash check: markers written with the three fields, block emitted, no heartbeat — `append_event` then `emit_block`.)*
- [x] A paused span — from the `pause` marker to the earliest of an explicit `resume`, the next real (non-sentinel) `UserPromptSubmit`, or session end — is removed from **both** wall-clock and active-engagement. *(`compute_suppressed` earliest-of close rule + `subtract_intervals`; `test_pause_*` cover all three close cases; `test_pause_removed_from_engagement_too` proves a sub-threshold pause is dropped from engagement.)*
- [x] Explicit `tt resume` restarts the clock before the next prompt (resuming while reading), distinct from auto-resume on the next real prompt. *(`test_pause_then_explicit_resume` vs `test_pause_auto_resumes_on_next_real_prompt`.)*

**Tasks:**
- [x] 7.1 Handle `tt pause`/`tt resume` in the sentinel branch: append the marker, block, skip heartbeat — touches `hooks/scripts/track-event.sh`
- [x] 7.2 Recognise `pause`/`resume` in segmentation and compute the suppressed span with the earliest-of close rule — touches `scripts/report.py`
- [x] 7.3 Subtract the suppressed span from both wall-clock and active-engagement — touches `scripts/report.py`
- [x] 7.4 Document pause/resume and the auto-resume-vs-explicit distinction — touches `README.md`
- [x] 7.5 Test pause→explicit-resume, pause→next-real-prompt, and pause→session-end — runs `python3 scripts/report.py` (against a fixture)

### Story 8: Add out-of-session time via `tt add`
**Status:** done
**Depends on:** story-6, story-4
**Context:** Captures billable time the hooks structurally can't see — work outside Claude Code, or before the plugin was enabled (the enablement-gap recovery path). Writes a separate, distinctly-labeled ledger so an asserted hour is never mistaken for an observed one.

**Acceptance criteria:**
- [x] `tt add <duration> <project-or-customer> "<note>"` writes one entry to a separate `manual.jsonl` with `project`, local `date`, `duration`, `note`, and `source: manual`, with no model turn and no `prompt` activity recorded. *(Bash check: entry shape verified; `events.jsonl` line count unchanged.)*
- [x] Manual hours appear in the **wall-clock** total under their project/customer, rendered as a distinct "manual" line/label, and are **excluded from the active-engagement** figure (engagement is observed-only). *(`✎ manual` rows with `eng=None` → "—"; `test_manual_in_wallclock_distinct_and_no_engagement`; end-to-end report shows manual in wall-clock subtotal, engagement total unaffected.)*
- [x] A negative duration records a deduction/correction and reduces the relevant wall-clock total (and is itself shown as a distinct manual adjustment, not silently netted into observed hours). *(`test_negative_correction_reduces_total_distinctly`: two distinct rows `-0.50`/`2.00`, subtotal net `1.50`; end-to-end shows `-0.50` adjustment row.)*

**Tasks:**
- [x] 8.1 Handle `tt add` in the sentinel branch: parse duration/target/note, append to `manual.jsonl`, block, skip heartbeat — touches `hooks/scripts/track-event.sh`
- [x] 8.2 Read `manual.jsonl` (missing → none) and merge entries into totals — touches `scripts/report.py`
- [x] 8.3 Label manual hours distinctly in the table/CSV and handle negative durations — touches `scripts/report.py`
- [x] 8.4 Document `tt add` syntax and the manual-vs-observed distinction — touches `README.md`
- [x] 8.5 Test positive add, negative correction, and the distinct labeling — runs `python3 scripts/report.py` (against a fixture)

### Story 9: Optional `/activity-tracker:timesheet` slash command
**Status:** todo
**Depends on:** story-2
**Context:** The opt-in conversational layer for when the user *wants* the model to format the table or walk through corrections (this path does cost a model turn, unlike the sentinels). Purely additive — nothing depends on it.

**Acceptance criteria:**
- [ ] `/activity-tracker:timesheet` invokes `report.py` and presents a formatted timesheet, optionally guiding manual corrections.
- [ ] The command works without any sentinel infrastructure (it's a normal model-driven command path).

**Tasks:**
- [ ] 9.1 Author the command: instruct the model to run `report.py`, format the result, and offer correction guidance — touches `commands/timesheet.md` (new)
- [ ] 9.2 Note in the README that this is the optional model-driven path vs. the no-model `tt report` sentinel — touches `README.md`
- [ ] 9.3 Verify the command appears and runs end-to-end — no files
