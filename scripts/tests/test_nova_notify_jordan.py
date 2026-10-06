#!/usr/bin/env python3
"""Tests for nova_notify_jordan.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nj = _load("nova_notify_jordan_t", SCRIPTS / "nova_notify_jordan.py")
SRC = (SCRIPTS / "nova_notify_jordan.py").read_text()
# stub the module's own outbound handle at load: nothing can ever reach Slack/Discord
nj.nova_config = types.SimpleNamespace(post_both=mock.MagicMock(), SLACK_CHAN="C_TEST_CHAT")

TS = datetime(2026, 10, 1, 9, 30)


class _Cur:
    def __init__(self, rows):
        self.rows = rows; self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.cur = _Cur(rows); self.autocommit = False

    def cursor(self):
        return self.cur


def _run(rows, argv=()):
    conn = _Conn(rows)
    nj.nova_config.post_both.reset_mock()
    with mock.patch.object(nj.psycopg2, "connect", return_value=conn) as c, \
         mock.patch.object(sys, "argv", ["nova_notify_jordan.py", *argv]):
        rc = nj.main()
    return rc, conn, c


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        _, conn, _ = _run([(1, TS, "t", "m")])
        for sql, params in conn.cur.sql:
            self.assertIn("%s", sql)
            self.assertIsNotNone(params)


class TestPerformance(unittest.TestCase):
    def test_bundle_10k_rows(self):
        rows = [(i, TS, "topic", f"msg {i}") for i in range(10_000)]
        t0 = time.perf_counter()
        out = nj.bundle(rows)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertIn("(10000)", out)


class TestRetry(unittest.TestCase):
    def test_pg_down_is_single_attempt(self):
        # RETRY GAP: main()/psycopg2.connect — one attempt; a launchd job, the next run is the retry.
        # The failure escapes before any post, so nothing half-delivered is marked sent.
        with mock.patch.object(nj.psycopg2, "connect", side_effect=nj.psycopg2.OperationalError("down")) as c, \
             mock.patch.object(sys, "argv", ["x"]):
            nj.nova_config.post_both.reset_mock()
            with self.assertRaises(nj.psycopg2.OperationalError):
                nj.main()
        self.assertEqual(c.call_count, 1)
        nj.nova_config.post_both.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_bundle_with_and_without_topic(self):
        out = nj.bundle([(1, TS, "dns", " hello "), (2, TS, "", "bare")])
        self.assertIn("(2)", out)
        self.assertIn("[dns] hello", out)
        self.assertIn("_Thu 09:30_ bare", out)

    def test_bundle_empty(self):
        self.assertEqual(nj.bundle([]), "*Things I noticed since we last talked* (0):")

    def test_limits(self):
        self.assertGreater(nj.MAX_ITEMS, 0)
        self.assertGreater(nj.MAX_AGE_DAYS, 0)


class TestIntegration(unittest.TestCase):
    def test_uses_reach_audiences_and_dsn(self):
        import nova_reach
        self.assertIs(nj.DIRECT_AUDIENCES, nova_reach.DIRECT_AUDIENCES)
        _, conn, c = _run([])
        self.assertEqual(c.call_args[0][0], nova_reach.OPS_DSN)
        sql, params = conn.cur.sql[0]
        self.assertIn("reach_log", sql)
        self.assertIn("status='held'", sql)
        self.assertEqual(params, (list(nova_reach.DIRECT_AUDIENCES), nj.MAX_AGE_DAYS, nj.MAX_ITEMS))


class TestFunctional(unittest.TestCase):
    def test_delivers_and_marks_sent(self):
        rc, conn, _ = _run([(7, TS, "x", "one"), (9, TS, "", "two")])
        self.assertEqual(rc, 0)
        nj.nova_config.post_both.assert_called_once()
        self.assertEqual(nj.nova_config.post_both.call_args[1]["slack_channel"], "C_TEST_CHAT")
        sql, params = conn.cur.sql[-1]
        self.assertIn("SET status='sent'", sql)
        self.assertEqual(params, ([7, 9],))

    def test_empty_drawer_posts_nothing(self):
        rc, conn, _ = _run([])
        self.assertEqual(rc, 0)
        nj.nova_config.post_both.assert_not_called()
        self.assertEqual(len(conn.cur.sql), 1)

    def test_dry_run_sends_and_marks_nothing(self):
        rc, conn, _ = _run([(7, TS, "x", "one")], argv=["--dry-run"])
        self.assertEqual(rc, 0)
        nj.nova_config.post_both.assert_not_called()
        self.assertFalse(any("UPDATE" in s for s, _ in conn.cur.sql))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_notify_jordan.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
