#!/usr/bin/env python3
"""Tests for nova_local_situation.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

2026-10-09 (organ audit M3): merged into nova_bodach_watch.py. main(minutes, alert) is now a thin
wrapper onto Bodach's situation step, so these tests drive it through Bodach with the two-man gate
held open (the gate itself is tested in test_nova_bodach_watch_7cat) and W.connect mocked."""
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_local_situation.py"
SRC = SCRIPT.read_text()


import nova_local_situation as ls  # noqa: E402
import nova_bodach_watch as B  # noqa: E402
import nova_watch_common as W  # noqa: E402
# Home now comes from the private service_config 'home' row; tests pin a public ZIP centroid.
TEST_HOME = (34.169, -118.325)   # Burbank 91506 ZIP centroid (public), not the house
HELI = ("a1b2c3", "N911LA", 12, 900, 0.8, True)
PLANE = ("d4e5f6", "SWA123", 6, 2200, 1.5, False)
NEAR_FIRE = ("Vehicle Fire", "Olive Ave / Buena Vista", "Burbank", 34.175, -118.33)     # ~0.5 mi
FAR_FIRE = ("Vehicle Fire", "I-5 / Stadium Way", "LA", 34.07, -118.23)                 # ~9 mi
NEAR_HAZARD = ("Traffic Hazard", "Olive Ave", "Burbank", 34.175, -118.33)


class _Cur:
    """Answers the five queries in order: flights(all), chp(all), scanner(one), presence(one), baseline(one)."""
    def __init__(self, flights=(), chp=(), scanner=(0,), presence=(0, 0), baseline=(0.0,), scanner_exc=None):
        self.all = [list(flights), list(chp)]; self.one = [scanner, presence, baseline]
        self.sql = []; self.scanner_exc = scanner_exc

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split()))
        if self.scanner_exc and "FROM memories" in sql:
            self.one.pop(0)                                   # the scanner answer is never fetched
            raise self.scanner_exc

    def fetchall(self):
        return self.all.pop(0)

    def fetchone(self):
        return self.one.pop(0)


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.rollbacks = 0; self.closed = False

    def cursor(self):
        return self.cur

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _run(cur, minutes=20, alert=False, notify=None, gate=None):
    conn = _Conn(cur)
    nn = types.ModuleType("nova_notify"); nn.notify = notify or MagicMock()
    out = io.StringIO()
    with patch.object(W, "connect", MagicMock(return_value=conn)), patch.dict(sys.modules, {"nova_notify": nn}), \
            patch.object(ls, "home_coords", MagicMock(return_value=TEST_HOME)), patch.object(ls.time, "sleep"), \
            patch.object(B, "_camera_ok", return_value=True), \
            patch.object(B, "_bodach_alerted_recently", return_value=False), \
            patch.object(B, "situation_gate", return_value=gate or {"allowed": True, "reason": "test"}), \
            redirect_stdout(out):
        rc = ls.main(minutes, alert)
    return rc, out.getvalue(), conn, nn.notify


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("psycopg2", SRC)          # the connection is Bodach's (W.connect) since the merge

    def test_interpolated_sql_only_carries_typed_numbers(self):
        # the queries are f-strings, but the only interpolations are the int window and two numeric constants;
        # argparse enforces type=int so no string ever reaches them
        names = set(re.findall(r"\{(\w+)\}", "".join(re.findall(r'f"""(.*?)"""', SRC, re.S))))
        self.assertEqual(names, {"minutes", "LOW_FT", "NEAR_MI"})
        self.assertIn('ap.add_argument("--minutes", type=int', SRC)
        rc, _, conn, _ = _run(_Cur(), minutes=20)
        for sql in conn.cur.sql:
            self.assertIn("interval '20 minutes'", sql)

    def test_read_only_against_pg(self):
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", SRC))
        _, _, conn, _ = _run(_Cur())
        self.assertTrue(all(s.startswith("SELECT") for s in conn.cur.sql))
        self.assertTrue(conn.closed)


class TestPerformance(unittest.TestCase):
    def test_miles_10k_under_bound(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            ls.miles(*TEST_HOME, 34.0 + i / 100_000, -118.0 - i / 100_000)
        self.assertLess(time.perf_counter() - t0, 0.5)

    def test_main_scales_with_10k_chp_rows(self):
        rows = [NEAR_HAZARD] * 10_000
        t0 = time.perf_counter()
        rc, out, _, _ = _run(_Cur(chp=rows))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertIn("score 0 from 0 signal", out)


class TestRetry(unittest.TestCase):
    def test_scanner_query_failure_rolls_back_and_continues(self):
        # RETRY GAP: memories query — one attempt; failure rolls back and the other three feeds still score
        rc, out, conn, notify = _run(_Cur(flights=[HELI], chp=[NEAR_FIRE], scanner_exc=RuntimeError("no memories table")), alert=True)
        self.assertEqual(rc, 0)
        self.assertEqual(conn.rollbacks, 1)
        self.assertIn("score 4 from 2 signal", out)
        notify.assert_called_once()

    def test_pg_connect_failure_escapes_without_alerting(self):
        # W.connect retries 3x with backoff (tested in nova_watch_common); after that the error escapes
        # (the scheduler re-runs) and nothing is posted
        nn = types.ModuleType("nova_notify"); nn.notify = MagicMock()
        conn = MagicMock(side_effect=RuntimeError("pg down"))
        with patch.object(W, "connect", conn), patch.dict(sys.modules, {"nova_notify": nn}), \
                redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                ls.main(20, True)
        nn.notify.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_miles_known_distances(self):
        self.assertEqual(ls.miles(*TEST_HOME, *TEST_HOME), 0.0)
        self.assertAlmostEqual(ls.miles(34.0, -118.0, 35.0, -118.0), 69.09, delta=0.1)          # one degree of latitude
        self.assertAlmostEqual(ls.miles(34.169, -118.325, 33.9425, -118.408), 16.3, delta=0.5)  # home -> LAX

    def test_fires_rule(self):
        self.assertTrue(ls.fires({"score": 4, "signals": ["a", "b"]}))
        self.assertFalse(ls.fires({"score": 4, "signals": ["a"]}))
        self.assertFalse(ls.fires({"score": 3, "signals": ["a", "b", "c"]}))

    def test_constants(self):
        self.assertEqual((ls.NEAR_MI, ls.LOW_FT), (2.0, 2500))
        self.assertFalse(hasattr(ls, "HOME_LAT"))   # coordinates are private config, not code

    def test_empty_world_is_quiet(self):
        rc, out, _, notify = _run(_Cur(), alert=True)
        self.assertEqual(rc, 0)
        self.assertIn("situation score 0 from 0 signal(s) over 20 min", out)
        self.assertNotIn("SITUATION", out)
        notify.assert_not_called()


class TestIntegration(unittest.TestCase):
    def test_scoring_weights_and_the_two_signal_rule(self):
        rc, out, _, notify = _run(_Cur(flights=[HELI]), alert=True)
        self.assertIn("score 2 from 1 signal", out); self.assertIn("below threshold", out); notify.assert_not_called()
        rc, out, _, notify = _run(_Cur(flights=[HELI, PLANE]), alert=True)
        self.assertIn("score 3 from 2 signal", out); notify.assert_not_called()
        rc, out, _, notify = _run(_Cur(flights=[HELI], chp=[NEAR_FIRE]), alert=True)
        self.assertIn("score 4 from 2 signal", out); notify.assert_called_once()

    def test_chp_base_rate_calibration(self):
        rc, out, _, _ = _run(_Cur(chp=[FAR_FIRE, NEAR_HAZARD]))
        self.assertIn("score 0 from 0 signal", out)                   # far serious + near trivial: both ignored
        rc, out, _, _ = _run(_Cur(chp=[NEAR_FIRE]))
        self.assertIn("CHP: Vehicle Fire at Olive Ave / Buena Vista — 0.5 mi away", out)

    def test_motion_spike_is_relative_to_the_seven_day_baseline(self):
        rc, out, _, _ = _run(_Cur(presence=(30, 3), baseline=(10.0,)))
        self.assertIn("exterior motion SPIKE: 30 detections across 3 zone(s) vs 10 typical", out)
        rc, out, _, _ = _run(_Cur(presence=(800, 6), baseline=(700.0,)))
        self.assertIn("score 0", out)                                 # a busy afternoon is not a spike
        rc, out, _, _ = _run(_Cur(presence=(4, 2), baseline=(1.0,)))
        self.assertIn("score 0", out)                                 # below the 5-detection floor

    def test_scanner_needs_three_transmissions(self):
        rc, out, _, _ = _run(_Cur(scanner=(2,)))
        self.assertIn("score 0", out)
        rc, out, _, _ = _run(_Cur(scanner=(3,)))
        self.assertIn("scanner: 3 dispatch transmissions", out)


class TestFunctional(unittest.TestCase):
    def test_golden_path_alerts_when_feeds_agree(self):
        rc, out, conn, notify = _run(_Cur(flights=[HELI], chp=[NEAR_FIRE], scanner=(5,)), minutes=30, alert=True)
        self.assertEqual(rc, 0)
        self.assertIn("score 5 from 3 signal(s) over 30 min", out)
        msg = notify.call_args[0][0]
        self.assertTrue(msg.startswith("Something is happening nearby: HELICOPTER N911LA loitering: 12 samples, as low as 900ft, 0.8nm out; CHP: Vehicle Fire"))
        self.assertEqual(notify.call_args[1], {"level": "warning", "category": "local"})
        self.assertIn("alerted", out)
        self.assertTrue(conn.closed)

    def test_without_alert_flag_the_situation_is_logged_only(self):
        rc, out, _, notify = _run(_Cur(flights=[HELI], chp=[NEAR_FIRE]), alert=False)
        self.assertIn("SITUATION: Something is happening nearby", out)
        notify.assert_not_called()

    def test_wrapper_says_where_it_went(self):
        rc, out, _, _ = _run(_Cur())
        self.assertIn("merged into nova_bodach_watch.py on 2026-10-09", out)

    def test_two_man_gate_can_hold_the_alert(self):
        rc, out, _, notify = _run(_Cur(flights=[HELI], chp=[NEAR_FIRE]), alert=True,
                                  gate={"allowed": False, "reason": "Jordan depleted"})
        self.assertIn("SITUATION:", out)
        self.assertIn("held by the two-man rule", out)
        notify.assert_not_called()

    def test_error_path_notify_failure_is_logged_and_rc_stays_zero(self):
        rc, out, conn, _ = _run(_Cur(flights=[HELI], chp=[NEAR_FIRE]), alert=True, notify=MagicMock(side_effect=RuntimeError("bus down")))
        self.assertEqual(rc, 0)
        self.assertIn("notify failed after 3 attempts: bus down", out)
        self.assertTrue(conn.closed)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_and_import_never_runs_main(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--alert", r.stdout)
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_local_situation"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
