#!/usr/bin/env python3
"""Tests for nova_gateway/session.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). asyncpg.create_pool and the pool are mocked; no PG is touched.
Written by Jordan Koch (via Claude)."""
import asyncio
import importlib
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

ss = importlib.import_module("nova_gateway.session")
SRC = (SCRIPTS / "nova_gateway" / "session.py").read_text()


class _Pool:
    def __init__(self, fail=False):
        self.fail = fail; self.executed = []

    async def execute(self, sql, *a):
        self.executed.append((sql, a))
        if self.fail:
            raise RuntimeError("relation does not exist")


def _ctx(pool=None):
    return types.SimpleNamespace(pg_pool=pool)


def _run(c):
    return asyncio.run(c)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_values_always_bound_never_interpolated(self):
        hostile = "x'); DELETE FROM gateway_traces; --"
        pool = _Pool()
        _run(ss.log_turn(_ctx(pool), hostile, "chat", "user", hostile, turn_index=1))
        sql, args = pool.executed[0]
        self.assertNotIn(hostile, sql)
        self.assertIn(hostile, args)
        # the only f-string SQL is the fixed ALTER column list, never user data
        fsql = re.findall(r'f"(ALTER|INSERT|SELECT|UPDATE|DELETE)', SRC)
        self.assertEqual(fsql, ["ALTER"])

    def test_turn_log_stores_hash_and_preview_only(self):
        pool = _Pool()
        _run(ss.log_turn(_ctx(pool), "s", "chat", "user", "z" * 5000))
        args = pool.executed[0][1]
        self.assertEqual(len(args[5]), 32)          # md5 hex, not the content
        self.assertEqual(len(args[6]), 200)         # preview capped


class TestPerformance(unittest.TestCase):
    def test_pool_created_once_then_reused(self):
        made = []

        async def mk(*a, **k):
            made.append(1); return _Pool()

        ctx = _ctx()
        with mock.patch.object(ss.asyncpg, "create_pool", new=mk):
            t0 = time.perf_counter()
            for _ in range(10_000):
                _run_coro_fast(ss.get_pg(ctx))
            self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(made), 1)


def _run_coro_fast(coro):
    """Drive a coroutine that never truly suspends without spinning up an event loop."""
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value
    raise AssertionError("coroutine suspended")


class TestRetry(unittest.TestCase):
    def test_get_pg_retries_then_succeeds(self):
        attempts, sleeps = [], []
        pool = _Pool()

        async def mk(*a, **k):
            attempts.append(1)
            if len(attempts) < 3:
                raise OSError("pg down")
            return pool

        async def _sleep(s):
            sleeps.append(s)

        ctx = _ctx()
        with mock.patch.object(ss.asyncpg, "create_pool", new=mk), mock.patch.object(ss.asyncio, "sleep", new=_sleep):
            self.assertIs(_run(ss.get_pg(ctx)), pool)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [3, 3])

    def test_get_pg_gives_up_after_ten(self):
        attempts = []

        async def mk(*a, **k):
            attempts.append(1); raise OSError("pg down")

        async def _sleep(s):
            pass

        with mock.patch.object(ss.asyncpg, "create_pool", new=mk), mock.patch.object(ss.asyncio, "sleep", new=_sleep):
            with self.assertRaises(OSError):
                _run(ss.get_pg(_ctx()))
        self.assertEqual(len(attempts), 10)

    def test_audit_writers_fail_open(self):
        pool = _Pool(fail=True)
        ctx = _ctx(pool)
        _run(ss.log_session_start(ctx, "s", "slack", "chat"))
        _run(ss.log_privacy_block(ctx, [{"content": "x"}]))
        _run(ss.log_degraded_event(ctx, "e", "n"))
        _run(ss.log_tool_execution(ctx, "s", "t", {"a": 1}, "r", 5))
        _run(ss.log_trace(ctx, "tr", "slack", "chat", "u", "r", "ollama", [], 1, 2, 3, 4))
        self.assertEqual(len(pool.executed), 5)


class TestUnit(unittest.TestCase):
    def test_degraded_preview_capped(self):
        pool = _Pool()
        _run(ss.log_degraded_event(_ctx(pool), "e", "n" * 500))
        self.assertTrue(pool.executed[0][1][2].startswith("DEGRADED: "))
        self.assertEqual(len(pool.executed[0][1][2]), 200)

    def test_privacy_block_never_stores_content(self):
        pool = _Pool()
        _run(ss.log_privacy_block(_ctx(pool), [{"content": "my secret diagnosis"}, {}]))
        self.assertNotIn("diagnosis", " ".join(map(str, pool.executed[0][1])))

    def test_trace_truncates_and_serialises(self):
        pool = _Pool()
        _run(ss.log_trace(_ctx(pool), "tr", "slack", "chat", "u" * 3000, "r" * 3000, "ollama",
                          [{"name": "x"}], 1, 2, 3, 4))
        a = pool.executed[0][1]
        self.assertEqual((len(a[3]), len(a[4])), (2000, 2000))
        self.assertEqual(a[6], '[{"name": "x"}]')


class TestIntegration(unittest.TestCase):
    def test_pool_uses_config_dsn(self):
        import nova_gateway.config as cfg
        seen = {}

        async def mk(dsn, **k):
            seen["dsn"] = dsn; seen.update(k); return _Pool()

        with mock.patch.object(ss.asyncpg, "create_pool", new=mk):
            _run(ss.get_pg(_ctx()))
        self.assertEqual(seen["dsn"], cfg.PG_DSN)
        self.assertEqual(seen["command_timeout"], 30)

    def test_tool_log_targets_query_log_with_tool_columns(self):
        pool = _Pool()
        _run(ss.log_tool_execution(_ctx(pool), "s", "web_search", {"q": "x"}, "res" * 1000, 12))
        sql, a = pool.executed[0]
        self.assertIn("INSERT INTO gateway_query_log", sql)
        self.assertEqual(a[5], "web_search")
        self.assertEqual(len(a[7]), 2000)


class TestFunctional(unittest.TestCase):
    def test_ensure_schema_creates_tables_and_columns(self):
        pool = _Pool()
        _run(ss.ensure_pg_schema(_ctx(pool)))
        sqls = [s for s, _ in pool.executed]
        self.assertIn("CREATE TABLE IF NOT EXISTS claude_queue", sqls[0])
        self.assertEqual(sum("ALTER TABLE gateway_query_log" in s for s in sqls), 5)
        self.assertIn("CREATE TABLE IF NOT EXISTS gateway_traces", sqls[-1])

    def test_ensure_schema_tolerates_alter_failures(self):
        class P(_Pool):
            async def execute(self, sql, *a):
                self.executed.append((sql, a))
                if sql.startswith("ALTER"):
                    raise RuntimeError("no table")
        pool = P()
        _run(ss.ensure_pg_schema(_ctx(pool)))
        self.assertIn("gateway_traces", pool.executed[-1][0])


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_gateway.session"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertNotIn("__main__", SRC)


if __name__ == "__main__":
    unittest.main()
