#!/usr/bin/env python3
"""Tests for nova_autonomy_rules_audit.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Offline: psycopg2.connect is never reached (a fake connection is injected or connect is patched).
"""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_autonomy_rules_audit.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("nova_autonomy_rules_audit", SCRIPT)
A = importlib.util.module_from_spec(spec)
spec.loader.exec_module(A)

ORPHANS_2026_10_08 = ["list_watchers", "quarantine_device", "run_sandboxed", "secret_check",
                      "secret_list", "secret_set", "unquarantine_device"]


class _Cur:
    def __init__(self, rows):
        self.rows, self.sql, self.params, self.rowcount = rows, [], [], 0

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params)
        if sql.startswith("DELETE"):
            self.rowcount = len(params[0])

    def fetchall(self):
        return [(r,) for r in self.rows]


class _Conn:
    def __init__(self, rows):
        self.cur, self.commits, self.closed = _Cur(rows), 0, False

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


KNOWN = {"run_script", "send_message", "set_dial", "camera_snap", "web_search"}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_delete_is_parameterized_with_explicit_list(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertIn("DELETE FROM autonomy_rules WHERE action_type = ANY(%s)", SRC)

    def test_unreadable_registry_never_marks_everything_orphan(self):
        # fail safe: if the gateway registry can't load, nothing is pruned
        self.assertEqual(A.orphan_rules(["run_script", "send_message"], set()), [])
        conn = _Conn(["run_script"])
        with mock.patch.object(A, "known_tools", return_value=set()):
            self.assertEqual(A.run(prune=True, conn=conn), [])
        self.assertFalse(any(s.startswith("DELETE") for s in conn.cur.sql))


class TestPerformance(unittest.TestCase):
    def test_orphan_scan_10k_rules_fast(self):
        rules = [f"tool_{i}" for i in range(10_000)] + list(KNOWN)
        t0 = time.perf_counter()
        out = A.orphan_rules(rules, KNOWN)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(len(out), 10_000)


class TestRetry(unittest.TestCase):
    def test_connect_retries_then_succeeds(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise RuntimeError("pg down")
            return "conn"
        with mock.patch("psycopg2.connect", side_effect=flaky), mock.patch.object(A.time, "sleep") as sl:
            self.assertEqual(A._connect(), "conn")
        self.assertEqual(calls["n"], 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [1.0, 2.0])   # exponential backoff

    def test_connect_gives_up_loudly(self):
        with mock.patch("psycopg2.connect", side_effect=RuntimeError("pg down")), \
                mock.patch.object(A.time, "sleep"):
            with self.assertRaises(RuntimeError):           # never fails silently
                A._connect()


class TestUnit(unittest.TestCase):
    def test_orphan_rules_edges(self):
        self.assertEqual(A.orphan_rules([], KNOWN), [])
        self.assertEqual(A.orphan_rules(None, KNOWN), [])
        self.assertEqual(A.orphan_rules(["*", "", None, "run_script"], KNOWN), [])
        self.assertEqual(A.orphan_rules(["b", "a", "a", "run_script"], KNOWN), ["a", "b"])

    def test_the_2026_10_08_orphans_are_detected(self):
        self.assertEqual(A.orphan_rules(ORPHANS_2026_10_08 + sorted(KNOWN), KNOWN), ORPHANS_2026_10_08)


class TestIntegration(unittest.TestCase):
    def test_known_tools_includes_registry_and_extended(self):
        names = A.known_tools()
        for t in ("run_script", "send_message", "set_dial", "camera_snap", "ui_click"):
            self.assertIn(t, names)
        for t in ORPHANS_2026_10_08:                         # none of the pruned names came back
            self.assertNotIn(t, names)

    def test_reads_autonomy_rules_table(self):
        conn = _Conn([])
        with mock.patch.object(A, "known_tools", return_value=KNOWN):
            A.run(conn=conn)
        self.assertEqual(conn.cur.sql[0], "SELECT DISTINCT action_type FROM autonomy_rules")


class TestFunctional(unittest.TestCase):
    def test_report_only_does_not_delete(self):
        conn = _Conn(["run_script", "secret_set"])
        with mock.patch.object(A, "known_tools", return_value=KNOWN):
            self.assertEqual(A.run(conn=conn), ["secret_set"])
        self.assertEqual(len(conn.cur.sql), 1)
        self.assertEqual(conn.commits, 0)
        self.assertTrue(conn.closed)

    def test_prune_deletes_only_orphans(self):
        conn = _Conn(["run_script", "secret_set", "list_watchers"])
        with mock.patch.object(A, "known_tools", return_value=KNOWN):
            A.run(prune=True, conn=conn)
        self.assertEqual(conn.cur.params[-1], (["list_watchers", "secret_set"],))
        self.assertEqual(conn.commits, 1)

    def test_db_error_closes_connection_and_raises(self):
        conn = _Conn([])
        conn.cur.execute = mock.Mock(side_effect=RuntimeError("boom"))
        with mock.patch.object(A, "known_tools", return_value=KNOWN):
            with self.assertRaises(RuntimeError):
                A.run(conn=conn)
        self.assertTrue(conn.closed)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=SCRIPTS, capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--prune", r.stdout)

    def test_import_does_not_connect(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            s = importlib.util.spec_from_file_location("audit_probe", SCRIPT)
            m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
        self.assertTrue(callable(m.orphan_rules))


if __name__ == "__main__":
    unittest.main()
