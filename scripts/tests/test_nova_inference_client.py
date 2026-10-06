#!/usr/bin/env python3
"""Tests for nova_inference_client.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_inference_client.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ic = _load("inference_client_under_test", SCRIPT)


def _resp(data):
    r = MagicMock(); r.__enter__.return_value.read.return_value = json.dumps(data).encode(); r.__exit__.return_value = False
    return r


class _Clock:
    """Deterministic time for queue_and_wait: no module-level clocks, no real sleeping."""
    def __init__(self):
        self.now = 1000.0; self.slept = []

    def time(self):
        return self.now

    def sleep(self, s):
        self.slept.append(s); self.now += s


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_loopback_only(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertEqual(ic.QUEUE_URL, "http://127.0.0.1:37470")
        self.assertNotIn("subprocess", SRC); self.assertNotIn("shell=True", SRC)

    def test_prompt_is_json_encoded_never_interpolated_into_the_url(self):
        evil = 'x" ; DROP TABLE x; --\n/../admin'
        with patch.object(ic.urllib.request, "urlopen", return_value=_resp({"request_id": "r1", "queued": True})) as u:
            ic.queue_inference(evil, intent="quick")
        req = u.call_args[0][0]
        self.assertEqual(req.full_url, f"{ic.QUEUE_URL}/queue/submit")
        self.assertEqual(json.loads(req.data)["prompt"], evil)
        self.assertEqual(req.get_method(), "POST")

    def test_request_id_lands_in_the_path_verbatim(self):
        with patch.object(ic.urllib.request, "urlopen", return_value=_resp({"status": "done"})) as u:
            ic.get_result("abc-123")
        self.assertEqual(u.call_args[0][0], f"{ic.QUEUE_URL}/queue/result/abc-123")


class TestPerformance(unittest.TestCase):
    def test_10k_submissions_under_2s(self):
        with patch.object(ic.urllib.request, "urlopen", return_value=_resp({"request_id": "r", "queued": True})):
            t0 = time.perf_counter()
            for i in range(10_000):
                ic.queue_inference(f"prompt {i}", options={"temperature": 0.1})
            self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_submit_is_one_shot_and_fails_open(self):
        # RETRY GAP: queue_inference()/urlopen — one attempt; returns {"error", "queued": False}, never raises
        with patch.object(ic.urllib.request, "urlopen", side_effect=OSError("queue down")) as u:
            r = ic.queue_inference("hi")
        self.assertEqual(u.call_count, 1)
        self.assertEqual(r, {"error": "queue down", "queued": False})

    def test_result_stats_health_fail_open(self):
        # RETRY GAP: get_result()/queue_stats()/queue_health() — each one attempt; safe defaults on failure
        with patch.object(ic.urllib.request, "urlopen", side_effect=OSError("x")):
            self.assertIsNone(ic.get_result("r1"))
            self.assertEqual(ic.queue_stats(), {"error": "x"})
            self.assertEqual(ic.queue_health(), {"error": "x", "ok": False})
            self.assertIsNone(ic.queue_fire_and_forget("hi"))

    def test_queue_and_wait_polls_until_the_deadline_then_times_out(self):
        clk = _Clock()
        u = MagicMock(side_effect=[_resp({"request_id": "r9", "queued": True})] + [_resp({"status": "pending"})] * 100)
        with patch.object(ic.urllib.request, "urlopen", u), patch.object(ic, "time", clk):
            r = ic.queue_and_wait("hi", timeout=1.0)
        self.assertEqual(r, {"error": "timeout", "request_id": "r9"})
        self.assertEqual(len(clk.slept), 5)                               # 1.0s / 0.2s polls, no busy loop
        self.assertEqual(u.call_count, 6)


class TestUnit(unittest.TestCase):
    def test_submit_payload_shape_and_defaults(self):
        with patch.object(ic.urllib.request, "urlopen", return_value=_resp({"request_id": "r1", "queued": True})) as u:
            self.assertEqual(ic.queue_inference("hi"), {"request_id": "r1", "queued": True})
        req = u.call_args[0][0]
        self.assertEqual(json.loads(req.data), {"prompt": "hi", "intent": "conversation", "priority": 2, "system": "",
                                                "model": "", "options": {}, "callback_channel": ""})
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(u.call_args[1]["timeout"], 5)

    def test_get_result_pending_vs_done(self):
        with patch.object(ic.urllib.request, "urlopen", return_value=_resp({"status": "pending"})):
            self.assertIsNone(ic.get_result("r"))
        with patch.object(ic.urllib.request, "urlopen", return_value=_resp({"status": "done", "response": "4"})):
            self.assertEqual(ic.get_result("r")["response"], "4")

    def test_fire_and_forget_returns_the_id_and_passes_the_callback(self):
        with patch.object(ic.urllib.request, "urlopen", return_value=_resp({"request_id": "ff1", "queued": True})) as u:
            self.assertEqual(ic.queue_fire_and_forget("sum", intent="summarize", callback_channel="C1"), "ff1")
        body = json.loads(u.call_args[0][0].data)
        self.assertEqual((body["intent"], body["priority"], body["callback_channel"]), ("summarize", 3, "C1"))


class TestIntegration(unittest.TestCase):
    def test_queue_and_wait_chains_submit_and_poll(self):
        clk = _Clock()
        u = MagicMock(side_effect=[_resp({"request_id": "r2", "queued": True}), _resp({"status": "pending"}),
                                   _resp({"status": "done", "response": "4"})])
        with patch.object(ic.urllib.request, "urlopen", u), patch.object(ic, "time", clk):
            r = ic.queue_and_wait("What is 2+2?", intent="quick", priority=1, timeout=30)
        self.assertEqual(r["response"], "4")
        self.assertEqual(u.call_args_list[1][0][0], f"{ic.QUEUE_URL}/queue/result/r2")
        self.assertEqual(clk.slept, [0.2])

    def test_stats_and_health_hit_their_endpoints(self):
        with patch.object(ic.urllib.request, "urlopen", return_value=_resp({"depth": 3})) as u:
            self.assertEqual(ic.queue_stats(), {"depth": 3})
            self.assertEqual(u.call_args[0][0], f"{ic.QUEUE_URL}/queue/stats")
        with patch.object(ic.urllib.request, "urlopen", return_value=_resp({"ok": True})) as u:
            self.assertEqual(ic.queue_health(), {"ok": True})
            self.assertEqual(u.call_args[0][0], f"{ic.QUEUE_URL}/health")


class TestFunctional(unittest.TestCase):
    def test_golden_path_blocking_call_returns_the_model_answer(self):
        clk = _Clock()
        with patch.object(ic.urllib.request, "urlopen", side_effect=[_resp({"request_id": "g1", "queued": True}),
                                                                      _resp({"status": "done", "response": "Paris"})]), \
             patch.object(ic, "time", clk):
            self.assertEqual(ic.queue_and_wait("capital of France?")["response"], "Paris")
        self.assertEqual(clk.slept, [])

    def test_rejected_submission_short_circuits_without_polling(self):
        u = MagicMock(return_value=_resp({"queued": False, "error": "queue full"}))
        with patch.object(ic.urllib.request, "urlopen", u):
            r = ic.queue_and_wait("hi")
        self.assertEqual(r, {"queued": False, "error": "queue full"})
        self.assertEqual(u.call_count, 1)


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_has_no_entrypoint(self):
        # a library module: no main(), no __main__ block, nothing to run — import must be a no-op
        self.assertNotIn("__main__", SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_inference_client as m; assert callable(m.queue_and_wait)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
