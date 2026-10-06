#!/usr/bin/env python3
"""Tests for nova_maintenance.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). PG is mocked. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mm = _load("nova_maintenance_t", SCRIPTS / "nova_maintenance.py")
SRC = (SCRIPTS / "nova_maintenance.py").read_text()


class _Cur:
    def __init__(self, row=None):
        self.row = row; self.sql = []

    def __enter__(self): return self
    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row=None):
        self.cur = _Cur(row); self.commits = 0

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def cursor(self): return self.cur
    def commit(self): self.commits += 1


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_parameterized_and_reason_is_json_encoded(self):
        conn = _Conn()
        evil = "x'); SELECT pg_sleep(9); --"
        with mock.patch.object(mm, "_conn", return_value=conn), mock.patch("builtins.print"):
            mm.start(5, evil)
        sql, params = conn.cur.sql[0]
        self.assertNotIn(evil, sql)
        self.assertEqual(json.loads(params[2])["reason"], evil)

    def test_probe_and_strix_never_muted(self):
        self.assertNotIn("probe", mm.SECURITY_CATEGORIES)
        self.assertNotIn("strix", mm.SECURITY_CATEGORIES)
        self.assertIn("wazuh", mm.SECURITY_CATEGORIES)


class TestPerformance(unittest.TestCase):
    def test_window_predicate_10k(self):
        now = datetime.now(timezone.utc)
        s = {"active": True, "until": (now + timedelta(hours=1)).isoformat()}
        t0 = time.perf_counter()
        for _ in range(10_000):
            mm._window_active(s, now)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_pg_down_fails_open(self):
        # RETRY GAP: get_state()/_conn — one attempt; any error => "not in maintenance" (never mutes alerts)
        with mock.patch.object(mm, "_conn", side_effect=RuntimeError("pg down")) as c:
            self.assertEqual(mm.get_state(), {"active": False})
            self.assertFalse(mm.is_active())
        self.assertEqual(c.call_count, 2)


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with mock.patch("builtins.print"):
            mm._selftest()

    def test_window_edges(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertFalse(mm._window_active({}, now))
        self.assertTrue(mm._window_active({"active": True, "until": None}, now))
        self.assertFalse(mm._window_active({"active": True, "until": "2025-12-31T00:00:00+00:00"}, now))


class TestIntegration(unittest.TestCase):
    def test_get_state_reads_service_config_key(self):
        conn = _Conn(row=('{"active": true}',))
        with mock.patch.object(mm, "_conn", return_value=conn):
            self.assertEqual(mm.get_state(), {"active": True})
        sql, params = conn.cur.sql[0]
        self.assertIn("service_config", sql)
        self.assertEqual(params, ("nova", "maintenance_mode"))

    def test_jsonb_dict_passthrough_and_missing_row(self):
        with mock.patch.object(mm, "_conn", return_value=_Conn(row=({"active": True, "until": "x"},))):
            self.assertEqual(mm.get_state()["until"], "x")
        with mock.patch.object(mm, "_conn", return_value=_Conn(row=None)):
            self.assertEqual(mm.get_state(), {"active": False})


class TestFunctional(unittest.TestCase):
    def test_start_then_is_active(self):
        conn = _Conn()
        with mock.patch.object(mm, "_conn", return_value=conn), mock.patch("builtins.print"):
            mm.start(30, "strix run")
        stored = json.loads(conn.cur.sql[0][1][2])
        self.assertTrue(stored["active"])
        self.assertEqual(conn.commits, 1)
        with mock.patch.object(mm, "_conn", return_value=_Conn(row=(stored,))):
            self.assertTrue(mm.is_active())

    def test_stop_writes_inactive(self):
        conn = _Conn()
        with mock.patch.object(mm, "_conn", return_value=conn), mock.patch("builtins.print"):
            mm.stop()
        self.assertFalse(json.loads(conn.cur.sql[0][1][2])["active"])

    def test_expired_window_is_inactive(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        with mock.patch.object(mm, "_conn", return_value=_Conn(row=({"active": True, "until": past},))):
            self.assertFalse(mm.is_active())


class TestFrame(unittest.TestCase):
    def test_selftest_cli_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_maintenance.py"), "selftest"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest OK", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
