#!/usr/bin/env python3
"""Tests for nova_gateway/taskflow.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_gateway" / "taskflow.py"
SRC = SCRIPT.read_text()


def _load():
    # loaded by path so the package __init__ (which pulls in the whole gateway) never runs
    spec = importlib.util.spec_from_file_location("ngw_taskflow", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.log = logging.getLogger("ngw_taskflow.test"); mod.log.addHandler(logging.NullHandler()); mod.log.propagate = False
    return mod


tf = _load()


def run(coro):
    return asyncio.run(coro)


class FakePool:
    """In-memory flow_runs table speaking just enough asyncpg to drive the state machine."""
    def __init__(self, fail_times=0):
        self.rows = {}; self.calls = []; self.fail_times = fail_times

    def _maybe_fail(self):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise ConnectionError("pool down")

    async def execute(self, sql, *args):
        self._maybe_fail(); self.calls.append((sql, args))
        if sql.lstrip().startswith("INSERT"):
            fid, owner, ctrl, goal, step, state, now = args
            self.rows[fid] = {"flow_id": fid, "status": "running", "goal": goal, "current_step": step,
                              "state_json": state, "wait_json": None, "blocked_summary": None, "revision": 0}
            return "INSERT 0 1"
        if "status = 'waiting'" in sql:
            reason, meta, now, fid, rev = args
            r = self.rows.get(fid)
            if r and r["revision"] == rev:
                r.update(status="waiting", blocked_summary=reason, wait_json=meta, revision=rev + 1); return "UPDATE 1"
        elif "status = 'running', current_step" in sql:
            step, state, now, fid, rev = args
            r = self.rows.get(fid)
            if r and r["revision"] == rev:
                r.update(status="running", current_step=step, state_json=state, blocked_summary=None,
                         revision=rev + 1); return "UPDATE 1"
        elif "SET current_step" in sql:
            step, state, now, fid, rev = args
            r = self.rows.get(fid)
            if r and r["revision"] == rev:
                r.update(current_step=step, state_json=state, revision=rev + 1); return "UPDATE 1"
        elif "status = 'completed'" in sql:
            r = self.rows.get(args[1])
            if r and r["status"] in ("running", "waiting"):
                r["status"] = "completed"
                if len(args) > 2:
                    r["state_json"] = args[2]
                return "UPDATE 1"
        elif "status = 'failed'" in sql:
            err, now, fid = args
            r = self.rows.get(fid)
            if r and r["status"] in ("running", "waiting"):
                r.update(status="failed", blocked_summary=err); return "UPDATE 1"
        return "UPDATE 0"

    async def fetchrow(self, sql, *args):
        self._maybe_fail(); self.calls.append((sql, args))
        r = self.rows.get(args[0])
        if not r:
            return None
        m = re.search(r"status = '(\w+)'", sql)
        if m and r["status"] != m.group(1):
            return None
        return r

    async def fetch(self, sql, *args):
        self._maybe_fail()
        return [r for r in self.rows.values() if r["status"] in ("running", "waiting")][: args[0]]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_finish_state_is_bound_not_spliced(self):
        # regression: final_state was f-string-spliced into the UPDATE; a quote in it broke/injected the SQL
        pool = FakePool()
        fid = run(tf.create_flow(pool, "g", "s1"))
        evil = {"note": "it's'; DROP TABLE flow_runs;--"}
        self.assertTrue(run(tf.finish_flow(pool, fid, evil)))
        sql, args = pool.calls[-1]
        self.assertNotIn("DROP", sql)
        self.assertIn("state_json = $3", sql)
        self.assertEqual(json.loads(args[2]), evil)

    def test_every_value_is_a_placeholder(self):
        self.assertNotRegex(SRC, r"'\{json\.dumps")
        self.assertNotRegex(SRC, r"=\s*'\{")


class TestPerformance(unittest.TestCase):
    def test_1k_flows_through_full_lifecycle(self):
        pool = FakePool()
        async def go():
            for i in range(1000):
                fid = await tf.create_flow(pool, f"g{i}", "s")
                await tf.advance_step(pool, fid, "s2", {"i": i})
                await tf.finish_flow(pool, fid)
        t0 = time.perf_counter()
        run(go())
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertTrue(all(r["status"] == "completed" for r in pool.rows.values()))


class TestRetry(unittest.TestCase):
    def test_pool_failure_fails_open_everywhere(self):
        # RETRY GAP: every taskflow call — one pool round-trip; failure is logged and a safe default returned
        pool = FakePool(fail_times=99)
        self.assertEqual(run(tf.create_flow(pool, "g", "s")), "")
        self.assertFalse(run(tf.advance_step(pool, "f", "s")))
        self.assertFalse(run(tf.set_waiting(pool, "f", "r")))
        self.assertFalse(run(tf.resume_flow(pool, "f", "s")))
        self.assertFalse(run(tf.finish_flow(pool, "f")))
        self.assertFalse(run(tf.fail_flow(pool, "f", "e")))
        self.assertEqual(run(tf.get_flow(pool, "f")), {})
        self.assertEqual(run(tf.list_active_flows(pool)), [])

    def test_one_failure_then_caller_retry_succeeds(self):
        pool = FakePool(fail_times=1)
        self.assertEqual(run(tf.create_flow(pool, "g", "s")), "")
        self.assertTrue(run(tf.create_flow(pool, "g", "s")))


class TestUnit(unittest.TestCase):
    def test_now_ms(self):
        self.assertAlmostEqual(tf._now_ms() / 1000, time.time(), delta=2)

    def test_unknown_flow_is_false_or_empty(self):
        pool = FakePool()
        self.assertFalse(run(tf.advance_step(pool, "nope", "s")))
        self.assertFalse(run(tf.resume_flow(pool, "nope", "s")))
        self.assertEqual(run(tf.get_flow(pool, "nope")), {})

    def test_fail_flow_truncates_error(self):
        pool = FakePool()
        fid = run(tf.create_flow(pool, "g", "s"))
        self.assertTrue(run(tf.fail_flow(pool, fid, "x" * 2000)))
        self.assertEqual(len(pool.rows[fid]["blocked_summary"]), 500)

    def test_stale_revision_loses(self):
        pool = FakePool()
        fid = run(tf.create_flow(pool, "g", "s"))
        stale = dict(pool.rows[fid])
        run(tf.advance_step(pool, fid, "s2"))
        async def race():
            orig = pool.fetchrow
            async def old(sql, *a): return stale
            pool.fetchrow = old
            try:
                return await tf.advance_step(pool, fid, "s3")
            finally:
                pool.fetchrow = orig
        self.assertFalse(run(race()))


class TestIntegration(unittest.TestCase):
    def test_wait_then_resume_carries_input(self):
        pool = FakePool()
        fid = run(tf.create_flow(pool, "approve deploy", "ask", {"a": 1}))
        self.assertTrue(run(tf.set_waiting(pool, fid, "needs Jordan", {"q": "ok?"})))
        self.assertFalse(run(tf.advance_step(pool, fid, "x")))           # waiting flows cannot advance
        self.assertTrue(run(tf.resume_flow(pool, fid, "deploy", {"answer": "yes"})))
        f = run(tf.get_flow(pool, fid))
        self.assertEqual((f["status"], f["step"]), ("running", "deploy"))
        self.assertEqual(f["state"], {"a": 1, "last_input": {"answer": "yes"}})
        self.assertIsNone(f["waiting_on"])


class TestFunctional(unittest.TestCase):
    def test_full_lifecycle(self):
        pool = FakePool()
        fid = run(tf.create_flow(pool, "write essay", "outline"))
        self.assertEqual(len(fid), 36)
        self.assertTrue(run(tf.advance_step(pool, fid, "draft", {"words": 900})))
        self.assertEqual(len(run(tf.list_active_flows(pool))), 1)
        self.assertTrue(run(tf.finish_flow(pool, fid, {"done": True})))
        f = run(tf.get_flow(pool, fid))
        self.assertEqual(f["status"], "completed")
        self.assertEqual(f["state"], {"done": True})
        self.assertEqual(run(tf.list_active_flows(pool)), [])
        self.assertFalse(run(tf.finish_flow(pool, fid)))                   # cannot finish twice


class TestFrame(unittest.TestCase):
    def test_import_smoke_by_path(self):
        code = ("import importlib.util, sys; s = importlib.util.spec_from_file_location('t', sys.argv[1]); "
                "m = importlib.util.module_from_spec(s); s.loader.exec_module(m); print(callable(m.create_flow))")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")

    def test_library_module_has_no_entrypoint(self):
        self.assertNotIn("__main__", SRC)
        self.assertNotIn("asyncio.run(", SRC)


if __name__ == "__main__":
    unittest.main()
