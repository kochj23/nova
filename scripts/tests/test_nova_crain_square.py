#!/usr/bin/env python3
"""Tests for nova_crain_square.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import subprocess
import sys
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_crain_square as C  # noqa: E402

SRC = (SCRIPTS / "nova_crain_square.py").read_text()
DAY = date(2026, 10, 7)
T0 = datetime(2026, 10, 7, tzinfo=C.W.TZ)


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = next((list(v) for k, v in self.routes.items() if k in sql), [])

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def fake_conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


def mm_rows():
    rows = []
    for i in range(300):
        t = T0 + timedelta(minutes=i)
        rows += [(t, "master_bedroom", i % 5 == 0), (t, "patio", i % 5 == 0), (t, "office", i % 3 == 0)]
    return rows


ROUTES = {
    "FROM service_config": [],
    "to_regclass": [("crain_square_residuals",)],
    "FROM crain_square_residuals WHERE day >=": [],
    "method='mmwave'": mm_rows(),
    "FROM telemetry.ha_sensors": [(T0 + timedelta(hours=1),), (T0 + timedelta(hours=5),)],
    "metadata->>'camera' = ANY": [(T0 + timedelta(hours=1, seconds=40), "front_door")],
    "FROM telemetry.climate": ([("patio", "fp300", T0 + timedelta(hours=h), 80.0) for h in range(10)]
                               + [("patio", "homekit", T0 + timedelta(hours=h), 90.0) for h in range(10)]),
    "INSERT INTO crain_square_residuals": [(DAY,)],
}


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized_and_no_secrets(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_hostile_subject_stays_a_parameter(self):
        evil = "x'; DROP TABLE source_ledger; --"
        cur = FakeCur({"INSERT INTO crain_square_residuals": []})
        C.write(cur, DAY, [{"invariant": "temp_pair", "subject": evil, "residual": 1.0, "n": 3, "flag": None,
                            "out_of_control": False, "detail": {}}])
        ins = [(s, p) for s, p in cur.sql if "INSERT INTO crain_square_residuals" in s][0]
        self.assertNotIn(evil, ins[0])
        self.assertIn(evil, ins[1])

    def test_invariants_never_name_people(self):
        self.assertNotIn("person=", SRC)
        self.assertNotIn("presence_state", SRC)


class TestPerformance(unittest.TestCase):
    def test_full_day_motion_and_mmwave_under_bound(self):
        cams = {"c": sorted(T0 + timedelta(seconds=7 * i) for i in range(10000))}
        ev = [T0 + timedelta(minutes=5 * i) for i in range(280)]
        per = {r: {T0 + timedelta(minutes=i): (i + k) % 4 == 0 for i in range(1440)} for k, r in enumerate("abcdef")}
        t = time.monotonic()
        C.motion_vs_camera("s", ev, cams, T0, T0 + timedelta(days=1))
        C.mmwave_identity(per)
        self.assertLess(time.monotonic() - t, 5.0)


class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("connection refused")
            return mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=flaky):
            C.W.connect(_sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)

    def test_query_failure_fails_open(self):
        # RETRY GAP: _q — a failed read is not retried; it degrades to "nothing known"
        with mock.patch("builtins.print"):
            self.assertEqual(C.history(FakeCur(boom=True), DAY), {})
            self.assertEqual(C.load_all(FakeCur(boom=True), T0, T0 + timedelta(days=1), C.MOTION_PAIRS,
                                        C.TEMP_PAIRS)[-1]["residual"], None)

    def test_config_failure_uses_defaults(self):
        with mock.patch("builtins.print"):
            self.assertEqual(C.config(FakeCur(boom=True)), (C.MOTION_PAIRS, C.TEMP_PAIRS))


class TestUnit(unittest.TestCase):
    def test_mmwave_identity_catches_copy(self):
        per = {}
        for t, r, o in mm_rows():
            per.setdefault(r, {})[t] = o
        res = {r["subject"]: r for r in C.mmwave_identity(per)}
        self.assertEqual(res["master_bedroom|patio"]["residual"], 1.0)
        self.assertIn("duplicate of master_bedroom", res["master_bedroom|patio"]["flag"])
        self.assertIsNone(res["master_bedroom|office"]["flag"])
        self.assertEqual(C.mmwave_identity({}), [])

    def test_motion_no_events(self):
        r = C.motion_vs_camera("s", [], {"c": []}, T0, T0 + timedelta(hours=1))[0]
        self.assertIsNone(r["residual"])
        self.assertEqual(r["n"], 0)

    def test_temp_pair_edges(self):
        sa = {h: 70.0 for h in range(8)}
        self.assertIsNone(C.temp_pair("a", "b", sa, {})["residual"])
        self.assertIn("identical", C.temp_pair("a", "b", sa, dict(sa))["flag"])
        self.assertEqual(C.temp_pair("a", "b", sa, {h: 72.0 for h in range(8)})["residual"], -2.0)

    def test_control_needs_history(self):
        self.assertFalse(C.control({"residual": 9.0, "detail": {}}, [1.0] * 3)["out_of_control"])
        self.assertTrue(C.control({"residual": 9.0, "detail": {}}, [1.0] * 10)["out_of_control"])
        self.assertFalse(C.control({"residual": None, "detail": {}}, [1.0] * 10)["out_of_control"])

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(C.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_imported(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("from nova_cardinal import mmwave_health", SRC)
        self.assertIn("from nova_cardinal import record_outcome", SRC)
        self.assertIn("from nova_buick8_log import log_unexplained", SRC)
        self.assertIn("W.episodes(", SRC)

    def test_config_keys(self):
        cur = FakeCur({"FROM service_config": []})
        C.config(cur)
        keys = [p for s, p in cur.sql if "service_config" in s]
        self.assertEqual(keys, [("crain_square", "motion_pairs"), ("crain_square", "temp_pairs")])

    def test_camera_outcome_goes_to_cardinal_once(self):
        r = {"invariant": "motion_vs_camera", "subject": "s|front_door", "residual": 0.5, "n": 2, "flag": None,
             "out_of_control": False, "detail": {"camera": "front_door", "hit_rate": 0.5}}
        with mock.patch("nova_cardinal.record_outcome") as ro:
            self.assertEqual(C.write(FakeCur({"INSERT INTO crain_square_residuals": [(DAY,)]}), DAY, [r]), (1, 1))
            self.assertEqual(C.write(FakeCur({"INSERT INTO crain_square_residuals": []}), DAY, [r]), (0, 0))
        args, kw = ro.call_args
        self.assertEqual(args[1:4], ("camera:front_door", "camera", 0.5))
        self.assertEqual(kw["recorded_by"], "crain_square")

    def test_persistent_goes_to_buick8_without_cause(self):
        r = {"invariant": "temp_pair", "subject": "a|b", "residual": 9.0, "detail": {}, "persistent": True}
        with mock.patch("nova_buick8_log.log_unexplained") as lu:
            self.assertEqual(C.report(FakeCur(), DAY, [r, dict(r, persistent=False)]), 1)
        args, kw = lu.call_args
        self.assertEqual(args[0], "house_invariant")
        self.assertEqual(kw["source"], "crain_square")
        self.assertNotIn("cause", kw)


class TestFunctional(unittest.TestCase):
    def _run(self, dry, routes=ROUTES):
        cur = FakeCur(routes)
        with mock.patch.object(C.W, "connect", return_value=fake_conn(cur)), \
                mock.patch("nova_cardinal.record_outcome") as ro, \
                mock.patch("nova_buick8_log.log_unexplained") as lu, mock.patch("builtins.print"):
            rows = C.run(DAY, dry=dry)
        return cur, rows, ro, lu

    def test_run_writes_residuals_and_feeds_cardinal(self):
        cur, rows, ro, lu = self._run(False)
        inv = {r["invariant"] for r in rows}
        self.assertEqual(inv, {"mmwave_identity", "motion_vs_camera", "temp_pair"})
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS crain_square_residuals" in s for s, _ in cur.sql))
        self.assertEqual(sum("INSERT INTO crain_square_residuals" in s for s, _ in cur.sql), len(rows))
        cams = {c[0][1] for c in ro.call_args_list}
        self.assertEqual(cams, {"camera:front_door", "camera:front_yard_alt"})
        lu.assert_not_called()      # no history, so nothing is persistent
        fd = [r for r in rows if r["subject"].endswith("|front_door")][0]
        self.assertEqual(fd["detail"]["hit_rate"], 0.5)

    def test_dry_run_writes_nothing(self):
        cur, rows, ro, lu = self._run(True)
        self.assertTrue(rows)
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE", "DELETE")))
        ro.assert_not_called()
        lu.assert_not_called()

    def test_persistent_after_two_days_out(self):
        hist = [("temp_pair", "patio/fp300|patio/homekit", DAY - timedelta(days=d), 0.0, d == 1)
                for d in range(10, 0, -1)]
        cur, rows, ro, lu = self._run(False, dict(ROUTES, **{"FROM crain_square_residuals WHERE day >=": hist}))
        self.assertEqual(lu.call_count, 1)
        self.assertEqual(lu.call_args[0][1], "temp_pair:patio/fp300|patio/homekit")


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_crain_square.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_crain_square.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
