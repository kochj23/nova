#!/usr/bin/env python3
"""Tests for nova_turning_point.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_turning_point as tp  # noqa: E402

SRC = (SCRIPTS / "nova_turning_point.py").read_text()


class _Cur:
    def __init__(self, spent=0, boom=False):
        self.spent, self.boom, self.sql = spent, boom, []
        self.connection = mock.MagicMock()

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")

    def fetchone(self):
        return (self.spent,)


def _decide(cur, **kw):
    with mock.patch.object(tp, "calibrated", side_effect=lambda oc, s, d: s), \
         mock.patch.object(tp, "weekly_budget", return_value=40), \
         mock.patch("nova_restraint.record_restraint") as rr:
        r = tp.decide(cur, kw.pop("kind", "reach"), text="hello", **kw)
    return r, rr


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')

    def test_ledger_keeps_what_was_held(self):
        r, rr = _decide(_Cur(spent=40), stakes=0.5, ceiling="mention")
        self.assertFalse(r["allowed"])
        kw = rr.call_args.kwargs
        self.assertEqual(kw["would_have_said"], "hello")
        self.assertTrue(kw["reason"].startswith("HELD"))
        self.assertEqual(kw["channel"], "turning-point")


class TestPerformance(unittest.TestCase):
    def test_pick_is_cheap(self):
        t = time.monotonic()
        for i in range(20000):
            tp.pick_rung(i / 20000, 0.5)
        self.assertLess(time.monotonic() - t, 1.0)


class TestRetry(unittest.TestCase):
    def test_ledger_down_fails_open(self):
        r, _ = _decide(_Cur(boom=True), stakes=0.6, ceiling="mention")
        self.assertTrue(r["allowed"])
        self.assertIn("failing open", r["reason"])


class TestUnit(unittest.TestCase):
    def test_ladder_least_to_most_invasive(self):
        self.assertEqual(tp.ladder(), ["journal", "mention", "recommend", "act"])
        self.assertEqual(tp.ladder("mention"), ["journal", "mention"])

    def test_lowest_rung_that_stakes_warrant(self):
        self.assertEqual(tp.pick_rung(0.2, 0.9), "journal")
        self.assertEqual(tp.pick_rung(0.5, 0.9), "mention")
        self.assertEqual(tp.pick_rung(0.7, 0.9), "recommend")
        self.assertEqual(tp.pick_rung(0.95, 0.9), "act")
        self.assertEqual(tp.pick_rung(0.95, 0.9, ceiling="mention"), "mention")

    def test_low_calibrated_confidence_drops_a_rung(self):
        self.assertEqual(tp.pick_rung(0.7, 0.2), "mention")
        self.assertEqual(tp.pick_rung(0.5, 0.2), "journal")

    def test_budget_scales_with_dial(self):
        with mock.patch("nova_voice.dial", return_value=50):
            self.assertEqual(tp.weekly_budget(), 40)
        with mock.patch("nova_voice.dial", return_value=0):
            self.assertEqual(tp.weekly_budget(), 12)


class TestIntegration(unittest.TestCase):
    def test_uses_soft_certainty_calibration(self):
        with mock.patch("nova_soft_certainty.calibrate", return_value=0.1) as cal:
            self.assertEqual(tp.calibrated(object(), 0.8, "relationship"), 0.1)
        cal.assert_called_once()

    def test_relationship_reach_at_threshold_still_mentions(self):
        # live calibration 2026-10-08: relationship 0.6 -> 0.353 (>= MIN_CONF), so the
        # default reach bar still reaches; a weaker one is only noted.
        self.assertEqual(tp.pick_rung(0.6, 0.353, "mention"), "mention")
        self.assertEqual(tp.pick_rung(0.6, 0.30, "mention"), "journal")


class TestFunctional(unittest.TestCase):
    def test_spend_when_budget_left(self):
        r, rr = _decide(_Cur(spent=5), stakes=0.6, ceiling="mention")
        self.assertTrue(r["allowed"])
        self.assertEqual(r["cost"], 1)
        self.assertTrue(rr.call_args.kwargs["detail"]["turning_point"]["spent"])

    def test_late_budget_needs_high_stakes(self):
        self.assertFalse(_decide(_Cur(spent=35), stakes=0.5, ceiling="mention")[0]["allowed"])
        self.assertTrue(_decide(_Cur(spent=35), stakes=0.8, ceiling="mention")[0]["allowed"])

    def test_force_spends_past_budget(self):
        r, _ = _decide(_Cur(spent=99), stakes=0.8, ceiling="recommend", force=True)
        self.assertTrue(r["allowed"])

    def test_dry_run_writes_nothing(self):
        r, rr = _decide(_Cur(), stakes=0.6, ceiling="mention", dry=True)
        rr.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_imports_cleanly_and_guarded(self):
        r = subprocess.run([sys.executable, "-c", "import nova_turning_point"], cwd=SCRIPTS,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertNotRegex(SRC, r"/Users/[a-z]")


if __name__ == "__main__":
    unittest.main()
