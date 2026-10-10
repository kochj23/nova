#!/usr/bin/env python3
"""Tests for nova_chronic_failures.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Since 2026-10-09 (organ-audit merge M2) main() is a thin wrapper around
nova_task_sentinel.py --daily; these tests drive the wrapper end to end with PG faked."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_chronic_failures.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("chronicf", SCRIPTS / "nova_chronic_failures.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cf = _load()
cf.log = lambda *a, **k: None


class _Cur:
    """Answers main()'s queries by substring and records every execute."""
    def __init__(self, runs, queued=False):
        self.runs = runs; self.queued = queued; self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params)); self._last = sql

    def fetchall(self):
        return self.runs

    def fetchone(self):
        if "FROM claude_queue" in self._last:
            return (1,) if self.queued else None
        if "FROM scheduler_runs" in self._last:
            return ("failed", "Traceback: boom")
        if "claude_sessions" in self._last:
            return ("sess-1",)
        return None


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.autocommit = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _chronic_rows(task="prober"):
    t = date.today()          # computed at call time, never pinned
    return [(task, t - timedelta(days=i), 9) for i in (1, 2, 3)]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))
        hostile = "x' OR '1'='1"
        cur = _Cur(_chronic_rows(hostile))
        with patch.object(cf.psycopg2, "connect", return_value=_Conn(cur)), patch.object(sys, "argv", ["x"]):
            cf.main()
        ins = [p for s, p in cur.sql if s.startswith("INSERT INTO claude_queue")]
        self.assertIn(hostile, ins[0][1])      # the hostile name rides as a bound value
        self.assertTrue(all(hostile not in s for s, _ in cur.sql))

    def test_only_writes_are_session_registration_and_claude_queue(self):
        self.assertEqual(re.findall(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+(\w+)", SRC), [])   # wrapper writes nothing itself
        daily = (SCRIPTS / "nova_task_sentinel.py").read_text().split("def run_daily(")[1].split("\ndef ")[0]
        writes = re.findall(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+(\w+)", daily)
        self.assertEqual(writes, [("INSERT INTO", "claude_sessions"), ("INSERT INTO", "claude_queue")])


class TestPerformance(unittest.TestCase):
    def test_chronic_on_10k_rows_fast(self):
        t = date.today()
        rows = [(f"t{i % 3000}", t - timedelta(days=1 + i % 4), 3 + i % 9) for i in range(10_000)]
        t0 = time.perf_counter()
        cf.chronic(rows, t)
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestRetry(unittest.TestCase):
    def test_connect_is_one_shot(self):
        # RETRY GAP: main()/psycopg2.connect — single attempt with connect_timeout=8; the scheduler reruns it
        calls = []
        def boom(*a, **k):
            calls.append(k.get("connect_timeout")); raise cf.psycopg2.OperationalError("down")
        with patch.object(cf.psycopg2, "connect", side_effect=boom), patch.object(sys, "argv", ["x"]):
            with self.assertRaises(cf.psycopg2.OperationalError):
                cf.main()
        self.assertEqual(calls, [8])


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        cf.selftest()

    def test_edges(self):
        t = date.today()
        self.assertEqual(cf.chronic([], t), {})
        rows = [("a", t - timedelta(days=1), 6), ("a", t - timedelta(days=2), 6)]
        self.assertEqual(cf.chronic(rows, t), {})                    # missing a day
        self.assertEqual(set(cf.chronic(rows, t, days=2)), {"a"})
        self.assertEqual(cf.chronic(rows, t, fail_per_day=6, days=2), {})


class TestIntegration(unittest.TestCase):
    def test_queries_shared_scheduler_runs_and_dedups_on_prefix(self):
        cur = _Cur(_chronic_rows(), queued=True)
        with patch.object(cf.psycopg2, "connect", return_value=_Conn(cur)), patch.object(sys, "argv", ["x"]):
            cf.main()
        self.assertIn("FROM scheduler_runs", cur.sql[0][0])
        self.assertEqual(cur.sql[0][1], (cf.DAYS + 1,))
        dedup = [p for s, p in cur.sql if "FROM claude_queue" in s][0]
        self.assertEqual(dedup, ("Chronic failure: prober %", cf.OPEN))
        self.assertFalse(any(s.startswith("INSERT") for s, _ in cur.sql))


class TestFunctional(unittest.TestCase):
    def test_main_queues_one_item(self):
        cur = _Cur(_chronic_rows())
        with patch.object(cf.psycopg2, "connect", return_value=_Conn(cur)), patch.object(sys, "argv", ["x"]):
            cf.main()
        ins = [p for s, p in cur.sql if s.startswith("INSERT INTO claude_queue")]
        self.assertEqual(len(ins), 1)
        sid, desc, ctx = ins[0]
        self.assertEqual(sid, "sess-1")
        self.assertTrue(desc.startswith("Chronic failure: prober"))
        self.assertIn("Traceback: boom", ctx)
        # the queue session is registered in claude_sessions BEFORE the insert (FK)
        order = [s.split("(")[0].strip() for s, _ in cur.sql if s.startswith("INSERT")]
        self.assertEqual(order, ["INSERT INTO claude_sessions", "INSERT INTO claude_queue"])
        self.assertEqual([p for s, p in cur.sql if s.startswith("INSERT INTO claude_sessions")], [("sess-1",)])

    def test_wrapper_runs_task_sentinel_daily(self):
        import nova_task_sentinel
        with patch.object(nova_task_sentinel, "run_daily", return_value=0) as rd, \
             patch.object(sys, "argv", ["x", "--dry-run"]):
            cf.main()
        rd.assert_called_once_with(dry_run=True)

    def test_dry_run_writes_nothing(self):
        cur = _Cur(_chronic_rows())
        with patch.object(cf.psycopg2, "connect", return_value=_Conn(cur)), \
             patch.object(sys, "argv", ["x", "--dry-run"]):
            cf.main()
        self.assertFalse(any(s.startswith("INSERT") for s, _ in cur.sql))


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_chronic_failures.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest ok", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_chronic_failures"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
