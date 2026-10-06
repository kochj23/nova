#!/usr/bin/env python3
"""Tests for nova_motion_detector_live.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_motion_detector_live.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_motion_detector_live_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


md = _load()
try:
    import cv2
    import numpy as np
except ImportError:      # pragma: no cover
    cv2 = None


class _Dirs(unittest.TestCase):
    """CLIPS_DIR / FRAMES_DIR redirected to a tempdir; memory server + ffmpeg mocked; output silenced."""
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.clips, self.frames = root / "clips", root / "frames"
        ps = {"clips": mock.patch.object(md, "CLIPS_DIR", self.clips),
              "frames": mock.patch.object(md, "FRAMES_DIR", self.frames),
              "url": mock.patch.object(md.urllib.request, "urlopen", side_effect=OSError("offline")),
              "run": mock.patch.object(md.subprocess, "run"),
              "out": mock.patch("sys.stdout", new_callable=io.StringIO)}
        self.m = {k: p.start() for k, p in ps.items()}
        self.addCleanup(lambda: ([p.stop() for p in ps.values()], self.td.cleanup()))

    def img(self, name, white_frac=0.0, size=(40, 40)):
        a = np.zeros(size, dtype=np.uint8)
        a[: int(size[0] * white_frac), :] = 255
        p = self.td.name + "/" + name
        cv2.imwrite(p, a)
        return p


class TestSecurity(_Dirs):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"rtsp://[^/\s\"']+:[^@/\s\"']+@")       # no user:pass in the RTSP URL

    def test_ffmpeg_argv_list_no_shell(self):
        md.capture_clip("rtsp://cam; echo pwned", duration=1)
        argv = self.m["run"].call_args[0][0]
        self.assertEqual(argv[0], "ffmpeg")
        self.assertIn("rtsp://cam; echo pwned", argv)
        self.assertNotIn("shell", self.m["run"].call_args[1])

    def test_cleanup_only_touches_motion_clips(self):
        self.clips.mkdir(parents=True)
        old = time.time() - 30 * 86400
        for n in ("motion_1.mp4", "family_video.mp4"):
            (self.clips / n).write_bytes(b"x"); os.utime(self.clips / n, (old, old))
        self.assertEqual(md.cleanup_old_clips(), 1)
        self.assertTrue((self.clips / "family_video.mp4").exists())


class TestPerformance(_Dirs):
    @unittest.skipIf(cv2 is None, "cv2 not installed")
    def test_motion_diff_on_1080p_is_fast(self):
        a, b = self.img("a.png", 0.0, (1080, 1920)), self.img("b.png", 0.5, (1080, 1920))
        t0 = time.perf_counter()
        for _ in range(10):
            pct = md.detect_motion_in_frames(a, b)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertAlmostEqual(pct, 50.0, delta=0.5)


class TestRetry(_Dirs):
    def test_remember_fails_open(self):
        # RETRY GAP: remember() — one POST to the memory server; failure logged, None returned
        self.assertIsNone(md.remember("x"))
        self.assertEqual(self.m["url"].call_count, 1)

    def test_capture_timeout_and_error_fail_open(self):
        # RETRY GAP: capture_clip — one ffmpeg attempt; timeout / non-zero exit -> None, never raises
        self.m["run"].side_effect = subprocess.TimeoutExpired("ffmpeg", 1)
        self.assertIsNone(md.capture_clip("rtsp://x", duration=1))
        self.m["run"].side_effect = None
        self.m["run"].return_value = mock.Mock(returncode=1, stderr="401 Unauthorized")
        self.assertIsNone(md.capture_clip("rtsp://x", duration=1))
        self.assertEqual(self.m["run"].call_count, 2)


class TestUnit(_Dirs):
    @unittest.skipIf(cv2 is None, "cv2 not installed")
    def test_detect_motion_edges(self):
        a = self.img("a.png", 0.0)
        self.assertEqual(md.detect_motion_in_frames(a, a), 0)
        self.assertEqual(md.detect_motion_in_frames(a, "/nonexistent.png"), 0)
        b = self.img("b.png", 0.25, (80, 80))                      # different size -> resized, still compared
        self.assertAlmostEqual(md.detect_motion_in_frames(a, b), 25.0, delta=3)

    def test_latest_frame_snapshot_is_unique_copy(self):
        self.assertIsNone(md.get_latest_frame())
        self.frames.mkdir(parents=True)
        (self.frames / "front_door_latest.jpg").write_bytes(b"jpg")
        s1, s2 = md.get_latest_frame(), md.get_latest_frame()
        self.assertNotEqual(s1, s2)
        self.assertEqual(Path(s1).read_bytes(), b"jpg")

    def test_storage_stats(self):
        self.m["run"].return_value = mock.Mock(returncode=0, stdout="1.2G\t/x\n")
        self.assertEqual(md.get_storage_stats(), "1.2G")
        self.m["run"].side_effect = OSError("no du")
        self.assertEqual(md.get_storage_stats(), "unknown")


class TestIntegration(_Dirs):
    def test_successful_capture_is_remembered_as_vision(self):
        def ffmpeg(cmd, **k):
            Path(cmd[-1]).write_bytes(b"\0" * 2048)
            return mock.Mock(returncode=0, stderr="")
        self.m["run"].side_effect = ffmpeg
        r = mock.MagicMock(); r.__enter__.return_value.read.return_value = b'{"id": 7}'
        self.m["url"].side_effect = None; self.m["url"].return_value = r
        clip = md.capture_clip(md.RTSP_URL, duration=5, quality="low")
        self.assertTrue(Path(clip).exists())
        argv = self.m["run"].call_args[0][0]
        self.assertIn("scale=720:-1", argv)
        self.assertEqual(argv[argv.index("-preset") + 1], "ultrafast")
        req = self.m["url"].call_args[0][0]
        self.assertTrue(req.full_url.endswith("/remember"))
        self.assertEqual(json.loads(req.data)["source"], "vision")


class TestFunctional(_Dirs):
    def test_sustained_motion_triggers_one_capture_then_stops(self):
        frames = ["f0", "f1", "f2", "f3"]
        sleeps = []
        def sleep(s):
            sleeps.append(s)
            if len(sleeps) >= len(frames):
                raise KeyboardInterrupt          # the loop's own clean-exit path
        with mock.patch.object(md, "get_latest_frame", side_effect=frames), \
                mock.patch.object(md, "detect_motion_in_frames", return_value=40.0), \
                mock.patch.object(md, "capture_clip", return_value="/c/motion.mp4") as cap, \
                mock.patch.object(md, "remember") as rem, \
                mock.patch.object(md.os, "remove"), \
                mock.patch.object(md.time, "sleep", side_effect=sleep), \
                mock.patch.object(md, "datetime") as dt:
            base = _dt.datetime.now()                 # clock advances a minute per call: cooldown always clear
            dt.now.side_effect = [base + _dt.timedelta(minutes=i) for i in range(50)]
            md.motion_monitor_loop()
        cap.assert_called_once()
        rem.assert_called_once()
        self.assertEqual(sleeps, [30, 30, 30, 30])

    def test_main_cleanup_mode(self):
        with mock.patch.object(sys, "argv", ["x", "cleanup"]), \
                mock.patch.object(md, "cleanup_old_clips", return_value=3), \
                mock.patch.object(md, "motion_monitor_loop") as loop:
            md.main()
        loop.assert_not_called()
        self.assertIn("Cleaned 3 clips", self.m["out"].getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_motion_detector_live"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
