#!/usr/bin/env python3
"""Tests for nova_intent_router.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The module pings Redis at import, so redis.Redis is mocked to fail for the load (REDIS_AVAILABLE=False);
cache tests use a fake client. Every backend call goes through a mocked urlopen; the OpenRouter key is
seeded in the module cache so Keychain is never read."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_intent_router.py"
SRC = SCRIPT.read_text()


def _load():
    import redis
    with patch.object(redis, "Redis", side_effect=ConnectionError("no redis in tests")):
        spec = importlib.util.spec_from_file_location("nova_intent_router_t", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


ir = _load()


def _resp(obj):
    r = MagicMock()
    r.read.return_value = json.dumps(obj).encode()
    r.__enter__.return_value = r
    return r


def _ollama(text="hello", n=10):
    return _resp({"message": {"content": text}, "eval_count": n, "eval_duration": 1e9})


def _http_err(code):
    return urllib.error.HTTPError("u", code, "err", {}, None)


class FakeRedis:
    def __init__(self):
        self.kv = {}

    def get(self, k): return self.kv.get(k)
    def setex(self, k, ttl, v): self.kv[k] = v


class _Base(unittest.TestCase):
    def setUp(self):
        boom = MagicMock(side_effect=AssertionError("unmocked outbound"))
        for p in (patch.object(ir.urllib.request, "urlopen", boom), patch("subprocess.run", boom),
                  patch.object(ir, "REDIS_AVAILABLE", False), patch.object(ir, "_openrouter_key_cache", "k-test")):
            p.start()
            self.addCleanup(p.stop)


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/_-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"sk-or-v1-[0-9a-f]{20,}")

    def test_no_intent_routes_to_cloud(self):
        self.assertEqual(ir.CLOUD_INTENTS, frozenset())
        self.assertTrue(all(v[0] == ir.Backend.LOCAL for v in ir.INTENT_MAP.values()))

    def test_private_intent_failure_never_touches_cloud(self):
        intent = sorted(ir.PRIVATE_INTENTS)[0]
        with patch.object(ir.urllib.request, "urlopen", side_effect=urllib.error.URLError("down")), \
                patch.object(ir, "query_cloud") as cloud, redirect_stderr(io.StringIO()):
            r = ir.route(intent, "my bank balance")
        cloud.assert_not_called()
        self.assertFalse(r["success"])
        self.assertIn("NEVER be sent to cloud", r["error"])


class TestPerformance(_Base):
    def test_cache_key_10k_fast_and_stable(self):
        t0 = time.perf_counter()
        keys = {ir._cache_key("code_review", f"prompt {i}") for i in range(10_000)}
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(keys), 10_000)
        self.assertEqual(ir._cache_key("a", "b"), ir._cache_key("a", "b"))


class TestRetry(_Base):
    def test_cloud_429_then_500_then_success(self):
        uo = MagicMock(side_effect=[_http_err(429), _http_err(500), _resp({"choices": [{"message": {"content": "ok"}}]})])
        with patch.object(ir.urllib.request, "urlopen", uo), redirect_stderr(io.StringIO()):
            r = ir.query_cloud("p", intent="conversation")
        self.assertEqual(uo.call_count, 3)
        self.assertTrue(r["success"])
        self.assertEqual(r["model"], ir.OPENROUTER_MODEL_FALLBACK)

    def test_cloud_generic_error_retries_once_then_fails_open(self):
        uo = MagicMock(side_effect=OSError("reset"))
        with patch.object(ir.urllib.request, "urlopen", uo):
            r = ir.query_cloud("p")
        self.assertEqual(uo.call_count, 2)
        self.assertEqual((r["success"], r["error"]), (False, "reset"))

    def test_local_ollama_is_one_shot(self):
        # RETRY GAP: _query_ollama — one urlopen attempt; URLError becomes success=False
        uo = MagicMock(side_effect=urllib.error.URLError("refused"))
        with patch.object(ir.urllib.request, "urlopen", uo):
            r = ir.query_local("p", "coder")
        self.assertEqual(uo.call_count, 1)
        self.assertIn("Ollama unavailable", r["error"])


class TestUnit(_Base):
    def test_unknown_model_key(self):
        self.assertFalse(ir.query_local("p", "nope")["success"])

    def test_temperature_option_override_and_default(self):
        with patch.object(ir.urllib.request, "urlopen", return_value=_ollama()) as uo:
            ir.query_local("p", "coder", intent="code_review")
            ir.query_local("p", "coder", intent="zzz", options={"temperature": 0.11})
        temps = [json.loads(c.args[0].data)["options"]["temperature"] for c in uo.call_args_list]
        self.assertEqual(temps, [0.30, 0.11])

    def test_image_generation_needs_no_llm(self):
        r = ir.route("image_generation", "a cat")
        self.assertEqual(r["backend"], "swarmui")

    def test_cloud_without_key(self):
        with patch.object(ir, "_load_openrouter_key", return_value=""):
            self.assertIn("key not found", ir.query_cloud("p")["error"])


class TestIntegration(_Base):
    def test_mlx_picks_served_model_id(self):
        models = _resp({"data": [{"id": "/x/other"}, {"id": "/Volumes/y/qwen2.5-32b-4bit"}]})
        chat = _resp({"choices": [{"message": {"content": "hi"}}], "usage": {"completion_tokens": 3}})
        with patch.object(ir.urllib.request, "urlopen", side_effect=[models, chat]) as uo:
            r = ir.query_local("p", "mlx_general", system="sys")
        body = json.loads(uo.call_args_list[1].args[0].data)
        self.assertEqual(body["model"], "/Volumes/y/qwen2.5-32b-4bit")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual((r["backend"], r["tokens"]), ("mlx", 3))

    def test_route_caches_non_voice_and_serves_cached(self):
        fake = FakeRedis()
        with patch.object(ir, "REDIS_AVAILABLE", True), patch.object(ir, "_redis_client", fake), \
                patch.object(ir.urllib.request, "urlopen", return_value=_ollama("v1")) as uo:
            a = ir.route("code_review", "def f(): pass")
            b = ir.route("code_review", "def f(): pass")
            ir.route("conversation", "hi")
            ir.route("conversation", "hi")
        self.assertEqual(uo.call_count, 3)       # 1 cached code_review + 2 fresh conversations
        self.assertTrue(b["cached"])
        self.assertEqual(a["response"], b["response"])


class TestFunctional(_Base):
    def test_voice_intent_injects_nova_identity(self):
        with patch.object(ir.urllib.request, "urlopen", return_value=_ollama("hey Jordan")) as uo:
            r = ir.route("slack_reply", "good morning")
        msgs = json.loads(uo.call_args.args[0].data)["messages"]
        self.assertEqual(msgs[0]["content"], ir.NOVA_SYSTEM_PROMPT)
        self.assertEqual((r["success"], r["intent"], r["response"]), (True, "slack_reply", "hey Jordan"))

    def test_unknown_intent_goes_local_mlx(self):
        with patch.object(ir, "query_local", return_value={"success": True, "response": "x"}) as ql, \
                redirect_stderr(io.StringIO()) as err:
            ir.route("made_up_intent", "p")
        self.assertEqual(ql.call_args.args[1], "mlx_general")
        self.assertIn("Unknown intent", err.getvalue())

    def test_cli_list_intents(self):
        with patch.object(sys, "argv", ["x", "--list-intents"]), redirect_stdout(io.StringIO()) as out:
            ir.main()
        self.assertIn("code_review", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_with_redis_stubbed_exits_zero(self):
        code = ("import sys, types, runpy\n"
                "r = types.ModuleType('redis')\n"
                "def _no(*a, **k): raise ConnectionError('offline')\n"
                "r.Redis = _no\n"
                "sys.modules.update({'redis': r})\n"
                f"sys.argv = [{str(SCRIPT)!r}, '--help']\n"
                f"runpy.run_path({str(SCRIPT)!r}, run_name='__main__')\n")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--list-intents", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertFalse(ir.REDIS_AVAILABLE)


if __name__ == "__main__":
    unittest.main()
