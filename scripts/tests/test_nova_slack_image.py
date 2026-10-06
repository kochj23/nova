#!/usr/bin/env python3
"""Tests for nova_slack_image.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import base64
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
import urllib.request  # noqa: F401
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_slack_image.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nslackimg", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


si = _load()


def _resp(obj):
    r = MagicMock()
    r.read.return_value = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return r


HISTORY = {"messages": [{"files": [{"id": "F123", "name": "cat.jpg", "mimetype": "image/png",
                                    "url_private_download": "https://files.slack.com/F123/cat.jpg"}]}]}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"xox[bp]-\d")

    def test_images_stay_local(self):
        self.assertFalse(si.USE_OPENROUTER)
        with patch.object(si.urllib.request, "urlopen", return_value=_resp({"response": "a cat"})) as uo, \
             patch.object(si, "get_openrouter_key") as key:
            si.analyze_image(b"img")
        self.assertTrue(uo.call_args[0][0].full_url.startswith("http://127.0.0.1:11434/"))
        key.assert_not_called()

    def test_token_from_keychain_in_header_only(self):
        with patch.object(si.subprocess, "run", return_value=MagicMock(stdout="tok\n")) as run:
            self.assertEqual(si.get_slack_token(), "tok")
        self.assertEqual(run.call_args[0][0][:2], ["security", "find-generic-password"])
        with patch.object(si.urllib.request, "urlopen", return_value=_resp(b"bytes")) as uo:
            si.download_slack_file("https://files.slack.com/x.jpg", "SEKRET")
        req = uo.call_args[0][0]
        self.assertNotIn("SEKRET", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Bearer SEKRET")


class TestPerformance(unittest.TestCase):
    def test_history_scan_over_many_files(self):
        hist = {"messages": [{"files": [{"id": f"F{i}", "name": f"n{i}.jpg"} for i in range(100)]} for _ in range(100)]}
        hist["messages"][-1]["files"].append({"id": "FTARGET", "name": "t.jpg", "url_private": "https://f/t"})
        t0 = time.perf_counter()
        with patch.object(si.urllib.request, "urlopen", side_effect=[_resp(hist), _resp(b"img")]):
            data, _, name = si.download_slack_file("FTARGET", "t")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual((data, name), (b"img", "t.jpg"))


class TestRetry(unittest.TestCase):
    def test_download_failure_propagates_once(self):
        # RETRY GAP: download_slack_file — one GET, no retry; the CLI surfaces the error
        with patch.object(si.urllib.request, "urlopen", side_effect=OSError("timeout")) as uo:
            with self.assertRaises(OSError):
                si.download_slack_file("F1", "t")
        self.assertEqual(uo.call_count, 1)

    def test_ollama_failure_propagates_once(self):
        # RETRY GAP: analyze_image — one Ollama call, no retry
        with patch.object(si.urllib.request, "urlopen", side_effect=OSError("refused")) as uo:
            with self.assertRaises(OSError):
                si.analyze_image(b"x")
        self.assertEqual(uo.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_file_not_found(self):
        with patch.object(si.urllib.request, "urlopen", return_value=_resp({"messages": []})):
            with self.assertRaises(RuntimeError) as cm:
                si.download_slack_file("F404", "t")
        self.assertIn("F404 not found", str(cm.exception))

    def test_match_by_name_fragment(self):
        with patch.object(si.urllib.request, "urlopen", side_effect=[_resp(HISTORY), _resp(b"png")]):
            self.assertEqual(si.download_slack_file("cat", "t"), (b"png", "image/png", "cat.jpg"))

    def test_analyze_payload(self):
        with patch.object(si.urllib.request, "urlopen", return_value=_resp({"response": "  ok  "})) as uo:
            self.assertEqual(si.analyze_image(b"\x00\x01", "what?"), "ok")
        body = json.loads(uo.call_args[0][0].data)
        self.assertEqual(body["images"], [base64.b64encode(b"\x00\x01").decode()])
        self.assertFalse(body["stream"])


class TestIntegration(unittest.TestCase):
    def test_download_then_analyze(self):
        with patch.object(si.urllib.request, "urlopen", side_effect=[_resp(HISTORY), _resp(b"png"), _resp({"response": "a cat"})]) as uo:
            data, mt, _ = si.download_slack_file("F123", "t")
            self.assertEqual(si.analyze_image(data), "a cat")
        self.assertIn("channel=C0AMNQ5GX70", uo.call_args_list[0][0][0].full_url)
        self.assertEqual(uo.call_args_list[1][0][0].full_url, "https://files.slack.com/F123/cat.jpg")


class TestFunctional(unittest.TestCase):
    def test_main_prints_description(self):
        with patch.object(sys, "argv", ["x", "F123", "is", "it", "a", "cat?"]), patch.object(si, "get_slack_token", return_value="t"), \
             patch.object(si, "download_slack_file", return_value=(b"i", "image/png", "cat.jpg")), \
             patch.object(si, "analyze_image", return_value="yes, a cat") as an, \
             redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            si.main()
        self.assertEqual(out.getvalue().strip(), "yes, a cat")
        self.assertEqual(an.call_args[0][1], "is it a cat?")

    def test_main_without_token_exits_1(self):
        with patch.object(sys, "argv", ["x", "F1"]), patch.object(si, "get_slack_token", return_value=""), \
             patch.object(si, "download_slack_file") as dl, redirect_stdout(io.StringIO()) as out:
            with self.assertRaises(SystemExit) as cm:
                si.main()
        self.assertEqual(cm.exception.code, 1)
        dl.assert_not_called()
        self.assertIn("No Slack token", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_usage_without_args(self):
        # no --help: any argv is a file id (and reads the Keychain), so the frame check is the bare usage path
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage: nova_slack_image.py <file_id> [prompt]", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch("subprocess.run", side_effect=AssertionError("import must not touch the Keychain")):
            _load()


if __name__ == "__main__":
    unittest.main()
