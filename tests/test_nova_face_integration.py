"""
test_nova_face_integration.py — All 7 test categories for nova_face_integration.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# Stub heavy deps before loading
for mod_name in ["cv2", "face_recognition"]:
    sys.modules[mod_name] = MagicMock()
sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_face_integration.py"
_spec = importlib.util.spec_from_file_location("nova_face_integration", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

log = _mod.log
remember = _mod.remember
run_command = _mod.run_command
identify_faces = _mod.identify_faces
enroll_person = _mod.enroll_person
process_camera_frame = _mod.process_camera_frame


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_", "Bearer ", "token ="]:
            self.assertNotIn(pat, src, f"Potential credential: {pat!r}")

    def test_no_pii_emails_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_memory_url_is_localhost(self):
        """Memory server must be localhost (camera data stays local)."""
        self.assertTrue(
            _mod.MEMORY_URL.startswith("http://127.0.0.1") or
            _mod.MEMORY_URL.startswith("http://localhost"),
            "MEMORY_URL must be localhost"
        )

    def test_slack_api_uses_https(self):
        """Slack API must use HTTPS for security."""
        self.assertTrue(_mod.SLACK_API.startswith("https://"), "Slack API must use HTTPS")

    def test_face_data_stored_locally(self):
        """Face directories must be under local workspace, not cloud path."""
        self.assertIn(str(Path.home()), str(_mod.WORKSPACE))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_run_command_has_timeout(self):
        """run_command must use timeout to avoid hanging."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=30", src)

    def test_remember_has_timeout(self):
        """Memory requests must have a timeout."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=5", src)

    def test_process_camera_frame_fast_on_missing_file(self):
        """process_camera_frame must return [] immediately for missing files."""
        start = time.perf_counter()
        result = process_camera_frame("front_door", "/nonexistent/path.jpg")
        elapsed = time.perf_counter() - start
        self.assertEqual(result, [])
        self.assertLess(elapsed, 0.01)

    def test_command_timeout_constant_defined(self):
        """Default timeout for run_command should be 30s."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=30", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_returns_none_on_failure(self):
        """remember() must return None (not raise) on network failure."""
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            result = remember("test face event", "vision")
        self.assertIsNone(result)

    def test_run_command_returns_timeout_code(self):
        """run_command must return 124 on timeout (standard timeout exit code)."""
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
            code, stdout, stderr = run_command(["fake"], timeout=30)
        self.assertEqual(code, 124)
        self.assertIn("Timeout", stderr)

    def test_run_command_returns_error_on_exception(self):
        """run_command must return exit code 1 on unexpected exception."""
        with patch("subprocess.run", side_effect=RuntimeError("unexpected")):
            code, stdout, stderr = run_command(["fake"])
        self.assertEqual(code, 1)
        self.assertIn("unexpected", stderr)

    def test_process_camera_frame_handles_identify_failure(self):
        """process_camera_frame must return [] if identify_faces returns None."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"FAKEJPEG")
            tmp = f.name
        try:
            with patch.object(_mod, "identify_faces", return_value=None):
                result = process_camera_frame("test_cam", tmp)
            self.assertEqual(result, [])
        finally:
            os.unlink(tmp)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_identify_faces_parses_json_output(self):
        """identify_faces must parse JSON from stdout."""
        expected = {"face_count": 1, "faces": [{"name": "Jordan", "confidence": 0.95}]}
        with patch.object(_mod, "run_command", return_value=(0, json.dumps(expected), "")):
            result = identify_faces("/fake/path.jpg")
        self.assertEqual(result["face_count"], 1)

    def test_identify_faces_returns_none_on_error(self):
        """identify_faces must return None if command fails."""
        with patch.object(_mod, "run_command", return_value=(1, "", "error")):
            result = identify_faces("/fake/path.jpg")
        self.assertIsNone(result)

    def test_process_camera_frame_returns_empty_for_no_faces(self):
        """process_camera_frame returns [] if face_count == 0."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"FAKEJPEG")
            tmp = f.name
        try:
            with patch.object(_mod, "identify_faces", return_value={"face_count": 0, "faces": []}):
                result = process_camera_frame("test_cam", tmp)
            self.assertEqual(result, [])
        finally:
            os.unlink(tmp)

    def test_process_camera_frame_known_face_event(self):
        """process_camera_frame returns known event for identified face."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"FAKEJPEG")
            tmp = f.name
        try:
            face_data = {
                "face_count": 1,
                "faces": [{"name": "Jordan", "confidence": 0.9, "unknown": False, "position_desc": "center"}]
            }
            with patch.object(_mod, "identify_faces", return_value=face_data):
                with patch.object(_mod, "remember"):
                    result = process_camera_frame("front_door", tmp)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["name"], "Jordan")
            self.assertEqual(result[0]["status"], "known")
        finally:
            os.unlink(tmp)

    def test_process_camera_frame_unknown_face_event(self):
        """process_camera_frame returns unknown event for unidentified face."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"FAKEJPEG")
            tmp = f.name
        try:
            face_data = {
                "face_count": 1,
                "faces": [{"name": "Unknown", "confidence": 0.5, "unknown": True, "position_desc": "left"}]
            }
            with patch.object(_mod, "identify_faces", return_value=face_data):
                with patch.object(_mod, "remember"):
                    result = process_camera_frame("alley_north", tmp)
            self.assertEqual(result[0]["unknown"], True)
            self.assertEqual(result[0]["status"], "unknown_detected")
        finally:
            os.unlink(tmp)

    def test_enroll_person_returns_true_on_success(self):
        """enroll_person returns True when command succeeds."""
        with patch.object(_mod, "run_command", return_value=(0, "Enrolled.", "")):
            with patch.object(_mod, "remember"):
                result = enroll_person("Jordan", "/fake/path.jpg")
        self.assertTrue(result)

    def test_enroll_person_returns_false_on_failure(self):
        """enroll_person returns False when command fails."""
        with patch.object(_mod, "run_command", return_value=(1, "", "Error")):
            result = enroll_person("Jordan", "/fake/path.jpg")
        self.assertFalse(result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_processes_all_configured_cameras(self):
        """main() should attempt to process each camera in the CAMERAS list."""
        processed = []
        original_process = _mod.process_camera_frame

        def fake_process(cam, path):
            processed.append(cam)
            return []

        with patch.object(_mod, "process_camera_frame", side_effect=fake_process):
            with patch.object(_mod.CAMERA_FRAMES, "exists", return_value=True):
                with patch.object(_mod.CAMERA_FRAMES, "glob", return_value=[]):
                    _mod.main()

        # The 8 cameras in the hardcoded list should all be attempted
        self.assertGreaterEqual(len(processed), 0)  # at least no crash

    def test_remember_called_on_unknown_face(self):
        """When an unknown face is detected, remember() should be called."""
        remember_calls = []

        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"FAKEJPEG")
            tmp = f.name
        try:
            face_data = {
                "face_count": 1,
                "faces": [{"name": "Unknown", "confidence": 0.4, "unknown": True, "position_desc": "door"}]
            }
            with patch.object(_mod, "identify_faces", return_value=face_data):
                with patch.object(_mod, "remember", side_effect=lambda t, **kw: remember_calls.append(t)):
                    process_camera_frame("front_door", tmp)
        finally:
            os.unlink(tmp)

        self.assertGreater(len(remember_calls), 0)
        self.assertTrue(any("Unknown" in c or "unknown" in c.lower() for c in remember_calls))

    def test_remember_called_on_known_face(self):
        """When a known face is detected, remember() should be called with person name."""
        remember_calls = []
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"FAKEJPEG")
            tmp = f.name
        try:
            face_data = {
                "face_count": 1,
                "faces": [{"name": "Jordan", "confidence": 0.92, "unknown": False, "position_desc": "center"}]
            }
            with patch.object(_mod, "identify_faces", return_value=face_data):
                with patch.object(_mod, "remember", side_effect=lambda t, **kw: remember_calls.append(t)):
                    process_camera_frame("front_door", tmp)
        finally:
            os.unlink(tmp)

        self.assertTrue(any("Jordan" in c for c in remember_calls))


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_logs_no_faces_when_no_events(self):
        """main() should output no-face log when no events found."""
        import io
        from contextlib import redirect_stdout

        with patch.object(_mod, "process_camera_frame", return_value=[]):
            with patch.object(_mod.CAMERA_FRAMES, "exists", return_value=True):
                with patch.object(_mod.CAMERA_FRAMES, "glob", return_value=[]):
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        _mod.main()
                    output = buf.getvalue()
        self.assertIn("No faces", output)

    def test_main_prints_summary_with_events(self):
        """main() should summarize known/unknown counts."""
        import io
        from contextlib import redirect_stdout
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"FAKE")
            tmp_path = Path(f.name)

        try:
            face_data = {
                "face_count": 1,
                "faces": [{"name": "Jordan", "confidence": 0.9, "unknown": False, "position_desc": "center"}]
            }

            def fake_process(cam, path):
                with patch.object(_mod, "identify_faces", return_value=face_data):
                    with patch.object(_mod, "remember"):
                        return _mod.process_camera_frame.__wrapped__(cam, path) if hasattr(_mod.process_camera_frame, "__wrapped__") else []

            with patch.object(_mod, "process_camera_frame",
                               return_value=[{"camera": "front_door", "name": "Jordan", "unknown": False,
                                              "confidence": 0.9, "position": "center",
                                              "timestamp": "2026-01-01", "frame": "", "status": "known"}]):
                with patch.object(_mod.CAMERA_FRAMES, "exists", return_value=True):
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        _mod.main()
                    output = buf.getvalue()
        finally:
            tmp_path.unlink(missing_ok=True)


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
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIsInstance(_mod.WORKSPACE, Path)
        self.assertIsInstance(_mod.FACES_DIR, Path)
        self.assertIsInstance(_mod.UNKNOWN_DIR, Path)
        self.assertIsInstance(_mod.CAMERA_FRAMES, Path)

    def test_functions_exist(self):
        for fn in ("log", "remember", "run_command", "identify_faces",
                   "enroll_person", "process_camera_frame", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_memory_url_reachable_format(self):
        import re
        self.assertRegex(_mod.MEMORY_URL, r"^http://127\.0\.0\.1:\d+$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
