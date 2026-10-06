#!/usr/bin/env python3
"""Tests for nova_dream_video_comfyui.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

SwarmUI HTTP and ffmpeg are always mocked; WORKSPACE / DREAM_DIR and the SwarmUI output tree are
redirected into a tempdir."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_dream_video_comfyui.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dv = _load("nova_dream_video_t", SCRIPT)


def _resp(obj):
    r = MagicMock()
    r.__enter__.return_value.read.return_value = json.dumps(obj).encode()
    return r


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        (root / "ws" / "dream_videos").mkdir(parents=True)
        for attr, val in (("WORKSPACE", root / "ws"), ("DREAM_DIR", root / "ws" / "dream_videos")):
            p = patch.object(dv, attr, val)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(dv.Path, "home", return_value=root)   # SwarmUI output tree lives under home
        p.start()
        self.addCleanup(p.stop)
        boom = MagicMock(side_effect=AssertionError("unmocked outbound"))
        for target in (patch.object(dv.urllib.request, "urlopen", boom), patch.object(dv.subprocess, "run", boom)):
            target.start()
            self.addCleanup(target.stop)
        self.raw = root / "AI/SwarmUI/Output/local/raw"

    def _frame_file(self, rel="2026-01-01/f.png"):
        f = self.raw / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"png")
        return "View/local/raw/" + rel


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_swarmui_is_local_and_ffmpeg_is_argv(self):
        self.assertTrue(dv.SWARMUI_URL.startswith("http://localhost:"))
        self.assertNotIn("shell=True", SRC)


class TestPerformance(_Base):
    def test_concat_list_for_many_frames_fast(self):
        paths = [f"/x/f{i}.png" for i in range(10_000)]
        with patch.object(dv.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")), \
                redirect_stdout(io.StringIO()):
            t0 = time.perf_counter()
            self.assertTrue(dv.frames_to_video(paths, "/x/out.mp4"))
            self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(_Base):
    def test_swarmui_down_fails_open(self):
        # RETRY GAP: get_session — one urlopen attempt; generate_dream_video returns None, no raise
        with patch.object(dv.urllib.request, "urlopen", side_effect=OSError("refused")) as uo, \
                redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(dv.generate_dream_video("p", num_frames=3))
        self.assertEqual(uo.call_count, 1)
        self.assertIn("Cannot connect to SwarmUI", out.getvalue())

    def test_frame_failure_is_one_shot_and_returns_none(self):
        # RETRY GAP: generate_frame — a failed frame is skipped, not retried
        with patch.object(dv.urllib.request, "urlopen", side_effect=TimeoutError("slow")) as uo, \
                redirect_stdout(io.StringIO()):
            self.assertIsNone(dv.generate_frame("s", "p", 1))
        self.assertEqual(uo.call_count, 1)


class TestUnit(_Base):
    def test_generate_frame_error_and_empty(self):
        with redirect_stdout(io.StringIO()):
            with patch.object(dv.urllib.request, "urlopen", return_value=_resp({"error": "no model"})):
                self.assertIsNone(dv.generate_frame("s", "p", 1))
            with patch.object(dv.urllib.request, "urlopen", return_value=_resp({"images": []})):
                self.assertIsNone(dv.generate_frame("s", "p", 1))
            with patch.object(dv.urllib.request, "urlopen", return_value=_resp({"images": ["View/local/raw/no.png"]})):
                self.assertIsNone(dv.generate_frame("s", "p", 1))

    def test_frames_to_video_empty_and_ffmpeg_failure(self):
        self.assertFalse(dv.frames_to_video([], "/x.mp4"))
        with patch.object(dv.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "bad codec")), \
                redirect_stdout(io.StringIO()) as out:
            self.assertFalse(dv.frames_to_video(["/a.png"], "/x.mp4"))
        self.assertIn("ffmpeg failed: bad codec", out.getvalue())
        self.assertFalse((dv.DREAM_DIR / "frames.txt").exists())

    def test_generate_frame_copies_into_workspace_with_model(self):
        rel = self._frame_file()
        with patch.object(dv.urllib.request, "urlopen", return_value=_resp({"images": [rel]})) as uo, \
                redirect_stdout(io.StringIO()):
            out = dv.generate_frame("sess", "a moon", 2, model="juggernaut")
        self.assertEqual(Path(out), dv.WORKSPACE / "f.png")
        self.assertEqual(Path(out).read_bytes(), b"png")
        body = json.loads(uo.call_args.args[0].data)
        self.assertEqual((body["session_id"], body["model"], body["width"]), ("sess", "juggernaut", 1024))


class TestIntegration(_Base):
    def test_session_then_frames_then_ffmpeg(self):
        rel = self._frame_file()
        answers = [_resp({"session_id": "S1"})] + [_resp({"images": [rel]})] * 3
        with patch.object(dv.urllib.request, "urlopen", side_effect=answers) as uo, \
                patch.object(dv.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run, \
                redirect_stdout(io.StringIO()):
            out = dv.generate_dream_video("ocean", num_frames=3)
        urls = [c.args[0].full_url for c in uo.call_args_list]
        self.assertTrue(urls[0].endswith("/API/GetNewSession"))
        self.assertTrue(all(u.endswith("/API/GenerateText2Image") for u in urls[1:]))
        self.assertIn("cinematic frame 3/3", json.loads(uo.call_args.args[0].data)["prompt"])
        self.assertEqual(run.call_args.args[0][0], "ffmpeg")
        self.assertTrue(out.startswith(str(dv.DREAM_DIR)) and out.endswith(".mp4"))


class TestFunctional(_Base):
    def test_main_golden_path_prints_video(self):
        with patch.object(sys, "argv", ["x", "neon", "city"]), \
                patch.object(dv, "generate_dream_video", return_value="/v/dream.mp4") as g, \
                redirect_stdout(io.StringIO()) as out:
            dv.main()
        g.assert_called_once_with("neon city")
        self.assertIn("Video: /v/dream.mp4", out.getvalue())

    def test_main_failure_exits_1(self):
        with patch.object(sys, "argv", ["x"]), patch.object(dv, "generate_dream_video", return_value=None) as g, \
                redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                dv.main()
        self.assertEqual(cm.exception.code, 1)
        g.assert_called_once_with("surreal dream landscape")
        self.assertIn("Video generation failed", err.getvalue())

    def test_no_frames_returns_none(self):
        with patch.object(dv, "get_session", return_value="S"), patch.object(dv, "generate_frame", return_value=None), \
                redirect_stdout(io.StringIO()):
            self.assertIsNone(dv.generate_dream_video("p", num_frames=2))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # any argv becomes a prompt (no --help), so the smoke is an import in a child process
        r = subprocess.run([sys.executable, "-c", "import nova_dream_video_comfyui as m; print(m.SWARMUI_URL)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "http://localhost:7801")
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
