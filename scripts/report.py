#!/usr/bin/env python3
"""Time Tracker — report engine.

Reads the append-only event log produced by the capture hook and derives
wall-clock time per project. Stdlib only (no third-party dependencies).

Store location: ${TIME_TRACKER_DIR:-$HOME/time-tracker}
  events-YYYY-MM.jsonl   observed session events (rotated monthly at write time)
  events.jsonl           legacy pre-rotation log, still read when present

Core model
----------
Sub-interval segmentation: one session_id may emit several `session_start`
events (startup, then resume/clear/compact). Each `session_start` OPENS a
sub-interval; it CLOSES at the next `session_end` for that session, or — if
none appears before the following `session_start` or the end of the log — at
the LAST heartbeat (prompt/stop/start) seen before that point. This single
rule covers both the crash-without-session_end case and the closed-overnight
gap (time while Claude was closed is never counted).

Wall-clock (the billable number) = the UNION of sub-interval coverage per
project (overlapping concurrent sessions are never summed), bucketed into
local-timezone days (an interval crossing local midnight is split so each day
gets its portion). Per-project total = sum across the days in range.
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import tomllib
from datetime import date, datetime, timedelta, time

HEARTBEAT_EVENTS = {"session_start", "prompt", "stop", "tool"}
DEFAULT_IDLE_THRESHOLD_SECONDS = 15 * 60  # 15 minutes


def parse_duration(text, bare_unit_seconds=60):
    """Parse a duration like '15', '15m', '90s', '1.5h'.

    A bare number uses `bare_unit_seconds` (minutes for --idle-threshold).
    """
    s = str(text).strip().lower()
    if s.endswith("h"):
        return float(s[:-1]) * 3600
    if s.endswith("m"):
        return float(s[:-1]) * 60
    if s.endswith("s"):
        return float(s[:-1])
    return float(s) * bare_unit_seconds


# --------------------------------------------------------------------------- #
# Loading                                                                     #
# --------------------------------------------------------------------------- #
def store_dir(override=None):
    if override:
        return override
    return os.environ.get("TIME_TRACKER_DIR") or os.path.join(
        os.path.expanduser("~"), "time-tracker"
    )


LEGACY_EVENTS_FILE = "events.jsonl"
MONTHLY_EVENTS_RE = re.compile(r"^events-(\d{4})-(\d{2})\.jsonl$")


def discover_event_files(sdir, date_from=None, date_to=None):
    """Return the event-file paths to load from a store directory.

    Events are written to monthly files (events-YYYY-MM.jsonl); the legacy
    pre-rotation events.jsonl is always included when present (it may span any
    months). With a date filter, only monthly files within
    [date_from - 1 month, date_to + 1 month] are loaded: the lookback catches
    a session opened in the previous month, the lookahead catches the closing
    events of a session that crossed the range's final midnight. A session
    left open across two month boundaries without a resume is not
    reconstructed — an accepted loss for a rare case.
    """
    paths = []
    legacy = os.path.join(sdir, LEGACY_EVENTS_FILE)
    if os.path.exists(legacy):
        paths.append(legacy)
    lo = None if date_from is None else date_from.year * 12 + date_from.month - 2
    hi = None if date_to is None else date_to.year * 12 + date_to.month
    try:
        names = sorted(os.listdir(sdir))
    except OSError:
        names = []
    for name in names:
        m = MONTHLY_EVENTS_RE.match(name)
        if not m:
            continue
        month_index = int(m.group(1)) * 12 + int(m.group(2)) - 1
        if (lo is None or month_index >= lo) and (hi is None or month_index <= hi):
            paths.append(os.path.join(sdir, name))
    return paths


def load_events(paths):
    """Return events from one path or a list of paths (monthly rotation splits
    one log across files; order doesn't matter — consumers sort by ts).
    Missing/empty files -> []."""
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    events = []
    for path in paths:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue  # tolerate a partially-written trailing line
                if "ts" not in ev or "event" not in ev:
                    continue
                events.append(ev)
    return events


def load_projects(path):
    """Load the absolute-path -> {customer, name?} mapping from projects.toml.

    Missing file -> {} (every project then renders as unmapped). A malformed
    file is treated the same way rather than crashing the report.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except (tomllib.TOMLDecodeError, OSError):
        return {}
    # Only keep table entries shaped like a project mapping.
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def load_manual(path):
    """Load user-asserted time entries from manual.jsonl. Missing -> []."""
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("source") == "manual":
                entries.append(entry)
    return entries


# --------------------------------------------------------------------------- #
# Segmentation                                                                #
# --------------------------------------------------------------------------- #
def build_intervals(events):
    """Apply sub-interval segmentation per session.

    Returns a list of intervals, each a dict:
        {project, session_id, start, end, heartbeats: [ts, ...]}
    `heartbeats` are the event timestamps inside the sub-interval (used later
    for the active-engagement overlay); `start`/`end` are epoch seconds.
    """
    by_session = {}
    for ev in events:
        by_session.setdefault(ev.get("session_id", ""), []).append(ev)

    intervals = []
    for sid, evs in by_session.items():
        evs.sort(key=lambda e: e["ts"])
        cur_start = None
        cur_project = None
        last_hb = None
        hbs = []
        for ev in evs:
            etype = ev.get("event")
            ts = ev["ts"]
            if etype == "session_start":
                if cur_start is not None:
                    intervals.append(_mk(cur_project, sid, cur_start, last_hb, hbs))
                cur_start = ts
                cur_project = ev.get("project", "")
                last_hb = ts
                hbs = [ts]
            elif etype == "session_end":
                if cur_start is not None:
                    intervals.append(_mk(cur_project, sid, cur_start, ts, hbs))
                    cur_start = None
                    cur_project = None
                    last_hb = None
                    hbs = []
            elif etype in HEARTBEAT_EVENTS:
                if cur_start is not None:
                    last_hb = ts
                    hbs.append(ts)
        if cur_start is not None:
            intervals.append(_mk(cur_project, sid, cur_start, last_hb, hbs))
    return intervals


def _mk(project, sid, start, end, hbs):
    return {
        "project": project or "",
        "session_id": sid,
        "start": start,
        "end": end,
        "heartbeats": list(hbs),
    }


# --------------------------------------------------------------------------- #
# Union + day bucketing                                                       #
# --------------------------------------------------------------------------- #
def union_intervals(spans):
    """Merge a list of (start, end) tuples into non-overlapping union spans."""
    spans = sorted(spans)
    merged = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def split_by_day(start, end):
    """Yield (date, seconds) splitting [start, end] at LOCAL midnights.

    Zero-duration spans (start == end) yield their start day with 0 seconds so
    a session that produced no measurable time still surfaces as a row.
    Uses fromtimestamp/timestamp round-trips so the local timezone (and DST)
    is honored.
    """
    if end <= start:
        yield (datetime.fromtimestamp(start).date(), 0.0)
        return
    cur = start
    while cur < end:
        d = datetime.fromtimestamp(cur).date()
        next_midnight = datetime.combine(d + timedelta(days=1), time.min).timestamp()
        seg_end = min(end, next_midnight)
        yield (d, seg_end - cur)
        cur = seg_end


def subtract_intervals(spans, holes):
    """Union `spans`, then remove the coverage of `holes`. Returns unioned spans."""
    spans = union_intervals(spans)
    holes = union_intervals(holes)
    if not holes:
        return spans
    result = []
    for s, e in spans:
        cur = s
        for hs, he in holes:
            if he <= cur or hs >= e:
                continue
            if hs > cur:
                result.append((cur, hs))
            cur = max(cur, he)
            if cur >= e:
                break
        if cur < e:
            result.append((cur, e))
    return result


def _spans_to_project_days(spans_by_project, suppressed=None):
    suppressed = suppressed or {}
    result = {}
    for project, spans in spans_by_project.items():
        spans = subtract_intervals(spans, suppressed.get(project, []))
        day_secs = {}
        for s, e in spans:
            for d, secs in split_by_day(s, e):
                day_secs[d] = day_secs.get(d, 0.0) + secs
        result[project] = day_secs
    return result


def compute_suppressed(events):
    """Return {project: [(start, end)]} of deliberately-paused spans.

    A `pause` marker opens a suppressed span; it closes at the earliest of an
    explicit `resume`, the next real `prompt` (auto-resume), a `session_end`,
    or — if none appears — the session's last event. `tool` heartbeats never
    close a pause (Claude may still be finishing a turn the user paused
    through). Spans are attributed to the project of the nearest preceding
    session_start, i.e. the cwd active when the pause was typed.
    """
    by_session = {}
    for ev in events:
        by_session.setdefault(ev.get("session_id", ""), []).append(ev)

    result = {}
    for evs in by_session.values():
        evs = sorted(evs, key=lambda e: e["ts"])
        # Fallback for a pause seen before any session_start (truncated log).
        cur_proj = next(
            (e.get("project", "") for e in evs if e.get("event") == "session_start"),
            evs[0].get("project", "") if evs else "",
        )
        paused = False
        pstart = None
        pproj = None
        last_ts = None
        for ev in evs:
            t = ev["ts"]
            et = ev.get("event")
            last_ts = t
            if et == "session_start":
                cur_proj = ev.get("project", "") or cur_proj
            if et == "pause":
                if not paused:
                    paused = True
                    pstart = t
                    pproj = cur_proj
            elif et in ("resume", "prompt", "session_end"):
                if paused:
                    result.setdefault(pproj, []).append((pstart, t))
                    paused = False
                    pstart = None
        if paused:
            result.setdefault(pproj, []).append((pstart, last_ts))
    return result


def wall_clock_by_project_day(intervals, suppressed=None):
    """Return {project: {date: seconds}} of unioned wall-clock time.

    `suppressed` (from compute_suppressed) is removed from the coverage.
    """
    spans_by_project = {}
    for iv in intervals:
        spans_by_project.setdefault(iv["project"], []).append((iv["start"], iv["end"]))
    return _spans_to_project_days(spans_by_project, suppressed)


def active_spans(interval, threshold):
    """Split a sub-interval into active spans by cutting out idle gaps.

    An idle gap is a span between consecutive in-interval events (including the
    session_start and the closing boundary) that is STRICTLY longer than
    `threshold` seconds. Gaps at or under the threshold stay counted. A
    zero-duration interval contributes no active span.
    """
    pts = sorted(set([interval["start"]] + list(interval["heartbeats"]) + [interval["end"]]))
    spans = []
    seg_start = interval["start"]
    prev = interval["start"]
    for t in pts:
        if t <= prev:
            continue
        if t - prev > threshold:
            if prev > seg_start:
                spans.append((seg_start, prev))
            seg_start = t
        prev = t
    if prev > seg_start:
        spans.append((seg_start, prev))
    return spans


def engagement_by_project_day(intervals, threshold, suppressed=None):
    """Return {project: {date: seconds}} of unioned active-engagement time.

    Active spans are unioned across sessions, so time that is idle in one
    session but active in a concurrent one is still counted as engaged.
    Deliberately-paused (`suppressed`) spans are also removed.
    """
    spans_by_project = {}
    for iv in intervals:
        for s, e in active_spans(iv, threshold):
            spans_by_project.setdefault(iv["project"], []).append((s, e))
    return _spans_to_project_days(spans_by_project, suppressed)


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #
def fmt_hours(seconds):
    return f"{seconds / 3600:.2f}"


def fmt_hm(seconds):
    """Humanize seconds as '2h 45m' / '2h' / '45m', rounded to the minute.

    Negative values (manual corrections) keep their sign: '-30m'.
    """
    sign = "-" if seconds < 0 else ""
    mins = round(abs(seconds) / 60)
    h, m = divmod(mins, 60)
    if h and m:
        return f"{sign}{h}h {m}m"
    if h:
        return f"{sign}{h}h"
    return f"{sign}{m}m"


# --------------------------------------------------------------------------- #
# Filtering                                                                   #
# --------------------------------------------------------------------------- #
def parse_date(text):
    """Parse a YYYY-MM-DD string into a date."""
    return datetime.strptime(text, "%Y-%m-%d").date()


def month_range(text):
    """Return (first_day, last_day) for a YYYY-MM string."""
    year, month = (int(x) for x in text.split("-"))
    first = date(year, month, 1)
    if month == 12:
        last = date(year, 12, 31)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    return first, last


PERIOD_WORDS = ("today", "yesterday", "week", "last-week", "month", "last-month")


def period_range(word, today=None):
    """Expand a period shorthand into (date_from, date_to). Weeks run Mon-Sun."""
    t = today or date.today()
    if word == "today":
        return t, t
    if word == "yesterday":
        y = t - timedelta(days=1)
        return y, y
    if word == "week":
        mon = t - timedelta(days=t.weekday())
        return mon, mon + timedelta(days=6)
    if word == "last-week":
        mon = t - timedelta(days=t.weekday() + 7)
        return mon, mon + timedelta(days=6)
    if word == "month":
        return month_range(f"{t.year:04d}-{t.month:02d}")
    if word == "last-month":
        last = t.replace(day=1) - timedelta(days=1)
        return month_range(f"{last.year:04d}-{last.month:02d}")
    raise ValueError(f"unknown period shorthand: {word}")


def period_label(date_from, date_to):
    """Human label for a date filter; collapses an exact calendar month."""
    if date_from is None and date_to is None:
        return "all time"
    if date_from and date_to:
        if month_range(f"{date_from.year:04d}-{date_from.month:02d}") == (date_from, date_to):
            return f"{date_from.year:04d}-{date_from.month:02d}"
        return f"{date_from} to {date_to}"
    if date_from:
        return f"from {date_from}"
    return f"through {date_to}"


def data_span(sdir):
    """(first_day, last_day) covered by observed events in the whole store.

    Used to explain an empty filtered report. Returns None for an empty store.
    """
    events = load_events(discover_event_files(sdir))
    if not events:
        return None
    tss = [e["ts"] for e in events]
    return (
        datetime.fromtimestamp(min(tss)).date(),
        datetime.fromtimestamp(max(tss)).date(),
    )


def filter_by_date(project_day, dfrom, dto):
    """Keep only day buckets within [dfrom, dto] (inclusive); drop empty projects."""
    if dfrom is None and dto is None:
        return project_day
    out = {}
    for project, days in project_day.items():
        kept = {
            d: s
            for d, s in days.items()
            if (dfrom is None or d >= dfrom) and (dto is None or d <= dto)
        }
        if kept:
            out[project] = kept
    return out


def filter_by_customer(project_day, projects, customer):
    """Keep only projects mapped to the given customer."""
    return {
        project: days
        for project, days in project_day.items()
        if projects.get(project, {}).get("customer") == customer
    }


def resolve_manual_rows(entries, projects, dfrom=None, dto=None, customer=None):
    """Turn manual.jsonl entries into renderable rows.

    Each row is a dict {customer, display, wc} where `wc` is seconds (possibly
    negative for a correction). The `target` resolves to either a known project
    path, a known customer name, or an unmapped label. Manual time carries NO
    active-engagement (engagement is observed-only) and each entry stays a
    DISTINCT row (a negative entry shows as its own adjustment, never netted
    into observed hours).
    """
    known_customers = {m.get("customer") for m in projects.values() if m.get("customer")}
    rows = []
    for entry in entries:
        # Date filter (manual entries carry their own local date).
        d_raw = entry.get("date")
        if (dfrom or dto) and d_raw:
            try:
                d = parse_date(d_raw)
            except ValueError:
                d = None
            if d is not None:
                if dfrom and d < dfrom:
                    continue
                if dto and d > dto:
                    continue

        target = entry.get("project", "")
        if target in projects:
            cust = projects[target].get("customer") or UNMAPPED_LABEL
            base = projects[target].get("name") or target
        elif target in known_customers:
            cust = target
            base = ""
        else:
            cust = UNMAPPED_LABEL
            base = target

        if customer is not None and cust != customer:
            continue

        try:
            secs = parse_duration(entry.get("duration", "0"), bare_unit_seconds=3600)
        except (ValueError, AttributeError):
            continue
        note = entry.get("note") or ""
        label = "✎ manual"
        if base:
            label += f": {base}"
        if note:
            label += f" — {note}"
        rows.append({"customer": cust, "display": label, "wc": secs})
    return rows


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #
UNMAPPED_LABEL = "⚠ unmapped"


def _build_groups(wc_by_project_day, eng_by_project_day, projects, manual_rows):
    """customer_label -> list of {display, wc, eng} (eng is None for manual)."""
    groups = {}
    for project in wc_by_project_day:
        mapping = projects.get(project, {})
        customer = mapping.get("customer")
        label = customer if customer else UNMAPPED_LABEL
        display = mapping.get("name") or project or "(unknown)"
        wc = sum(wc_by_project_day[project].values())
        eng = sum(eng_by_project_day.get(project, {}).values())
        groups.setdefault(label, []).append({"display": display, "wc": wc, "eng": eng})
    for row in manual_rows or []:
        groups.setdefault(row["customer"], []).append(
            {"display": row["display"], "wc": row["wc"], "eng": None}
        )
    return groups


def _ordered_labels(groups):
    ordered = sorted(k for k in groups if k != UNMAPPED_LABEL)
    if UNMAPPED_LABEL in groups:
        ordered.append(UNMAPPED_LABEL)
    return ordered


def render_csv(wc_by_project_day, eng_by_project_day, projects=None, manual_rows=None):
    projects = projects or {}
    groups = _build_groups(wc_by_project_day, eng_by_project_day, projects, manual_rows)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["customer", "project", "wall_clock_hours", "active_engagement_hours"])
    out_rows = []
    for label in groups:
        cust = "unmapped" if label == UNMAPPED_LABEL else label
        for row in groups[label]:
            eng = "" if row["eng"] is None else fmt_hours(row["eng"])
            out_rows.append((cust, row["display"], fmt_hours(row["wc"]), eng))
    for row in sorted(out_rows):
        writer.writerow(row)
    return buf.getvalue().rstrip("\n")


def render_markdown(
    wc_by_project_day, eng_by_project_day, projects=None, manual_rows=None, title=None
):
    if not wc_by_project_day and not manual_rows:
        return "No activity recorded."
    projects = projects or {}
    groups = _build_groups(wc_by_project_day, eng_by_project_day, projects, manual_rows)

    lines = []
    if title:
        lines += [title, ""]
    lines += [
        "| Customer | Project | Wall-clock | Active-engagement |",
        "| --- | --- | ---: | ---: |",
    ]
    total_wc = 0.0
    total_eng = 0.0
    for label in _ordered_labels(groups):
        items = sorted(groups[label], key=lambda r: r["display"])
        sub_wc = 0.0
        sub_eng = 0.0
        for row in items:
            eng_cell = "—" if row["eng"] is None else fmt_hm(row["eng"])
            lines.append(f"| {label} | {row['display']} | {fmt_hm(row['wc'])} | {eng_cell} |")
            sub_wc += row["wc"]
            if row["eng"] is not None:
                sub_eng += row["eng"]
        if len(items) > 1:
            lines.append(
                f"| {label} | _subtotal_ | **{fmt_hm(sub_wc)}** | **{fmt_hm(sub_eng)}** |"
            )
        total_wc += sub_wc
        total_eng += sub_eng
    # Total wall-clock also shows decimal hours — the number an invoice wants.
    lines.append(
        f"| **Total** |  | **{fmt_hm(total_wc)}** ({fmt_hours(total_wc)}h) | **{fmt_hm(total_eng)}** |"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def build_report(
    events_path,  # one events file, or a list of them (monthly rotation)
    idle_threshold=DEFAULT_IDLE_THRESHOLD_SECONDS,
    projects_path=None,
    manual_path=None,
    date_from=None,
    date_to=None,
    customer=None,
    as_csv=False,
):
    events = load_events(events_path)
    projects = load_projects(projects_path) if projects_path else {}
    manual_entries = load_manual(manual_path) if manual_path else []

    intervals = build_intervals(events)
    suppressed = compute_suppressed(events)
    wc = wall_clock_by_project_day(intervals, suppressed)
    eng = engagement_by_project_day(intervals, idle_threshold, suppressed)

    wc = filter_by_date(wc, date_from, date_to)
    eng = filter_by_date(eng, date_from, date_to)
    if customer is not None:
        wc = filter_by_customer(wc, projects, customer)
        eng = filter_by_customer(eng, projects, customer)

    manual_rows = resolve_manual_rows(
        manual_entries, projects, date_from, date_to, customer
    )

    if not wc and not manual_rows:
        return "No activity recorded."
    if as_csv:
        return render_csv(wc, eng, projects, manual_rows)
    title = f"Time report — {period_label(date_from, date_to)}"
    if customer is not None:
        title += f" · customer: {customer}"
    title += f" · idle threshold {fmt_hm(idle_threshold)}"
    return render_markdown(wc, eng, projects, manual_rows, title=title)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Time Tracker report engine.")
    parser.add_argument(
        "--dir",
        help="Store directory (default: $TIME_TRACKER_DIR or ~/time-tracker).",
    )
    parser.add_argument(
        "--idle-threshold",
        default="15m",
        help="Idle gap above which time is excluded from active-engagement "
        "(bare number = minutes; suffix s/m/h). Default 15m.",
    )
    parser.add_argument("--from", dest="date_from", help="Start date (YYYY-MM-DD), inclusive.")
    parser.add_argument("--to", dest="date_to", help="End date (YYYY-MM-DD), inclusive.")
    parser.add_argument("--month", help="Whole-month shorthand (YYYY-MM) for --from/--to.")
    parser.add_argument("--customer", help="Restrict the report to one customer.")
    parser.add_argument("--csv", action="store_true", help="Emit CSV instead of a Markdown table.")
    parser.add_argument(
        "period",
        nargs="?",
        choices=PERIOD_WORDS,
        help="Period shorthand (e.g. 'report today'); alternative to --month/--from/--to.",
    )
    args = parser.parse_args(argv)

    if args.month and (args.date_from or args.date_to):
        parser.error("--month cannot be combined with --from/--to")
    if args.period and (args.month or args.date_from or args.date_to):
        parser.error(f"'{args.period}' cannot be combined with --month/--from/--to")

    date_from = date_to = None
    if args.period:
        date_from, date_to = period_range(args.period)
    elif args.month:
        date_from, date_to = month_range(args.month)
    else:
        if args.date_from:
            date_from = parse_date(args.date_from)
        if args.date_to:
            date_to = parse_date(args.date_to)

    sdir = store_dir(args.dir)
    events_paths = discover_event_files(sdir, date_from, date_to)
    projects_path = os.path.join(sdir, "projects.toml")
    manual_path = os.path.join(sdir, "manual.jsonl")
    idle = parse_duration(args.idle_threshold)
    out = build_report(
        events_paths,
        idle_threshold=idle,
        projects_path=projects_path,
        manual_path=manual_path,
        date_from=date_from,
        date_to=date_to,
        customer=args.customer,
        as_csv=args.csv,
    )
    if out == "No activity recorded." and (date_from or date_to):
        # A filter that matched nothing is usually a mistyped period, not an
        # empty store — say what span the store actually covers.
        out = f"No activity in the selected period ({period_label(date_from, date_to)})."
        span = data_span(sdir)
        if span:
            out += f" The store has observed activity from {span[0]} to {span[1]}."
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
