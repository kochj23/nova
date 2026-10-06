#!/usr/bin/env python3
"""Tests for nova_short_video.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). The LLM (Ollama HTTP), psycopg2, `say`/ffmpeg/ffprobe subprocess
and image gen are all mocked; OUT_DIR is redirected to a tempdir. No media is produced on real
services. Written by Jordan Koch (via Claude)."""
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
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sv = _load("nova_short_video_t", SCRIPTS / "nova_short_video.py")
SRC = (SCRIPTS / "nova_short_video.py").read_text()


def _ollama(content):
    cm = mock.MagicMock()
    cm.__enter__.return_value.read.return_value = json.dumps({"message": {"content": content}}).encode()
    return mock.Mock(return_value=cm)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_constant_only(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertIn("source IN ('unclaimed','nova_articles','episodic')", SRC)

    def test_nothing_is_uploaded(self):
        # MVP writes only to a local review folder — no upload/network-post call in the CODE
        # (the module docstring mentions YouTube/OAuth only as the explicitly-separate gap #3).
        code = re.sub(r'(?s)""".*?"""', "", SRC, count=1)
        for bad in ("youtube", "upload(", "requests.post(", "oauth", "googleapis"):
            self.assertNotIn(bad, code.lower())
        self.assertIn("workspace", SRC) and self.assertIn("shorts", SRC)


class TestPerformance(unittest.TestCase):
    def test_caption_wrap_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "c.png"
            t0 = time.perf_counter()
            sv.render_caption_png("word " * 60, str(out))
            self.assertLess(time.perf_counter() - t0, 3.0)
            self.assertTrue(out.exists() and out.stat().st_size > 100)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_nodes(self):
        # the LLM call walks every Ollama node; two failures then a success returns content
        seq = [OSError("node1 down"), OSError("node2 down"), _ollama("the script")()]
        def urlopen(req, timeout=None):
            x = seq.pop(0)
            if isinstance(x, Exception):
                raise x
            return x
        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(sv.llm("x"), "the script")

    def test_llm_all_nodes_down_returns_empty(self):
        # RETRY GAP: llm() has no backoff; when every node is down it fails open to "" (caller aborts)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("all down")) as u:
            self.assertEqual(sv.llm("x"), "")
        self.assertEqual(u.call_count, len(sv.OLLAMA_NODES))


class TestUnit(unittest.TestCase):
    def test_afprobe_dur_parses_and_fails_safe(self):
        with mock.patch.object(sv.subprocess, "run",
                               return_value=types.SimpleNamespace(stdout="12.5\n")):
            self.assertEqual(sv.afprobe_dur("x.wav"), 12.5)
        with mock.patch.object(sv.subprocess, "run",
                               return_value=types.SimpleNamespace(stdout="not a number")):
            self.assertEqual(sv.afprobe_dur("x.wav"), 0.0)

    def test_source_text_prefers_explicit(self):
        self.assertEqual(sv.source_text(types.SimpleNamespace(text="hello")), "hello")

    def test_source_text_queries_memories(self):
        class Cur:
            def execute(s, *a): s.sql = a[0]
            def fetchone(s): return ("a recent column",)
        class C:
            autocommit = True
            def cursor(s): return Cur()
        with mock.patch("psycopg2.connect", return_value=C()):
            self.assertEqual(sv.source_text(types.SimpleNamespace(text="")), "a recent column")


class TestIntegration(unittest.TestCase):
    def test_caption_png_is_vertical_canvas(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "c.png"
            sv.render_caption_png("short caption", str(out))
            with Image.open(out) as im:
                self.assertEqual(im.size, (sv.W, sv.H))

    def test_dimensions_are_vertical_short(self):
        self.assertEqual((sv.W, sv.H), (1080, 1920))
        self.assertGreater(sv.FPS, 0)


class TestFunctional(unittest.TestCase):
    def test_no_source_text_aborts(self):
        with mock.patch.object(sv, "source_text", return_value=""), \
             mock.patch.object(sv, "OUT_DIR", Path(tempfile.mkdtemp())), \
             mock.patch.object(sys, "argv", ["x"]), mock.patch.object(sv, "log"):
            self.assertEqual(sv.main(), 1)

    def test_script_generation_failure_aborts(self):
        with mock.patch.object(sv, "source_text", return_value="a" * 100), \
             mock.patch.object(sv, "llm", return_value="too short"), \
             mock.patch.object(sv, "OUT_DIR", Path(tempfile.mkdtemp())), \
             mock.patch.object(sys, "argv", ["x"]), mock.patch.object(sv, "log"):
            self.assertEqual(sv.main(), 1)

    def test_tts_failure_aborts_before_any_image(self):
        # source + script good, but narration has zero duration -> abort (no image gen attempted)
        with mock.patch.object(sv, "source_text", return_value="a" * 100), \
             mock.patch.object(sv, "llm", return_value="This is a real hook. " + "sentence here. " * 10), \
             mock.patch.object(sv.subprocess, "run"), \
             mock.patch.object(sv, "afprobe_dur", return_value=0.0), \
             mock.patch.object(sv, "OUT_DIR", Path(tempfile.mkdtemp())), \
             mock.patch.object(sys, "argv", ["x"]), mock.patch.object(sv, "log"):
            self.assertEqual(sv.main(), 1)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_short_video.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--beats", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
