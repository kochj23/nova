#!/usr/bin/env python3
"""Tests for nova_mlx_chat.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). The curl subprocess is mocked throughout; no MLX server is touched.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_mlx_chat.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("mlxchat", SCRIPTS / "nova_mlx_chat.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mc = _load()


def _ok(obj, rc=0):
    return SimpleNamespace(returncode=rc, stdout=json.dumps(obj) if obj is not None else "", stderr="")


MODELS = {"data": [{"id": "mlx-community/Qwen3-8B-4bit"}, {"id": "m2"}]}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_prompt_rides_in_json_body_not_shell(self):
        self.assertNotIn("shell=True", SRC)
        evil = "$(rm -rf ~); `id` \"quoted\""
        with patch.object(mc.subprocess, "run", return_value=_ok({"choices": [{"message": {"content": "x"}}]})) as r:
            mc.MLXChatClient("http://127.0.0.1:5000").query(evil)
        argv = r.call_args[0][0]
        self.assertIsInstance(argv, list)
        body = json.loads(argv[argv.index("-d") + 1])
        self.assertEqual(body["messages"][-1]["content"], evil)


class TestPerformance(unittest.TestCase):
    def test_detect_cache_avoids_10k_subprocesses(self):
        c = mc.MLXChatClient("http://127.0.0.1:5000")
        with patch.object(mc.subprocess, "run", return_value=_ok({"version": "1"})) as r:
            t0 = time.perf_counter()
            for _ in range(10_000):
                c.detect(fast=True)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(r.call_count, 1)


class TestRetry(unittest.TestCase):
    def test_query_failure_fails_open(self):
        # RETRY GAP: MLXChatClient.query — one curl attempt; timeout/error returns None
        calls = []

        def boom(*a, **k):
            calls.append(1); raise subprocess.TimeoutExpired("curl", 32)

        c = mc.MLXChatClient()
        with patch.object(mc.subprocess, "run", side_effect=boom), redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(c.query("hi"))
        self.assertEqual(len(calls), 1)
        self.assertIn("Error querying MLX", err.getvalue())

    def test_probes_fail_open(self):
        c = mc.MLXChatClient()
        with patch.object(mc.subprocess, "run", side_effect=OSError("no curl")):
            self.assertFalse(c.detect()["running"])
            self.assertFalse(c.health_check()["mlx_ready"])
            self.assertEqual(c.list_models(), [])
            self.assertIsNone(c.current_model())


class TestUnit(unittest.TestCase):
    def test_detect_parses_port_and_version(self):
        c = mc.MLXChatClient("http://127.0.0.1:5050")
        with patch.object(mc.subprocess, "run", return_value=_ok({"version": "0.9"})):
            d = c.detect()
        self.assertEqual((d["running"], d["port"], d["version"]), (True, 5050, "0.9"))
        with patch.object(mc.subprocess, "run", return_value=_ok(None)):
            self.assertFalse(mc.MLXChatClient().detect()["running"])

    def test_query_edges(self):
        c = mc.MLXChatClient()
        with patch.object(mc.subprocess, "run", return_value=_ok({"choices": []})):
            self.assertIsNone(c.query("x"))
        with patch.object(mc.subprocess, "run", return_value=_ok({"choices": [{"message": {"content": "  hi  "}}]})) as r:
            self.assertEqual(c.query("x", system="be brief", max_tokens=9), "hi")
        body = json.loads(r.call_args[0][0][-1])
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])
        self.assertEqual(body["max_tokens"], 9)


class TestIntegration(unittest.TestCase):
    def test_endpoints_and_models_chain(self):
        c = mc.MLXChatClient("http://127.0.0.1:5050")
        with patch.object(mc.subprocess, "run", return_value=_ok(MODELS)) as r:
            self.assertEqual(c.current_model(), "mlx-community/Qwen3-8B-4bit")
            h = c.health_check()
        urls = [call[0][0][-1] for call in r.call_args_list]
        self.assertEqual(urls, ["http://127.0.0.1:5050/v1/models"] * 2)
        self.assertEqual(h["models_loaded"], ["mlx-community/Qwen3-8B-4bit", "m2"])


class TestFunctional(unittest.TestCase):
    def _main(self, argv, run):
        with patch.object(mc.subprocess, "run", **run), patch.object(sys, "argv", ["x"] + argv), \
             redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            rc = mc.main()
        return rc, out.getvalue()

    def test_prompt_golden_path_json(self):
        rc, out = self._main(["--prompt", "hello", "--json"],
                             {"return_value": _ok({"choices": [{"message": {"content": "hey"}}]})})
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out), {"success": True, "model": mc.DEFAULT_MODEL, "response": "hey"})

    def test_offline_and_no_command(self):
        rc, out = self._main(["--detect"], {"side_effect": OSError("down")})
        self.assertEqual(rc, 1)
        self.assertIn("offline", out)
        rc, out = self._main(["--list-models", "--json"], {"return_value": _ok(MODELS)})
        self.assertEqual((rc, len(json.loads(out)["models"])), (0, 2))
        rc, out = self._main([], {"return_value": _ok(None)})
        self.assertEqual(rc, 1)
        self.assertIn("usage", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_mlx_chat.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--health-check", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_mlx_chat"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
