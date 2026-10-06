#!/usr/bin/env python3
"""Tests for nova_gateway/channels/claude.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). The gateway pool, Redis, agent and Slack are all mocked; no
network, no PG, no daemon loop is ever started (the poll loop is driven one iteration at a time).
Written by Jordan Koch (via Claude)."""
import asyncio
import importlib
import re
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

cc = importlib.import_module("nova_gateway.channels.claude")
SRC = (SCRIPTS / "nova_gateway" / "channels" / "claude.py").read_text()


def _run(coro):
    return asyncio.run(coro)


class _Pool:
    """asyncpg-style pool stub. fetch returns queued row-batches; execute/fetchval recorded."""
    def __init__(self, maxid=0, batches=None):
        self.maxid = maxid; self.batches = list(batches or []); self.executed = []

    async def fetchval(self, sql, *a):
        return self.maxid

    async def fetch(self, sql, *a):
        return self.batches.pop(0) if self.batches else []

    async def execute(self, sql, *a):
        self.executed.append(sql)
        return "DELETE 3"


class _Ctx:
    """Minimal GatewayContext stand-in."""
    def __init__(self, stopped=False):
        self.shutdown = types.SimpleNamespace(_stop=stopped, is_set=lambda: self.shutdown._stop)
        self.channel_locks = {"claude-code": asyncio.Lock()}
        self.claude_active_task = None
        self.claude_editing_files = []
        self.tokens = {"slack_bot": "xoxb-test"}


def _patch_pg(ctx, pool):
    return mock.patch.object(cc, "get_pg", new=mock.AsyncMock(return_value=pool))


def _stopper(ctx):
    """An asyncio.sleep stand-in that lets the loop run exactly one cycle, then stops it cleanly."""
    async def _sleep(_secs):
        ctx.shutdown._stop = True
    return _sleep


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_not_fstring(self):
        # the SELECTs use $1 placeholders; no f-string interpolation of the watermark id
        self.assertNotRegex(SRC, r'fetch\(\s*f"')
        self.assertIn("id > $1", SRC)

    def test_test_pings_never_become_conversations(self):
        self.assertIn('startswith("test_ping")', SRC)
        self.assertIn('_mmeta.get("test")', SRC)


class TestPerformance(unittest.TestCase):
    def test_poll_batch_capped_at_five(self):
        # the production query bounds each poll with LIMIT 5 — no unbounded drain
        self.assertRegex(SRC, r"ORDER BY id ASC LIMIT 5")


class TestRetry(unittest.TestCase):
    def test_agent_error_writes_error_reply_and_keeps_looping(self):
        # RETRY GAP: run_agent has no retry; a failure is caught, an error note is written back,
        # the watermark still advances, and the channel survives to the next poll.
        ctx = _Ctx(stopped=False)
        rows = [{"id": 7, "message": "hello", "metadata": None}]
        pool = _Pool(maxid=0, batches=[rows, []])
        writes = []
        async def drive():
            with _patch_pg(ctx, pool), \
                 mock.patch.object(cc, "run_agent", new=mock.AsyncMock(side_effect=RuntimeError("llm down"))), \
                 mock.patch.object(cc, "write_message_for_claude",
                                   new=mock.AsyncMock(side_effect=lambda c, msg, metadata=None: writes.append((msg, metadata)))), \
                 mock.patch.object(cc, "gen_trace_id", return_value="tid"), \
                 mock.patch.object(cc, "session_id", return_value="sid"), \
                 mock.patch.object(cc, "_check_claude_scratchpad", new=mock.AsyncMock()), \
                 mock.patch.object(cc.asyncio, "sleep", new=mock.AsyncMock(side_effect=_stopper(ctx))):
                await cc.run_claude_channel(ctx)
        _run(drive())
        self.assertEqual(len(writes), 1)
        self.assertIn("Error processing your message", writes[0][0])
        self.assertTrue(writes[0][1]["error"])


class TestUnit(unittest.TestCase):
    def test_scratchpad_tracks_active_task(self):
        ctx = _Ctx()
        r = mock.Mock()
        r.get.return_value = "refactor nova_router"
        r.keys.return_value = ["nova:editing:a.py", "nova:editing:b.py"]
        with mock.patch.object(cc, "_get_redis", return_value=r):
            _run(cc._check_claude_scratchpad(ctx))
        self.assertEqual(ctx.claude_active_task, "refactor nova_router")
        self.assertEqual(ctx.claude_editing_files, ["a.py", "b.py"])

    def test_scratchpad_no_redis_is_noop(self):
        ctx = _Ctx()
        with mock.patch.object(cc, "_get_redis", return_value=None):
            _run(cc._check_claude_scratchpad(ctx))
        self.assertIsNone(ctx.claude_active_task)


class TestIntegration(unittest.TestCase):
    def test_idle_scratchpad_cleans_stale_messages(self):
        ctx = _Ctx(); ctx.claude_active_task = "old"
        pool = _Pool()
        r = mock.Mock(); r.get.return_value = None; r.keys.return_value = []
        with mock.patch.object(cc, "_get_redis", return_value=r), _patch_pg(ctx, pool):
            _run(cc._check_claude_scratchpad(ctx))
        self.assertIsNone(ctx.claude_active_task)
        self.assertTrue(any("DELETE FROM claude_messages" in s for s in pool.executed))

    def test_uses_bridge_session_and_notify_channel(self):
        self.assertTrue(hasattr(cc, "CLAUDE_BRIDGE_SESSION") and hasattr(cc, "SLACK_NOTIFY_CHANNEL"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_replies_and_advances_watermark(self):
        ctx = _Ctx(stopped=False)
        rows = [{"id": 11, "message": "hi nova", "metadata": None}]
        pool = _Pool(maxid=5, batches=[rows, []])
        writes = []
        async def drive():
            with _patch_pg(ctx, pool), \
                 mock.patch.object(cc, "run_agent", new=mock.AsyncMock(return_value="hi claude")), \
                 mock.patch.object(cc, "write_message_for_claude",
                                   new=mock.AsyncMock(side_effect=lambda c, msg, metadata=None: writes.append((msg, metadata)))), \
                 mock.patch.object(cc, "gen_trace_id", return_value="tid"), \
                 mock.patch.object(cc, "session_id", return_value="sid"), \
                 mock.patch.object(cc, "_check_claude_scratchpad", new=mock.AsyncMock()), \
                 mock.patch.object(cc.asyncio, "sleep", new=mock.AsyncMock(side_effect=_stopper(ctx))):
                await cc.run_claude_channel(ctx)
        _run(drive())
        self.assertEqual(writes[0][0], "hi claude")
        self.assertEqual(writes[0][1]["in_reply_to"], 11)

    def test_test_ping_is_skipped_not_answered(self):
        ctx = _Ctx(stopped=False)
        rows = [{"id": 9, "message": "test_ping_from_tests", "metadata": None}]
        pool = _Pool(maxid=0, batches=[rows, []])
        agent = mock.AsyncMock()
        async def drive():
            with _patch_pg(ctx, pool), \
                 mock.patch.object(cc, "run_agent", new=agent), \
                 mock.patch.object(cc, "write_message_for_claude", new=mock.AsyncMock()), \
                 mock.patch.object(cc, "_check_claude_scratchpad", new=mock.AsyncMock()), \
                 mock.patch.object(cc.asyncio, "sleep", new=mock.AsyncMock(side_effect=_stopper(ctx))):
                await cc.run_claude_channel(ctx)
        _run(drive())
        agent.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_is_clean_and_no_loop_on_import(self):
        self.assertTrue(asyncio.iscoroutinefunction(cc.run_claude_channel))
        self.assertTrue(asyncio.iscoroutinefunction(cc._check_claude_scratchpad))

    def test_shutdown_set_means_loop_exits_immediately(self):
        ctx = _Ctx(stopped=True)  # is_set() -> True
        pool = _Pool()
        async def drive():
            with _patch_pg(ctx, pool), mock.patch.object(cc, "run_agent", new=mock.AsyncMock()) as a:
                await asyncio.wait_for(cc.run_claude_channel(ctx), timeout=5)
                return a
        a = _run(drive())
        a.assert_not_called()


if __name__ == "__main__":
    unittest.main()
