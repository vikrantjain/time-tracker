# Approach: Activity Tracker — Claude Code Time-Tracking Plugin

**Status:** Complete
**Last updated:** 2026-06-03

## Problem Statement

A Claude Code plugin that, when enabled in a project, records how much time is spent
working **with Claude Code** in that project, so the data can be used to:

1. **Bill customers** — wall-clock time is the billable number, reported as a per-project
   breakdown grouped by customer.
2. **Analyze productivity** — understand where time goes.

**Confirmed framing (from the user):**

- **Scope of truth:** "Time inside Claude Code sessions" is an acceptable definition of
  billable/productive time. The plugin is *not* expected to capture work done outside
  Claude Code (editor, terminal, meetings, etc.). This is a conscious, accepted limitation.
- **Project / customer model:** One project = one directory. A customer can have multiple
  projects. Reporting must produce a per-project time breakdown that rolls up to a customer.
- **Time definition:** **Wall-clock time is the required/primary metric** (session open
  duration). **Active-engagement time** is a secondary signal used to *manually correct*
  wall-clock figures before sharing with the customer (e.g. subtract a long idle gap).

**Success looks like:** At the end of a billing period, the user can produce a trustworthy,
per-customer / per-project report of wall-clock hours (with active-engagement figures
alongside for sanity-checking) drawn from data the plugin captured automatically during
Claude Code sessions.

## Feasibility Assessment

**Verdict:** **Feasible with caveats.**

Verified against the official Claude Code hooks reference
(<https://code.claude.com/docs/en/hooks>, fetched 2026-06-03):

- `SessionStart` and `SessionEnd` both fire with `session_id` and `cwd` — enough to bound a
  session and identify the project directory.
- `SessionStart` carries a `source` matcher (`startup` / `resume` / `clear` / `compact`);
  `SessionEnd` carries a `reason` (`clear` / `resume` / `logout` / `prompt_input_exit` /
  `bypass_permissions_disabled` / `other`).
- `UserPromptSubmit` (every prompt) and `Stop` (every time Claude finishes a response) fire
  per turn — the raw material for active-engagement time.
- **Hook payloads contain no timestamp field** — the hook *script* must capture its own
  wall-clock (e.g. `date +%s`). Verified: no documented payload includes a time field.

**Caveats (must be engineered around):**

1. **`SessionEnd` is not guaranteed to fire on abrupt termination** (killed terminal, crash,
   machine sleep/shutdown). The docs list only normal-exit reasons. Relying on `SessionEnd`
   alone for the end timestamp risks a lost or dangling session — i.e. a corrupted billable
   number. → Mitigation chosen in Architecture below (heartbeat fallback).
2. **`cwd` is captured at session start**, but Claude can `cd` mid-session (`CwdChanged`
   exists). Time is anchored to the project root where the session started; mid-session `cd`
   is ignored. Minor.

## Decisions

### Architecture: session-time capture model
**Question:** How does the plugin capture session time, given `SessionEnd` is not guaranteed
to fire on crash/kill/sleep?
**Options considered:**
1. Event log + heartbeat — each hook appends one timestamped line; metrics derived at report time.
2. Session record + heartbeat — one in-place-updated row per session.
3. SessionStart + SessionEnd only — minimal, lossy.

**Decision:** **Append-only event log + heartbeat.** Hooks register on `SessionStart`,
`UserPromptSubmit`, `Stop`, and `SessionEnd`. Each fires a small script that appends one
timestamped JSON line to the store. Wall-clock and active-engagement are both computed at
**report time** from the event stream.
**Rationale:** Crash-robust and auditable — the two properties billing needs. If `SessionEnd`
is lost, the session's last recorded event bounds it (a conservative under-count, the safe
direction for billing). In-place updates (option 2) are fragile under concurrent sessions and
partial writes; option 3 loses sessions outright and yields no engagement signal.

### Architecture: deriving wall-clock vs. active-engagement
**Question:** How are the two metrics computed from the event stream?
**Decision:**
- **Sub-interval segmentation (the core rule):** a `session_id` may have several `SessionStart`
  events (initial `startup`, then `resume`/`clear`/`compact`). Each `SessionStart` *opens* a
  sub-interval; it *closes* at the next `SessionEnd` for that session, or — if none appears
  before the following `SessionStart` or the end of the log — at the **last heartbeat** before
  that point. This one rule covers both the crash/missing-`SessionEnd` fallback *and* the
  "closed overnight then resumed" gap: time while Claude was closed is never counted.
- **Wall-clock** = sum of sub-interval durations (then unioned across overlapping sessions — see
  overlapping-session decision).
- **Active-engagement** = wall-clock minus idle gaps that exceed the idle threshold, where a gap
  is the interval between consecutive events within a sub-interval (notably `Stop` → next
  `UserPromptSubmit` = user reading/away).
- **Day bucketing & filters** use the machine's **local timezone** (billing periods are local
  days); durations are computed from epoch timestamps so they're DST-safe.
**Rationale:** Matches the user's stated use: wall-clock is the billable number;
active-engagement is the overlay used to manually subtract long idle gaps before invoicing. The
segmentation rule is what keeps wall-clock honest across resumes and crashes without ever
inflating it.

### Architecture: overlapping-session handling
**Question:** How is time counted when multiple Claude Code sessions run in the same project
concurrently?
**Decision:** Report-time wall-clock is the **union of session intervals** per project (and per
day), never the arithmetic sum. Overlapping sessions collapse into one interval.
**Rationale:** You cannot bill two hours for one wall-clock hour. Summing would over-bill
whenever two windows are open at once. This is a correctness rule, not a preference.

### Data model: store format
**Question:** What on-disk format does the event store use?
**Options considered:**
1. JSONL append log — `echo >>` from the hook, atomic small-line appends, no hot-path dependency.
2. SQLite — queryable but whole-DB write locks + a sqlite3 dependency in every hook.
3. CSV — simple but fragile quoting/escaping, less self-describing.

**Decision:** **JSONL append log.** One JSON object per line.
**Rationale:** Append-only fits the capture model exactly, stays dependency-free in the hook
hot path, is human-readable/auditable for billing, and small single-line appends are atomic on
Linux so concurrent sessions don't corrupt the file.

### Data model: store location
**Question:** Where does the event store live?
**Decision:** A **single visible central directory**, default `~/activity-tracker/`, overridable
via the `ACTIVITY_TRACKER_DIR` env var. Records are keyed by **absolute project path** (the
`cwd` at `SessionStart`). The tracker does **not** manage git — backup/sync is a separate,
out-of-scope concern.
**Rationale:** A single central store lets reporting aggregate across all of a customer's
projects and hold one customer-mapping config; keying by absolute path survives a project being
moved. Visible (not under `.claude`, not hidden) per the user's preference. Decoupling from git
keeps the hook hot path fast and avoids commit spam / auth prompts mid-session.

### Data model: what is recorded per event (content)
**Question:** What fields go in each event line?
**Decision:** **Metadata only**, no prompt/response text. Each line carries: epoch timestamp
`ts` and ISO `iso`; `event` (`session_start` | `prompt` | `stop` | `session_end` | `pause` |
`resume`); `session_id`; `project` (absolute `cwd`); and the relevant matcher (`source` for
session_start, `reason` for session_end). A session is anchored to its `session_start` project;
mid-session `cd` is ignored. User-asserted manual time blocks live in a **separate** `manual.jsonl`
(see the manual time-entry feature), never interleaved with observed events.
**Rationale:** Pure time metadata is safe to relocate/back up anywhere with no secret-leak risk,
and is all the math needs. Per-session billing descriptions, if ever wanted, are added by hand at
correction time rather than harvested from prompts.

### Architecture: which hooks fire (heartbeat granularity)
**Question:** Which hook events does the plugin register?
**Options considered:**
1. SessionStart + SessionEnd + UserPromptSubmit + Stop — one line per turn boundary.
2. Add PostToolUse — finer heartbeat resolution during long turns.

**Decision:** `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `Stop` only. `PostToolUse` is
**not** registered.
**Rationale:** A long single turn already spans `UserPromptSubmit` (start) → `Stop` (end), so
that whole working interval counts as engaged without per-tool events. Adding `PostToolUse`
multiplies event volume for no change to either metric. Keep the event stream minimal.
**Note:** the `UserPromptSubmit` hook does **double duty** — it records the heartbeat for normal
prompts *and* intercepts typed sentinel commands (see command-delivery decision). Sentinel prompts
are acted on and **not** recorded as activity.

### Technology: implementation languages
**Question:** What languages implement the hook scripts vs. the reporting engine?
**Decision:** **Hooks in bash** (a one-line `echo` appending a JSON object built from the hook's
stdin via `jq` + `date`); **reporting engine in Python 3, stdlib only**.
**Rationale:** Bash matches every other plugin here and adds no startup cost to the hot path.
The reporting math (interval unions, idle-gap subtraction, grouping) is painful in bash/jq but
natural in Python. Verified on the target machine: `python3` 3.12.3, `jq` 1.7, `bash` 5.2 — and
`tomllib` is in the stdlib (Python ≥3.11), so the engine needs **no third-party dependencies**.

### Configuration: project→customer mapping file
**Question:** What format and shape is the central mapping config?
**Decision:** A hand-edited **TOML** file in the store dir (e.g. `projects.toml`), read-only to
the tool via Python's stdlib `tomllib`. Each entry maps an absolute project path → `customer`
(+ optional display `name`). Projects present in the event log but absent from the config are
**flagged as "unmapped"** in the report rather than silently dropped.
**Rationale:** TOML hand-edits cleanly (comments, no trailing-comma traps) and parses with zero
deps on the confirmed Python 3.12. `tomllib` is read-only, which is fine — the tool only reads
the map; the user (or `/timesheet`, via the Edit tool) maintains it. Flagging unmapped projects
prevents silently un-billed time. ⚠️ Requires Python ≥3.11 for `tomllib` (satisfied here).

### Architecture: command delivery (no install, no model)
**Question:** How does the user invoke pause/resume/add/report with **nothing installed on the
machine** and **without a model turn**?
**Context (verified):** Plugin **slash commands are prompt-based** — they expand into a prompt sent
to the model, so each costs a model turn (tokens + latency) and logs activity; a plugin cannot ship
a no-model slash command. But a **`UserPromptSubmit` hook can intercept a typed prompt and block it
before the model**: returning `{"decision":"block","reason":"…"}` (exit 0) *"prevents the prompt
from being processed and erases it from context"*, shows `reason` to the **user** (not to Claude),
and **no model turn occurs** (`suppressOriginalPrompt: true` hides the typed text).
(<https://code.claude.com/docs/en/hooks>, UserPromptSubmit decision control, fetched 2026-06-03.)
**Decision:** User actions are **typed sentinel commands** caught by the plugin's own
`UserPromptSubmit` hook — no install, no launcher, no model turn, entirely in-plugin:
- The user types a sentinel, e.g. `tt pause`, `tt resume`, `tt add 2h Acme "meeting"`,
  `tt report [filters]`.
- The hook recognises it, performs the action (write to the store / run `report.py`), returns the
  confirmation or report in `reason`, and **blocks** the prompt (model never invoked) with
  `suppressOriginalPrompt: true`.
- Sentinel prompts are **not** recorded as activity — the same hook skips its heartbeat for them;
  non-sentinel prompts proceed normally and *are* recorded.
- An **optional** `/activity-tracker:timesheet` slash command remains for when the user *wants* the
  model to format the report or walk through corrections (that path does invoke the model).
**Sentinel token:** a distinctive prefix (working default `tt `) chosen to be unlikely to begin a
real prompt; documented in the README, with an escape for the rare real prompt that needs it.
**Rationale:** The hook-block mechanism is the *only* way to get a user-triggered action that is
no-model, no-activity, **and** entirely inside the plugin with nothing installed — satisfying all
three constraints at once. Slash commands stay available only where a model turn is actually wanted.
**⚠️ Build-time checks:** (1) confirm a blocked prompt isn't itself surfaced to our heartbeat logic
(it shouldn't be, since the hook decides); (2) confirm the `reason` field renders a multi-line
report acceptably (else write the report to a file and show its path).

### Reporting: interface and output
**Question:** How is the report invoked and what does it emit?
**Decision:** Two no-install paths, both calling the same `scripts/report.py`: (1) the
**`tt report [filters]` sentinel** — the hook runs the engine and returns the output in the block
`reason`, shown to the user with **no model turn**; (2) the **optional `/activity-tracker:timesheet`
slash command** for when you want Claude to format the table or walk through corrections (model
turn). The engine emits a **Markdown table by default** (customer → project → wall-clock +
active-engagement), with `--csv` export and filters for date range (`--from`/`--to` or `--month`)
and `--customer`.
**Rationale:** The sentinel gives a pure zero-token, zero-activity report on demand; the slash
command adds the conversational layer when useful. One engine, one set of billable numbers. CSV
covers invoice/spreadsheet import; date and customer filters match a billing-period workflow.

### Reporting: log immutability and manual correction
**Question:** How are manual corrections handled without compromising the audit trail?
**Decision:** The event log is **append-only and never mutated**. The report **surfaces idle
gaps and the active-engagement figure** alongside wall-clock so the user can decide adjustments;
corrections are applied by the user at report time (the report — script or optional command —
surfaces this). Durable
adjustments/additions are handled by the manual time-entry ledger and pause/resume features below
(both in v1) rather than by mutating the observed log.
**Rationale:** An immutable raw log is the trustworthy billing audit trail. Corrections are human
judgment (the user's stated workflow), so v1 only needs to *expose* idle time clearly; durable
adjustment records can come later if the manual step proves repetitive.

### Architecture: idle threshold for active-engagement
**Question:** What gap length counts as "idle"?
**Decision:** Default **15 minutes**, configurable via a CLI flag (e.g. `--idle-threshold`). A
gap between consecutive events longer than the threshold is subtracted from **active-engagement
only** — never from wall-clock.
**Rationale:** 15 min is a reasonable "stepped away" cutoff and easily overridden per the user's
billing judgment. Keeping the subtraction off wall-clock preserves wall-clock as the raw billable
number while active-engagement stays the correction overlay.

### Architecture: plugin layout
**Question:** What is the on-disk structure of the plugin?
**Decision:**
```
activity-tracker/
├── .claude-plugin/plugin.json     # manifest (name, version, description, author)
├── hooks/
│   ├── hooks.json                 # registers SessionStart/UserPromptSubmit/Stop/SessionEnd
│   └── scripts/track-event.sh     # heartbeat recorder + sentinel interpreter (pause/resume/add/report)
├── scripts/report.py              # report engine (called by the hook and the optional command)
├── commands/
│   └── timesheet.md               # OPTIONAL /activity-tracker:timesheet (model) — formatted report
└── README.md                      # setup: enable at --scope local; sentinel token; projects.toml
```
**No `bin/`, no launcher, no `pause.md`/`resume.md`/`add-time.md`** — pause/resume/add/report are
**typed sentinels** handled by the `UserPromptSubmit` hook (no install, no model). The **store**
(`$ACTIVITY_TRACKER_DIR`, default visible `~/activity-tracker/`) holds `events.jsonl`,
`manual.jsonl`, and the hand-edited `projects.toml` — kept **out of** the plugin dir. Plus
registration in the marketplace `.claude-plugin/marketplace.json` (root of `my-claude-plugins`).
**Rationale:** Matches marketplace convention (`hooks/hooks.json`, `hooks/scripts/*.sh` as in
`claude-session-profiler`; `commands/*.md` as in `profile-builder`). Every user action runs through
the already-present `UserPromptSubmit` hook, so **nothing is installed outside the plugin**.
Component dirs sit at the plugin root, not inside `.claude-plugin/` (per the docs' "common mistake").

### Operational: storage-location constraints (why data lives outside the plugin)
**Finding (verified — machine + docs):** an installed plugin's *code* lives at a **versioned,
ephemeral** cache path, `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` (e.g.
`…/approach-refiner/0.1.0/`); the docs say `${CLAUDE_PLUGIN_ROOT}` *"changes when the plugin
updates… do not write state here."* `${CLAUDE_PLUGIN_DATA}` (`~/.claude/plugins/data/<id>/`) survives
updates but is **deleted on uninstall**. (<https://code.claude.com/docs/en/plugins-reference>,
environment variables / persistent data dir, fetched 2026-06-03.)
**Decision:** Billing data lives in the visible `~/activity-tracker/` (per the store-location
decision) — **not** in the plugin's code dir (wiped on update) and **not** in `${CLAUDE_PLUGIN_DATA}`
(wiped on uninstall). This is a deliberate split: *code* stays inside the plugin; *billing records*
must outlive any plugin version or uninstall, so they sit in a stable user dir. Writing data files
there is **not an "install"** (no executable, alias, or PATH change).
**Rationale:** Losing billing records to a plugin update or uninstall is unacceptable — durability
beats co-location for this data. The "nothing installed" constraint is fully met by the
sentinel-hook design (no launcher, no PATH/alias changes); only data sits outside, as plain files.

### Architecture: enablement scope
**Question:** Is tracking opt-in per project, or user-wide?
**Options considered:**
1. Per-project opt-in — enable the plugin in each project's `.claude/settings.json`.
2. User-wide — enable once; the customer map decides what's billable.

**Decision:** **Per-project opt-in.** This is not a constraint the plugin imposes — it uses
Claude Code's standard enablement, which works at `user`, `project`, or `local` scope. The user's
**preference is `local` scope**: enable `activity-tracker` in each customer project's
`.claude/settings.local.json` (and any personal project they want productivity data on). Hooks
then fire only for sessions in those projects.
**Scope behavior (verified):** the enablement scope decides which settings file holds the
`enabledPlugins` entry — `local` → `.claude/settings.local.json`, `project` →
`.claude/settings.json`, `user` → `~/.claude/settings.json` — and project/local settings apply
only to sessions in that project. So a project/local-scoped plugin is active (hooks load) **only**
for that project; no cross-project coupling.
(<https://code.claude.com/docs/en/plugins-reference>, "Plugin installation scopes", fetched
2026-06-03.)
**Why `local` specifically:** `.claude/settings.local.json` is gitignored by convention, so the
decision to track a customer's project stays on the user's machine and is never committed into
the customer's repository. (`project` scope would commit the opt-in; `user` scope would track all
sessions everywhere, including personal/non-work dirs — which the user does not want.)
**Accepted limitation:** forgetting to enable it in a new project means that project's time is
untracked until noticed (a not-enabled plugin cannot remind you). **Recovery:** backfill with the
`tt add` sentinel once enabled.
**Rationale:** Matches the "import in a project" framing and gives the user selective,
machine-private control over exactly which projects are tracked.

### Operational: store bootstrapping
**Question:** What if the store directory or files don't exist yet?
**Decision:** The `track-event.sh` hook creates the store dir (`mkdir -p`) and appends to the
log on first use; the reporting engine treats a missing/empty log as "no activity" and a missing
`projects.toml` as "all projects unmapped" rather than erroring.
**Rationale:** Zero-setup first run; the plugin works the moment it's enabled, and reporting
degrades gracefully before any mapping is configured.

### Feature: manual time entry (out-of-session / pre-enablement work)
**Question:** How is time added for work the hooks can't see — work outside Claude Code, or in a
project before the plugin was enabled?
**Decision:** Adding time is a **`tt add …` sentinel** (no model; caught by the `UserPromptSubmit`
hook — see command delivery) that writes to a **separate `manual.jsonl` ledger**, kept distinct
from the observed `events.jsonl`; an optional `/activity-tracker:add-time` slash command remains for
natural-language entry (Claude resolving the project/date for you, model turn). Each entry records
`project` (an absolute path, or a customer/project label from `projects.toml`), `date` (local),
`duration`, a free-text `note`, and `source: manual`. The report **merges** manual entries into the per-project
/ per-customer totals but **labels manual hours distinctly from observed hours**. Entries may be
negative to record a deduction/correction.
**Rationale:** Keeps the observed log pure and auditable while letting the user assert real
billable time the hooks structurally cannot capture (gaps #2/#3). A separate file + explicit
provenance label means a manually-asserted hour is never silently mistaken for an observed one —
the property billing trust depends on. Duration+date+note beats start/end times: faster, and
exact clock times are rarely recalled after the fact. This **supersedes** the previously-deferred
"manual adjustments overlay" Open Question — it is now in v1.

### Feature: pause / resume (exclude a known idle span like lunch)
**Question:** How does the user exclude a deliberate idle span from a session they leave open?
**Decision:** Pause/resume are **`tt pause` / `tt resume` sentinels** (no model; caught by the
`UserPromptSubmit` hook, which blocks the prompt before the model — see command delivery). The hook
appends a `pause` / `resume` marker; because it runs *as the hook*, each marker carries the full
payload — `session_id`, `project` (`cwd`), and `ts`. The suppressed span runs from the `pause`
marker to the **earliest of**: an explicit `resume` marker, the next real (non-sentinel)
`UserPromptSubmit`, or session end. It is removed from **both** wall-clock and active-engagement.
Auto-resume (next real prompt) and explicit `tt resume` coexist — explicit resume restarts the clock
*before* the next prompt (e.g. resuming while you read), which auto-resume alone cannot.
**Rationale:** Marking the gap in the moment beats reconstructing a weeks-old lunch, and the
hook-block path means pausing costs no model turn and isn't itself logged (the hook skips recording
sentinel prompts). Keying auto-resume off the next *real, non-sentinel* `UserPromptSubmit` is what
distinguishes "back to work" from the pause action itself. Reuses the immutable event log: no new
store, no mutation.

## Open Questions

_None — all resolved._ (The per-project hook-scoping question was verified against the plugins
reference; see the enablement-scope decision. A quick two-project smoke test at build time is
still wise, but the design is no longer blocked on it.)

## Out of Scope

- Tracking work done outside Claude Code sessions (accepted limitation).
- Git backup / cross-machine sync of the store (handled separately if needed).
- Storing prompt/response content or any per-session text (metadata only).
