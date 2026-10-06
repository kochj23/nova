#!/usr/bin/env python3
"""Tests for nova_frame_index.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_frame_index.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="fidx-test-"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fi = _load("frame_index_under_test", SCRIPT)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b


def _fake_urlopen(describe="a man at a desk reading  on-screen text", remember_ok=True):
    calls = []

    def urlopen(req, timeout=None):
        calls.append((req.full_url, json.loads(req.data.decode()) if req.data else None))
        if "/api/generate" in req.full_url:
            return _Resp({"response": describe})
        if "/remember" in req.full_url:
            if not remember_ok:
                raise OSError("memory server down")
            return _Resp({"ok": True})
        raise AssertionError(req.full_url)
    urlopen.calls = calls
    return urlopen


def _fake_run(duration="30.0", make_frame=True):
    calls = []

    def run(cmd, capture_output=False, text=False, **kw):
        calls.append(cmd)
        if cmd[0] == "ffprobe":
            return types.SimpleNamespace(returncode=0, stdout=duration, stderr="")
        if cmd[0] == "ffmpeg" and make_frame:
            Path(cmd[-1]).write_bytes(b"\xff\xd8jpeg")
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    run.calls = calls
    return run


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("@", fi.OLLAMA + fi.MEMORY)       # no userinfo in service URLs

    def test_media_tools_are_argv_and_frames_live_in_a_private_tempdir(self):
        self.assertNotIn("shell=True", SRC)
        self.assertIn("tempfile.mkdtemp(", SRC)
        run = _fake_run()
        hostile = "/x/y/evil; touch pwned.mp4"
        with patch.object(fi.subprocess, "run", run), patch.object(fi.urllib.request, "urlopen", _fake_urlopen()), \
             redirect_stdout(io.StringIO()):
            fi.index_video(hostile, "S", 1)
        self.assertEqual(run.calls[0][0], "ffprobe")
        self.assertIn(hostile, run.calls[1])   # passed as ONE argv element, never a shell string


class TestPerformance(unittest.TestCase):
    def test_pure_helpers_10k(self):
        t0 = time.perf_counter()
        stamps = [fi.hms(t * 7.3) for t in range(10_000)]
        shows = [fi.show_from_path(f"/Volumes/Data/TVShows/Show{i % 50}/s01e0{i % 9}.mkv") for i in range(10_000)]
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(stamps[0], "00:00:00")
        self.assertEqual(shows[1], "Show1")


class TestRetry(unittest.TestCase):
    def test_remember_fails_open(self):
        # RETRY GAP: remember()/urlopen — one POST; failure prints to stderr and returns False
        uo = _fake_urlopen(remember_ok=False)
        err = io.StringIO()
        with patch.object(fi.urllib.request, "urlopen", uo), redirect_stderr(err):
            self.assertFalse(fi.remember("t", {}))
        self.assertEqual(len(uo.calls), 1)
        self.assertIn("remember failed", err.getvalue())

    def test_vision_outage_escapes_index_video(self):
        # RETRY GAP: describe()/_ollama — no retry and no guard; a VLM outage aborts the whole index pass
        run = _fake_run()
        with patch.object(fi.subprocess, "run", run), \
             patch.object(fi.urllib.request, "urlopen", MagicMock(side_effect=OSError("ollama down"))) as uo:
            with self.assertRaises(OSError):
                fi.index_video("/v.mp4", "S", 3)
        self.assertEqual(uo.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_hms(self):
        self.assertEqual(fi.hms(0), "00:00:00")
        self.assertEqual(fi.hms(3661), "01:01:01")
        self.assertEqual(fi.hms(86399.9), "23:59:59")

    def test_show_from_path(self):
        self.assertEqual(fi.show_from_path("/Volumes/Data/TVShows/The Show/s01e01.mkv"), "The Show")
        self.assertEqual(fi.show_from_path("/a/youtube/Channel/clip.mp4"), "Channel")
        self.assertEqual(fi.show_from_path("/a/b/movie.mp4"), "movie")
        self.assertEqual(fi.show_from_path("/a/TVShows"), "TVShows")      # root with nothing after it -> stem

    def test_duration_parses_or_zero(self):
        with patch.object(fi.subprocess, "run", _fake_run("12.5\n")):
            self.assertEqual(fi.duration("/v.mp4"), 12.5)
        with patch.object(fi.subprocess, "run", _fake_run("")):
            self.assertEqual(fi.duration("/v.mp4"), 0.0)


class TestIntegration(unittest.TestCase):
    def test_remember_targets_frame_vision_source_async(self):
        uo = _fake_urlopen()
        with patch.object(fi.urllib.request, "urlopen", uo):
            self.assertTrue(fi.remember("txt", {"kind": "frame"}))
        url, body = uo.calls[0]
        self.assertEqual(url, fi.MEMORY + "/remember?async=1")
        self.assertEqual(body, {"text": "txt", "source": "frame_vision", "metadata": {"kind": "frame"}})

    def test_describe_sends_model_and_base64_frame(self):
        frame = TMP / "f.jpg"; frame.write_bytes(b"abc")
        uo = _fake_urlopen(describe="  two   lines\nhere ")
        with patch.object(fi.urllib.request, "urlopen", uo):
            self.assertEqual(fi.describe(str(frame)), "two lines here")
        url, body = uo.calls[0]
        self.assertEqual(url, fi.OLLAMA + "/api/generate")
        self.assertEqual((body["model"], body["images"], body["stream"]), (fi.VLM, ["YWJj"], False))


class TestFunctional(unittest.TestCase):
    def test_index_video_golden_path(self):
        run, uo, out = _fake_run("30.0"), _fake_urlopen(), io.StringIO()
        with patch.object(fi.subprocess, "run", run), patch.object(fi.urllib.request, "urlopen", uo), redirect_stdout(out):
            stored = fi.index_video("/media/TVShows/Show/ep.mkv", "Show", 3)
        self.assertEqual(stored, 3)
        ffmpeg = [c for c in run.calls if c[0] == "ffmpeg"]
        self.assertEqual([c[2] for c in ffmpeg], ["5.0", "15.0", "25.0"])       # evenly spaced, centred
        remembers = [b for u, b in uo.calls if "/remember" in u]
        self.assertEqual(remembers[1]["text"], "[Show — frame @ 00:00:15] a man at a desk reading on-screen text")
        self.assertEqual(remembers[1]["metadata"], {"kind": "frame", "show": "Show", "video": "ep.mkv",
                                                    "t_seconds": 15.0, "timestamp": "00:00:15"})
        self.assertFalse(Path(ffmpeg[0][-1]).exists())                             # frame files are cleaned up
        self.assertEqual(out.getvalue().count("["), 3)

    def test_empty_descriptions_and_missing_frames_store_nothing(self):
        with patch.object(fi.subprocess, "run", _fake_run("30.0", make_frame=False)), \
             patch.object(fi.urllib.request, "urlopen", _fake_urlopen()) as uo:
            self.assertEqual(fi.index_video("/v.mp4", "S", 2), 0)
        with patch.object(fi.subprocess, "run", _fake_run("0")), \
             patch.object(fi.urllib.request, "urlopen", _fake_urlopen(describe="")) as uo, redirect_stdout(io.StringIO()):
            self.assertEqual(fi.index_video("/v.mp4", "S", 2), 0)      # duration 0 -> 60s default, empty desc skipped
        self.assertTrue(all("/api/generate" in u for u, _ in uo.calls))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--frames", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_frame_index"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
