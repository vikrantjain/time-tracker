# time-tracker

A Claude Code plugin. Hooks capture session metadata to a JSONL store, and `scripts/report.py`
derives billable time from it. README.md specifies the user-facing behavior and the store format.

## Tests

Run all four suites. Any one alone gives a false green for the code the others cover.

```
python3 -m unittest discover -s tests -p 'test_*.py'
bash tests/test_tt_dispatch.sh
bash tests/test_track_event.sh
claude plugin test .        # hooks/register.tsx
```

When you drive a hook or `tt-dispatch.sh` by hand, export `TIME_TRACKER_DIR` to a temp dir first.
Without it, your test events land in the real billing store at `~/.time-tracker`.

## Invariants that span files

Each rule below is implemented in more than one place. If one copy changes and the others do not,
the billing numbers come out wrong and nothing errors.

- **Store path.** `${TIME_TRACKER_DIR:-$HOME/.time-tracker}` is hardcoded in
  `hooks/scripts/track-event.sh`, `scripts/tt-dispatch.sh` and `store_dir()` in `report.py`. If
  they diverge, the hook writes to one directory and reports read from another. Reports then come
  back empty.
- **What closes a pause.** `resume`, `prompt` and `session_end` close a pause. `tool` does not. The
  set appears in `should_throttle` (track-event.sh), `pause_state` (tt-dispatch.sh), and
  `compute_suppressed` and `session_pause_state` (report.py). If throttling drops the prompt that
  closes a pause, the report suppresses billable time until the session ends. `hooks/register.tsx`
  has no copy: it reads pause state from `report.py --status --json`. Keep it that way.
- **Bare-number durations.** A `tt add` duration is stored as the user typed it, so `2` has no
  unit. `resolve_manual_rows` in report.py reads a bare number as hours. Changing that unit
  re-prices every existing `manual.jsonl` entry.
- **Duration grammar.** `dur_re` in tt-dispatch.sh must accept exactly what `parse_duration` in
  report.py parses. Input that passes the regex but fails the parser is confirmed as recorded and
  then dropped from every report.
- **Append-only store.** Never rewrite or delete lines in `events-*.jsonl` or `manual.jsonl`.
  Concurrent sessions append to the same file, so a rewrite loses their lines. Corrections are new
  lines: a negative `tt add`, a `manual_undo` tombstone, a pause marker. In `hooks/register.tsx`,
  `$.fs.write` replaces a whole file, so the module must write through `tt-dispatch.sh` instead.
- **Metadata only.** No stored line may contain prompt or response text. The README promises this
  to users, and a field that breaks the promise produces no visible symptom.

## Shipping

Bump `version` in `.claude-plugin/plugin.json` with every user-visible change. Installed plugins
are cached per version. An unbumped change does not reach users who update.
