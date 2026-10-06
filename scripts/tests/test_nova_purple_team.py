#!/usr/bin/env python3
"""Tests for nova_purple_team.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The harness fires UDP syslog at a detector and opens maintenance windows: every socket,
maintenance call, PG query and notify is mocked here, and the default (--list) path is proven
to fire nothing."""
import datetime as _dt
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_purple_team.py"
SRC = PATH.read_text()
_TMP = Path(tempfile.mkdtemp(prefix="purple_test_"))


def _load():
    spec = importlib.util.spec_from_file_location("nova_purple_team_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pt = _load()
pt.LOG_FILE = _TMP / "purple.log"
pt.notify = mock.MagicMock()


class _Cur:
    def __init__(self, hits):
        self.hits = list(hits); self.params = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.sql = sql; self.params.append(params)

    def fetchone(self):
        return self.hits.pop(0) if self.hits else None


class _World(unittest.TestCase):
    """Mocks the socket, maintenance module, PG and clock for every test."""
    def setUp(self):
        pt.notify.reset_mock(side_effect=True)
        self.maint = types.SimpleNamespace(start=mock.MagicMock(), stop=mock.MagicMock())
        self.sock = mock.MagicMock()
        self.cur = _Cur([])
        conn = mock.Mock(); conn.cursor.return_value = self.cur
        self.conn = conn
        ps = {"sock": mock.patch.object(pt.socket, "socket", return_value=self.sock),
              "maint": mock.patch.dict(sys.modules, {"nova_maintenance": self.maint}),
              "pg": mock.patch.object(pt.psycopg2, "connect", return_value=conn),
              "sleep": mock.patch.object(pt.time, "sleep"),
              "out": mock.patch("sys.stdout", new_callable=io.StringIO)}
        self.m = {k: p.start() for k, p in ps.items()}
        self.addCleanup(lambda: [p.stop() for p in ps.values()])

    def sent(self):
        return [c[0][0].decode() for c in self.sock.sendto.call_args_list]


class TestSecurity(_World):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_fired_traffic_is_synthetic_and_attributable(self):
        for t in pt.CATALOG:
            t["fire"]("127.0.0.1")
        lines = self.sent()
        self.assertTrue(lines)
        for l in lines:
            self.assertIn(pt.SIM_HOST, l)
            for ip in re.findall(r"\b\d+\.\d+\.\d+\.\d+\b", l):
                self.assertTrue(ip.startswith("203.0.113."), ip)   # RFC 5737 TEST-NET only
        self.assertEqual({c[0][1] for c in self.sock.sendto.call_args_list}, {("127.0.0.1", pt.SYSLOG_PORT)})

    def test_detection_query_parameterized_and_scoped_to_sim(self):
        pt._detected_since(self.conn, "auth_failure", 1.0)
        self.assertNotIn("auth_failure", self.cur.sql)
        self.assertEqual(self.cur.params[0], ("auth_failure", 1.0, f"%{pt.SIM_HOST}%", f"%{pt.SIM_HOST}%", f"%{pt.TESTNET_IP}%"))

    def test_list_is_default_and_fires_nothing(self):
        with mock.patch.object(sys, "argv", ["x"]):
            self.assertEqual(pt.main(), 0)
        self.sock.sendto.assert_not_called()
        self.maint.start.assert_not_called()
        self.m["pg"].assert_not_called()


class TestPerformance(_World):
    def test_scorecard_on_10k_results(self):
        res = [{"id": f"t{i}", "attack": "A", "outcome": "CAUGHT" if i % 2 else "MISSED", "latency_s": 1.0}
               for i in range(10_000)]
        t0 = time.perf_counter()
        pt._scorecard(res)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(pt.notify.call_args[1]["meta"], {"caught": 5000, "scored": 10000})


class TestRetry(_World):
    def test_detection_poll_retries_until_hit(self):
        hit = {"id": 9, "ts": _dt.datetime.now() + _dt.timedelta(seconds=2), "title": "Suspicious DNS"}
        self.cur.hits = [None, None, hit]
        tech = dict(pt.CATALOG[2])
        out = pt.run([tech], "127.0.0.1")
        self.assertEqual(out[0]["outcome"], "CAUGHT")
        self.assertEqual(len(self.cur.params), 3)                      # polled twice, caught on third
        self.assertEqual([c[0][0] for c in self.m["sleep"].call_args_list], [3, 3])

    def test_window_expiry_misses_and_fire_error_recorded(self):
        tech = dict(pt.CATALOG[2], window_s=0)
        self.assertEqual(pt.run([tech], "x")[0]["outcome"], "MISSED")
        boom = dict(pt.CATALOG[2], fire=mock.Mock(side_effect=OSError("unreachable")))
        r = pt.run([boom], "x")[0]
        self.assertEqual(r["outcome"], "error")
        self.assertIn("unreachable", r["detail"])


class TestUnit(_World):
    def test_syslog_line_format(self):
        pt._syslog("10.9.9.9", "hello", facility_severity=13, tag="t", pid=1)
        line = self.sent()[0]
        self.assertRegex(line, r"^<13>\w{3} [ \d]\d \d\d:\d\d:\d\d PURPLE-TEAM-SIM t\[1\]: hello$")
        self.sock.close.assert_called_once()

    def test_scorecard_levels(self):
        pt._scorecard([{"id": "a", "attack": "x", "outcome": "CAUGHT", "latency_s": 2}])
        self.assertEqual(pt.notify.call_args[1]["level"], "info")
        pt._scorecard([{"id": "a", "attack": "x", "outcome": "MISSED"},
                       {"id": "b", "attack": "y", "outcome": "skipped"}])
        self.assertEqual(pt.notify.call_args[1]["level"], "warning")
        self.assertIn("0/1 techniques caught, 1 skipped/errored", pt.notify.call_args[1]["body"])

    def test_unknown_technique_rc2(self):
        with mock.patch.object(sys, "argv", ["x", "--run", "--technique", "nope"]), \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(pt.main(), 2)
        self.sock.sendto.assert_not_called()


class TestIntegration(_World):
    def test_catalog_categories_match_detector(self):
        self.assertEqual([t["category"] for t in pt.CATALOG],
                         ["auth_failure", "sensitive_access", "suspicious_dns", "off_hours_auth"])
        self.assertTrue(all(callable(t["fire"]) and t["window_s"] > 0 for t in pt.CATALOG))

    def test_maintenance_window_wraps_run_even_on_error(self):
        self.cur.execute = mock.Mock(side_effect=RuntimeError("pg gone"))
        with self.assertRaises(RuntimeError):
            pt.run([dict(pt.CATALOG[2])], "x")
        self.maint.start.assert_called_once()
        self.maint.stop.assert_called_once()
        self.conn.close.assert_called_once()


class TestFunctional(_World):
    def test_run_one_technique_without_maintenance(self):
        hit = {"id": 4, "ts": _dt.datetime.now() + _dt.timedelta(seconds=1), "title": "Brute force"}
        self.cur.hits = [hit]
        with mock.patch.object(sys, "argv", ["x", "--run", "--technique", "auth_brute_force", "--no-maintenance"]):
            self.assertEqual(pt.main(), 0)
        self.assertEqual(self.sock.sendto.call_count, 7)
        self.maint.start.assert_not_called()
        self.assertIn("1/1 techniques caught", pt.notify.call_args[1]["body"])

    def test_time_gated_technique_skipped_outside_window(self):
        gated = dict(pt.CATALOG[3], time_gated=lambda: False)
        out = pt.run([gated], "x")
        self.assertEqual(out[0]["outcome"], "skipped")
        self.sock.sendto.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(PATH), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--no-maintenance", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_purple_team"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
