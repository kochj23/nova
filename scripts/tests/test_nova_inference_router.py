#!/usr/bin/env python3
"""Tests for nova_inference_router.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Backends are mocked at urlopen; the HTTP handler is driven in memory
(no port is ever bound, no health loop started). Written by Jordan Koch (via Claude)."""
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
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_inference_router.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("infrouter", SCRIPTS / "nova_inference_router.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ir = _load()


def _st(healthy=True, models=(), inflight=0, gen_lat=2.0, ok=0, fail=0, kind="ollama"):
    return {"healthy": healthy, "models": set(models), "inflight": inflight, "lat": 1.0,
            "gen_lat": gen_lat, "ok": ok, "fail": fail, "kind": kind}


class _Resp:
    def __init__(self, body=b"{}", status=200):
        self.body = body; self.status = status; self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self.body


def _handler(path, payload=None, raw=None):
    h = ir.Handler.__new__(ir.Handler)
    body = raw if raw is not None else json.dumps(payload or {}).encode()
    h.path = path; h.command = "POST"; h.request_version = "HTTP/1.1"; h.requestline = "POST " + path
    h.client_address = ("127.0.0.1", 0)
    h.headers = {"Content-Length": str(len(body))}
    h.rfile = io.BytesIO(body); h.wfile = io.BytesIO()
    return h


def _status(h):
    return int(h.wfile.getvalue().split(b" ", 2)[1])


def _body(h):
    return json.loads(h.wfile.getvalue().split(b"\r\n\r\n", 1)[1])


class _Base(unittest.TestCase):
    def setUp(self):
        self._saved = dict(ir._state)
        ir._state.clear()

    def tearDown(self):
        ir._state.clear(); ir._state.update(self._saved)


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_unknown_model_never_proxied(self):
        h = _handler("/api/chat", {"model": "http://evil.example/x"})
        with patch.object(ir.urllib.request, "urlopen") as uo:
            h.do_POST()
        uo.assert_not_called()
        self.assertEqual(_status(h), 503)

    def test_bad_json_rejected(self):
        h = _handler("/api/chat", raw=b"{not json")
        h.do_POST()
        self.assertEqual(_status(h), 400)

    def test_upstream_only_registry_hosts(self):
        hosts = {h for pool in ir.POOLS.values() for h, _, _, _ in pool}
        self.assertTrue(all(h.startswith("nova-core") for h in hosts))


class TestPerformance(_Base):
    def test_pick_10k_fast(self):
        for h, p, k, m in ir.POOLS["fast"]:
            ir._state[(h, p)] = _st(models=[m], kind=k)
        t0 = time.perf_counter()
        for _ in range(10_000):
            ir.route("fast")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(_Base):
    def test_mlx_probe_tries_paths_until_one_answers(self):
        calls = []

        def uo(url, timeout=None):
            calls.append(url)
            if len(calls) < 3:
                raise OSError("refused")
            return _Resp()

        with patch.object(ir.urllib.request, "urlopen", side_effect=uo):
            self.assertEqual(ir._probe("h", 5050, "mlx"), (True, set()))
        self.assertEqual([c.rsplit(":5050", 1)[1] for c in calls], ["/v1/models", "/health", "/"])

    def test_proxy_failure_is_502_and_counted(self):
        # RETRY GAP: Handler._proxy — one upstream attempt; failure returns 502 and bumps the fail count
        # (feeds the bandit), inflight is always released.
        key = (ir.N6, 11434)
        ir._state[key] = _st(models=["qwen3:30b-a3b"])
        h = _handler("/api/chat", {"model": "code"})
        with patch.object(ir, "_pick", return_value=(ir.N6, 11434, "ollama", "qwen3:30b-a3b")), \
             patch.object(ir.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            h.do_POST()
        self.assertEqual(uo.call_count, 1)
        self.assertEqual(_status(h), 502)
        self.assertEqual((ir._state[key]["fail"], ir._state[key]["inflight"]), (1, 0))


class TestUnit(_Base):
    def test_resolve(self):
        self.assertEqual(ir._resolve("code")[0], "code")
        self.assertEqual(ir._resolve("deepseek-r1:8b")[0], "reasoner")
        self.assertEqual(ir._resolve("nope"), (None, None))

    def test_eligible_filters(self):
        pool = [("a", 1, "ollama", "m"), ("b", 1, "ollama", "m"), ("c", 1, "mlx", "x"), ("d", 1, "ollama", "m")]
        ir._state[("a", 1)] = _st(models=["m"])
        ir._state[("b", 1)] = _st(models=["other"])           # model not pulled
        ir._state[("c", 1)] = _st(kind="mlx")                 # mlx trusts registry
        ir._state[("d", 1)] = _st(healthy=False, models=["m"])
        self.assertEqual([e[0] for e in ir._eligible(pool)], ["a", "c"])

    def test_inflight_cap_and_least_loaded(self):
        ir._state[(ir.N7, 11434)] = _st(models=["llama3.2:3b"], inflight=2)
        pool = [(ir.N7, 11434, "ollama", "llama3.2:3b"), ("x", 1, "ollama", "llama3.2:3b")]
        ir._state[("x", 1)] = _st(models=["llama3.2:3b"], inflight=5)
        self.assertEqual(ir._pick(pool)[0], "x")              # N7 at its cap of 2
        ir._state[(ir.N7, 11434)]["inflight"] = 0
        self.assertEqual(ir._pick(pool)[0], ir.N7)
        self.assertIsNone(ir._pick([]))

    def test_score_without_bandit(self):
        with patch.object(ir, "BANDIT", False):
            self.assertEqual(ir._score(("h", 1, "k", "m", 2, 1.5, 0, 0)), 4.5)


class TestIntegration(_Base):
    def test_route_to_probe_composition(self):
        with patch.object(ir.urllib.request, "urlopen",
                          return_value=_Resp(json.dumps({"models": [{"name": "deepseek-r1:8b"}]}).encode())):
            healthy, models = ir._probe(ir.N6, 11434, "ollama")
        ir._state[(ir.N6, 11434)] = _st(healthy=healthy, models=models)
        self.assertEqual(ir.route("reasoner"), ((ir.N6, 11434, "ollama", "deepseek-r1:8b"), "reasoner"))
        self.assertIn("no healthy backend", ir.route("vision")[1])


class TestFunctional(_Base):
    def test_post_rewrites_model_and_relays(self):
        key = (ir.N6, 11434)
        ir._state[key] = _st(models=["deepseek-r1:8b"])
        sent = {}

        def uo(req, timeout=None):
            sent["url"] = req.full_url; sent["body"] = json.loads(req.data)
            return _Resp(b'{"message": {"content": "hi"}}')

        h = _handler("/api/chat", {"model": "reasoner", "messages": []})
        with patch.object(ir.urllib.request, "urlopen", side_effect=uo):
            h.do_POST()
        self.assertEqual(_status(h), 200)
        self.assertEqual(sent["url"], f"http://{ir.N6}:11434/api/chat")
        self.assertEqual(sent["body"]["model"], "deepseek-r1:8b")
        self.assertEqual(sent["body"]["keep_alive"], ir.KEEP_ALIVE)
        self.assertIn(b"X-Nova-Backend", h.wfile.getvalue())
        self.assertEqual((ir._state[key]["ok"], ir._state[key]["inflight"]), (1, 0))

    def test_health_and_status_endpoints(self):
        ir._state[("a", 1)] = _st(models=["m"])
        h = _handler("/health"); h.command = "GET"; h.do_GET()
        self.assertTrue(_body(h)["ok"])
        h = _handler("/pool/status"); h.command = "GET"; h.do_GET()
        self.assertIn("a:1", _body(h)["backends"])
        h = _handler("/nope"); h.command = "GET"; h.do_GET()
        self.assertEqual(_status(h), 404)


class TestFrame(unittest.TestCase):
    def test_import_never_starts_server(self):
        # main() binds :37475 and starts the probe loop, so the smoke is an import only
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_inference_router, threading; "
                            "print(threading.active_count())"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1")


if __name__ == "__main__":
    unittest.main()
