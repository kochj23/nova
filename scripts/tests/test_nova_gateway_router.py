#!/usr/bin/env python3
"""Tests for nova_gateway/router.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_gateway" / "router.py"
SRC = SCRIPT.read_text()

with patch("psycopg2.connect", side_effect=OSError("offline test")):
    import nova_gateway.router as rt


class _Resp:
    def __init__(self, data, status=200):
        self.data = data; self.status_code = status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _Http:
    """Fake async http client: get() answers health, post() answers per-URL fragment."""
    def __init__(self, posts=None, healthy=True):
        self.posts = posts or {}; self.healthy = healthy; self.calls = []

    async def get(self, url, timeout=None):
        self.calls.append(("GET", url, None))
        return _Resp({"data": [{"id": "m"}]}, 200 if self.healthy else 503)

    async def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append(("POST", url, json))
        for frag, ans in self.posts.items():
            if frag in url:
                if isinstance(ans, Exception):
                    raise ans
                return _Resp(ans)
        raise RuntimeError("no route")


OLLAMA_OK = {"message": {"content": "<think>hmm</think> hello"}}
OAI_OK = {"choices": [{"message": {"content": " from mlx "}}]}


def _route(http, **kw):
    r = rt.ModelRouter()
    with patch.object(rt, "nova_lb", None), patch.object(rt, "_best_url", side_effect=lambda k, d: d), \
            patch.object(rt.ModelRouter, "_log_inference", new=AsyncMock()):
        async def go():
            return await r.route([{"role": "user", "content": "hi"}], ctx=SimpleNamespace(http=http), **kw)
        return r, asyncio.run(go())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/_-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"sk-or-[A-Za-z0-9]")
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))

    def test_private_never_reaches_cloud(self):
        http = _Http(posts={"/api/chat": RuntimeError("x"), "/v1/chat": RuntimeError("y"),
                            "/chat/completions": OAI_OK})
        with self.assertRaises(RuntimeError):
            _route(http, private=True, tokens={"openrouter": "k"})
        self.assertFalse(any(rt.OPENROUTER in u for _, u, _ in http.calls))

    def test_private_content_blocks_openrouter(self):
        http = _Http(posts={"/api/chat": RuntimeError("x"), "/v1/chat": RuntimeError("y")})
        with patch.object(rt, "is_private_content", return_value=True):
            with self.assertRaises(RuntimeError) as e:
                _route(http, tokens={"openrouter": "k"})
        self.assertIn("privacy policy blocked", str(e.exception))


class TestPerformance(unittest.TestCase):
    def test_strip_and_tools_payload_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            rt._strip_thinking(f"<think>reason {i}</think> answer {i}")
        reg = {f"t{i}": {"description": "d", "parameters": {}} for i in range(10_000)}
        self.assertEqual(len(rt.build_tools_payload(reg)), 10_000)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_mid_request_failure_fails_over_to_next_backend(self):
        http = _Http(posts={"/api/chat": RuntimeError("ollama 500"), "/v1/chat/completions": OAI_OK})
        r, out = _route(http)
        self.assertEqual(out, "from mlx")
        self.assertEqual(r.active_backend, "mlx")
        self.assertNotIn("ollama", r._health_cache)          # invalidated for a fresh probe next time
        self.assertEqual(sum(1 for m, u, _ in http.calls if m == "POST"), 2)

    def test_all_backends_down_raises_summary(self):
        with self.assertRaises(RuntimeError) as e:
            _route(_Http(healthy=False))
        self.assertIn("health check failed", str(e.exception))


class TestUnit(unittest.TestCase):
    def test_strip_thinking_edges(self):
        self.assertEqual(rt._strip_thinking("<think>x</think> hi"), "hi")
        self.assertEqual(rt._strip_thinking("bare reasoning</think>\nanswer"), "answer")
        self.assertEqual(rt._strip_thinking("plain"), "plain")
        self.assertEqual(rt._strip_thinking("<think>only</think>"), "<think>only</think>")

    def test_best_url_falls_back_on_pg_failure(self):
        rt._RANK_CACHE.update(ts=0.0, val=None)
        with patch("psycopg2.connect", side_effect=OSError("down")):
            self.assertEqual(rt._best_url("ollama", "http://static:1"), "http://static:1")

    def test_resolve_backend_static_without_lb(self):
        r = rt.ModelRouter()
        with patch.object(rt, "nova_lb", None):
            self.assertEqual(r._resolve_backend("ollama", "http://s"), ("http://s", None))
        lb = SimpleNamespace(pick_node_shared=lambda protocol: {"ip": "10.0.0.9", "port": 11434, "name": "n9"})
        with patch.object(rt, "nova_lb", lb):
            self.assertEqual(r._resolve_backend("ollama", "http://s"), ("http://10.0.0.9:11434", "n9"))
            self.assertEqual(r._resolve_backend("llamacpp", "http://s"), ("http://s", None))


class TestIntegration(unittest.TestCase):
    def test_best_url_reads_llm_ping_ranking_and_prefers_warm_hw_order(self):
        rank = {"ollama": [
            {"url": "http://192.168.1.86:11434", "status": "up", "has_chat_model": True, "loaded": [rt.CHAT_MODEL]},
            {"url": "http://192.168.1.6:11434", "status": "up", "has_chat_model": True, "loaded": [rt.CHAT_MODEL]},
            {"url": "http://192.168.1.5:11434", "status": "down", "has_chat_model": True}]}
        cur = MagicMock(); cur.fetchone.return_value = (json.dumps(rank),)
        conn = MagicMock(); conn.cursor.return_value = cur
        rt._RANK_CACHE.clear(); rt._RANK_CACHE.update(ts=0.0, val=None)
        with patch.object(rt, "CHAT_NODE_ORDER", ["192.168.1.6", "192.168.1.86"]), \
                patch("psycopg2.connect", return_value=conn):
            self.assertEqual(rt._best_url("ollama", "d"), "http://192.168.1.6:11434")
        self.assertIn("key='ranking'", cur.execute.call_args.args[0])
        self.assertIn("nova_llm_ping", cur.execute.call_args.args[0])
        rt._RANK_CACHE.clear(); rt._RANK_CACHE.update(ts=0.0, val=None)


class TestFunctional(unittest.TestCase):
    def test_ollama_golden_path_payload_and_content(self):
        http = _Http(posts={"/api/chat": OLLAMA_OK})
        r, out = _route(http, system="be nice", max_tokens=100)
        # 2026-10-08: thinking off for chat (latency + the 45 s timeouts); stray think blocks are stripped
        self.assertEqual(out, "hello")
        body = [j for m, u, j in http.calls if m == "POST"][0]
        self.assertEqual(body["messages"][0], {"role": "system", "content": "be nice"})
        self.assertEqual(body["options"]["num_predict"], 100)
        self.assertFalse(body["think"])

    def test_raw_response_carries_tool_calls(self):
        http = _Http(posts={"/api/chat": {"message": {"content": "", "tool_calls": [{"function": {"name": "f"}}]}}})
        _, out = _route(http, raw_response=True, tools=[{"type": "function"}])
        self.assertEqual(out["choices"][0]["message"]["tool_calls"][0]["function"]["name"], "f")

    def test_status_serves_cache_without_blocking(self):
        r = rt.ModelRouter()
        r._health_cache["ollama"] = (True, time.time())
        with patch.object(r, "_check_health", new=AsyncMock(return_value=False)):
            s = asyncio.run(r.status())
        self.assertTrue(s["ollama"]["healthy"])
        self.assertFalse(s["mlx"]["healthy"])
        self.assertEqual(s["active"], "unknown")


class TestFailover20261008(unittest.TestCase):
    """2026-10-08: the 'Something went wrong on my end' replies — one slow ollama node timed out (empty error
    text), MLX/llama.cpp could not take the request, and the turn died. Now: a second ollama node is tried,
    llama.cpp gets a request that fits its 8k context, and errors carry their type."""

    def test_second_ollama_node_answers_when_first_times_out(self):
        class H(_Http):
            async def post(self, url, json=None, headers=None, timeout=None):
                self.calls.append(("POST", url, json))
                if url.startswith("http://slow"):
                    import httpx
                    raise httpx.ReadTimeout("")
                return _Resp({"message": {"content": "from the second node"}})
        http = H()
        with patch.object(rt, "_ollama_candidates", return_value=["http://slow:1", "http://fast:2"]):
            _, out = _route(http)
        self.assertEqual(out, "from the second node")
        self.assertEqual([u for m, u, j in http.calls if m == "POST"],
                         ["http://slow:1/api/chat", "http://fast:2/api/chat"])

    def test_candidates_put_sticky_first_then_warm_hw_order(self):
        rt._RANK_CACHE.clear()
        rt._RANK_CACHE.update(ts=9e18, last_ollama="http://192.168.1.7:11434", val={"ollama": [
            {"url": "http://192.168.1.86:11434", "status": "up", "has_chat_model": True, "loaded": [rt.CHAT_MODEL]},
            {"url": "http://192.168.1.7:11434", "status": "up", "has_chat_model": True, "loaded": [rt.CHAT_MODEL]},
            {"url": "http://192.168.1.6:11434", "status": "up", "has_chat_model": True, "loaded": [rt.CHAT_MODEL]},
        ]})
        try:
            with patch.object(rt, "OLLAMA_TRIES", 3):
                self.assertEqual(rt._ollama_candidates("http://d"),
                                 ["http://192.168.1.7:11434", "http://192.168.1.6:11434", "http://192.168.1.86:11434"])
            rt._forget_sticky("http://192.168.1.7:11434")
            self.assertNotIn("last_ollama", rt._RANK_CACHE)
        finally:
            rt._RANK_CACHE.clear(); rt._RANK_CACHE.update(ts=0.0, val=None)

    def test_llamacpp_gets_compact_request_without_tools(self):
        http = _Http(posts={"/api/chat": RuntimeError("x"), "/v1/chat/completions": OAI_OK})
        r = rt.ModelRouter()
        r.BACKENDS = [b for b in r.BACKENDS if b[0] == "llamacpp"]
        big = [{"role": "user", "content": "q" * 50_000}] * 10
        with patch.object(rt, "nova_lb", None), patch.object(rt.ModelRouter, "_log_inference", new=AsyncMock()):
            out = asyncio.run(r.route(big, system="S" * 60_000, tools=[{"type": "function"}],
                                      ctx=SimpleNamespace(http=http)))
        self.assertEqual(out, "from mlx")
        body = [j for m, u, j in http.calls if m == "POST"][0]
        self.assertNotIn("tools", body)
        chars = sum(len(m["content"]) for m in body["messages"])
        self.assertLess(chars / 3.2, rt.LLAMACPP_CTX)

    def test_llamacpp_context_overflow_retries_smaller(self):
        class H(_Http):
            n = 0
            async def post(self, url, json=None, headers=None, timeout=None):
                self.calls.append(("POST", url, {**json, "messages": list(json["messages"])}))
                if "/api/chat" in url:
                    raise RuntimeError("ollama down")
                H.n += 1
                if H.n == 1:
                    r = _Resp({"error": {}}, 400); r.text = "exceed_context_size_error"; return r
                return _Resp(OAI_OK)
        http = H()
        r = rt.ModelRouter(); r.BACKENDS = [b for b in r.BACKENDS if b[0] == "llamacpp"]
        with patch.object(rt, "nova_lb", None), patch.object(rt.ModelRouter, "_log_inference", new=AsyncMock()):
            out = asyncio.run(r.route([{"role": "user", "content": "hi"}], system="S" * 40_000,
                                      ctx=SimpleNamespace(http=http)))
        self.assertEqual(out, "from mlx")
        first, second = [j for m, u, j in http.calls if m == "POST"]
        self.assertLess(len(second["messages"][0]["content"]), len(first["messages"][0]["content"]))

    def test_errors_name_their_type(self):
        import httpx
        self.assertEqual(rt._err(httpx.ReadTimeout("")), "ReadTimeout")
        self.assertEqual(rt._err(ValueError("bad\nmore")), "ValueError: bad")

    def test_budget_exhausted_stops_the_chain(self):
        http = _Http(posts={"/api/chat": OLLAMA_OK})
        with self.assertRaises(RuntimeError) as cm:
            _route(http, budget_s=0.5)
        self.assertIn("budget", str(cm.exception))


class TestFrame(unittest.TestCase):
    def test_import_has_no_side_effects(self):
        self.assertNotIn('if __name__ == "__main__":', SRC)
        code = ("import sys;sys.path.insert(0,'.');import psycopg2;"
                "psycopg2.connect=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
                "import nova_gateway.router as r;print(r.ModelRouter.HEALTH_TTL)")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "30.0")


if __name__ == "__main__":
    unittest.main()
