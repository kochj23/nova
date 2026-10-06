#!/usr/bin/env python3
"""Tests for nova_inference_queue.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Redis is an in-memory fake (module _rc), the intent router is mocked, aiohttp handlers are called with
fake request objects, LOG_FILE points at a tempdir, and main() (which binds port 37470) is never run."""
import asyncio
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_inference_queue.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))
TMP = tempfile.TemporaryDirectory()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


iq = _load("nova_inference_queue_t", SCRIPT)
iq.LOG_FILE = Path(TMP.name) / "iq.log"


class FakeRedis:
    def __init__(self):
        self.z, self.kv, self.sets, self.h, self.published = {}, {}, {}, {}, []

    def zcard(self, k): return len(self.z)
    def zadd(self, k, m): self.z.update(m)

    def zpopmin(self, k, count=1):
        items = sorted(self.z.items(), key=lambda kv: kv[1])[:count]
        for m, _ in items:
            del self.z[m]
        return items

    def get(self, k): return self.kv.get(k)
    def setex(self, k, ttl, v): self.kv[k] = v
    def sadd(self, k, v): self.sets.setdefault(k, set()).add(v)
    def srem(self, k, v): self.sets.get(k, set()).discard(v)
    def scard(self, k): return len(self.sets.get(k, ()))
    def publish(self, ch, msg): self.published.append((ch, msg))
    def hincrby(self, k, f, n): self.h[f] = self.h.get(f, 0) + n
    def hgetall(self, k): return {f: str(v) for f, v in self.h.items()}


class _Req:
    def __init__(self, body=None, bad=False, match=None):
        self._body, self._bad, self.match_info = body, bad, match or {}

    async def json(self):
        if self._bad:
            raise ValueError("bad json")
        return self._body


class _Base(unittest.TestCase):
    def setUp(self):
        self.r = FakeRedis()
        iq._rc = self.r
        self.addCleanup(setattr, iq, "_rc", None)
        for k in iq._stats:
            iq._stats[k] = 0
        iq._shutdown = False


def _body(resp):
    return json.loads(resp.text)


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(iq.REDIS_URL, r"//[^/]*:[^/]*@")   # no inline redis password

    def test_submit_rejects_bad_input(self):
        self.assertEqual(asyncio.run(iq.handle_submit(_Req(bad=True))).status, 400)
        self.assertEqual(asyncio.run(iq.handle_submit(_Req({"prompt": "   "}))).status, 400)
        self.assertEqual(self.r.zcard(iq.QUEUE_KEY), 0)


class TestPerformance(_Base):
    def test_submit_10k_p1_fast_and_ordered(self):
        t0 = time.perf_counter()
        for i in range(10_000):   # P1 cap is depth 9999, so exactly one is shed
            iq.submit_request(f"p{i}", "quick", 1)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual((self.r.zcard(iq.QUEUE_KEY), iq._stats["shed"]), (9_999, 1))


class TestRetry(_Base):
    def test_route_failure_stores_error_result_no_retry(self):
        # RETRY GAP: process_request/route — one attempt; the error is stored as the result, never raised
        route = MagicMock(side_effect=RuntimeError("all backends down"))
        with patch.dict(sys.modules, {"nova_intent_router": types.SimpleNamespace(route=route)}), \
                redirect_stdout(io.StringIO()):
            asyncio.run(iq.process_request({"id": "r1", "intent": "q", "prompt": "p", "priority": 2,
                                            "submitted_at": time.time()}))
        self.assertEqual(route.call_count, 1)
        self.assertEqual(json.loads(self.r.get(iq.RESULTS_PREFIX + "r1"))["error"], "all backends down")
        self.assertEqual((iq._stats["errors"], iq._stats["active"]), (1, 0))

    def test_get_result_polls_until_ready(self):
        calls = {"n": 0}

        def get(k):
            calls["n"] += 1
            return json.dumps({"response": "4"}) if calls["n"] >= 3 else None
        self.r.get = get
        with patch.object(iq.time, "sleep"):
            self.assertEqual(iq.get_result("r1", timeout=5), {"response": "4"})
        self.assertEqual(calls["n"], 3)

    def test_get_result_timeout(self):
        clock = iter([0, 0, 100])
        with patch.object(iq.time, "time", side_effect=lambda: next(clock)), patch.object(iq.time, "sleep"):
            self.assertEqual(iq.get_result("r9", timeout=1), {"error": "timeout", "request_id": "r9"})


class TestUnit(_Base):
    def test_load_shedding_thresholds(self):
        for i in range(30):
            iq.submit_request(f"p{i}", "q", 1)
        self.assertTrue(iq.submit_request("bg", "q", 4)["shed"])
        self.assertTrue(iq.submit_request("ok", "q", 3)["queued"])
        self.assertTrue(iq.submit_request("ok", "q", 99)["queued"])     # unknown priority -> 9999
        self.assertEqual(iq._stats["shed"], 1)

    def test_score_orders_priority_then_fifo(self):
        iq.submit_request("late p3", "q", 3)
        iq.submit_request("p1 a", "q", 1)
        iq.submit_request("p1 b", "q", 1)
        order = [json.loads(m)["prompt"] for m, _ in self.r.zpopmin(iq.QUEUE_KEY, count=3)]
        self.assertEqual(order, ["p1 a", "p1 b", "late p3"])


class TestIntegration(_Base):
    def test_process_request_uses_intent_router_and_publishes(self):
        route = MagicMock(return_value={"response": "hi"})
        with patch.dict(sys.modules, {"nova_intent_router": types.SimpleNamespace(route=route)}):
            asyncio.run(iq.process_request({"id": "r2", "intent": "chat", "prompt": "p", "priority": 1,
                                            "system": "", "submitted_at": time.time(), "callback_channel": "cb"}))
        self.assertEqual(route.call_args.kwargs["intent"], "chat")
        self.assertIsNone(route.call_args.kwargs["system"])
        self.assertEqual(json.loads(self.r.get(iq.RESULTS_PREFIX + "r2"))["response"], "hi")
        self.assertEqual(self.r.published[0][0], "cb")
        self.assertEqual(self.r.h, {"completed": 1, "p1_completed": 1})
        self.assertIn("from nova_intent_router import route", SRC)


class TestFunctional(_Base):
    def test_http_submit_result_stats_health(self):
        resp = asyncio.run(iq.handle_submit(_Req({"prompt": "2+2?", "intent": "quick", "priority": 1})))
        self.assertEqual(resp.status, 200)
        rid = _body(resp)["request_id"]
        self.assertEqual(asyncio.run(iq.handle_result(_Req(match={"request_id": rid}))).status, 202)
        self.r.setex(iq.RESULTS_PREFIX + rid, 1, json.dumps({"response": "4"}))
        self.assertEqual(_body(asyncio.run(iq.handle_result(_Req(match={"request_id": rid})))), {"response": "4"})
        self.r.hincrby(iq.STATS_KEY, "p1_completed", 2)
        self.assertEqual(_body(asyncio.run(iq.handle_stats(None)))["by_priority"]["p1"], 2)
        health = _body(asyncio.run(iq.handle_health(None)))
        self.assertEqual((health["ok"], health["queue_depth"]), (True, 1))

    def test_submit_shed_returns_429(self):
        with patch.object(iq, "SHED_THRESHOLDS", {4: 0}):
            self.assertEqual(asyncio.run(iq.handle_submit(_Req({"prompt": "x", "priority": 4}))).status, 429)

    def test_worker_loop_pops_and_dispatches(self):
        iq.submit_request("job", "q", 2)
        seen = []

        async def fake_dispatch(payload):
            seen.append(payload["prompt"])
            iq._semaphore.release()
            iq._shutdown = True

        async def go():
            with patch.object(iq, "_dispatch", fake_dispatch), patch.object(iq.asyncio, "sleep", _nosleep):
                await asyncio.wait_for(iq.worker_loop(), 2)
                await asyncio.sleep(0)
        with redirect_stdout(io.StringIO()):
            asyncio.run(go())
        self.assertEqual(seen, ["job"])


_real_sleep = asyncio.sleep


async def _nosleep(*_a, **_k):
    await _real_sleep(0)


class TestFrame(unittest.TestCase):
    def test_import_never_binds_or_runs(self):
        # the __main__ path binds 0.0.0.0:37470 and loops forever, so the smoke is an import in a child
        code = ("import importlib.util as u;"
                f"s=u.spec_from_file_location('q', {str(SCRIPT)!r}); m=u.module_from_spec(s);"
                "s.loader.exec_module(m); print(m.HTTP_PORT, m._rc)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "37470 None")
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
