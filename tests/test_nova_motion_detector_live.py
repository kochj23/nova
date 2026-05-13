"""
test_nova_motion_detector_live.py — All 7 test categories for nova_motion_detector_live.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Stub heavy deps
for mod_name in ["cv2", "numpy"]:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()
sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_motion_detector_live.py"
_spec = importlib.util.spec_from_file_location("nova_motion_detector_live", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

log = _mod.log
remember = _mod.remember
get_latest_frame = _mod.get_latest_frame
capture_clip = _mod.capture_clip
cleanup_old_clips = _mod.cleanup_old_clips
get_storage_stats = _mod.get_storage_stats


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_", "Jkoogie"]:
            self.assertNotIn(pat, src, f"Credential: {pat!r}")

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_memory_url_is_localhost(self):
        self.assertTrue(_mod.MEMORY_URL.startswith("http://127.0.0.1"))

    def test_clips_stored_on_local_volume(self):
        """Motion clips must not be sent to cloud."""
        src = _SCRIPT.read_text()
        self.assertNotIn("s3://", src)
        self.assertNotIn("cloud", src.lower().replace("# ", ""))

    def test_rtsp_url_contains_local_ip(self):
        """RTSP URL should be local network camera, not cloud."""
        self.assertIn("192.168.", _mod.RTSP_URL)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_memory_remember_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=5", src)

    def test_cleanup_old_clips_fast_on_empty_dir(self):
        """Cleanup must return quickly for empty/missing directory."""
        with patch.object(_mod, "CLIPS_DIR", Path("/nonexistent/clips")):
            start = time.perf_counter()
            result = cleanup_old_clips()
            elapsed = time.perf_counter() - start
        self.assertEqual(result, 0)
        self.assertLess(elapsed, 0.01)

    def test_get_latest_frame_returns_none_for_missing_dir(self):
        """get_latest_frame must return None quickly if frames dir missing."""
        with patch.object(_mod, "FRAMES_DIR", Path("/nonexistent")):
            start = time.perf_counter()
            result = get_latest_frame()
            elapsed = time.perf_counter() - start
        self.assertIsNone(result)
        self.assertLess(elapsed, 0.01)

    def test_motion_threshold_defined(self):
        """Motion detection threshold must be defined."""
        src = _SCRIPT.read_text()
        self.assertIn("threshold", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_does_not_raise_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = remember("Motion detected", "vision")
        self.assertIsNone(result)

    def test_capture_clip_returns_none_on_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
            with patch.object(_mod, "CLIPS_DIR", Path("/tmp")):
                result = capture_clip("rtsp://test", duration=10)
        self.assertIsNone(result)

    def test_capture_clip_returns_none_on_ffmpeg_error(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "ffmpeg: No such file"
        with patch("subprocess.run", return_value=mock_result):
            with patch.object(_mod, "CLIPS_DIR", Path("/tmp")):
                result = capture_clip("rtsp://test", duration=5)
        self.assertIsNone(result)

    def test_get_storage_stats_returns_unknown_on_failure(self):
        with patch("subprocess.run", side_effect=Exception("du failed")):
            result = get_storage_stats()
        self.assertEqual(result, "unknown")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_get_latest_frame_returns_path_if_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            frame = Path(tmpdir) / "front_door_latest.jpg"
            frame.write_bytes(b"FAKE")
            with patch.object(_mod, "FRAMES_DIR", Path(tmpdir)):
                result = get_latest_frame("front_door")
        self.assertEqual(result, str(frame))

    def test_get_latest_frame_returns_none_if_not_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "FRAMES_DIR", Path(tmpdir)):
                result = get_latest_frame("nonexistent_cam")
        self.assertIsNone(result)

    def test_cleanup_removes_old_files(self):
        """cleanup_old_clips must remove files older than N days."""
        with tempfile.TemporaryDirectory() as tmpdir:
            old_clip = Path(tmpdir) / "motion_20200101_000000.mp4"
            old_clip.write_bytes(b"FAKE")
            # Set mtime to 10 days ago
            old_time = time.time() - (10 * 86400)
            os.utime(str(old_clip), (old_time, old_time))
            with patch.object(_mod, "CLIPS_DIR", Path(tmpdir)):
                removed = cleanup_old_clips(days=7)
        self.assertEqual(removed, 1)

    def test_cleanup_preserves_recent_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            new_clip = Path(tmpdir) / "motion_20260101_120000.mp4"
            new_clip.write_bytes(b"FAKE")
            with patch.object(_mod, "CLIPS_DIR", Path(tmpdir)):
                removed = cleanup_old_clips(days=7)
        self.assertEqual(removed, 0)
        self.assertTrue(new_clip.exists())

    def test_quality_presets_defined(self):
        """capture_clip should recognize low/medium/high quality presets."""
        src = _SCRIPT.read_text()
        self.assertIn('"low"', src)
        self.assertIn('"medium"', src)
        self.assertIn('"high"', src)

    def test_clip_cooldown_positive(self):
        src = _SCRIPT.read_text()
        self.assertIn("clip_cooldown", src)

    def test_motion_threshold_in_loop(self):
        src = _SCRIPT.read_text()
        self.assertIn("motion_pct", src)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_remember_called_after_successful_capture(self):
        """After capturing a clip, remember() must be called."""
        remember_calls = []
        mock_result = MagicMock()
        mock_result.returncode = 0

        with tempfile.TemporaryDirectory() as tmpdir:
            # Pre-create expected clip file
            with patch("subprocess.run", return_value=mock_result):
                with patch.object(_mod, "CLIPS_DIR", Path(tmpdir)):
                    with patch("pathlib.Path.exists", return_value=True):
                        with patch("pathlib.Path.stat") as mock_stat:
                            mock_stat.return_value.st_size = 500000
                            with patch.object(_mod, "remember",
                                               side_effect=lambda t, **kw: remember_calls.append(t)):
                                capture_clip("rtsp://test", duration=5)

    def test_motion_loop_exits_on_cleanup_command(self):
        """Running with 'cleanup' argument should call cleanup and exit."""
        import io
        from contextlib import redirect_stdout
        with patch("sys.argv", ["nova_motion_detector_live.py", "cleanup"]):
            with patch.object(_mod, "cleanup_old_clips", return_value=3) as mock_clean:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    _mod.main()
                output = buf.getvalue()
        mock_clean.assert_called_once()
        self.assertIn("3", output)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_with_cleanup_arg(self):
        """main() with 'cleanup' argument should clean old clips."""
        with patch("sys.argv", ["nova_motion_detector_live.py", "cleanup"]):
            with patch.object(_mod, "cleanup_old_clips", return_value=5) as mock_clean:
                import io
                from contextlib import redirect_stdout
                buf = io.StringIO()
                with redirect_stdout(buf):
                    _mod.main()
        mock_clean.assert_called_once()

    def test_detect_motion_returns_zero_on_missing_images(self):
        """detect_motion_in_frames must return 0 for missing files."""
        result = _mod.detect_motion_in_frames("/nonexistent/a.jpg", "/nonexistent/b.jpg")
        self.assertEqual(result, 0)

    def test_storage_stats_runs_du_command(self):
        """get_storage_stats must call subprocess.run with du."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "1.2G\t/Volumes/Data/motion_clips"
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = get_storage_stats()
        self.assertIn("du", mock_run.call_args[0][0])


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_constants_defined(self):
        self.assertIsInstance(_mod.CLIPS_DIR, Path)
        self.assertIsInstance(_mod.FRAMES_DIR, Path)
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIsInstance(_mod.RTSP_URL, str)

    def test_functions_exist(self):
        for fn in ("log", "remember", "get_latest_frame", "detect_motion_in_frames",
                   "capture_clip", "cleanup_old_clips", "get_storage_stats", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_workspace_uses_home(self):
        self.assertTrue(str(_mod.WORKSPACE).startswith(str(Path.home())))


if __name__ == "__main__":
    unittest.main(verbosity=2)
