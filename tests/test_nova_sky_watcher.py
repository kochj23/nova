"""
test_nova_sky_watcher.py — All 7 test categories for nova_sky_watcher.py
Written by Jordan Koch.
"""
import importlib.util, json, sys, time, tempfile, math, unittest
from datetime import datetime, date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.SLACK_PHOTOS = "#nova-photos"
_nova_cfg.post_both = MagicMock()
_nova_cfg.slack_bot_token.return_value = "xoxb-test"
sys.modules["nova_config"] = _nova_cfg

# Stub camera_config
_cam_cfg = MagicMock()
_cam_cfg.CAMERAS = {"front_yard": "rtsps://test/1", "back_patio": "rtsps://test/2"}
sys.modules["camera_config"] = _cam_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_sky_watcher.py"
_spec = importlib.util.spec_from_file_location("nova_sky_watcher", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

solar_times = _mod.solar_times
is_golden_hour = _mod.is_golden_hour
get_golden_hours = _mod.get_golden_hours
frame_color_score = _mod.frame_color_score
pick_best_frame = _mod.pick_best_frame
load_state = _mod.load_state
save_state = _mod.save_state


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_"]:
            self.assertNotIn(pat, src)
    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)
    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)
    def test_slack_token_from_nova_config(self):
        src = _SCRIPT.read_text()
        self.assertIn("nova_config.slack_bot_token", src)
    def test_vector_url_from_nova_config(self):
        src = _SCRIPT.read_text()
        self.assertIn("nova_config.VECTOR_URL", src)
    def test_rtsp_tokens_not_in_source(self):
        """Camera RTSP tokens must come from camera_config (gitignored), not hardcoded."""
        src = _SCRIPT.read_text()
        self.assertNotIn("?enableSrtp", src)


class TestPerformance(unittest.TestCase):
    def test_solar_times_fast(self):
        dt = datetime.now()
        start = time.perf_counter()
        for _ in range(1000):
            solar_times(dt, 34.1808, -118.3090)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)
    def test_golden_hours_computed_daily(self):
        """Golden hour windows should be computed from solar times."""
        gs, gset, sunrise, sunset = get_golden_hours()
        self.assertLess(gs[0], gs[1])
        self.assertLess(gset[0], gset[1])
        self.assertLess(sunrise, sunset)
    def test_frame_color_score_returns_float(self):
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"\xff\xd8\xff" + b"\x80" * 300)
            tmp = f.name
        import os
        try:
            result = frame_color_score(Path(tmp))
        finally:
            os.unlink(tmp)
        self.assertIsInstance(result, (int, float))
        self.assertGreaterEqual(result, 0)


class TestRetry(unittest.TestCase):
    def test_vector_remember_does_not_raise(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            _mod.vector_remember("Sky photo captured", {})
    def test_slack_post_suppressed_quiet_hours(self):
        """Slack posts must be suppressed during quiet hours (23:00-07:00)."""
        with patch.object(_mod, "_is_quiet_hours", return_value=True):
            with patch.object(_nova_cfg, "post_both") as mock_post:
                _mod.slack_post("Test message")
        mock_post.assert_not_called()
    def test_capture_frame_returns_false_on_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 10)):
            result = _mod.capture_frame("front_yard", "rtsps://test", Path("/tmp/out.jpg"))
        self.assertFalse(result)


class TestUnit(unittest.TestCase):
    def test_solar_times_burbank_sunrise_before_noon(self):
        dt = datetime(2026, 6, 21)  # Summer solstice
        sunrise, sunset, noon = solar_times(dt, 34.1808, -118.3090)
        self.assertLess(sunrise, noon)
        self.assertLess(noon, sunset)
    def test_solar_times_sunrise_before_sunset(self):
        for month in [1, 6, 12]:
            dt = datetime(2026, month, 15)
            sunrise, sunset, _ = solar_times(dt, 34.1808, -118.3090)
            self.assertLess(sunrise, sunset, f"Month {month}: sunrise not before sunset")
    def test_is_quiet_hours_true_at_midnight(self):
        with patch("nova_sky_watcher.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 0
            result = _mod._is_quiet_hours()
        self.assertTrue(result)
    def test_is_quiet_hours_false_at_noon(self):
        with patch("nova_sky_watcher.datetime") as mock_dt:
            mock_dt.now.return_value.hour = 12
            result = _mod._is_quiet_hours()
        self.assertFalse(result)
    def test_load_state_defaults(self):
        with patch.object(_mod, "STATE_FILE", Path("/nonexistent/state.json")):
            state = load_state()
        self.assertIn("last_capture", state)
        self.assertIn("total_frames", state)
    def test_save_and_load_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "state.json"):
                state = {"last_capture": "2026-01-01T06:00:00", "frames_today": 5, "total_frames": 100}
                save_state(state)
                loaded = load_state()
        self.assertEqual(loaded["total_frames"], 100)
    def test_pick_best_frame_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = pick_best_frame(Path(tmpdir), "sunrise")
        self.assertIsNone(result)
    def test_latitude_longitude_burbank(self):
        self.assertAlmostEqual(_mod.LATITUDE, 34.18, places=1)
        self.assertAlmostEqual(_mod.LONGITUDE, -118.31, places=1)


class TestIntegration(unittest.TestCase):
    def test_golden_hour_windows_encompass_solar_event(self):
        """Golden hour window must include sunrise/sunset time."""
        gs, gset, sunrise, sunset = get_golden_hours()
        self.assertLessEqual(gs[0], sunrise)
        self.assertGreaterEqual(gs[1], sunrise)
        self.assertLessEqual(gset[0], sunset)
        self.assertGreaterEqual(gset[1], sunset)
    def test_capture_sky_frame_returns_none_with_no_cameras(self):
        with patch.object(_mod, "SKY_CAMERAS", []):
            with tempfile.TemporaryDirectory() as tmpdir:
                with patch.object(_mod, "SKY_ARCHIVE", Path(tmpdir)):
                    with patch.object(_mod, "current_session", return_value=("sunrise", datetime.now())):
                        result, cam = _mod.capture_sky_frame()
        self.assertIsNone(result)
        self.assertIsNone(cam)


class TestFunctional(unittest.TestCase):
    def test_main_not_golden_hour_exits(self):
        """main() during non-golden-hour must exit without capturing."""
        with patch.object(_mod, "current_session", return_value=(None, None)):
            with patch.object(_mod, "capture_sky_frame") as mock_cap:
                with patch.object(_mod, "save_state"):
                    with patch.object(_mod, "load_state", return_value={"last_capture": "", "sessions_today": [],
                                                                          "frames_today": 0, "total_frames": 0}):
                        with patch.object(_mod, "get_golden_hours", return_value=(
                            (datetime.now() - timedelta(hours=2), datetime.now() - timedelta(hours=1)),
                            (datetime.now() + timedelta(hours=4), datetime.now() + timedelta(hours=5)),
                            datetime.now() - timedelta(hours=1, minutes=30),
                            datetime.now() + timedelta(hours=4, minutes=30),
                        )):
                            _mod.main()
        mock_cap.assert_not_called()
    def test_generate_timelapse_needs_min_frames(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "BEST_DIR", Path(tmpdir)):
                result = _mod.generate_timelapse(days=7)
        self.assertIsNone(result)


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.LATITUDE, float)
        self.assertIsInstance(_mod.LONGITUDE, float)
        self.assertIsInstance(_mod.GOLDEN_BEFORE, int)
        self.assertIsInstance(_mod.GOLDEN_AFTER, int)
        self.assertIsInstance(_mod.SKY_ARCHIVE, Path)
        self.assertIsInstance(_mod.STATE_FILE, Path)
    def test_functions_exist(self):
        for fn in ("solar_times", "get_golden_hours", "is_golden_hour", "current_session",
                   "capture_frame", "frame_color_score", "pick_best_frame",
                   "load_state", "save_state", "post_session_best",
                   "generate_timelapse", "post_weekly_timelapse", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
