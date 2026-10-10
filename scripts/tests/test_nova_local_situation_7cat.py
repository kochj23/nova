#!/usr/bin/env python3
"""7-category tests for nova_local_situation (fused "is something happening near us?").
Focus (2026-10-08): home coordinates come from the PRIVATE service_config 'home' row, never
from code, and never reach a log line or an alert. Offline: no PostgreSQL, no notifier.
2026-10-09: merged into nova_bodach_watch.py (organ audit M3); main() is a thin wrapper onto Bodach's
situation step, driven here with W.connect mocked and the two-man gate held open.
Written by Jordan Koch (via Claude).
"""
import io
import json
import re
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_local_situation as L  # noqa: E402
import nova_bodach_watch as B  # noqa: E402
import nova_watch_common as W  # noqa: E402

SRC = (SCRIPTS / "nova_local_situation.py").read_text()
HOME = {"lat": 12.3456, "lon": -65.4321, "label": "test home"}   # synthetic, not the real house
COORD_RX = re.compile(r"12\.34|65\.43")


class Cur:
    """Fake cursor: service_config 'home', one close serious CHP incident, a helicopter orbit."""
    def __init__(self, home=HOME, chp_near=True):
        self.home, self.chp_near, self.sql, self._rows = home, chp_near, [], []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if "service_config" in sql:
            self._rows = [(self.home,)] if self.home is not None else []
        elif "overhead_flights" in sql:
            self._rows = [("abc123", "HELI1", 9, 900, 0.5, True)]
        elif "chp_incidents" in sql:
            self._rows = ([("1183-Trfc Collision-1141 Enrt", "Olive Ave", "Glendale", HOME["lat"] + 0.003,
                            HOME["lon"])] if self.chp_near else [])
        elif "FROM memories" in sql:
            self._rows = [(0,)]
        elif "count(DISTINCT room)" in sql:
            self._rows = [(0, 0)]
        else:
            self._rows = [(0.0,)]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class Conn:
    def __init__(self, cur):
        self._c, self.autocommit = cur, False

    def cursor(self):
        return self._c

    def rollback(self):
        pass

    def close(self):
        pass


def run(cur, alert=False, notify=None):
    buf = io.StringIO()
    fake_notify = mock.MagicMock() if notify is None else notify
    mod = type(sys)("nova_notify"); mod.notify = fake_notify
    with mock.patch.object(W, "connect", return_value=Conn(cur)), \
            mock.patch.object(B, "_camera_ok", return_value=True), \
            mock.patch.object(B, "_bodach_alerted_recently", return_value=False), \
            mock.patch.object(B, "situation_gate", return_value={"allowed": True, "reason": "test"}), \
            mock.patch.object(L.time, "sleep"), \
            mock.patch.dict(sys.modules, {"nova_notify": mod}), redirect_stdout(buf):
        rc = L.main(20, alert)
    return rc, buf.getvalue(), fake_notify


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_coordinates(self):
        self.assertNotRegex(SRC, r"HOME_LAT\s*,\s*HOME_LON\s*=")
        self.assertNotRegex(SRC, r"-?\d{2,3}\.\d{3,}\s*,\s*-?\d{2,3}\.\d{3,}")

    def test_coordinates_never_logged_or_alerted(self):
        rc, out, n = run(Cur(), alert=True)
        self.assertEqual(rc, 0)
        self.assertNotRegex(out, COORD_RX)
        n.assert_called_once()
        self.assertNotRegex(json.dumps([n.call_args.args, n.call_args.kwargs], default=str), COORD_RX)

    def test_home_lookup_is_parameterised(self):
        cur = Cur()
        L.home_coords(cur)
        sql, params = cur.sql[0]
        self.assertIn("%s", sql)
        self.assertEqual(params, ("home",))

    def test_no_user_paths(self):
        self.assertNotRegex(SRC, r"/Users/[a-z]")


class TestPerformance(unittest.TestCase):
    def test_miles_fast(self):
        t = time.perf_counter()
        for _ in range(20000):
            L.miles(34.0, -118.0, 34.1, -118.1)
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_single_home_lookup_per_run(self):
        cur = Cur()
        run(cur)
        self.assertEqual(sum("service_config" in s for s, _ in cur.sql), 1)


class TestRetry(unittest.TestCase):
    def test_retry_helper_backs_off_then_succeeds(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("down")
            return "ok"
        with mock.patch.object(L.time, "sleep") as sl, redirect_stdout(io.StringIO()) as out:
            self.assertEqual(L._retry(flaky, what="notify"), "ok")
        self.assertEqual(calls["n"], 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [1.0, 2.0])   # exponential backoff
        self.assertIn("attempt 1/3 failed", out.getvalue())

    def test_pg_connect_gives_up_loudly(self):
        with mock.patch.object(W, "connect", side_effect=OSError("down")), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                L.main(20, False)

    def test_notify_retried_and_failure_logged(self):
        n = mock.MagicMock(side_effect=RuntimeError("slack 500"))
        rc, out, _ = run(Cur(), alert=True, notify=n)
        self.assertEqual(rc, 0)
        self.assertEqual(n.call_count, 3)
        self.assertIn("notify failed after 3 attempts", out)


class TestUnit(unittest.TestCase):
    def test_home_coords_parses_dict_and_json(self):
        self.assertEqual(L.home_coords(Cur()), (12.3456, -65.4321))
        self.assertEqual(L.home_coords(Cur(home=json.dumps(HOME))), (12.3456, -65.4321))

    def test_home_coords_missing_or_bad(self):
        self.assertIsNone(L.home_coords(Cur(home=None)))
        self.assertIsNone(L.home_coords(Cur(home={"lat": "x"})))
        self.assertIsNone(L.home_coords(Cur(home={"lat": 200, "lon": 0})))

    def test_home_coords_db_error_none(self):
        cur = mock.MagicMock()
        cur.execute.side_effect = RuntimeError("no table")
        self.assertIsNone(L.home_coords(cur))

    def test_miles_zero(self):
        self.assertAlmostEqual(L.miles(1, 1, 1, 1), 0.0)


class TestIntegration(unittest.TestCase):
    def test_assess_kinds_parallel_to_signals(self):
        with redirect_stdout(io.StringIO()):
            res = L.assess(Cur(), 20)
        self.assertEqual(res["kinds"], ["air", "chp"])
        self.assertEqual(len(res["kinds"]), len(res["signals"]))
        self.assertTrue(L.fires(res))

    def test_motion_flag_skips_camera_queries(self):
        cur = Cur()
        with redirect_stdout(io.StringIO()):
            L.assess(cur, 20, motion=False)
        self.assertFalse(any("telemetry.presence" in s for s, _ in cur.sql))

    def test_chp_distance_uses_config_home(self):
        rc, out, _ = run(Cur())
        self.assertIn("CHP:", out)
        self.assertIn("0.2 mi away", out)

    def test_without_home_config_chp_skipped_not_crashed(self):
        rc, out, _ = run(Cur(home=None))
        self.assertEqual(rc, 0)
        self.assertNotIn("CHP:", out)
        self.assertIn("not configured", out)


class TestFunctional(unittest.TestCase):
    def test_two_signals_raise_situation(self):
        rc, out, n = run(Cur(), alert=True)
        self.assertIn("SITUATION:", out)
        n.assert_called_once()

    def test_single_signal_stays_quiet(self):
        rc, out, n = run(Cur(chp_near=False), alert=True)
        self.assertNotIn("SITUATION:", out)
        n.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_imports_and_help(self):
        import subprocess
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_local_situation.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--minutes", r.stdout)


if __name__ == "__main__":
    unittest.main()
