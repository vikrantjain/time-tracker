#!/usr/bin/env python3
"""Activity Tracker — report engine.

Reads the append-only event log produced by the capture hook and derives
wall-clock time per project. Stdlib only (no third-party dependencies).

Store location: ${ACTIVITY_TRACKER_DIR:-$HOME/activity-tracker}
  events.jsonl   observed session events (this story)

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
import json
import os
import sys
from datetime import datetime, timedelta, time

HEARTBEAT_EVENTS = {"session_start", "prompt", "stop"}
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
    return os.environ.get("ACTIVITY_TRACKER_DIR") or os.path.join(
        os.path.expanduser("~"), "activity-tracker"
    )


def load_events(path):
    """Return events sorted by (session_id, ts). Missing/empty file -> []."""
    if not os.path.exists(path):
        return []
    events = []
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


def _spans_to_project_days(spans_by_project):
    result = {}
    for project, spans in spans_by_project.items():
        day_secs = {}
        for s, e in union_intervals(spans):
            for d, secs in split_by_day(s, e):
                day_secs[d] = day_secs.get(d, 0.0) + secs
        result[project] = day_secs
    return result


def wall_clock_by_project_day(intervals):
    """Return {project: {date: seconds}} of unioned wall-clock time."""
    spans_by_project = {}
    for iv in intervals:
        spans_by_project.setdefault(iv["project"], []).append((iv["start"], iv["end"]))
    return _spans_to_project_days(spans_by_project)


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


def engagement_by_project_day(intervals, threshold):
    """Return {project: {date: seconds}} of unioned active-engagement time.

    Active spans are unioned across sessions, so time that is idle in one
    session but active in a concurrent one is still counted as engaged.
    """
    spans_by_project = {}
    for iv in intervals:
        for s, e in active_spans(iv, threshold):
            spans_by_project.setdefault(iv["project"], []).append((s, e))
    return _spans_to_project_days(spans_by_project)


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #
def fmt_hours(seconds):
    return f"{seconds / 3600:.2f}"


def render_markdown(wc_by_project_day, eng_by_project_day):
    if not wc_by_project_day:
        return "No activity recorded."
    lines = [
        "| Project | Wall-clock (h) | Active-engagement (h) |",
        "| --- | ---: | ---: |",
    ]
    total_wc = 0.0
    total_eng = 0.0
    for project in sorted(wc_by_project_day):
        wc = sum(wc_by_project_day[project].values())
        eng = sum(eng_by_project_day.get(project, {}).values())
        total_wc += wc
        total_eng += eng
        lines.append(f"| {project or '(unknown)'} | {fmt_hours(wc)} | {fmt_hours(eng)} |")
    lines.append(f"| **Total** | **{fmt_hours(total_wc)}** | **{fmt_hours(total_eng)}** |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def build_report(events_path, idle_threshold=DEFAULT_IDLE_THRESHOLD_SECONDS):
    events = load_events(events_path)
    if not events:
        return "No activity recorded."
    intervals = build_intervals(events)
    wc = wall_clock_by_project_day(intervals)
    eng = engagement_by_project_day(intervals, idle_threshold)
    return render_markdown(wc, eng)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Activity Tracker report engine.")
    parser.add_argument(
        "--dir",
        help="Store directory (default: $ACTIVITY_TRACKER_DIR or ~/activity-tracker).",
    )
    parser.add_argument(
        "--idle-threshold",
        default="15m",
        help="Idle gap above which time is excluded from active-engagement "
        "(bare number = minutes; suffix s/m/h). Default 15m.",
    )
    args = parser.parse_args(argv)

    sdir = store_dir(args.dir)
    events_path = os.path.join(sdir, "events.jsonl")
    idle = parse_duration(args.idle_threshold)
    print(build_report(events_path, idle_threshold=idle))
    return 0


if __name__ == "__main__":
    sys.exit(main())
