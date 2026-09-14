#!/usr/bin/env python3
"""Regression tests for the task_sentinel schedule-memorization fix.

The bug: the learned cadence was a median over EVERY successful-run gap in the 7-day
window, so after a cron change the derived interval stayed anchored to the OLD cadence
for up to a week and the task tripped STALE deterministically every sweep. The fix
re-derives the interval each run from only the most recent RECENT_GAPS gaps (a rolling
window), so it tracks the current cadence within a few runs.

Seven categories:
 1. rolling-window       — interval reflects RECENT gaps, not the whole-window median
 2. cron-slowdown        — 1h->6h no longer false-STALE once running on the new cadence
 3. cron-speedup         — 6h->1h reflected in the derived interval
 4. window-bounded       — only the last RECENT_GAPS gaps feed the median
 5. genuine-stale-kept   — a truly-stopped steady task is still caught (no regression)
 6. healthy-unaffected   — a task on steady cadence stays healthy
 7. too-few-oks          — <2 successful runs -> no interval, no crash

Pure logic, pinned clock. No DB.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nova_task_sentinel as ts

NOW = 1_000_000_000_000  # ms
HOUR = 3_600_000         # ms


def successes_at(ages_ms):
    """Build success runs at the given ages (ms before NOW), newest-first order-agnostic."""
    return [{"started_at": NOW - a, "status": "success", "exit_code": 0} for a in ages_ms]


def _ages_from_gaps(gaps_h, last_age_h):
    """Ages (ms) for runs whose most-recent run is last_age_h old and whose successive
    older gaps are gaps_h (newest gap first)."""
    ages = [last_age_h]
    for g in gaps_h:
        ages.append(ages[-1] + g)
    return [int(a * HOUR) for a in ages]


class TestRollingCadence(unittest.TestCase):

    def test_1_interval_tracks_recent_gaps_not_whole_window(self):
        # Old history: many hourly gaps. Recent history: 6h gaps. The rolling median
        # must land near 6h (current cadence), NOT ~1h (whole-window median).
        recent = [6, 6, 6, 6]          # newest gaps = 6h
        old = [1] * 20                 # ancient hourly gaps
        ages = _ages_from_gaps(recent + old, last_age_h=0.5)
        h = ts.classify_task(successes_at(ages), now_ms=NOW)
        self.assertAlmostEqual(h["interval_s"] / 3600.0, 6.0, delta=0.5)

    def test_2_cron_slowdown_no_longer_false_stale(self):
        # Task moved hourly -> every 6h. It just ran 6h ago (on the NEW schedule).
        # Whole-window median (~1h) would flag STALE (6h > 3*1h and > 3h floor).
        # Rolling median (~6h) => stale bar is 3*6h=18h => 6h ago is healthy.
        recent = [6, 6, 6]
        old = [1] * 20
        ages = _ages_from_gaps(recent + old, last_age_h=6.0)
        h = ts.classify_task(successes_at(ages), now_ms=NOW)
        self.assertNotEqual(h["state"], "stale")
        self.assertEqual(h["state"], "healthy")

    def test_3_cron_speedup_reflected(self):
        # Was every 6h, now hourly. Recent gaps are 1h.
        recent = [1, 1, 1, 1]
        old = [6] * 10
        ages = _ages_from_gaps(recent + old, last_age_h=0.2)
        h = ts.classify_task(successes_at(ages), now_ms=NOW)
        self.assertAlmostEqual(h["interval_s"] / 3600.0, 1.0, delta=0.4)

    def test_4_only_recent_gaps_feed_median(self):
        # RECENT_GAPS newest gaps are all 6h; everything older is 1h. Median == 6h
        # exactly proves the window is bounded to RECENT_GAPS.
        recent = [6] * ts.RECENT_GAPS
        old = [1] * 30
        ages = _ages_from_gaps(recent + old, last_age_h=0.1)
        h = ts.classify_task(successes_at(ages), now_ms=NOW)
        self.assertAlmostEqual(h["interval_s"] / 3600.0, 6.0, delta=0.01)


class TestNoRegression(unittest.TestCase):

    def test_5_genuine_stale_still_caught(self):
        # Steady hourly cadence but the newest run is 12h old -> genuinely stale.
        ages = _ages_from_gaps([1, 1, 1, 1], last_age_h=12.0)
        h = ts.classify_task(successes_at(ages), now_ms=NOW)
        self.assertEqual(h["state"], "stale")

    def test_6_healthy_on_cadence_unaffected(self):
        ages = _ages_from_gaps([1, 1, 1, 1, 1], last_age_h=0.1)
        h = ts.classify_task(successes_at(ages), now_ms=NOW)
        self.assertEqual(h["state"], "healthy")
        self.assertAlmostEqual(h["interval_s"] / 3600.0, 1.0, delta=0.05)

    def test_7_too_few_successes_no_interval(self):
        # 1 success + 2 running (neutral) clears MIN_RUNS but gives <2 ok gaps.
        runs = [{"started_at": NOW - 60_000, "status": "running", "exit_code": None},
                {"started_at": NOW - 120_000, "status": "running", "exit_code": None},
                {"started_at": NOW - 300_000, "status": "success", "exit_code": 0}]
        h = ts.classify_task(runs, now_ms=NOW)
        self.assertIsNone(h["interval_s"])
        self.assertIn(h["state"], ("healthy", "unknown"))  # never crashes, never false-stale


if __name__ == "__main__":
    unittest.main(verbosity=2)
