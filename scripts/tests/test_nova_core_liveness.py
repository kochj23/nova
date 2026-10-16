#!/usr/bin/env python3
"""Tests for nova_core_liveness.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_core_liveness.py"
SRC = SCRIPT.read_text()
INJECT = "Title' OR '1'='1"


def _load():
    spec = importlib.util.spec_from_file_location("ncl", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("psycopg2.connect", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


ncl = _load()
ncl.notify = MagicMock(return_value=True)          # pages never leave the test process


class _Cur:
    """Cursor answering freshness + per-service status queries; records every statement."""
    def __init__(self, health_age=10, cap_age=10, status="up"):
        self.health_age, self.cap_age, self.status = health_age, cap_age, status
        self.sql = []; self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params)); self._last = sql

    def fetchone(self):
        if "max(checked_at)" in self._last:
            return (self.health_age,)
        if "capacity_snapshots" in self._last:
            return (self.cap_age,)
        return (self.status, "2026-01-01 00:00")


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.commits = 0; self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _quiet():
    return redirect_stdout(io.StringIO())


class _Stop(Exception):
    pass


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ncl.DSN)

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')
        cur = _Cur(); ncl.page(_Conn(cur), "k", INJECT, "body")
        sql, params = cur.sql[0]
        self.assertNotIn(INJECT, sql)
        self.assertIn(INJECT, params[1])


class TestPerformance(unittest.TestCase):
    def test_check_with_10k_keystones_is_bounded(self):
        many = [(f"svc{i}", "127.0.0.1", 1, f"s{i}") for i in range(10_000)]
        t0 = time.perf_counter()
        with patch.object(ncl, "KEYSTONES", many), patch.object(ncl, "tcp_up", return_value=True):
            issues = ncl.check(_Conn(_Cur()))
        self.assertEqual(issues, [])
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_main_reconnects_after_two_failed_connects(self):
        conn = _Conn(_Cur())
        connect = MagicMock(side_effect=[RuntimeError("pg down"), RuntimeError("pg down"), conn])
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            if len(sleeps) >= 3:
                raise _Stop()
        with patch.object(ncl.psycopg2, "connect", connect), patch.object(ncl.time, "sleep", side_effect=sleep), \
             patch.object(ncl, "tcp_up", return_value=True), _quiet():
            with self.assertRaises(_Stop):
                ncl.main()
        self.assertEqual(connect.call_count, 3)
        self.assertEqual(sleeps, [ncl.INTERVAL_S] * 3)
        self.assertEqual(conn.commits, 1)

    def test_tcp_probe_fails_closed_to_down(self):
        # RETRY GAP: tcp_up() — single connect attempt; any error means "down", never raises
        with patch.object(ncl.socket, "create_connection", side_effect=OSError("refused")) as m:
            self.assertFalse(ncl.tcp_up("10.0.0.1", 1))
        self.assertEqual(m.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_stale_none_and_bad_status_all_flagged(self):
        with patch.object(ncl, "tcp_up", return_value=True):
            keys = [k for k, _, _ in ncl.check(_Conn(_Cur(health_age=None, cap_age=99_999, status="down")))]
        self.assertIn("stale:health_checks", keys)
        self.assertIn("stale:capacity", keys)
        self.assertIn("status:postgresql", keys)

    def test_fresh_and_healthy_is_quiet(self):
        for st in ("up", "ok", "healthy"):
            with patch.object(ncl, "tcp_up", return_value=True):
                self.assertEqual(ncl.check(_Conn(_Cur(status=st))), [])

    def test_tcp_down_names_host_and_port(self):
        with patch.object(ncl, "tcp_up", return_value=False):
            issues = ncl.check(_Conn(_Cur()))
        tcp = [i for i in issues if i[0].startswith("tcp:")]
        self.assertEqual(len(tcp), len(ncl.KEYSTONES))
        self.assertIn("6379", next(b for k, _, b in tcp if k == "tcp:Redis"))

    def test_helpers_swallow_db_errors(self):
        class Boom:
            def cursor(self):
                raise RuntimeError("db gone")
        ncl.notify.reset_mock()
        ncl.ensure_session(Boom()); ncl.heartbeat(Boom())
        with _quiet():
            ncl.page(Boom(), "k", "t", "b")           # notify still fires, queue error is printed not raised
        self.assertTrue(ncl.notify.called)


class TestIntegration(unittest.TestCase):
    def test_pgbouncer_port_matches_listen_port(self):
        ports = {n: p for n, _h, p, _s in ncl.KEYSTONES}
        self.assertEqual(ports["PgBouncer"], 5432)

    def test_page_uses_critical_notify_and_claude_queue(self):
        ncl.notify.reset_mock()
        cur = _Cur(); ncl.page(_Conn(cur), "tcp:Redis", "Keystone DOWN: Redis", "b")
        kw = ncl.notify.call_args.kwargs
        self.assertEqual(kw["level"], "critical")
        self.assertTrue(kw["dedup_key"].startswith("core-liveness:tcp:Redis:"))
        self.assertIn("INSERT INTO claude_queue", cur.sql[0][0])
        self.assertIn("NOT EXISTS", cur.sql[0][0])


class TestFunctional(unittest.TestCase):
    def test_run_once_pages_each_issue_and_writes_heartbeat(self):
        ncl.notify.reset_mock()
        cur = _Cur(cap_age=5000); conn = _Conn(cur)
        with patch.object(ncl, "tcp_up", return_value=True), _quiet():
            n = ncl.run_once(conn)
        self.assertEqual(n, 1)
        self.assertEqual(ncl.notify.call_count, 1)
        stmts = [s for s, _ in cur.sql]
        self.assertTrue(any("claude_sessions" in s for s in stmts))
        self.assertTrue(any("INSERT INTO health_checks" in s for s in stmts))
        self.assertEqual(conn.commits, 1)

    def test_healthy_run_pages_nothing(self):
        ncl.notify.reset_mock()
        with patch.object(ncl, "tcp_up", return_value=True), _quiet():
            self.assertEqual(ncl.run_once(_Conn(_Cur())), 0)
        ncl.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        # the script has no --help (bare run = daemon loop), so the smoke is an import
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_core_liveness"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()


# ── #46(a) Chanos filter: the heartbeat is earned, never a literal 'up' ─────────
def _hb(cur):
    return [p for s, p in cur.sql if "INSERT INTO health_checks" in s]


class TestHeartbeatEarned(unittest.TestCase):
    def test_unit_heartbeat_status_table(self):
        self.assertEqual(ncl.heartbeat_status(0, 0), ("up", None))
        self.assertEqual(ncl.heartbeat_status(2, 0)[0], "degraded")
        self.assertEqual(ncl.heartbeat_status(2, 1)[0], "down")
        self.assertEqual(ncl.heartbeat_status(0, 0, "OperationalError: gone")[0], "down")

    def test_security_status_is_a_parameter_not_a_literal(self):
        hb = SRC.split("def heartbeat(")[1].split("\ndef ")[0]
        self.assertNotIn("'up',0", hb.replace(" ", ""))
        self.assertIn("%s", hb)

    def test_functional_healthy_cycle_writes_up(self):
        cur = _Cur()
        with patch.object(ncl, "tcp_up", return_value=True), _quiet():
            ncl.run_once(_Conn(cur))
        self.assertEqual(_hb(cur)[0][0], "up")

    def test_functional_closed_port_keystone_writes_degraded(self):
        # the Chanos injection: one KEYSTONE pointed at a closed port -> it pages and the heartbeat goes red
        import socket as _s
        sock = _s.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
        ncl.notify.reset_mock(); cur = _Cur()
        with patch.object(ncl, "KEYSTONES", [("Decoy", "127.0.0.1", port, None)]), _quiet():
            n = ncl.run_once(_Conn(cur))
        self.assertEqual(n, 1)
        self.assertEqual(ncl.notify.call_args.kwargs["level"], "critical")
        self.assertEqual(_hb(cur)[0][0], "degraded")

    def test_retry_undelivered_page_writes_down(self):
        cur = _Cur(cap_age=5000)
        with patch.object(ncl, "tcp_up", return_value=True), patch.object(ncl, "notify", return_value=False), _quiet():
            ncl.run_once(_Conn(cur))
        self.assertEqual(_hb(cur)[0][0], "down")

    def test_integration_check_failure_writes_down_then_raises(self):
        class C(_Cur):
            def execute(self, sql, params=None):
                super().execute(sql, params)
                if "max(checked_at)" in sql:
                    raise RuntimeError("relation gone")

        class Conn(_Conn):
            def rollback(self):
                pass
        cur = C()
        with patch.object(ncl, "tcp_up", return_value=True), _quiet(), self.assertRaises(RuntimeError):
            ncl.run_once(Conn(cur))
        st, detail = _hb(cur)[0]
        self.assertEqual(st, "down"); self.assertIn("relation gone", detail)

    def test_performance_status_is_pure_and_fast(self):
        t = time.perf_counter()
        for i in range(100000):
            ncl.heartbeat_status(i % 3, i % 2)
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_frame_docstring_documents_earned_status(self):
        self.assertIn("heartbeat_status()", SRC.split('"""')[1])
