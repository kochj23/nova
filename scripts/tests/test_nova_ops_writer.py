#!/usr/bin/env python3
"""Tests for nova_ops_writer.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). asyncpg is never connected: the pool is a stub injected per test and
the module's queue/worker globals are reset after each one. Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_ops_writer.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("opswriter", SCRIPTS / "nova_ops_writer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ow = _load()
ow.log = MagicMock()            # nova_logger appends to the real nova.jsonl; keep tests off it


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def execute(self, sql, *args):
        self.pool.calls += 1
        if self.pool.fail_first > 0:
            self.pool.fail_first -= 1
            raise OSError("connection reset")
        self.pool.done.append((sql, args))


class _Acq:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool)

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, fail_first=0):
        self.fail_first = fail_first; self.calls = 0; self.done = []; self.closed = False

    def acquire(self):
        return _Acq(self)

    async def close(self):
        self.closed = True


def _reset():
    ow._POOL = None; ow._POOL_LOCK = None; ow._QUEUE = None; ow._WORKER_TASK = None


def _start(**kw):
    a = dict(run_id="r1", task_id="t", task_script="s.py", task_group="g", scheduled_at_ms=1, started_at_ms=2,
             consecutive_failures=0, run_count=5, was_retry=False)
    a.update(kw)
    ow.record_run_start(**a)


def _end(**kw):
    a = dict(run_id="r1", ended_at_ms=3, duration_ms=1, exit_code=0, status="success", error_tail="",
             stdout_tail="ok", retry_recovered=False)
    a.update(kw)
    ow.record_run_end(**a)


def _drive(pool, body, sleeps=None):
    async def go():
        ow._POOL = pool
        body()
        await ow.close()

    async def _sleep(s):
        if sleeps is not None:
            sleeps.append(s)

    with patch.object(ow.asyncio, "sleep", new=_sleep):
        asyncio.run(go())


class _Base(unittest.TestCase):
    def setUp(self):
        _reset(); ow.log.reset_mock()

    def tearDown(self):
        _reset()


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(ow.DB_DSN, r"kochj:[^@]+@")

    def test_values_are_bound_never_interpolated(self):
        pool = _Pool()
        hostile = "t'); DELETE FROM scheduler_runs; --"
        _drive(pool, lambda: _start(task_id=hostile))
        sql, args = pool.done[0]
        self.assertNotIn(hostile, sql)
        self.assertIn(hostile, args)
        self.assertIn("$1", sql)


class TestPerformance(_Base):
    def test_enqueue_never_blocks_and_caps_queue(self):
        async def go():
            ow._POOL = None
            with patch.object(ow, "_ensure_worker", side_effect=lambda: setattr(ow, "_QUEUE", ow._QUEUE or asyncio.Queue(maxsize=500))):
                t0 = time.perf_counter()
                for i in range(10_000):
                    _end(run_id=f"r{i}")
                return time.perf_counter() - t0, ow._QUEUE.qsize()
        elapsed, size = asyncio.run(go())
        self.assertLess(elapsed, 2.0)
        self.assertEqual(size, 500)                     # bounded: extra writes are dropped, not buffered
        self.assertTrue(any("queue full" in c[0][0] for c in ow.log.call_args_list))


class TestRetry(_Base):
    def test_write_retries_with_backoff_then_succeeds(self):
        pool = _Pool(fail_first=2)
        sleeps = []
        _drive(pool, lambda: _start(), sleeps)
        self.assertEqual(pool.calls, 3)
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual(len(pool.done), 1)

    def test_gives_up_after_three_attempts(self):
        pool = _Pool(fail_first=99)
        _drive(pool, lambda: _end(), [])
        self.assertEqual(pool.calls, 3)
        self.assertTrue(any("after 3 attempts" in c[0][0] for c in ow.log.call_args_list))

    def test_pool_init_failure_disables_history(self):
        async def boom(*a, **k):
            raise OSError("pg down")

        async def go():
            with patch.object(ow.asyncpg, "create_pool", new=boom):
                return await ow._get_pool()
        self.assertIsNone(asyncio.run(go()))


class TestUnit(_Base):
    def test_sync_context_is_a_silent_noop(self):
        _start()
        self.assertIsNone(ow._QUEUE)

    def test_close_without_anything_is_safe(self):
        asyncio.run(ow.close())
        self.assertIsNone(ow._POOL)


class TestIntegration(_Base):
    def test_start_then_end_round_trip(self):
        pool = _Pool()
        _drive(pool, lambda: (_start(run_id="abc"), _end(run_id="abc", status="failure", exit_code=2)))
        (s1, a1), (s2, a2) = pool.done
        self.assertIn("INSERT INTO scheduler_runs", s1)
        self.assertIn("ON CONFLICT (run_id) DO NOTHING", s1)
        self.assertIn("WHERE run_id = $1", s2)
        self.assertEqual((a1[0], a2[0], a2[4]), ("abc", "abc", "failure"))
        self.assertTrue(pool.closed)

    def test_uses_shared_logger(self):
        self.assertIn("from nova_logger import log", SRC)


class TestFunctional(_Base):
    def test_pool_created_once_with_bounded_size(self):
        made = []

        async def mk(dsn, **k):
            made.append(k); return _Pool()

        async def go():
            with patch.object(ow.asyncpg, "create_pool", new=mk):
                p1 = await ow._get_pool(); p2 = await ow._get_pool()
            return p1 is p2
        self.assertTrue(asyncio.run(go()))
        self.assertEqual(len(made), 1)
        self.assertEqual((made[0]["max_size"], made[0]["command_timeout"]), (3, 10))


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        with __import__("tempfile").TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", "import nova_ops_writer as w; print(w._POOL, w._QUEUE)"],
                               cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "None None")
        self.assertNotIn("__main__", SRC)


if __name__ == "__main__":
    unittest.main()
