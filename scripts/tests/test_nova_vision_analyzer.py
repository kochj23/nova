#!/usr/bin/env python3
"""Tests for nova_vision_analyzer.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
import urllib.parse
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_vision_analyzer.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with patch("psycopg2.connect", side_effect=OSError("offline test")):
    va = _load("vision_analyzer_t", SCRIPT)
SRC = SCRIPT.read_text()
va.notify = MagicMock()
va.log = lambda m: None
URLOPEN = MagicMock(side_effect=RuntimeError("urlopen not mocked in test"))
va.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=URLOPEN),
                                  parse=urllib.parse)
_TMP = tempfile.TemporaryDirectory()
va.CLIPS_DIR = Path(_TMP.name) / "clips"


def _resp(obj):
    r = MagicMock(); r.read.return_value = json.dumps(obj).encode()
    r.__enter__ = lambda s: s; r.__exit__ = lambda *a: False
    return r


def _wired(llm="All quiet.", events=({"text": "person at front door"},)):
    """Patch the three network helpers; returns the remember mock."""
    va.notify.reset_mock()
    return (patch.object(va, "query_local", return_value=llm), patch.object(va, "recall", return_value=list(events)),
            patch.object(va, "remember", return_value=1))


def _urls():
    return [c.args[0].full_url if hasattr(c.args[0], "full_url") else c.args[0] for c in URLOPEN.call_args_list]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_local_only(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertTrue(va.OLLAMA_URL.startswith("http://127.0.0.1:"))
        self.assertIsNone(re.search(r"openrouter|api\.openai|anthropic\.com", SRC, re.I))

    def test_recall_query_is_url_quoted(self):
        URLOPEN.reset_mock(); URLOPEN.side_effect = None; URLOPEN.return_value = _resp({"results": []})
        try:
            va.recall("a&n=999 b")
        finally:
            URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        self.assertIn("q=a%26n%3D999%20b&n=5", _urls()[0])


class TestPerformance(unittest.TestCase):
    def test_daily_prompt_caps_events(self):
        captured = []
        p1, p2, p3 = _wired(events=[{"text": "x" * 1000} for _ in range(10_000)])
        with p1 as q, p2, p3:
            t0 = time.perf_counter()
            va.analyze_daily_events()
            self.assertLess(time.perf_counter() - t0, 1.0)
        prompt = q.call_args.args[0]
        self.assertEqual(prompt.count("- " + "x" * 200), 15)


class TestRetry(unittest.TestCase):
    def test_network_helpers_fail_open(self):
        # RETRY GAP: remember / recall / query_local / describe_image — one attempt each, safe default on failure
        URLOPEN.reset_mock(); URLOPEN.side_effect = OSError("down")
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg") as img:
                self.assertIsNone(va.remember("t"))
                self.assertEqual(va.recall("q"), [])
                self.assertIsNone(va.query_local("p"))
                self.assertIsNone(va.describe_image(img.name))
        finally:
            URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        self.assertEqual(URLOPEN.call_count, 4)

    def test_llm_down_posts_nothing(self):
        p1, p2, p3 = _wired(llm=None)
        with p1, p2, p3 as rem:
            self.assertIsNone(va.analyze_daily_events())
        va.notify.assert_not_called(); rem.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_query_local_strips_think(self):
        URLOPEN.reset_mock(); URLOPEN.side_effect = None
        URLOPEN.return_value = _resp({"response": "<think>hmm</think> Low threat."})
        try:
            self.assertEqual(va.query_local("p"), "Low threat.")
        finally:
            URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        body = json.loads(URLOPEN.call_args.args[0].data)
        self.assertEqual((body["model"], body["think"]), (va.MODEL, False))

    def test_slack_post_title_and_body(self):
        va.notify.reset_mock()
        va.slack_post(":camera: *Daily Report*\n\nbody text", dedup_key="k")
        self.assertEqual(va.notify.call_args.args[0], "Daily Report")
        self.assertEqual(va.notify.call_args.kwargs["body"], "body text")
        va.slack_post("only title")
        self.assertIsNone(va.notify.call_args.kwargs["body"])


class TestIntegration(unittest.TestCase):
    def test_describe_image_sends_b64_to_vision_model(self):
        URLOPEN.reset_mock(); URLOPEN.side_effect = None; URLOPEN.return_value = _resp({"response": " a cat "})
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg") as img:
                img.write(b"\x89PNG"); img.flush()
                self.assertEqual(va.describe_image(img.name), "a cat")
        finally:
            URLOPEN.side_effect = RuntimeError("urlopen not mocked in test")
        body = json.loads(URLOPEN.call_args.args[0].data)
        self.assertEqual((body["model"], body["images"]), (va.VISION_MODEL, ["iVBORw=="]))

    def test_threat_profile_counts_recent_clips(self):
        va.CLIPS_DIR.mkdir(exist_ok=True)
        for n in ("motion_1.mp4", "motion_2.mp4", "other.mp4"):
            (va.CLIPS_DIR / n).write_bytes(b"")
        old = va.CLIPS_DIR / "motion_old.mp4"; old.write_bytes(b"")
        os.utime(old, (time.time() - 30 * 86400,) * 2)
        p1, p2, p3 = _wired(events=[])
        with p1 as q, p2, p3:
            va.analyze_threat_profile()
        self.assertIn("Motion clips this week: 2", q.call_args.args[0])
        self.assertEqual(va.notify.call_args.kwargs["dedup_key"], "vision-weekly-threat")


class TestFunctional(unittest.TestCase):
    def test_daily_golden_path(self):
        p1, p2, p3 = _wired()
        with p1, p2, p3 as rem:
            self.assertEqual(va.analyze_daily_events(), "All quiet.")
        self.assertTrue(rem.call_args.args[0].startswith("Daily vision report ("))
        self.assertEqual(va.notify.call_args.kwargs["dedup_key"], "vision-daily-report")

    def test_anomaly_cli_high_pages_critical_medium_does_not(self):
        p1, p2, p3 = _wired(llm="Yes. Check the gate.")
        with p1, p2, p3, patch.object(sys, "argv", ["x", "anomaly", "figure", "at", "gate", "high"]):
            va.main()
        self.assertEqual(va.notify.call_args.kwargs["level"], "critical")
        self.assertIn("figure at gate", va.notify.call_args.kwargs["body"])
        p1, p2, p3 = _wired(llm="No.")
        with p1, p2, p3, patch.object(sys, "argv", ["x", "anomaly", "cat"]):
            va.main()
        va.notify.assert_not_called()

    def test_describe_cli_failure_exits_1(self):
        with patch.object(va, "describe_image", return_value=None), patch.object(sys, "argv", ["x", "describe", "/no.jpg"]), \
                redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as e:
                va.main()
        self.assertEqual(e.exception.code, 1)


class TestFrame(unittest.TestCase):
    def test_unknown_subcommand_exits_zero_offline(self):
        code = ("import sys,runpy,psycopg2,urllib.request;sys.path.insert(0,'.');"
                "psycopg2.connect=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
                "urllib.request.urlopen=lambda *a,**k:(_ for _ in ()).throw(SystemExit(9));"
                f"sys.argv=[{str(SCRIPT)!r},'--help'];runpy.run_path(sys.argv[0],run_name='__main__')")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
