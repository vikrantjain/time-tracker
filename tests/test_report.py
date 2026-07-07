#!/usr/bin/env python3
"""Tests for the Time Tracker report engine (stdlib unittest)."""

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
    out = {"ts": t, "event": event, "session_id": sid, "project": project}
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

    def test_tool_events_bridge_long_turn_engagement(self):
        # A 40-min autonomous turn: without the tool heartbeats the
        # prompt->stop gap (>15m) would be dropped from engagement entirely.
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 0)),
        ]
        for i in range(1, 8):
            evs.append(ev("tool", "s", ts(2026, 3, 2, 10, 5 * i)))
        evs += [
            ev("stop", "s", ts(2026, 3, 2, 10, 40)),
            ev("session_end", "s", ts(2026, 3, 2, 10, 40)),
        ]
        ivs = report.build_intervals(evs)
        self.assertEqual(project_engagement(ivs, self.THRESH), {"/p/alpha": 2400})

    def test_tool_event_closes_crashed_interval(self):
        # tool is a heartbeat: a crash after tool activity bills up to it.
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 0)),
            ev("tool", "s", ts(2026, 3, 2, 10, 30)),
        ]
        ivs = report.build_intervals(evs)
        self.assertEqual(ivs[0]["end"] - ivs[0]["start"], 1800)


def write_projects_toml(text):
    fd, path = tempfile.mkstemp(suffix=".toml")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class CustomerMapping(unittest.TestCase):
    def _two_project_log(self):
        return [
            ev("session_start", "a", ts(2026, 3, 2, 10, 0), project="/p/acme"),
            ev("session_end", "a", ts(2026, 3, 2, 11, 0), project="/p/acme"),
            ev("session_start", "b", ts(2026, 3, 2, 10, 0), project="/p/beta"),
            ev("session_end", "b", ts(2026, 3, 2, 11, 0), project="/p/beta"),
        ]

    def test_missing_projects_toml_all_unmapped(self):
        self.assertEqual(report.load_projects("/nonexistent.toml"), {})
        wc = report.wall_clock_by_project_day(report.build_intervals(self._two_project_log()))
        out = report.render_markdown(wc, {}, {})
        self.assertIn(report.UNMAPPED_LABEL, out)
        self.assertIn("/p/acme", out)   # both projects listed under the unmapped group
        self.assertIn("/p/beta", out)

    def test_mapped_rollup_under_customer(self):
        toml = (
            '["/p/acme"]\n'
            'customer = "Acme Corp"\n'
            'name = "Acme Website"\n'
            '["/p/beta"]\n'
            'customer = "Acme Corp"\n'
        )
        path = write_projects_toml(toml)
        try:
            projects = report.load_projects(path)
        finally:
            os.remove(path)
        wc = report.wall_clock_by_project_day(report.build_intervals(self._two_project_log()))
        eng = report.engagement_by_project_day(
            report.build_intervals(self._two_project_log()), 15 * 60
        )
        out = report.render_markdown(wc, eng, projects)
        self.assertIn("Acme Corp", out)
        self.assertIn("Acme Website", out)            # display name used
        self.assertIn("_subtotal_", out)              # >1 project -> subtotal row
        self.assertNotIn(report.UNMAPPED_LABEL, out)  # nothing unmapped
        # subtotal wall-clock = 2h
        self.assertIn("**2h**", out)

    def test_partial_mapping_flags_only_unmapped(self):
        toml = '["/p/acme"]\ncustomer = "Acme Corp"\n'
        path = write_projects_toml(toml)
        try:
            projects = report.load_projects(path)
        finally:
            os.remove(path)
        wc = report.wall_clock_by_project_day(report.build_intervals(self._two_project_log()))
        out = report.render_markdown(wc, {}, projects)
        self.assertIn("Acme Corp", out)
        self.assertIn(report.UNMAPPED_LABEL, out)
        self.assertIn("/p/beta", out)  # unmapped project still listed, not dropped

    def test_malformed_toml_treated_as_empty(self):
        path = write_projects_toml("this is = = not valid toml [[[")
        try:
            self.assertEqual(report.load_projects(path), {})
        finally:
            os.remove(path)


class FiltersAndCsv(unittest.TestCase):
    def _multi_day_multi_customer(self):
        # acme on 2026-03-02 (1h) and 2026-03-10 (2h); beta on 2026-03-02 (1h).
        return [
            ev("session_start", "a1", ts(2026, 3, 2, 10, 0), project="/p/acme"),
            ev("session_end", "a1", ts(2026, 3, 2, 11, 0), project="/p/acme"),
            ev("session_start", "a2", ts(2026, 3, 10, 9, 0), project="/p/acme"),
            ev("session_end", "a2", ts(2026, 3, 10, 11, 0), project="/p/acme"),
            ev("session_start", "b1", ts(2026, 3, 2, 14, 0), project="/p/beta"),
            ev("session_end", "b1", ts(2026, 3, 2, 15, 0), project="/p/beta"),
        ]

    def _projects(self):
        toml = (
            '["/p/acme"]\ncustomer = "Acme Corp"\n'
            '["/p/beta"]\ncustomer = "Beta LLC"\n'
        )
        path = write_projects_toml(toml)
        try:
            return report.load_projects(path)
        finally:
            os.remove(path)

    def test_month_range(self):
        f, t = report.month_range("2026-02")
        self.assertEqual((f, t), (datetime(2026, 2, 1).date(), datetime(2026, 2, 28).date()))
        f, t = report.month_range("2026-12")
        self.assertEqual((f, t), (datetime(2026, 12, 1).date(), datetime(2026, 12, 31).date()))

    def test_from_to_restricts_range(self):
        wc = report.wall_clock_by_project_day(
            report.build_intervals(self._multi_day_multi_customer())
        )
        only_2nd = report.filter_by_date(
            wc, datetime(2026, 3, 2).date(), datetime(2026, 3, 2).date()
        )
        secs = {p: round(sum(d.values())) for p, d in only_2nd.items()}
        # acme 1h + beta 1h on the 2nd; acme's 2h on the 10th excluded.
        self.assertEqual(secs, {"/p/acme": 3600, "/p/beta": 3600})

    def test_customer_filter(self):
        ivs = report.build_intervals(self._multi_day_multi_customer())
        wc = report.wall_clock_by_project_day(ivs)
        projects = self._projects()
        acme_only = report.filter_by_customer(wc, projects, "Acme Corp")
        self.assertEqual(set(acme_only), {"/p/acme"})

    def test_csv_parseable(self):
        ivs = report.build_intervals(self._multi_day_multi_customer())
        wc = report.wall_clock_by_project_day(ivs)
        eng = report.engagement_by_project_day(ivs, 15 * 60)
        out = report.render_csv(wc, eng, self._projects())
        import csv as _csv

        reader = list(_csv.reader(out.splitlines()))
        self.assertEqual(reader[0], ["customer", "project", "wall_clock_hours", "active_engagement_hours"])
        body = reader[1:]
        self.assertEqual(len(body), 2)  # one row per project
        acme = next(r for r in body if r[1] == "/p/acme")
        self.assertEqual(acme[0], "Acme Corp")
        self.assertEqual(acme[2], "3.00")  # 1h + 2h across both days

    def test_filters_compose_month_customer_csv(self):
        path = write_log(self._multi_day_multi_customer())
        toml_path = write_projects_toml(
            '["/p/acme"]\ncustomer = "Acme Corp"\n["/p/beta"]\ncustomer = "Beta LLC"\n'
        )
        try:
            f, t = report.month_range("2026-03")
            out = report.build_report(
                path,
                projects_path=toml_path,
                date_from=f,
                date_to=t,
                customer="Acme Corp",
                as_csv=True,
            )
        finally:
            os.remove(path)
            os.remove(toml_path)
        import csv as _csv

        rows = list(_csv.reader(out.splitlines()))[1:]
        self.assertEqual(len(rows), 1)             # only Acme
        self.assertEqual(rows[0][0], "Acme Corp")
        self.assertEqual(rows[0][2], "3.00")       # whole-month acme total

    def test_filter_excludes_all_yields_no_activity(self):
        path = write_log(self._multi_day_multi_customer())
        try:
            out = report.build_report(
                path,
                date_from=datetime(2025, 1, 1).date(),
                date_to=datetime(2025, 1, 31).date(),
            )
        finally:
            os.remove(path)
        self.assertEqual(out, "No activity recorded.")


class PauseResume(unittest.TestCase):
    THRESH = 15 * 60

    def _wc_eng(self, evs):
        ivs = report.build_intervals(evs)
        sup = report.compute_suppressed(evs)
        wc = report.wall_clock_by_project_day(ivs, sup)
        eng = report.engagement_by_project_day(ivs, self.THRESH, sup)
        sec = lambda d: {p: round(sum(v.values())) for p, v in d.items()}
        return sec(wc), sec(eng)

    def test_pause_then_explicit_resume(self):
        # 1h session; paused 10:10-10:40 (30m) then explicitly resumed.
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 0)),
            ev("stop", "s", ts(2026, 3, 2, 10, 10)),
            ev("pause", "s", ts(2026, 3, 2, 10, 10)),
            ev("resume", "s", ts(2026, 3, 2, 10, 40)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 45)),
            ev("session_end", "s", ts(2026, 3, 2, 11, 0)),
        ]
        wc, eng = self._wc_eng(evs)
        # wall-clock 60m - 30m paused = 30m removed from BOTH metrics.
        self.assertEqual(wc, {"/p/alpha": 1800})

    def test_pause_auto_resumes_on_next_real_prompt(self):
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 0)),
            ev("stop", "s", ts(2026, 3, 2, 10, 5)),
            ev("pause", "s", ts(2026, 3, 2, 10, 5)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 35)),   # 30m later -> auto-resume
            ev("session_end", "s", ts(2026, 3, 2, 10, 45)),
        ]
        wc, _ = self._wc_eng(evs)
        # 45m total - 30m paused = 15m.
        self.assertEqual(wc, {"/p/alpha": 900})

    def test_pause_until_session_end(self):
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 0)),
            ev("stop", "s", ts(2026, 3, 2, 10, 20)),
            ev("pause", "s", ts(2026, 3, 2, 10, 20)),
            ev("session_end", "s", ts(2026, 3, 2, 11, 0)),  # closes the pause
        ]
        wc, _ = self._wc_eng(evs)
        # 60m total - 40m paused (10:20-11:00) = 20m.
        self.assertEqual(wc, {"/p/alpha": 1200})

    def test_pause_removed_from_engagement_too(self):
        # A 5-min pause (UNDER the 15-min idle threshold) would otherwise be
        # counted as engaged time; the marker must remove it from engagement.
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 0)),
            ev("stop", "s", ts(2026, 3, 2, 10, 5)),    # active 10:00-10:05
            ev("pause", "s", ts(2026, 3, 2, 10, 5)),
            ev("resume", "s", ts(2026, 3, 2, 10, 10)),  # 5-min pause (< threshold)
            ev("prompt", "s", ts(2026, 3, 2, 10, 10)),
            ev("stop", "s", ts(2026, 3, 2, 10, 15)),    # active 10:10-10:15
            ev("session_end", "s", ts(2026, 3, 2, 10, 15)),
        ]
        wc, eng = self._wc_eng(evs)
        # Without the pause the whole 15m is active; the 5m pause drops it to 10m.
        self.assertEqual(wc, {"/p/alpha": 600})
        self.assertEqual(eng, {"/p/alpha": 600})

    def test_pause_attributed_to_project_at_pause_time(self):
        # One session, resumed under a different cwd: the pause typed in the
        # second sub-interval must be subtracted from the SECOND project.
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0), project="/p/alpha"),
            ev("session_end", "s", ts(2026, 3, 2, 11, 0), project="/p/alpha"),
            ev("session_start", "s", ts(2026, 3, 2, 12, 0), project="/p/beta"),
            ev("prompt", "s", ts(2026, 3, 2, 12, 0), project="/p/beta"),
            ev("pause", "s", ts(2026, 3, 2, 12, 10), project="/p/beta"),
            ev("resume", "s", ts(2026, 3, 2, 12, 40), project="/p/beta"),
            ev("session_end", "s", ts(2026, 3, 2, 13, 0), project="/p/beta"),
        ]
        self.assertEqual(set(report.compute_suppressed(evs)), {"/p/beta"})
        wc, _ = self._wc_eng(evs)
        # alpha keeps its full hour; beta loses the 30-min pause.
        self.assertEqual(wc, {"/p/alpha": 3600, "/p/beta": 1800})

    def test_tool_event_does_not_close_pause(self):
        # Tool activity mid-pause is Claude finishing a turn, not the user
        # returning; only the next prompt auto-resumes.
        evs = [
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 0)),
            ev("pause", "s", ts(2026, 3, 2, 10, 10)),
            ev("tool", "s", ts(2026, 3, 2, 10, 20)),
            ev("prompt", "s", ts(2026, 3, 2, 10, 40)),
            ev("session_end", "s", ts(2026, 3, 2, 10, 50)),
        ]
        sup = report.compute_suppressed(evs)
        self.assertEqual(
            sup, {"/p/alpha": [(ts(2026, 3, 2, 10, 10), ts(2026, 3, 2, 10, 40))]}
        )

    def test_subtract_intervals_helper(self):
        self.assertEqual(
            report.subtract_intervals([(0, 100)], [(20, 30), (50, 60)]),
            [(0, 20), (30, 50), (60, 100)],
        )
        self.assertEqual(report.subtract_intervals([(0, 100)], []), [(0, 100)])


class ManualTime(unittest.TestCase):
    def _manual_file(self, entries):
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for e in entries:
                e.setdefault("source", "manual")
                fh.write(json.dumps(e) + "\n")
        return path

    def _projects(self):
        path = write_projects_toml('["/p/acme"]\ncustomer = "Acme Corp"\nname = "Acme Website"\n')
        try:
            return report.load_projects(path), path
        finally:
            pass

    def test_load_manual_filters_non_manual(self):
        path = self._manual_file([
            {"project": "/p/acme", "date": "2026-03-02", "duration": "2h", "note": "call"},
            {"source": "other", "project": "/p/acme", "duration": "9h"},  # ignored
        ])
        try:
            entries = report.load_manual(path)
        finally:
            os.remove(path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["duration"], "2h")

    def test_manual_in_wallclock_distinct_and_no_engagement(self):
        projects = report.load_projects(
            write_projects_toml('["/p/acme"]\ncustomer = "Acme Corp"\n')
        )
        rows = report.resolve_manual_rows(
            [{"project": "/p/acme", "date": "2026-03-02", "duration": "2h", "note": "phone call"}],
            projects,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["customer"], "Acme Corp")
        self.assertIn("✎ manual", rows[0]["display"])     # distinct label
        self.assertIn("phone call", rows[0]["display"])
        self.assertEqual(rows[0]["wc"], 2 * 3600)
        # Rendered: manual row carries no engagement ("—"); wall-clock counts it.
        out = report.render_markdown({}, {}, projects, rows)
        self.assertIn("✎ manual", out)
        self.assertIn("2h", out)
        manual_line = next(l for l in out.splitlines() if "✎ manual" in l)
        self.assertIn("—", manual_line)                    # engagement blank

    def test_bare_duration_is_hours(self):
        rows = report.resolve_manual_rows(
            [{"project": "x", "duration": "3", "note": ""}], {}
        )
        self.assertEqual(rows[0]["wc"], 3 * 3600)

    def test_negative_correction_reduces_total_distinctly(self):
        projects = report.load_projects(
            write_projects_toml('["/p/acme"]\ncustomer = "Acme Corp"\n')
        )
        manual = [
            {"project": "/p/acme", "date": "2026-03-02", "duration": "2h", "note": "added"},
            {"project": "/p/acme", "date": "2026-03-02", "duration": "-30m", "note": "overbilled, correction"},
        ]
        rows = report.resolve_manual_rows(manual, projects)
        self.assertEqual(len(rows), 2)                     # both kept as distinct rows
        wcs = sorted(r["wc"] for r in rows)
        self.assertEqual(wcs, [-1800, 7200])               # negative is its own adjustment
        out = report.render_markdown({}, {}, projects, rows)
        self.assertIn("correction", out)
        self.assertIn("-30m", out)                         # correction cell keeps its sign
        # Net manual contribution to the customer subtotal/total = 1.5h; the
        # total row also carries the decimal form for invoicing.
        self.assertIn("**1h 30m**", out)
        self.assertIn("(1.50h)", out)

    def test_manual_via_build_report_and_date_filter(self):
        events = write_log([
            ev("session_start", "s", ts(2026, 3, 2, 10, 0), project="/p/acme"),
            ev("session_end", "s", ts(2026, 3, 2, 11, 0), project="/p/acme"),
        ])
        proj = write_projects_toml('["/p/acme"]\ncustomer = "Acme Corp"\n')
        manual = self._manual_file([
            {"project": "/p/acme", "date": "2026-03-02", "duration": "2h", "note": "in range"},
            {"project": "/p/acme", "date": "2026-04-15", "duration": "5h", "note": "out of range"},
        ])
        try:
            f, t = report.month_range("2026-03")
            out = report.build_report(
                events, projects_path=proj, manual_path=manual, date_from=f, date_to=t
            )
        finally:
            for p in (events, proj, manual):
                os.remove(p)
        self.assertIn("in range", out)
        self.assertNotIn("out of range", out)   # April entry filtered out
        # observed 1h + manual 2h = 3h subtotal for Acme.
        self.assertIn("**3h**", out)


class MonthlyRotation(unittest.TestCase):
    def _store(self, files):
        d = tempfile.mkdtemp()
        for name, events in files.items():
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                for e in events:
                    fh.write(json.dumps(e) + "\n")
        return d

    def test_discovery_windows_monthly_files(self):
        d = self._store({
            "events.jsonl": [],
            "events-2026-01.jsonl": [],
            "events-2026-03.jsonl": [],
            "events-2026-05.jsonl": [],
            "events-2026-07.jsonl": [],
        })
        got = report.discover_event_files(
            d, datetime(2026, 3, 5).date(), datetime(2026, 4, 20).date()
        )
        names = [os.path.basename(p) for p in got]
        # Legacy always loads; monthly window = Feb..May, so only 03 and 05.
        self.assertEqual(
            names, ["events.jsonl", "events-2026-03.jsonl", "events-2026-05.jsonl"]
        )

    def test_discovery_no_filter_loads_everything(self):
        d = self._store({
            "events-2026-01.jsonl": [],
            "events-2026-12.jsonl": [],
            "notes.txt-events-2026-02.jsonl": [],  # non-matching name ignored
        })
        names = [os.path.basename(p) for p in report.discover_event_files(d)]
        self.assertEqual(names, ["events-2026-01.jsonl", "events-2026-12.jsonl"])

    def test_load_events_accepts_single_path_and_list(self):
        d = self._store({
            "events-2026-03.jsonl": [ev("session_start", "s", ts(2026, 3, 2, 10, 0))],
            "events-2026-04.jsonl": [ev("session_end", "s", ts(2026, 4, 1, 10, 0))],
        })
        one = report.load_events(os.path.join(d, "events-2026-03.jsonl"))
        both = report.load_events(report.discover_event_files(d))
        self.assertEqual(len(one), 1)
        self.assertEqual(len(both), 2)

    def test_session_spanning_month_boundary_reassembled(self):
        # session_start lands in the March file, session_end in April's; a
        # March report must still see a closed interval (23:00 -> midnight).
        d = self._store({
            "events-2026-03.jsonl": [
                ev("session_start", "s", ts(2026, 3, 31, 23, 0), project="/p/acme")
            ],
            "events-2026-04.jsonl": [
                ev("session_end", "s", ts(2026, 4, 1, 1, 0), project="/p/acme")
            ],
        })
        f, t = report.month_range("2026-03")
        out = report.build_report(
            report.discover_event_files(d, f, t), date_from=f, date_to=t
        )
        self.assertIn("/p/acme", out)
        self.assertIn("1h", out)  # March's share: 23:00 -> midnight


class RenderingUX(unittest.TestCase):
    def test_fmt_hm(self):
        self.assertEqual(report.fmt_hm(2 * 3600 + 45 * 60), "2h 45m")
        self.assertEqual(report.fmt_hm(2 * 3600), "2h")
        self.assertEqual(report.fmt_hm(45 * 60), "45m")
        self.assertEqual(report.fmt_hm(0), "0m")
        self.assertEqual(report.fmt_hm(-1800), "-30m")
        self.assertEqual(report.fmt_hm(29), "0m")     # rounds to the minute
        self.assertEqual(report.fmt_hm(31), "1m")

    def test_period_label(self):
        d = lambda y, m, dd: datetime(y, m, dd).date()
        self.assertEqual(report.period_label(None, None), "all time")
        self.assertEqual(report.period_label(d(2026, 3, 1), d(2026, 3, 31)), "2026-03")
        self.assertEqual(
            report.period_label(d(2026, 3, 5), d(2026, 4, 20)), "2026-03-05 to 2026-04-20"
        )
        self.assertEqual(report.period_label(d(2026, 3, 5), None), "from 2026-03-05")
        self.assertEqual(report.period_label(None, d(2026, 4, 20)), "through 2026-04-20")

    def test_header_line_states_period_customer_threshold(self):
        path = write_log([
            ev("session_start", "s", ts(2026, 3, 2, 10, 0), project="/p/acme"),
            ev("session_end", "s", ts(2026, 3, 2, 11, 0), project="/p/acme"),
        ])
        proj = write_projects_toml('["/p/acme"]\ncustomer = "Acme Corp"\n')
        try:
            f, t = report.month_range("2026-03")
            out = report.build_report(
                path, projects_path=proj, date_from=f, date_to=t, customer="Acme Corp"
            )
        finally:
            os.remove(path)
            os.remove(proj)
        header = out.splitlines()[0]
        self.assertIn("2026-03", header)
        self.assertIn("customer: Acme Corp", header)
        self.assertIn("idle threshold 15m", header)

    def test_csv_has_no_header_line_and_keeps_decimal(self):
        path = write_log([
            ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
            ev("session_end", "s", ts(2026, 3, 2, 11, 30)),
        ])
        try:
            out = report.build_report(path, as_csv=True)
        finally:
            os.remove(path)
        self.assertTrue(out.startswith("customer,project,"))
        self.assertIn("1.50", out)

    def test_empty_filtered_report_states_store_span(self):
        import contextlib
        import io as _io

        d = tempfile.mkdtemp()
        with open(os.path.join(d, "events-2026-03.jsonl"), "w", encoding="utf-8") as fh:
            for e in [
                ev("session_start", "s", ts(2026, 3, 2, 10, 0)),
                ev("session_end", "s", ts(2026, 3, 5, 11, 0)),
            ]:
                fh.write(json.dumps(e) + "\n")
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf):
            report.main(["--dir", d, "--month", "2025-01"])
        out = buf.getvalue()
        self.assertIn("No activity in the selected period (2025-01)", out)
        self.assertIn("from 2026-03-02 to 2026-03-05", out)

    def test_empty_unfiltered_report_unchanged(self):
        self.assertEqual(
            report.build_report("/nonexistent/path.jsonl"), "No activity recorded."
        )


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
            self.assertIn("| Project | Wall-clock |", out)
            self.assertIn("/p/alpha", out)
            self.assertIn("1h 30m", out)
            self.assertIn("**Total**", out)
            self.assertTrue(out.startswith("Time report — all time"))
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
