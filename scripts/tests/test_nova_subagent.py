#!/usr/bin/env python3
"""Tests for nova_subagent.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_subagent.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="subagent-test-"))


def _stub_modules():
    cfg = types.ModuleType("nova_config"); cfg.SLACK_NOTIFY = "C_TEST_NOTIFY"; cfg.post_both = MagicMock()
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock()
    lg = types.ModuleType("nova_logger"); lg.log = MagicMock()
    lg.LOG_INFO, lg.LOG_ERROR, lg.LOG_WARN, lg.LOG_DEBUG = "info", "error", "warn", "debug"
    rd = types.ModuleType("redis"); rd.from_url = MagicMock(side_effect=OSError("offline: redis stubbed"))
    return {"nova_config": cfg, "nova_notify": nn, "nova_logger": lg, "redis": rd}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()), patch.object(Path, "home", classmethod(lambda c: TMP)):
        spec.loader.exec_module(mod)
    return mod


sa = _load("subagent_under_test", SCRIPT)
assert str(sa.REGISTRY_PATH).startswith(str(TMP))
# Module-level stubs: no Ollama/MLX/memory server, no Slack, no real Redis.
sa.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=sa.urllib.request.Request,
                                                                urlopen=MagicMock(side_effect=OSError("offline"))),
                                  parse=sa.urllib.parse)
sa.nova_notify = MagicMock()
sa.log = MagicMock()


class _Redis:
    def __init__(self, messages=()):
        self.kv, self.hashes, self.published, self.deleted = {}, {}, [], []
        self.ps = types.SimpleNamespace(subscribed=[], unsubscribed=0, _msgs=list(messages))
        self.ps.subscribe = lambda ch: self.ps.subscribed.append(ch)
        self.ps.unsubscribe = lambda: setattr(self.ps, "unsubscribed", self.ps.unsubscribed + 1)
        self.ps.get_message = self._get_message

    def _get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        return self.ps._msgs.pop(0) if self.ps._msgs else None

    def pubsub(self): return self.ps
    def set(self, k, v, ex=None): self.kv[k] = (v, ex)
    def hset(self, k, mapping=None): self.hashes[k] = dict(mapping)
    def delete(self, k): self.deleted.append(k)
    def publish(self, ch, payload): self.published.append((ch, payload))


def _agent(messages=(), handle=None):
    r = _Redis(messages)

    class Echo(sa.SubAgent):
        name = "echo"; channels = ["email"]; description = "test agent"

        async def handle(self, task):
            if handle:
                return await handle(self, task)
            return {"echo": task.get("prompt")}

    with patch.object(sa.redis, "from_url", MagicMock(return_value=r)):
        a = Echo()
    return a, r


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertEqual(sa.REDIS_URL, "redis://localhost:6379"); self.assertNotIn("@", sa.REDIS_URL)

    def test_backends_are_loopback_only(self):
        self.assertTrue(sa.OLLAMA_URL.startswith("http://127.0.0.1:")); self.assertTrue(sa.MLX_URL.startswith("http://127.0.0.1:"))
        self.assertNotIn("subprocess", SRC); self.assertNotIn("psycopg2", SRC)

    def test_recall_query_is_url_encoded(self):
        a, r = _agent()
        uo = MagicMock(return_value=_Resp({"memories": [1]}))
        with patch.object(sa.urllib.request, "urlopen", uo):
            self.assertEqual(asyncio.run(a.recall("a b&c=d", n=2, source="s")), [1])
        self.assertIn("q=a%20b%26c%3Dd&n=2&source=s", uo.call_args[0][0])

    def test_task_payload_is_data_not_code(self):
        bad = {"type": "x", "data": "__import__('os').system('rm -rf /')"}
        a, r = _agent([{"type": "message", "channel": "nova:task:email", "data": json.dumps(bad)}, None])
        async def go():
            a._running = True
            async def stop():
                await asyncio.sleep(0.05); a._running = False
            asyncio.create_task(stop())
            with patch.object(sa, "HEARTBEAT_INTERVAL", 1000):
                await a._main_loop()
        asyncio.run(go())
        self.assertEqual(json.loads(r.published[0][1])["echo"], None)   # handled as a dict, never evaluated


class TestPerformance(unittest.TestCase):
    def test_loop_drains_10k_tasks_quickly(self):
        msgs = [{"type": "message", "channel": "nova:task:email", "data": json.dumps({"id": i, "prompt": f"p{i}"})} for i in range(10_000)]
        a, r = _agent(msgs)
        async def go():
            a._running = True
            async def stop():
                while r.ps._msgs:
                    await asyncio.sleep(0.01)
                a._running = False
            asyncio.create_task(stop())
            with patch.object(sa, "HEARTBEAT_INTERVAL", 1000):
                await a._main_loop()
        t0 = time.perf_counter(); asyncio.run(go())
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(len(r.published), 10_000); self.assertEqual(a._task_count, 10_000)

    def test_registry_round_trip_with_10k_runs_is_fast(self):
        a, r = _agent()
        reg = {"version": 2, "runs": {f"agent{i}": {"status": "stopped"} for i in range(10_000)}}
        t0 = time.perf_counter()
        a._save_registry(reg); self.assertEqual(len(a._load_registry()["runs"]), 10_000)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_inference_failure_propagates_once(self):
        # RETRY GAP: _infer_ollama / _infer_mlx — one urlopen each; failure is logged and re-raised to the handler
        a, r = _agent()
        uo = MagicMock(side_effect=OSError("ollama down"))
        with patch.object(sa.urllib.request, "urlopen", uo):
            with self.assertRaises(OSError):
                asyncio.run(a.infer("hi"))
            a.backend = "mlx"
            with self.assertRaises(OSError):
                asyncio.run(a.infer("hi"))
        self.assertEqual(uo.call_count, 2)

    def test_inference_timeout_becomes_timeouterror(self):
        a, r = _agent()
        async def slow(*args):
            await asyncio.sleep(1.0); return "late"
        with patch.object(a, "_infer_ollama", slow), patch.object(a, "INFERENCE_TIMEOUT", 0.05):
            t0 = time.perf_counter()
            with self.assertRaises(TimeoutError):
                asyncio.run(a.infer("hi"))
            self.assertLess(time.perf_counter() - t0, 0.8)

    def test_memory_and_health_fail_open(self):
        # RETRY GAP: recall/remember/is_backend_healthy — single attempt; [] / silent / False
        a, r = _agent()
        uo = MagicMock(side_effect=OSError("memory down"))
        with patch.object(sa.urllib.request, "urlopen", uo):
            self.assertEqual(asyncio.run(a.recall("q")), [])
            asyncio.run(a.remember("fact"))                      # must not raise
            self.assertFalse(a.is_backend_healthy())
        self.assertEqual(uo.call_count, 3)

    def test_handler_error_is_logged_and_loop_continues(self):
        async def boom(self_, task):
            if task.get("prompt") == "bad":
                raise RuntimeError("handler exploded")
            return {"ok": task["prompt"]}
        msgs = [{"type": "message", "channel": "nova:task:email", "data": json.dumps({"prompt": "bad"})},
                {"type": "message", "channel": "nova:task:email", "data": "not json"},
                {"type": "message", "channel": "nova:task:email", "data": json.dumps({"prompt": "good"})}]
        a, r = _agent(msgs, handle=boom)
        async def go():
            a._running = True
            async def stop():
                await asyncio.sleep(0.1); a._running = False
            asyncio.create_task(stop())
            with patch.object(sa, "HEARTBEAT_INTERVAL", 1000):
                await a._main_loop()
        asyncio.run(go())
        self.assertEqual(len(r.published), 1); self.assertEqual(json.loads(r.published[0][1])["ok"], "good")
        self.assertEqual(a._task_count, 2)            # counted after json.loads: the non-JSON frame never counts as a task
        self.assertIn("Expecting value", a._last_error)  # last error = the JSON decode failure that followed "handler exploded"


class TestUnit(unittest.TestCase):
    def test_abstract_handle_is_enforced(self):
        class Bad(sa.SubAgent):
            name = "bad"
        with patch.object(sa.redis, "from_url", MagicMock(return_value=_Redis())):
            with self.assertRaises(TypeError):
                Bad()

    def test_registry_register_and_deregister(self):
        a, r = _agent()
        sa.REGISTRY_PATH.write_text("{corrupt")
        self.assertEqual(a._load_registry(), {"version": 2, "runs": {}})
        a._task_count = 7; a._register()
        reg = a._load_registry()["runs"]["echo"]
        self.assertEqual((reg["status"], reg["model"], reg["channels"], reg["pid"]), ("running", "deepseek-r1:8b", ["email"], os.getpid()))
        self.assertEqual(r.kv["nova:agent:echo:status"], ("running", 90))
        a._deregister()
        reg = a._load_registry()["runs"]["echo"]
        self.assertEqual((reg["status"], reg["task_count"]), ("stopped", 7)); self.assertIn("stopped_at", reg)
        self.assertEqual(r.deleted, ["nova:agent:echo:status"])

    def test_infer_payloads_per_backend(self):
        a, r = _agent()
        uo = MagicMock(return_value=_Resp({"response": "ollama says hi"}))
        with patch.object(sa.urllib.request, "urlopen", uo):
            self.assertEqual(asyncio.run(a.infer("hi", system="sys", temperature=0.9, max_tokens=12)), "ollama says hi")
        req = uo.call_args[0][0]; body = json.loads(req.data)
        self.assertEqual(req.full_url, "http://127.0.0.1:11434/api/generate")
        self.assertEqual(body, {"model": "deepseek-r1:8b", "prompt": "hi", "system": "sys", "stream": False, "options": {"temperature": 0.9, "num_predict": 12}})
        a.backend = "mlx"
        uo = MagicMock(return_value=_Resp({"choices": [{"message": {"content": "mlx says hi"}}]}))
        with patch.object(sa.urllib.request, "urlopen", uo):
            self.assertEqual(asyncio.run(a.infer("hi", model="qwen")), "mlx says hi")
        body = json.loads(uo.call_args[0][0].data)
        self.assertEqual(body["model"], "qwen"); self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])
        a.backend = "nope"
        with self.assertRaises(ValueError):
            asyncio.run(a.infer("hi"))

    def test_dispatch_stamps_id_and_time(self):
        r = _Redis()
        with patch.object(sa.redis, "from_url", MagicMock(return_value=r)) as fu:
            sa.SubAgent.dispatch("email", {"prompt": "p"})
            sa.SubAgent.dispatch("email", {"id": "fixed", "prompt": "p"}, redis_url="redis://other:1")
        self.assertEqual(fu.call_args_list[1][0][0], "redis://other:1")
        ch, payload = r.published[0]; t = json.loads(payload)
        self.assertEqual(ch, "nova:task:email"); self.assertTrue(t["id"].startswith("email-")); self.assertIn("_dispatched_at", t)
        self.assertEqual(json.loads(r.published[1][1])["id"], "fixed")

    def test_is_backend_healthy_urls(self):
        a, r = _agent()
        uo = MagicMock()
        with patch.object(sa.urllib.request, "urlopen", uo):
            self.assertTrue(a.is_backend_healthy()); a.backend = "mlx"; self.assertTrue(a.is_backend_healthy())
        self.assertEqual([c[0][0] for c in uo.call_args_list], ["http://127.0.0.1:11434/api/tags", "http://127.0.0.1:5050/v1/models"])


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_imported_not_reimplemented(self):
        self.assertIn("from nova_notify import notify as nova_notify", SRC); self.assertIn("from nova_logger import log", SRC)
        self.assertNotIn("def log(", SRC); self.assertEqual(sa.SLACK_NOTIFY, "C_TEST_NOTIFY")

    def test_slack_helpers_funnel_through_the_notification_bus(self):
        a, r = _agent(); sa.nova_notify = MagicMock()
        asyncio.run(a.notify("*Flag*\nline two\nline three"))
        asyncio.run(a.report_to_jordan(""))
        first, second = sa.nova_notify.call_args_list
        self.assertEqual(first[0][0], "Flag"); self.assertEqual(first[1]["body"], "line two\nline three")
        self.assertEqual((first[1]["level"], first[1]["category"], first[1]["dedup_key"], first[1]["meta"]), ("warning", "subagent", "subagent-echo", {"agent": "echo"}))
        self.assertEqual(second[0][0], "subagent echo"); self.assertIsNone(second[1]["body"])

    def test_remember_targets_the_memory_server_with_agent_source(self):
        a, r = _agent(); uo = MagicMock()
        with patch.object(sa.urllib.request, "urlopen", uo):
            asyncio.run(a.remember("fact", metadata={"k": 1}))
        req = uo.call_args[0][0]
        self.assertEqual(req.full_url, "http://memory-server.digitalnoise.net:18790/remember")
        self.assertEqual(json.loads(req.data), {"text": "fact", "source": "subagent.echo", "metadata": {"k": 1}})

    def test_heartbeat_writes_status_and_meta(self):
        a, r = _agent(); a._running = True; a._start_time = sa.datetime.now(sa.timezone.utc); a._task_count = 3
        async def go():
            with patch.object(sa, "HEARTBEAT_INTERVAL", 0.01):
                t = asyncio.create_task(a._heartbeat_loop()); await asyncio.sleep(0.05); a._running = False; await t
        asyncio.run(go())
        self.assertEqual(r.kv["nova:agent:echo:status"][0], "running")
        self.assertEqual(r.hashes["nova:agent:echo:meta"]["tasks_completed"], "3")


class TestFunctional(unittest.TestCase):
    def test_golden_path_subscribe_handle_publish_deregister(self):
        task = {"id": "t1", "type": "summarize", "prompt": "hello"}
        a, r = _agent([{"type": "message", "channel": "nova:task:email", "data": json.dumps(task)}])
        async def go():
            a._running = True
            async def stop():
                await asyncio.sleep(0.05); a._running = False
            asyncio.create_task(stop())
            with patch.object(sa, "HEARTBEAT_INTERVAL", 1000):
                await a._main_loop()
            a._deregister()
        asyncio.run(go())
        self.assertEqual(r.ps.subscribed, ["nova:task:email", "nova:task:echo", "nova:task:broadcast"])
        ch, payload = r.published[0]; res = json.loads(payload)
        self.assertEqual(ch, "nova:result:echo")
        self.assertEqual((res["echo"], res["_agent"], res["_task_id"]), ("hello", "echo", "t1")); self.assertIn("_completed_at", res)
        self.assertEqual(r.ps.unsubscribed, 1)
        self.assertEqual(a._load_registry()["runs"]["echo"]["status"], "stopped")

    def test_run_installs_signal_handlers_and_always_deregisters(self):
        a, r = _agent()
        async def fake_loop(): raise KeyboardInterrupt
        with patch.object(a, "_main_loop", fake_loop), patch.object(sa.signal, "signal", MagicMock()) as sig, patch.object(a, "_deregister", MagicMock()) as dereg:
            a.run()
        self.assertEqual({c[0][0] for c in sig.call_args_list}, {sa.signal.SIGINT, sa.signal.SIGTERM}); dereg.assert_called_once()
        a._running = True; a._shutdown(); self.assertFalse(a._running)


class TestFrame(unittest.TestCase):
    def test_import_is_a_library_with_no_main(self):
        self.assertNotIn('if __name__ == "__main__":\n', SRC.replace('    if __name__ == "__main__":\n        MyAgent().run()', ""))
        box = TMP / "frame-home"; box.mkdir(exist_ok=True)
        r = subprocess.run([sys.executable, "-c", "import nova_subagent as m; print('IMPORT-OK', m.HEARTBEAT_INTERVAL)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(box)})
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(r.stdout.strip(), "IMPORT-OK 30")


if __name__ == "__main__":
    unittest.main()
