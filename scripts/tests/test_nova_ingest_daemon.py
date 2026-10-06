#!/usr/bin/env python3
"""Tests for nova_ingest_daemon.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        with patch("logging.basicConfig"):            # no FileHandler on the real daemon log
            spec.loader.exec_module(mod)
    finally:
        for s, h in saved.items():                    # the module installs SIGTERM/SIGINT handlers at import
            signal.signal(s, h)
    return mod


dm = _load("nova_ingest_daemon_t", SCRIPTS / "nova_ingest_daemon.py")
dm._bus_notify = MagicMock()                          # notification bus stubbed at load
dm.PID_FILE = Path(tempfile.mkdtemp()) / "nova_ingest_daemon.pid"
SRC = (SCRIPTS / "nova_ingest_daemon.py").read_text()
JOB = {"id": 7, "mode": "wiki", "query": "lisp machines", "vector": None, "target": None, "requested_by": "t"}
HOSTILE = "x; touch pwned"


class _Conn:
    def __init__(self, row=None):
        self.row = row; self.calls = []

    async def fetchrow(self, sql, *a):
        self.calls.append((sql, a)); return self.row

    async def execute(self, sql, *a):
        self.calls.append((sql, a))


class _Pool:
    def __init__(self, conn):
        self.conn = conn; self.closed = False

    def acquire(self):
        c = self.conn

        class _Ctx:
            async def __aenter__(s):
                return c

            async def __aexit__(s, *a):
                return False
        return _Ctx()

    async def close(self):
        self.closed = True


def _proc(rc, out):
    p = MagicMock(); p.communicate.return_value = (out, None); p.returncode = rc
    return p


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", dm.DB_DSN)

    def test_job_runs_as_argv_not_shell(self):
        with patch.object(dm.subprocess, "Popen", return_value=_proc(0, "")) as po:
            dm.run_ingest(dict(JOB, query=HOSTILE))
        argv = po.call_args[0][0]
        self.assertIsInstance(argv, list)
        self.assertIn(HOSTILE, argv)                       # one argv element, never shell-parsed
        self.assertNotIn("shell", po.call_args[1])

    def test_sql_uses_placeholders(self):
        conn = _Conn()
        asyncio.run(dm.complete_job(_Pool(conn), 3, False, 0, "x' OR '1'='1"))
        sql, args = conn.calls[0]
        self.assertNotIn("OR '1'", sql)
        self.assertEqual(args, ("failed", 0, "x' OR '1'='1", 3))


class TestPerformance(unittest.TestCase):
    def test_output_parse_fast_on_10k_lines(self):
        out = "\n".join(f"progress line {i}" for i in range(10_000)) + "\nDone: 4242 memories stored\n"
        t0 = time.perf_counter()
        with patch.object(dm.subprocess, "Popen", return_value=_proc(0, out)):
            self.assertEqual(dm.run_ingest(JOB), (True, 4242, None))
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_spawn_failure_fails_open(self):
        # RETRY GAP: run_ingest — one Popen; the job is marked failed, the daemon keeps polling
        with patch.object(dm.subprocess, "Popen", side_effect=OSError("no python")) as po:
            self.assertEqual(dm.run_ingest(JOB), (False, 0, "no python"))
        self.assertEqual(po.call_count, 1)
        self.assertIsNone(dm.current_process)

    def test_nonzero_exit_captures_tail(self):
        with patch.object(dm.subprocess, "Popen", return_value=_proc(2, "x" * 900 + "BOOM")):
            ok, n, err = dm.run_ingest(JOB)
        self.assertFalse(ok)
        self.assertTrue(err.endswith("BOOM"))
        self.assertLessEqual(len(err), 500)


class TestUnit(unittest.TestCase):
    def setUp(self):
        dm.PID_FILE.unlink(missing_ok=True)

    def test_pid_lock_fresh_stale_and_live(self):
        self.assertTrue(dm.acquire_pid_lock())
        self.assertEqual(dm.PID_FILE.read_text(), str(os.getpid()))
        self.assertFalse(dm.acquire_pid_lock())             # our own pid is alive
        dm.PID_FILE.write_text("not-a-pid")
        self.assertTrue(dm.acquire_pid_lock())              # stale file replaced
        dm.release_pid_lock()
        self.assertFalse(dm.PID_FILE.exists())

    def test_notify_level_and_title(self):
        dm._bus_notify.reset_mock()
        dm.notify(":x: Ingest job #1 *failed*: boom")
        args, kw = dm._bus_notify.call_args
        self.assertEqual(args[0], "Ingest job #1 failed: boom")
        self.assertEqual(kw["level"], "warning")
        dm.notify(":gear: started")
        self.assertEqual(dm._bus_notify.call_args[1]["level"], "info")

    def test_sigterm_handler_sets_flag(self):
        dm.shutdown_requested = False
        dm.handle_sigterm(signal.SIGTERM, None)
        self.assertTrue(dm.shutdown_requested)
        dm.shutdown_requested = False


class TestIntegration(unittest.TestCase):
    def test_command_targets_nova_ingest_with_defaults(self):
        with patch.object(dm.subprocess, "Popen", return_value=_proc(0, "")) as po:
            dm.run_ingest(JOB)
        argv = po.call_args[0][0]
        self.assertEqual(Path(argv[1]).name, "nova_ingest.py")
        self.assertEqual(argv[2:], ["wiki", "lisp machines", "--source", "wiki", "--target", "1000"])

    def test_claim_uses_skip_locked(self):
        conn = _Conn(row={"id": 1})
        self.assertEqual(asyncio.run(dm.claim_job(_Pool(conn))), {"id": 1})
        self.assertIn("FOR UPDATE SKIP LOCKED", conn.calls[0][0])
        self.assertIsNone(asyncio.run(dm.claim_job(_Pool(_Conn()))))


class TestFunctional(unittest.TestCase):
    def test_one_job_cycle_then_graceful_stop(self):
        dm._bus_notify.reset_mock(); dm.shutdown_requested = False
        conn = _Conn(row=JOB); pool = _Pool(conn)

        def run(job):
            dm.shutdown_requested = True                   # SIGTERM arrives mid-job: finish, then exit
            return True, 12, None

        async def create_pool(*a, **k):
            return pool
        with patch.object(dm.asyncpg, "create_pool", create_pool), patch.object(dm, "run_ingest", run):
            asyncio.run(asyncio.wait_for(dm.daemon_loop(), 5))
        dm.shutdown_requested = False
        titles = [c[0][0] for c in dm._bus_notify.call_args_list]
        self.assertIn("Ingest job #7 completed: 12 memories stored", titles)
        self.assertEqual(titles[-1], "Ingest daemon stopped")
        self.assertEqual(conn.calls[-1][1], ("completed", 12, None, 7))
        self.assertTrue(pool.closed)

    def test_main_exits_when_another_instance_holds_lock(self):
        dm.PID_FILE.write_text(str(os.getpid()))
        with patch.object(dm, "daemon_loop") as dl, self.assertRaises(SystemExit) as cm:
            dm.main()
        self.assertEqual(cm.exception.code, 1)
        dl.assert_not_called()
        dm.PID_FILE.unlink(missing_ok=True)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", "import nova_ingest_daemon"], cwd=str(SCRIPTS),
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
