#!/usr/bin/env python3
"""Tests for nova_sky_watcher.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_sky_watcher.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_sky_test_"))
CAMS = {"front_yard": "rtsp://cam.invalid/1", "back_patio": "rtsp://cam.invalid/2"}


def _load(stub=True):
    import nova_config
    cc = types.ModuleType("camera_config"); cc.CAMERAS = dict(CAMS)
    spec = importlib.util.spec_from_file_location("nskywatch", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(nova_config, "slack_bot_token", return_value="xoxb-test"), \
         patch.dict(sys.modules, {"camera_config": cc}), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")), \
         patch("subprocess.run", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    mod.SKY_ARCHIVE = TMP / "sky"; mod.BEST_DIR = mod.SKY_ARCHIVE / "best"
    mod.STATE_FILE = TMP / "state.json"
    if stub:
        mod.vector_remember = MagicMock()
        mod.slack_upload = MagicMock()
    return mod


sw = _load()


def _at(dt):
    return patch.object(sw, "NOW", dt)


def _golden():
    gs, gset, sunrise, sunset = sw.get_golden_hours()
    return gs, gset, sunrise, sunset


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"rtsp://[^\"'\s]*@")          # camera creds live in gitignored camera_config
        self.assertIn("from camera_config import CAMERAS", SRC)

    def test_quiet_hours_block_uploads(self):
        fresh = _load(stub=False)
        with patch.object(fresh, "_is_quiet_hours", return_value=True), patch.object(fresh.subprocess, "run") as run, \
             redirect_stdout(io.StringIO()):
            fresh.slack_upload("/x.jpg", "c")
        run.assert_not_called()

    def test_ffmpeg_capture_is_argv(self):
        self.assertNotIn("shell=True", SRC)
        out = TMP / "c.jpg"
        with patch.object(sw.subprocess, "run", return_value=MagicMock(returncode=0)) as run:
            sw.capture_frame("cam", "rtsp://x/1; rm", out)
        self.assertEqual(run.call_args[0][0][:3], ["ffmpeg", "-rtsp_transport", "tcp"])


class TestPerformance(unittest.TestCase):
    def test_solar_times_for_a_year_of_days(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            sw.solar_times(datetime(2026, 1, 1) + timedelta(days=i % 365), sw.LATITUDE, sw.LONGITUDE)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_capture_falls_through_cameras(self):
        # RETRY GAP: capture_frame — one ffmpeg attempt per camera; failure falls through to the next camera
        calls = []
        def cf(name, url, path):
            calls.append(name)
            if len(calls) == 1:
                return False
            path.write_bytes(b"x" * 2048); return True
        with _at(datetime(2026, 6, 1, 12, 0)), patch.object(sw, "capture_frame", side_effect=cf), redirect_stdout(io.StringIO()):
            path, cam = sw.capture_sky_frame()
        self.assertEqual(len(calls), 2)
        self.assertEqual(cam, calls[1])

    def test_capture_timeout_returns_false(self):
        with patch.object(sw.subprocess, "run", side_effect=subprocess.TimeoutExpired("ffmpeg", 10)), redirect_stdout(io.StringIO()):
            self.assertFalse(sw.capture_frame("c", "u", TMP / "t.jpg"))

    def test_vector_remember_fails_open(self):
        # RETRY GAP: vector_remember — one POST; failure swallowed
        fresh = _load(stub=False)
        with patch.object(fresh.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertIsNone(fresh.vector_remember("x"))
        self.assertEqual(uo.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_burbank_solstice_times_are_sane(self):
        sunrise, sunset, noon = sw.solar_times(datetime(2026, 6, 21), sw.LATITUDE, sw.LONGITUDE)
        self.assertLess(sunrise, noon); self.assertLess(noon, sunset)
        self.assertAlmostEqual((sunset - sunrise).total_seconds() / 3600, 14.4, delta=0.5)

    def test_session_detection(self):
        with _at(datetime(2026, 6, 1, 12, 0)):
            gs, gset, sunrise, sunset = _golden()
        with _at(sunrise):
            self.assertEqual(sw.current_session()[0], "sunrise")
            self.assertTrue(sw.is_golden_hour())
        with _at(sunset + timedelta(minutes=10)):
            self.assertEqual(sw.current_session()[0], "sunset")
        with _at(datetime(2026, 6, 1, 12, 0)):
            self.assertEqual(sw.current_session(), (None, None))

    def test_color_score_fallback_without_pil(self):
        f = TMP / "s.jpg"; f.write_bytes(b"x" * 4096)
        with patch.dict(sys.modules, {"PIL": None}):
            self.assertEqual(sw.frame_color_score(f), 4.0)

    def test_load_state_default_and_corrupt(self):
        sw.STATE_FILE.write_text("{bad")
        self.assertEqual(sw.load_state()["total_frames"], 0)


class TestIntegration(unittest.TestCase):
    def test_camera_rotation_differs_between_sessions(self):
        day = datetime(2026, 6, 1, 12, 0)
        with _at(day):
            _, _, sunrise, sunset = _golden()
        with _at(sunrise):
            am = sw._pick_session_camera()[0][0]
        with _at(sunset):
            pm = sw._pick_session_camera()[0][0]
        self.assertNotEqual(am, pm)
        self.assertEqual(set(CAMS), {am, pm})

    def test_best_frame_copied_and_posted(self):
        with _at(datetime(2026, 6, 2, 21, 0)):
            d = sw.SKY_ARCHIVE / "2026/06/02"; d.mkdir(parents=True, exist_ok=True)
            for n, size in (("sunset_front_yard_1.jpg", 10), ("sunset_back_patio_2.jpg", 99)):
                (d / n).write_bytes(b"x" * size)
            with patch.object(sw, "frame_color_score", side_effect=lambda p: p.stat().st_size), redirect_stdout(io.StringIO()):
                sw.post_session_best("sunset")
        self.assertTrue((sw.BEST_DIR / f"{sw.TODAY}_sunset.jpg").exists())
        self.assertIn("Best of 2 frames", sw.slack_upload.call_args[0][1])


class TestFunctional(unittest.TestCase):
    def setUp(self):
        sw.STATE_FILE.unlink(missing_ok=True)
        sw.slack_upload.reset_mock()

    def test_golden_hour_capture_updates_state(self):
        with _at(datetime(2026, 6, 1, 12, 0)):
            _, _, sunrise, _ = _golden()
        with _at(sunrise), patch.object(sw, "capture_sky_frame", return_value=(TMP / "f.jpg", "front_yard")), \
             redirect_stdout(io.StringIO()):
            sw.main()
        st = json.loads(sw.STATE_FILE.read_text())
        self.assertEqual((st["frames_today"], st["total_frames"]), (1, 1))
        self.assertEqual(st["last_capture"], sunrise.isoformat())

    def test_midnight_never_burns_the_sunrise_post(self):
        # regression: a negative (pre-window) delta passed `< 600`, posting at midnight and marking it done forever
        with _at(datetime(2026, 6, 1, 0, 5)), patch.object(sw, "post_session_best") as psb, redirect_stdout(io.StringIO()):
            sw.main()
        psb.assert_not_called()

    def test_post_fires_just_after_window_and_resets_daily(self):
        with _at(datetime(2026, 6, 1, 12, 0)):
            gs, _, _, _ = _golden()
        sw.save_state({"sessions_today": ["sunrise_posted"], "sessions_date": "1999-01-01"})
        with _at(gs[1] + timedelta(minutes=3)), patch.object(sw, "post_session_best") as psb, redirect_stdout(io.StringIO()):
            sw.main()
        psb.assert_called_once_with("sunrise")
        self.assertEqual(json.loads(sw.STATE_FILE.read_text())["sessions_today"], ["sunrise_posted"])


class TestFrame(unittest.TestCase):
    def test_solar_cli_offline(self):
        code = ("import sys, runpy; sys.path.insert(0, sys.argv[1]); import nova_config; "
                "nova_config.slack_bot_token = lambda: ''; sys.argv = [sys.argv[2], '--solar']; "
                "runpy.run_path(sys.argv[0], run_name='__main__')")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPTS), str(SCRIPT)], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Solar Times for Burbank", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch("subprocess.run", side_effect=AssertionError("import must not capture")):
            _load()


if __name__ == "__main__":
    unittest.main()
