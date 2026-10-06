#!/usr/bin/env python3
"""Tests for nova_gateway/context.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import ast
import asyncio
import dataclasses
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_gateway" / "context.py"
SRC = SCRIPT.read_text()
MAIN_SRC = (SCRIPTS / "nova_gateway" / "main.py").read_text()


def _load():
    # loaded by path so the package __init__ (which pulls in the whole gateway) never runs
    spec = importlib.util.spec_from_file_location("ngw_context", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cx = _load()
GC = cx.GatewayContext


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_tokens_default_empty_and_not_shared(self):
        a, b = GC(), GC()
        self.assertEqual(a.tokens, {})
        a.tokens["slack"] = "x"
        self.assertEqual(b.tokens, {})                 # one context's secrets never leak into another


class TestPerformance(unittest.TestCase):
    def test_10k_contexts_and_session_appends_are_fast(self):
        t0 = time.perf_counter()
        ctx = GC()
        for i in range(10_000):
            ctx.sessions[f"s{i % 100}"].append({"role": "user", "content": str(i)})
            ctx.agent_crash_counts[f"a{i % 10}"] += 1
        made = [GC() for _ in range(2_000)]
        self.assertEqual(len(ctx.sessions), 100)
        self.assertEqual(sum(ctx.agent_crash_counts.values()), 10_000)
        self.assertEqual(len(made), 2_000)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_module_makes_no_external_calls(self):
        # RETRY GAP: none applicable — context.py is pure state; it opens no connection, so there is nothing to retry.
        # Prove it: no network/db imports, and the unset handles stay None (callers must handle "no pool").
        tree = ast.parse(SRC)
        mods = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
        mods |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertTrue(mods.isdisjoint({"asyncpg", "httpx", "requests", "urllib", "psycopg2", "redis", "socket"}))
        ctx = GC()
        self.assertIsNone(ctx.pg_pool); self.assertIsNone(ctx.http); self.assertIsNone(ctx.redis_conn)


class TestUnit(unittest.TestCase):
    def test_defaults(self):
        ctx = GC()
        self.assertEqual(ctx.sessions["new"], [])
        self.assertEqual(ctx.agent_crash_counts["x"], 0)
        self.assertIsNone(ctx.claude_active_task)
        self.assertEqual(ctx.claude_editing_files, [])
        self.assertEqual(ctx.last_reload, 0.0)
        self.assertLessEqual(abs(ctx.start_time - time.time()), 5)

    def test_mutable_defaults_are_per_instance(self):
        a, b = GC(), GC()
        a.sessions["x"].append(1); a.claude_editing_files.append("f"); a.agent_disabled_until["z"] = 1
        self.assertEqual(dict(b.sessions), {}); self.assertEqual(b.claude_editing_files, [])
        self.assertEqual(b.agent_disabled_until, {})
        self.assertIsNot(a.shutdown, b.shutdown)

    def test_channel_locks_are_asyncio_locks_per_channel(self):
        ctx = GC()
        self.assertIsInstance(ctx.channel_locks["c1"], asyncio.Lock)
        self.assertIs(ctx.channel_locks["c1"], ctx.channel_locks["c1"])
        self.assertIsNot(ctx.channel_locks["c1"], ctx.channel_locks["c2"])


class TestIntegration(unittest.TestCase):
    def test_main_imports_and_builds_it_with_real_fields(self):
        self.assertIn("from nova_gateway.context import GatewayContext", MAIN_SRC)
        call = MAIN_SRC[MAIN_SRC.index("ctx = GatewayContext("):]
        call = call[:call.index(")\n") + 1]
        kwargs = set(re.findall(r"(\w+)=", call))
        fields = {f.name for f in dataclasses.fields(GC)}
        self.assertTrue(kwargs and kwargs <= fields, kwargs - fields)

    def test_field_names_used_across_gateway_exist(self):
        fields = {f.name for f in dataclasses.fields(GC)}
        used = set()
        for p in (SCRIPTS / "nova_gateway").rglob("*.py"):
            used |= set(re.findall(r"\bctx\.(sessions|channel_locks|agent_crash_counts|agent_disabled_until|shutdown|pg_pool)\b",
                                   p.read_text(errors="ignore")))
        self.assertTrue(used)
        self.assertTrue(used <= fields)


class TestFunctional(unittest.TestCase):
    def test_shutdown_event_and_lock_work_in_a_loop(self):
        async def scenario():
            ctx = GC()
            order = []

            async def worker(n):
                async with ctx.channel_locks["slack:C1"]:
                    order.append(("in", n)); await asyncio.sleep(0); order.append(("out", n))
            await asyncio.gather(worker(1), worker(2))
            asyncio.get_running_loop().call_soon(ctx.shutdown.set)
            await asyncio.wait_for(ctx.shutdown.wait(), 2)
            return order, ctx.shutdown.is_set()
        order, done = asyncio.run(scenario())
        self.assertTrue(done)
        self.assertEqual(order, [("in", 1), ("out", 1), ("in", 2), ("out", 2)])   # the lock serialises a channel


class TestFrame(unittest.TestCase):
    def test_compiles_and_imports_clean(self):
        code = ("import importlib.util as u; s=u.spec_from_file_location('c', %r); m=u.module_from_spec(s);"
                "s.loader.exec_module(m); m.GatewayContext()") % str(SCRIPT)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_is_a_library_not_a_script(self):
        self.assertNotIn("__main__", SRC)
        self.assertTrue(dataclasses.is_dataclass(GC))


if __name__ == "__main__":
    unittest.main()
