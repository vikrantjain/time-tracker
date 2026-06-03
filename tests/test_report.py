#!/usr/bin/env python3
"""Tests for the Activity Tracker report engine (stdlib unittest)."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import report  # noqa: E402


def ts(y, mo, d, h, mi=0, s=0):
    """Local-time epoch seconds (so day buckets match the machine tz)."""
    return datetime(y, mo, d, h, mi, s).timestamp()


def ev(event, sid, t, project="/p/alpha", **extra):
    out = {"ts": t, "iso": "", "event": event, "session_id": sid, "project": project}
    out.update(extra)
    return out


def write_log(events):
    fd, path = tempfile.mkstemp(suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")
    return path


def project_seconds(intervals_or_events):
    wc = report.wall_clock_by_project_day(intervals_or_events)
    return {p: round(sum(days.values())) for p, days in wc.items()}


class Segmentation(unittest.TestCase):
    def test_normal_start_to_end(self):
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0), source="startup"),
            ev("prompt", "s", ts(2026, 3, 2, 10, 10)),
            ev("stop", "s", ts(2026, 3, 2, 10, 20)),
            ev("session_end", "s", ts(2026, 3, 2, 11, 0), reason="exit"),
        ]
        ivs = report.build_intervals(evs)
        self.assertEqual(len(ivs), 1)
        self.assertEqual(ivs[0]["end"] - ivs[0]["start"], 3600)

    def test_missing_session_end_closes_at_last_heartbeat(self):
        # No session_end, end of log -> close at last heartbeat (stop @10:20).
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 10)),
            ev("stop", "s", ts(2026, 3, 2, 10, 20)),
        ]
        ivs = report.build_intervals(evs)
        self.assertEqual(len(ivs), 1)
        self.assertEqual(ivs[0]["end"] - ivs[0]["start"], 1200)

    def test_closed_overnight_gap_not_counted(self):
        # One session_id, two sub-intervals; the closed gap must not be billed.
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 10)),
            ev("stop", "s", ts(2026, 3, 2, 10, 20)),  # last heartbeat of sub-1
            ev("session_start", "s", ts(2026, 3, 2, 22, 0), source="resume"),
            ev("prompt", "s", ts(2026, 3, 2, 22, 5)),
            ev("session_end", "s", ts(2026, 3, 2, 22, 10)),
        ]
        ivs = report.build_intervals(evs)
        self.assertEqual(len(ivs), 2)
        self.assertEqual(ivs[0]["end"] - ivs[0]["start"], 1200)  # 10:00-10:20
        self.assertEqual(ivs[1]["end"] - ivs[1]["start"], 600)   # 22:00-22:10
        self.assertEqual(project_seconds(ivs), {"/p/alpha": 1800})


class UnionAndBuckets(unittest.TestCase):
    def test_overlapping_concurrent_sessions_union_not_sum(self):
        evs = [
            ev("session_start", "a", ts(2026, 3, 2, 10, 0)),
            ev("session_end", "a", ts(2026, 3, 2, 11, 0)),       # [10:00,11:00]
            ev("session_start", "b", ts(2026, 3, 2, 10, 30)),
            ev("session_end", "b", ts(2026, 3, 2, 11, 30)),      # [10:30,11:30]
        ]
        ivs = report.build_intervals(evs)
        # union = [10:00,11:30] = 1.5h, NOT 2h.
        self.assertEqual(project_seconds(ivs), {"/p/alpha": 5400})

    def test_separate_projects_not_unioned(self):
        evs = [
            ev("session_start", "a", ts(2026, 3, 2, 10, 0), project="/p/alpha"),
            ev("session_end", "a", ts(2026, 3, 2, 11, 0), project="/p/alpha"),
            ev("session_start", "b", ts(2026, 3, 2, 10, 0), project="/p/beta"),
            ev("session_end", "b", ts(2026, 3, 2, 11, 0), project="/p/beta"),
        ]
        ivs = report.build_intervals(evs)
        self.assertEqual(project_seconds(ivs), {"/p/alpha": 3600, "/p/beta": 3600})

    def test_zero_duration_session_appears(self):
        # session_start with no heartbeat -> [t,t], 0s, but project surfaces.
        evs = [ev("session_start", "s", ts(2026, 3, 2, 10, 0))]
        ivs = report.build_intervals(evs)
        self.assertEqual(len(ivs), 1)
        self.assertEqual(ivs[0]["end"] - ivs[0]["start"], 0)
        self.assertEqual(project_seconds(ivs), {"/p/alpha": 0})

    def test_interval_crossing_local_midnight_is_split(self):
        # 23:30 -> 00:30 next day: 30 min each side.
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 23, 30)),
            ev("session_end", "s", ts(2026, 3, 3, 0, 30)),
        ]
        ivs = report.build_intervals(evs)
        wc = report.wall_clock_by_project_day(ivs)
        days = wc["/p/alpha"]
        d1 = datetime(2026, 3, 2).date()
        d2 = datetime(2026, 3, 3).date()
        self.assertIn(d1, days)
        self.assertIn(d2, days)
        self.assertEqual(round(days[d1]), 1800)
        self.assertEqual(round(days[d2]), 1800)


def project_engagement(intervals, threshold):
    eng = report.engagement_by_project_day(intervals, threshold)
    return {p: round(sum(days.values())) for p, days in eng.items()}


class Engagement(unittest.TestCase):
    THRESH = 15 * 60

    def test_idle_gap_over_threshold_subtracted_from_engagement_only(self):
        # 30-min gap between stop and next prompt -> idle (>15m), subtracted.
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 0)),
            ev("stop", "s", ts(2026, 3, 2, 10, 10)),       # active 10:00-10:10
            ev("prompt", "s", ts(2026, 3, 2, 10, 40)),     # 30m idle gap
            ev("session_end", "s", ts(2026, 3, 2, 10, 50)),  # active 10:40-10:50
        ]
        ivs = report.build_intervals(evs)
        # wall-clock spans the whole 50 min; engagement drops the 30-min gap.
        self.assertEqual(project_seconds(ivs), {"/p/alpha": 3000})
        self.assertEqual(project_engagement(ivs, self.THRESH), {"/p/alpha": 1200})

    def test_gap_at_threshold_not_subtracted(self):
        # Exactly 15-min gap must NOT be subtracted (strictly-greater rule).
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("stop", "s", ts(2026, 3, 2, 10, 0)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 15)),     # exactly 15m
            ev("session_end", "s", ts(2026, 3, 2, 10, 20)),
        ]
        ivs = report.build_intervals(evs)
        self.assertEqual(project_seconds(ivs), {"/p/alpha": 1200})
        self.assertEqual(project_engagement(ivs, self.THRESH), {"/p/alpha": 1200})

    def test_threshold_override_changes_subtraction(self):
        # Two turns with a 10-min gap between them; active time on both ends.
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 0)),
            ev("stop", "s", ts(2026, 3, 2, 10, 5)),        # active 10:00-10:05
            ev("prompt", "s", ts(2026, 3, 2, 10, 15)),     # 10-min gap
            ev("stop", "s", ts(2026, 3, 2, 10, 20)),       # active 10:15-10:20
            ev("session_end", "s", ts(2026, 3, 2, 10, 20)),
        ]
        ivs = report.build_intervals(evs)
        # 15-min threshold: 10-min gap kept -> full 20 min.
        self.assertEqual(project_engagement(ivs, 15 * 60), {"/p/alpha": 1200})
        # 5-min threshold: 10-min gap now exceeds it -> the two 5-min spans only.
        self.assertEqual(project_engagement(ivs, 5 * 60), {"/p/alpha": 600})

    def test_wall_clock_unchanged_by_subtraction(self):
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("stop", "s", ts(2026, 3, 2, 10, 0)),
            ev("prompt", "s", ts(2026, 3, 2, 11, 0)),      # 1h idle
            ev("session_end", "s", ts(2026, 3, 2, 11, 10)),
        ]
        ivs = report.build_intervals(evs)
        wc = project_seconds(ivs)["/p/alpha"]
        eng = project_engagement(ivs, self.THRESH)["/p/alpha"]
        self.assertEqual(wc, 4200)        # full 70 min wall-clock
        self.assertEqual(eng, 600)        # only the 10-min tail engaged
        self.assertLess(eng, wc)

    def test_parse_duration(self):
        self.assertEqual(report.parse_duration("15"), 900)    # bare -> minutes
        self.assertEqual(report.parse_duration("15m"), 900)
        self.assertEqual(report.parse_duration("90s"), 90)
        self.assertEqual(report.parse_duration("1.5h"), 5400)


class EndToEnd(unittest.TestCase):
    def test_missing_log(self):
        self.assertEqual(report.build_report("/nonexistent/path.jsonl"),
                         "No activity recorded.")

    def test_empty_log(self):
        path = write_log([])
        try:
            self.assertEqual(report.build_report(path), "No activity recorded.")
        finally:
            os.remove(path)

    def test_markdown_table_renders(self):
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("session_end", "s", ts(2026, 3, 2, 11, 30)),
        ]
        path = write_log(evs)
        try:
            out = report.build_report(path)
            self.assertIn("| Project | Wall-clock (h) |", out)
            self.assertIn("/p/alpha", out)
            self.assertIn("1.50", out)
            self.assertIn("**Total**", out)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
