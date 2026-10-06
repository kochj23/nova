#!/usr/bin/env python3
"""Tests for nova_canary.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_canary.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _load("canary_under_test", SCRIPT)


def _redis_stub(ok=True):
    red = types.ModuleType("redis")
    if ok:
        red.from_url = lambda url: mock.MagicMock()
    else:
        red.from_url = mock.MagicMock(side_effect=ConnectionError("redis down"))
    return red


def _quick_status(sock_rc=0, sock_exc=None, redis_ok=True, ollama_done=True, ollama_exc=None):
    sock_cls = mock.MagicMock()
    if sock_exc:
        sock_cls.return_value.connect_ex.side_effect = sock_exc
    else:
        sock_cls.return_value.connect_ex.return_value = sock_rc
    resp = mock.MagicMock()
    resp.read.return_value = json.dumps({"done": ollama_done}).encode()
    uo = mock.MagicMock(side_effect=ollama_exc) if ollama_exc else mock.MagicMock(return_value=resp)
    with mock.patch("socket.socket", sock_cls), mock.patch.dict(sys.modules, {"redis": _redis_stub(redis_ok)}), \
         mock.patch.object(C.urllib.request, "urlopen", uo):
        status = C._quick_status()
    return status, sock_cls, uo


def _run_main(topic="topic-abc", status=None, ntfy_exc=None):
    status = status if status is not None else {"gateway": "up", "memory": "up", "scheduler": "up",
                                                   "redis": "up", "ollama_inference": "up"}
    uo = mock.MagicMock(side_effect=ntfy_exc) if ntfy_exc else mock.MagicMock()
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(C, "_get_topic", return_value=topic), mock.patch.object(C, "_quick_status", return_value=status), \
         mock.patch.object(C.urllib.request, "urlopen", uo), redirect_stdout(out), redirect_stderr(err):
        try:
            C.main()
            code = None
        except SystemExit as e:
            code = e.code
    return code, uo, out.getvalue(), err.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_topic(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token|topic)\s*=\s*['\"][A-Za-z0-9+/_-]{12,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"ntfy\.sh/[A-Za-z0-9_-]{6,}")        # topic is never a literal in the URL

    def test_topic_is_read_from_keychain_without_a_shell(self):
        with mock.patch.object(C.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "sekrit-topic\n", "")) as run:
            self.assertEqual(C._get_topic(), "sekrit-topic")
        argv, kw = run.call_args[0][0], run.call_args[1]
        self.assertEqual(argv[:2], ["security", "find-generic-password"])
        self.assertIn("nova-canary-topic", argv)
        self.assertFalse(kw.get("shell", False))
        self.assertNotIn("shell=True", SRC)

    def test_ping_targets_only_loopback_services(self):
        self.assertNotRegex(SRC, r"\b(?!127\.0\.0\.1)\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


class TestPerformance(unittest.TestCase):
    def test_main_hot_path_is_cheap(self):
        status = {f"svc{i}": ("up" if i % 2 else "down") for i in range(10_000)}
        t0 = time.perf_counter()
        for _ in range(50):
            code, uo, _, _ = _run_main(status=status)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertIsNone(code)

    def test_quick_status_has_a_socket_timeout(self):
        _, sock_cls, _ = _quick_status()
        sock_cls.return_value.settimeout.assert_called_with(2)


class TestRetry(unittest.TestCase):
    def test_ntfy_failure_fails_open_with_exit_zero(self):
        # RETRY GAP: main()/ntfy POST — one attempt; an unreachable ntfy.sh is logged and the process exits 0
        # on purpose so the scheduler never marks the canary itself as failed.
        code, uo, out, err = _run_main(ntfy_exc=OSError("unreachable"))
        self.assertEqual(code, 0)
        self.assertEqual(uo.call_count, 1)
        self.assertIn("ntfy.sh unreachable", err)
        self.assertNotIn("Sent", out)

    def test_missing_topic_exits_one_without_posting(self):
        # RETRY GAP: _get_topic — a single Keychain read; empty means exit 1, never a blind post.
        code, uo, _, err = _run_main(topic="")
        self.assertEqual(code, 1)
        uo.assert_not_called()
        self.assertIn("not in Keychain", err)

    def test_probe_failures_fail_open_to_down(self):
        status, _, _ = _quick_status(sock_exc=OSError("boom"), redis_ok=False, ollama_exc=TimeoutError("slow"))
        self.assertEqual(set(status.values()), {"down"})


class TestUnit(unittest.TestCase):
    def test_all_services_up(self):
        status, sock_cls, uo = _quick_status()
        self.assertEqual(status, {"gateway": "up", "memory": "up", "scheduler": "up", "redis": "up", "ollama_inference": "up"})
        self.assertEqual(uo.call_count, 1)

    def test_closed_port_is_down(self):
        status, _, _ = _quick_status(sock_rc=111)
        self.assertEqual(status["gateway"], "down")
        self.assertEqual(status["redis"], "up")

    def test_ollama_not_done_is_down(self):
        status, _, _ = _quick_status(ollama_done=False)
        self.assertEqual(status["ollama_inference"], "down")

    def test_get_topic_strips_whitespace(self):
        with mock.patch.object(C.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "  t1 \n", "")):
            self.assertEqual(C._get_topic(), "t1")


class TestIntegration(unittest.TestCase):
    def test_probes_the_real_nova_ports(self):
        _, sock_cls, uo = _quick_status()
        ports = {args[0][0][1] for args in sock_cls.return_value.connect_ex.call_args_list}
        self.assertEqual(ports, {18792, 18790, 37460})         # gateway v2, memory server, scheduler
        req = uo.call_args[0][0]
        self.assertEqual(req.full_url, "http://127.0.0.1:11434/api/generate")
        body = json.loads(req.data)
        self.assertEqual(body["options"]["num_predict"], 1)      # a real one-token generation, not /api/tags
        self.assertFalse(body["stream"])

    def test_status_shape_drives_the_message(self):
        code, uo, out, _ = _run_main(status={"gateway": "up", "memory": "down", "scheduler": "up", "redis": "down",
                                             "ollama_inference": "up"})
        req = uo.call_args[0][0]
        self.assertEqual(req.data.decode(), "DOWN: memory, redis")
        self.assertEqual(req.headers["Priority"], "high")


class TestFunctional(unittest.TestCase):
    def test_all_up_posts_a_silent_ping_to_the_keychain_topic(self):
        code, uo, out, _ = _run_main(topic="topic-abc")
        self.assertIsNone(code)
        req = uo.call_args[0][0]
        self.assertEqual(req.full_url, "https://ntfy.sh/topic-abc")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.headers["Priority"], "min")
        self.assertEqual(req.headers["Tags"], "robot")
        self.assertTrue(req.headers["Title"].decode().startswith("✓ Nova alive"))
        self.assertEqual(req.data.decode(), "gateway·memory·scheduler·redis all up")
        self.assertEqual(uo.call_args[1]["timeout"], 10)
        self.assertIn("[canary] Sent", out)

    def test_degraded_posts_high_priority(self):
        code, uo, _, _ = _run_main(status={"gateway": "down", "memory": "up", "scheduler": "up", "redis": "up",
                                           "ollama_inference": "up"})
        req = uo.call_args[0][0]
        self.assertEqual(req.headers["Priority"], "high")
        self.assertTrue(req.headers["Title"].decode().startswith("⚠ Nova degraded"))
        self.assertEqual(req.data.decode(), "DOWN: gateway")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_canary"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
