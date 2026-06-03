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


def wall_clock_by_project_day(intervals):
    """Return {project: {date: seconds}} of unioned wall-clock time."""
    spans_by_project = {}
    for iv in intervals:
        spans_by_project.setdefault(iv["project"], []).append((iv["start"], iv["end"]))

    result = {}
    for project, spans in spans_by_project.items():
        day_secs = {}
        for s, e in union_intervals(spans):
            for d, secs in split_by_day(s, e):
                day_secs[d] = day_secs.get(d, 0.0) + secs
        result[project] = day_secs
    return result


# --------------------------------------------------------------------------- #
# Rendering                                                                   #
# --------------------------------------------------------------------------- #
def fmt_hours(seconds):
    return f"{seconds / 3600:.2f}"


def render_markdown(wc_by_project_day):
    if not wc_by_project_day:
        return "No activity recorded."
    lines = ["| Project | Wall-clock (h) |", "| --- | ---: |"]
    total = 0.0
    for project in sorted(wc_by_project_day):
        secs = sum(wc_by_project_day[project].values())
        total += secs
        lines.append(f"| {project or '(unknown)'} | {fmt_hours(secs)} |")
    lines.append(f"| **Total** | **{fmt_hours(total)}** |")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #
def build_report(events_path):
    events = load_events(events_path)
    if not events:
        return "No activity recorded."
    intervals = build_intervals(events)
    wc = wall_clock_by_project_day(intervals)
    return render_markdown(wc)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Activity Tracker report engine.")
    parser.add_argument(
        "--dir",
        help="Store directory (default: $ACTIVITY_TRACKER_DIR or ~/activity-tracker).",
    )
    args = parser.parse_args(argv)

    sdir = store_dir(args.dir)
    events_path = os.path.join(sdir, "events.jsonl")
    print(build_report(events_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
