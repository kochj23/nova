#!/usr/bin/env python3
"""7-category tests for nova_claude_lease_reaper.py (Security, Performance, Retry, Unit, Integration,
Functional, Frame). PostgreSQL and nova_notify are mocked; nothing reaches the real queue or Slack.
The SQL it calls (claude_reap) is covered by test_claude_fleet_coordination_sql_7cat.py.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_claude_lease_reaper_7cat.py
"""
import importlib.util
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2

SCRIPT = Path(__file__).resolve().parents[1] / "nova_claude_lease_reaper.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("reaper_7cat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    stub = types.ModuleType("nova_notify")
    stub.notify = MagicMock()
    with patch.dict(sys.modules, {"nova_notify": stub}):
        spec.loader.exec_module(mod)
    return mod


def _conn(rows):
    cur = MagicMock()
    cur.fetchall.return_value = rows
    cur.__enter__.return_value = cur
    conn = MagicMock()
    conn.__enter__.return_value = conn
    conn.cursor.return_value = cur
    return conn, cur


class TestSecurity(unittest.TestCase):
    def test_only_calls_the_reap_function_no_dynamic_sql(self):
        self.assertEqual(SRC.count("cur.execute("), 1)
        self.assertNotRegex(SRC, r'execute\(f"')

    def test_no_secrets_or_home_paths(self):
        self.assertNotRegex(SRC, r"password\s*=|/Users/[a-z]|xox[bp]-")

    def test_hostile_description_is_trimmed_and_not_executed(self):
        m = _load()
        conn, _ = _conn([(1, "'; DELETE FROM claude_queue; --" + "A" * 5000, "h/s")])
        with patch.object(m.psycopg2, "connect", return_value=conn):
            m.main()
        body = m.notify.call_args.kwargs["body"]
        self.assertLess(len(body), 400)
        self.assertEqual(conn.cursor.return_value.execute.call_count, 1)


class TestPerformance(unittest.TestCase):
    def test_connect_has_timeout(self):
        m = _load()
        conn, _ = _conn([])
        with patch.object(m.psycopg2, "connect", return_value=conn) as c:
            m.main()
        self.assertEqual(c.call_args.kwargs.get("connect_timeout"), 10)

    def test_many_rows_handled_quickly(self):
        m = _load()
        conn, _ = _conn([(i, "d", "h/s") for i in range(5000)])
        t = time.perf_counter()
        with patch.object(m.psycopg2, "connect", return_value=conn):
            m.main()
        self.assertLess(time.perf_counter() - t, 2.0)
        self.assertEqual(m.notify.call_count, 5000)

    def test_retry_is_bounded(self):
        m = _load()
        with patch.object(m.psycopg2, "connect", side_effect=psycopg2.OperationalError("x")) as c, \
                patch.object(m.time, "sleep") as sl:
            with self.assertRaises(psycopg2.OperationalError):
                m._reap()
        self.assertEqual(c.call_count, 3)
        self.assertLessEqual(sum(a.args[0] for a in sl.call_args_list), 30)


class TestRetry(unittest.TestCase):
    def test_pg_blip_retried_with_backoff(self):
        m = _load()
        conn, _ = _conn([(4, "d", "h/s")])
        with patch.object(m.psycopg2, "connect", side_effect=[psycopg2.OperationalError("failover"), conn]), \
                patch.object(m.time, "sleep") as sl:
            m.main()
        sl.assert_called_once_with(5)
        m.notify.assert_called_once()

    def test_backoff_grows(self):
        m = _load()
        with patch.object(m.psycopg2, "connect", side_effect=psycopg2.OperationalError("x")), \
                patch.object(m.time, "sleep") as sl, self.assertRaises(psycopg2.OperationalError):
            m._reap()
        self.assertEqual([c.args[0] for c in sl.call_args_list], [5, 10])

    def test_notify_failure_is_not_silent_and_others_still_sent(self):
        m = _load()
        conn, _ = _conn([(1, "a", "h/s"), (2, "b", "h/s")])
        m.notify.side_effect = [RuntimeError("slack down"), None]
        with patch.object(m.psycopg2, "connect", return_value=conn), self.assertRaises(SystemExit) as e:
            m.main()
        self.assertEqual(e.exception.code, 1)
        self.assertEqual(m.notify.call_count, 2)

    def test_non_operational_errors_not_retried(self):
        m = _load()
        with patch.object(m.psycopg2, "connect", side_effect=psycopg2.ProgrammingError("no fn")) as c:
            with self.assertRaises(psycopg2.ProgrammingError):
                m._reap()
        self.assertEqual(c.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_null_description_tolerated(self):
        m = _load()
        conn, _ = _conn([(3, None, "h/s")])
        with patch.object(m.psycopg2, "connect", return_value=conn):
            m.main()
        self.assertTrue(m.notify.call_args.kwargs["body"].startswith("\nwas claimed by h/s"))

    def test_dedup_key_per_task_and_holder(self):
        m = _load()
        conn, _ = _conn([(3, "x", "a/b")])
        with patch.object(m.psycopg2, "connect", return_value=conn):
            m.main()
        self.assertEqual(m.notify.call_args.kwargs["dedup_key"], "claude-reap:3:a/b")


class TestIntegration(unittest.TestCase):
    def test_reap_rows_flow_into_notify_fields(self):
        m = _load()
        conn, cur = _conn([(11, "port tests", "core10/s1")])
        with patch.object(m.psycopg2, "connect", return_value=conn):
            m.main()
        cur.execute.assert_called_once_with("SELECT id, description, was FROM claude_reap()")
        kw = m.notify.call_args.kwargs
        self.assertEqual((kw["level"], kw["category"], kw["source"]), ("info", "claude_fleet", "nova_claude_lease_reaper"))
        self.assertIn("#11", m.notify.call_args.args[0])


class TestFunctional(unittest.TestCase):
    def test_golden_prints_count(self):
        m = _load()
        conn, _ = _conn([(1, "a", "h/s"), (2, "b", "h/s")])
        with patch.object(m.psycopg2, "connect", return_value=conn), patch("builtins.print") as pr:
            m.main()
        pr.assert_called_with("reaped 2")

    def test_pg_down_raises_after_retries(self):
        m = _load()
        with patch.object(m.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")), \
                patch.object(m.time, "sleep"), self.assertRaises(psycopg2.OperationalError):
            m.main()
        m.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_imports_and_has_main(self):
        m = _load()
        self.assertTrue(callable(m.main) and callable(m._reap))

    def test_compiles_standalone(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
