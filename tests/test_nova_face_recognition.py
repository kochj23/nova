"""
test_nova_face_recognition.py — All 7 test categories for nova_face_recognition.py
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

# Stub deps before loading
_nova_cfg = MagicMock()
_nova_cfg.slack_bot_token.return_value = "xoxb-test-token"
_nova_cfg.SLACK_PHOTOS = "#nova-photos"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.SLACK_API = "https://slack.com/api"
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
sys.modules["nova_config"] = _nova_cfg

for mod_name in ["cv2", "face_recognition"]:
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_face_recognition.py"
_spec = importlib.util.spec_from_file_location("nova_face_recognition", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_", "Jkoogie"]:
            self.assertNotIn(pat, src, f"Credential found: {pat!r}")

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_slack_token_loaded_from_nova_config(self):
        """Slack token must come from nova_config (Keychain), not hardcoded."""
        src = _SCRIPT.read_text()
        self.assertIn("nova_config.slack_bot_token", src, "Must use nova_config for Slack token")

    def test_face_data_stored_locally(self):
        """Unknown face crops stored in local workspace."""
        self.assertIn(str(Path.home()), str(_mod.UNKNOWN_DIR))

    def test_slack_api_uses_https(self):
        self.assertTrue(_mod.SLACK_API.startswith("https://"))

    def test_vision_model_is_local_ollama(self):
        self.assertTrue(
            _mod.OLLAMA_URL.startswith("http://127.0.0.1") or
            _mod.OLLAMA_URL.startswith("http://localhost"),
        )


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_vector_remember_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=10", src)

    def test_tolerance_constant_within_bounds(self):
        """Face recognition tolerance must be in (0, 1)."""
        self.assertGreater(_mod.TOLERANCE, 0)
        self.assertLess(_mod.TOLERANCE, 1.0)

    def test_person_cooldown_positive(self):
        self.assertGreater(_mod.PERSON_COOLDOWN, 0)

    def test_unknown_cooldown_positive(self):
        self.assertGreater(_mod.UNKNOWN_COOLDOWN, 0)

    def test_save_state_fast(self):
        """save_state must complete quickly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "state.json"):
                state = {"last_seen": {}, "unknown_alerts": {}}
                start = time.perf_counter()
                _mod.save_state(state)
                elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_vector_remember_does_not_raise_on_failure(self):
        """vector_remember must swallow exceptions silently."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            # Should not raise
            _mod.vector_remember("test text", {})

    def test_slack_post_does_not_raise_on_failure(self):
        """slack_post must catch exceptions."""
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            _mod.slack_post("Test message")

    def test_scan_cameras_handles_sam_exception(self):
        """scan_cameras must continue if one camera throws an exception."""
        mock_sam = MagicMock()
        mock_sam.identify.side_effect = RuntimeError("sam-faces crashed")

        with patch.object(_mod, "_load_sam_faces", return_value=mock_sam):
            with patch.object(_mod, "save_state"):
                with tempfile.TemporaryDirectory() as tmpdir:
                    fake_frame = Path(tmpdir) / "front_door_latest.jpg"
                    fake_frame.write_bytes(b"FAKEJPEG")
                    with patch.object(_mod, "CAMERA_FRAMES", Path(tmpdir)):
                        # Should not raise despite sam crash
                        result = _mod.scan_cameras()
        self.assertIsInstance(result, list)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_load_state_returns_defaults_when_missing(self):
        with patch.object(_mod, "STATE_FILE", Path("/nonexistent/state.json")):
            state = _mod.load_state()
        self.assertIn("last_seen", state)
        self.assertIn("unknown_alerts", state)

    def test_save_and_load_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "state.json"):
                state = {"last_seen": {"jordan": 12345.0}, "unknown_alerts": {}}
                _mod.save_state(state)
                loaded = _mod.load_state()
        self.assertEqual(loaded["last_seen"]["jordan"], 12345.0)

    def test_exterior_cameras_list_populated(self):
        self.assertGreater(len(_mod.EXTERIOR_CAMERAS), 0)
        for cam in _mod.EXTERIOR_CAMERAS:
            self.assertTrue(cam.endswith("_latest.jpg"), f"Camera file should end in _latest.jpg: {cam}")

    def test_person_cooldown_is_30_minutes(self):
        self.assertEqual(_mod.PERSON_COOLDOWN, 1800)

    def test_unknown_cooldown_is_10_minutes(self):
        self.assertEqual(_mod.UNKNOWN_COOLDOWN, 600)

    def test_log_function_exists_and_callable(self):
        self.assertTrue(callable(_mod.log))

    def test_describe_scene_returns_none_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            result = _mod.describe_scene("/fake/path.jpg")
        self.assertIsNone(result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_post_detections_posts_known_to_slack(self):
        """post_detections should call slack_post for known detections."""
        slack_calls = []
        with patch.object(_mod, "slack_post", side_effect=lambda t, **kw: slack_calls.append(t)):
            with patch.object(_mod, "vector_remember"):
                _mod.post_detections([{
                    "type": "known",
                    "name": "Jordan",
                    "camera": "Front Door",
                    "confidence": 95,
                }])
        self.assertGreater(len(slack_calls), 0)
        self.assertTrue(any("Jordan" in c for c in slack_calls))

    def test_post_detections_empty_list_does_nothing(self):
        """post_detections with empty list should not call anything."""
        slack_calls = []
        with patch.object(_mod, "slack_post", side_effect=lambda t, **kw: slack_calls.append(t)):
            _mod.post_detections([])
        self.assertEqual(len(slack_calls), 0)

    def test_scan_cameras_skips_old_frames(self):
        """Frames older than 5 minutes should be skipped."""
        mock_sam = MagicMock()
        with patch.object(_mod, "_load_sam_faces", return_value=mock_sam):
            with patch.object(_mod, "save_state"):
                with tempfile.TemporaryDirectory() as tmpdir:
                    # Write an old file
                    fake_frame = Path(tmpdir) / "front_door_latest.jpg"
                    fake_frame.write_bytes(b"FAKEJPEG")
                    # Set mtime to 10 minutes ago
                    import os as _os
                    old_time = time.time() - 600
                    _os.utime(str(fake_frame), (old_time, old_time))
                    with patch.object(_mod, "CAMERA_FRAMES", Path(tmpdir)):
                        result = _mod.scan_cameras()
        # sam.identify should NOT be called for old frames
        mock_sam.identify.assert_not_called()


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_calls_scan_and_post(self):
        """main() should call scan_cameras and post_detections."""
        with patch.object(_mod, "scan_cameras", return_value=[]) as mock_scan:
            with patch.object(_mod, "post_detections") as mock_post:
                _mod.main()
        mock_scan.assert_called_once()
        mock_post.assert_called_once_with([])

    def test_main_logs_results(self):
        """main() must log the count of known and unknown detections."""
        import io
        from contextlib import redirect_stdout
        with patch.object(_mod, "scan_cameras", return_value=[
            {"type": "known"}, {"type": "unknown"}
        ]):
            with patch.object(_mod, "post_detections"):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    _mod.main()
                output = buf.getvalue()
        self.assertIn("1 known", output)
        self.assertIn("1 unknown", output)


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
        self.assertIsInstance(_mod.EXTERIOR_CAMERAS, list)
        self.assertIsInstance(_mod.TOLERANCE, float)
        self.assertIsInstance(_mod.PERSON_COOLDOWN, int)
        self.assertIsInstance(_mod.UNKNOWN_COOLDOWN, int)
        self.assertIsInstance(_mod.WORKSPACE, Path)

    def test_functions_exist(self):
        for fn in ("log", "describe_scene", "slack_post", "slack_upload_image",
                   "vector_remember", "load_state", "save_state",
                   "scan_cameras", "post_detections", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
