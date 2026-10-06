#!/usr/bin/env python3
"""Tests for nova_gateway/agent.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_gateway" / "agent.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="gw-agent-test-"))
(TMP / ".openclaw" / "logs").mkdir(parents=True)

# the package's main.py opens ~/.openclaw/logs/nova_gateway_v2.log at import: point HOME at a tempdir for the load
with patch.dict(os.environ, {"HOME": str(TMP), "NOVA_TEST_QUIET": "1"}):
    import nova_gateway.agent as ag
    from nova_gateway.context import GatewayContext
_REAL_SUMMARY = ag._generate_cross_channel_summary


class _Pool:
    def __init__(self, fetchval=None, rows=()):
        self.executed = []; self._fetchval = fetchval; self.rows = list(rows)

    async def execute(self, sql, *args): self.executed.append((sql, args))
    async def fetchval(self, sql, *args): return self._fetchval
    async def fetch(self, sql, *args): return self.rows
    async def fetchrow(self, sql, *args): return None


def _ctx(router_answers=None):
    ctx = GatewayContext()
    ctx.startup_time = time.time() - 3600         # well past the startup grace
    ctx.router = MagicMock(); ctx.router.active_backend = "ollama"
    ctx.router.route = AsyncMock(side_effect=list(router_answers or []))
    ctx.http = MagicMock(); ctx.http.post = AsyncMock(); ctx.http.get = AsyncMock()
    return ctx


def _patched(pool, **extra):
    """Every outbound path of do_agent_work stubbed: PG pool, turn/trace logging, tools, memory lanes."""
    p = {"get_pg": AsyncMock(return_value=pool), "log_turn": AsyncMock(), "log_trace": AsyncMock(),
         "log_degraded_event": AsyncMock(), "_load_agent_docs": AsyncMock(return_value=""),
         "_gather_sentience_context": MagicMock(return_value=""), "get_cross_context": AsyncMock(return_value=""),
         "_experience_recall": AsyncMock(return_value=""), "_inject_memory": AsyncMock(return_value=""),
         "_remember_exchange": AsyncMock(), "_generate_cross_channel_summary": AsyncMock(),
         "_redis_publish": MagicMock(), "post_to_claude_slack": AsyncMock(),
         "execute_tool_calls": AsyncMock(return_value=("Hi Little Mister.", "")),
         "execute_tool_calls_legacy": AsyncMock(return_value=("", "")),
         "execute_spoken_tool_calls": AsyncMock(return_value=("", ""))}
    p.update(extra)
    _patched.last = p
    return patch.multiple(ag, **p)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_token_from_keychain(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('keychain("nova-slack-bot-token")', SRC)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r"\.(execute|fetch|fetchval|fetchrow)\(\s*f[\"']", SRC))
        pool = _Pool()
        with _patched(pool):
            asyncio.run(ag.queue_for_claude(_ctx(), "x'); --injected", priority=2))
        sql, args = pool.executed[0]
        self.assertNotIn("--injected", sql)
        self.assertIn("x'); --injected", args)

    def test_private_content_forces_local_routing(self):
        ctx = _ctx(["ok"])
        with _patched(_Pool()), patch.object(ag, "is_private_content", return_value=True):
            asyncio.run(ag.do_agent_work(ctx, "my bank pin", "gw2:slack:C1", "chat", "t1"))
        self.assertTrue(ctx.router.route.call_args.kwargs["private"])


class TestPerformance(unittest.TestCase):
    def test_house_facts_ranks_10k_rows_fast(self):
        now = datetime(2026, 1, 1)
        rows = [{"entity": f"device{i % 2000}", "attr": f"a{i % 5}", "value": str(i), "observed_at": now} for i in range(10_000)]
        rows.append({"entity": "garage-zigbee-plug", "attr": "firmware", "value": "1.2.3", "observed_at": now})
        with _patched(_Pool(rows=rows)):
            t0 = time.perf_counter()
            out = asyncio.run(ag._house_facts(_ctx(), "what firmware is the garage zigbee plug on?"))
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertIn("garage-zigbee-plug: firmware=1.2.3", out)


class TestRetry(unittest.TestCase):
    def test_empty_tool_followup_retried_once_with_bigger_budget(self):
        ctx = _ctx([{"choices": []}, "", "the answer"])
        tools = AsyncMock(return_value=("calling tool", "tool said 42"))
        with _patched(_Pool(), execute_tool_calls=tools):
            out = asyncio.run(ag.do_agent_work(ctx, "what is it", "gw2:slack:C1", "chat", "t1"))
        self.assertEqual(out, "the answer")
        self.assertEqual(ctx.router.route.call_count, 3)
        self.assertEqual(ctx.router.route.call_args.kwargs["max_tokens"], 2048)

    def test_experience_recall_fails_open(self):
        # RETRY GAP: _experience_recall/memory-server GET — one shot per source, "" on failure
        ctx = _ctx(); ctx.http.get = AsyncMock(side_effect=ConnectionError("down"))
        self.assertEqual(asyncio.run(ag._experience_recall(ctx, "tell me about last summer's trip")), "")
        self.assertEqual(ctx.http.get.call_count, 4)

    def test_all_backends_failing_gives_honest_reply(self):
        ctx = _ctx([RuntimeError("all backends down")])
        with _patched(_Pool()):
            out = asyncio.run(ag.do_agent_work(ctx, "hello there", "gw2:slack:C1", "chat", "t1"))
        self.assertIn("Something went wrong on my end", out)


class TestUnit(unittest.TestCase):
    def test_ids_and_approval_regex(self):
        self.assertEqual(ag.session_id("slack", "C1"), "gw2:slack:C1")
        self.assertRegex(ag.gen_trace_id(), r"^[0-9a-f]{8}$")
        self.assertEqual(ag._APPROVAL_RE.match("Approve 1234abcd ").groups(), ("Approve", "1234abcd"))
        self.assertIsNone(ag._APPROVAL_RE.match("approve this please"))

    def test_system_prompt_caps_bootstrap_docs(self):
        p = ag._system_prompt("chat", "D" * 20_000)
        self.assertNotIn("D" * 8001, p)
        self.assertTrue(p.endswith("D" * 8000))
        self.assertIn("--- IDENTITY & CONTEXT ---", p)
        self.assertNotIn("IDENTITY & CONTEXT", ag._system_prompt("home", ""))

    def test_compaction(self):
        msgs = [{"role": "user", "content": "hi"}] * 3
        self.assertIs(asyncio.run(ag._compact_if_needed(_ctx(), "s", "chat", msgs, "sys")), msgs)
        big = [{"role": "user", "content": "word " * 1500} for _ in range(10)]
        ctx = _ctx([RuntimeError("router down")])
        self.assertEqual(asyncio.run(ag._compact_if_needed(ctx, "s", "chat", big, "sys")), big[-6:])
        ctx = _ctx(["summary!"])
        out = asyncio.run(ag._compact_if_needed(ctx, "s", "chat", big, "sys"))
        self.assertEqual(len(out), 5)
        self.assertIn("summary!", out[0]["content"])
        self.assertTrue(ctx.router.route.call_args.kwargs["private"])

    def test_short_messages_skip_recall_and_tokens_fallback(self):
        self.assertEqual(asyncio.run(ag._experience_recall(_ctx(), "hi")), "")
        self.assertGreater(ag._count_tokens("hello world"), 0)
        self.assertEqual(ag._total_tokens([]), 0)


class TestIntegration(unittest.TestCase):
    def test_queue_dedup_and_escalation_priority(self):
        pool = _Pool(fetchval=1)
        with _patched(pool):
            asyncio.run(ag.queue_for_claude(_ctx(), "dup"))
        self.assertEqual(pool.executed, [])
        pool = _Pool()
        with _patched(pool):
            asyncio.run(ag.escalate_scheduler_failure(_ctx(), "t1", "x.py", "E" * 900, 4))
        sql, args = pool.executed[0]
        self.assertIn("INSERT INTO claude_queue", sql)
        self.assertEqual(args[:2], (ag.CLAUDE_BRIDGE_SESSION, 2))
        self.assertIn('"category": "code_bug"', args[3])

    def test_crash_breaker_trips_at_threshold(self):
        ctx = _ctx(); pool = _Pool()
        with _patched(pool):
            for _ in range(ag.CRASH_THRESHOLD):
                asyncio.run(ag._record_agent_crash(ctx, "chat", "t", "boom"))
        self.assertGreater(ctx.agent_disabled_until["chat"], time.time())
        self.assertTrue(any("claude_queue" in s for s, _ in pool.executed))
        self.assertEqual(sum("gateway_query_log" in s for s, _ in pool.executed), ag.CRASH_THRESHOLD)

    def test_cross_channel_summary_saved(self):
        ctx = _ctx()
        ctx.http.post = AsyncMock(return_value=MagicMock(status_code=200, json=lambda: {"response": "Summary: talked HVAC\nTopics: hvac, heat, x, y"}))
        save = AsyncMock()
        with _patched(_Pool(), save_cross_context=save), patch.object(ag.log, "debug") as dbg:
            asyncio.run(_REAL_SUMMARY(ctx, "gw2:discord:9", [{"role": "user", "content": "c"}]))
        self.assertTrue(save.await_count, dbg.call_args_list)
        args, kw = save.call_args
        self.assertEqual(args[1:4], ("discord", "gw2:discord:9", "talked HVAC"))
        self.assertEqual(kw["topics"], ["hvac", "heat", "x"])


class TestFunctional(unittest.TestCase):
    def test_run_agent_golden_path(self):
        ctx = _ctx([{"choices": [{"message": {"content": "Hi"}}]}])
        with _patched(_Pool()):
            m = _patched.last
            out = asyncio.run(ag.run_agent(ctx, "hello nova", "gw2:slack:C1", "chat"))
            self.assertEqual(out, "Hi Little Mister.")
            self.assertEqual([c.args[3] for c in m["log_turn"].call_args_list], ["user", "assistant"])
            m["log_trace"].assert_awaited_once()
        self.assertEqual(ctx.sessions["gw2:slack:C1"][-1], {"role": "assistant", "content": "Hi Little Mister."})

    def test_degraded_and_breaker_short_circuit(self):
        ctx = _ctx(); ctx.startup_time = time.time()
        self.assertIn("just coming back up", asyncio.run(ag.run_agent(ctx, "hi", "s", "chat")))
        ctx = _ctx(); ctx.agent_disabled_until["chat"] = time.time() + 60
        self.assertIn("trouble right now", asyncio.run(ag.run_agent(ctx, "hi", "s", "chat")))
        ctx.router.route.assert_not_called()

    def test_crash_in_work_is_recorded(self):
        ctx = _ctx()
        with _patched(_Pool()), patch.object(ag, "do_agent_work", AsyncMock(side_effect=ValueError("bad"))), \
             patch.object(ag, "_record_agent_crash", AsyncMock()) as rec:
            out = asyncio.run(ag.run_agent(ctx, "hello", "s", "chat", trace_id="tt"))
        self.assertIn("Something went wrong", out)
        rec.assert_awaited_once_with(ctx, "chat", "tt", "bad")


class TestFrame(unittest.TestCase):
    def test_import_is_clean_and_side_effect_free(self):
        tmp = tempfile.mkdtemp(prefix="gw-agent-frame-"); os.makedirs(os.path.join(tmp, ".openclaw", "logs"))
        r = subprocess.run([sys.executable, "-c", "import nova_gateway.agent as a; print(callable(a.run_agent))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "HOME": tmp, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")
        self.assertNotIn("__main__", SRC)


if __name__ == "__main__":
    unittest.main()
