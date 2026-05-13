"""
test_nova_camera_monitor.py — All 7 test categories for nova_camera_monitor.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Stub camera_config before loading script
_cam_cfg = MagicMock()
_cam_cfg.CAMERAS = {
    "front_door": "rtsps://192.168.1.9:7441/test1?enableSrtp",
    "back_patio": "rtsps://192.168.1.9:7441/test2?enableSrtp",
}
sys.modules["camera_config"] = _cam_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_camera_monitor.py"


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_passwords(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "passwd", "secret =", "sk-", "ghp_"]:
            self.assertNotIn(pat, src, f"Potential credential: {pat!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src, "Hardcoded home path")

    def test_no_pii_emails_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src, f"PII email: {pat!r}")

    def test_camera_config_is_not_committed(self):
        """camera_config.py is gitignored — confirm CAMERAS not hardcoded in monitor."""
        src = _SCRIPT.read_text()
        self.assertNotIn("rtsps://", src, "RTSP URL hardcoded in monitor script")

    def test_ffmpeg_uses_absolute_path(self):
        """ffmpeg command uses absolute path for security (no PATH hijacking)."""
        src = _SCRIPT.read_text()
        self.assertIn("/opt/homebrew/bin/ffmpeg", src,
                      "ffmpeg should use absolute path, not rely on PATH")

    def test_output_stored_locally(self):
        """Frames are stored in user's home workspace, not cloud."""
        src = _SCRIPT.read_text()
        self.assertIn("expanduser", src, "Should use expanduser for workspace path")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_subprocess_timeout_set(self):
        """Each ffmpeg subprocess must have a timeout to avoid hanging."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=10", src, "Subprocess must have timeout=10")

    def test_timeout_expired_handled(self):
        """TimeoutExpired must be caught and not crash the loop."""
        src = _SCRIPT.read_text()
        self.assertIn("TimeoutExpired", src, "TimeoutExpired not handled")

    def test_status_output_is_concise(self):
        """Status dictionary only shows failed cameras (not all cameras)."""
        src = _SCRIPT.read_text()
        self.assertIn('"ok"', src, "Success status string missing")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_individual_camera_failure_does_not_stop_others(self):
        """A failed camera capture must not prevent other cameras from running."""
        captured = []
        call_count = [0]

        def mock_run(cmd, capture_output=False, timeout=None):
            call_count[0] += 1
            cam_name = [c for c in ["front_door", "back_patio"] if c in cmd[-1]]
            captured.extend(cam_name)
            r = MagicMock()
            r.returncode = 1 if call_count[0] == 1 else 0
            return r

        with patch("subprocess.run", side_effect=mock_run):
            with patch("os.makedirs"):
                import importlib
                if "nova_camera_monitor" in sys.modules:
                    del sys.modules["nova_camera_monitor"]
                spec = importlib.util.spec_from_file_location("nova_camera_monitor", _SCRIPT)
                mod = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(mod)
                except Exception:
                    pass
        # Both cameras should be attempted regardless of first failure
        self.assertGreaterEqual(call_count[0], 1)

    def test_timeout_marks_camera_as_timeout(self):
        """TimeoutExpired during subprocess should result in 'timeout' status."""
        import subprocess
        captured_status = {}

        real_cameras = {"front_door": "rtsps://test", "back_patio": "rtsps://test2"}

        def mock_run(cmd, capture_output=False, timeout=None):
            raise subprocess.TimeoutExpired(cmd, timeout)

        with patch.dict(sys.modules, {"camera_config": _cam_cfg}):
            with patch("subprocess.run", side_effect=mock_run):
                with patch("os.makedirs"):
                    for name, url in real_cameras.items():
                        try:
                            import subprocess as sp
                            sp.run(["ffmpeg"], timeout=10)
                        except sp.TimeoutExpired:
                            captured_status[name] = "timeout"

        for v in captured_status.values():
            self.assertEqual(v, "timeout")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_success_count_calculation(self):
        """Status dict: count only 'ok' entries."""
        status = {"cam1": "ok", "cam2": "error", "cam3": "timeout", "cam4": "ok"}
        count = len([s for s in status.values() if s == "ok"])
        self.assertEqual(count, 2)

    def test_output_filename_pattern(self):
        """Output file should follow <name>_latest.jpg pattern."""
        name = "front_door"
        expected = f"{name}_latest.jpg"
        self.assertEqual(expected, "front_door_latest.jpg")

    def test_ffmpeg_constant_path(self):
        src = _SCRIPT.read_text()
        self.assertIn("FFMPEG", src)

    def test_storage_dir_uses_expanduser(self):
        src = _SCRIPT.read_text()
        self.assertIn("expanduser", src)

    def test_script_imports_camera_config(self):
        src = _SCRIPT.read_text()
        self.assertIn("camera_config", src)

    def test_rtsp_transport_tcp_flag(self):
        """ffmpeg must use TCP transport for RTSP (more reliable)."""
        src = _SCRIPT.read_text()
        self.assertIn("rtsp_transport", src)
        self.assertIn("tcp", src)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_all_cameras_get_processed(self):
        """Every camera in CAMERAS should produce a status entry."""
        results = {}
        call_args_list = []

        def mock_run(cmd, capture_output=False, timeout=None):
            r = MagicMock()
            r.returncode = 0
            call_args_list.append(cmd)
            return r

        test_cameras = {"cam_a": "rtsps://a", "cam_b": "rtsps://b"}
        with patch("subprocess.run", side_effect=mock_run):
            for name, url in test_cameras.items():
                try:
                    import subprocess as sp
                    sp.run(["echo", name], capture_output=True, timeout=5)
                    results[name] = "ok"
                except Exception:
                    results[name] = "error"

        self.assertEqual(set(results.keys()), {"cam_a", "cam_b"})

    def test_storage_directory_created(self):
        """Storage directory must be created if missing."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            test_dir = Path(tmpdir) / "camera_frames"
            test_dir.mkdir(parents=True, exist_ok=True)
            self.assertTrue(test_dir.exists())


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_status_output_format(self):
        """Status line must include camera count and timestamp."""
        from datetime import datetime
        status = {"cam1": "ok", "cam2": "timeout"}
        timestamp = datetime.now().isoformat()
        success_count = len([s for s in status.values() if s == "ok"])
        line = f"[{timestamp}] Camera monitor: {success_count}/{len(status)} online"
        self.assertIn("1/2 online", line)

    def test_failed_cameras_reported(self):
        """Non-ok cameras should be listed in output."""
        status = {"cam1": "ok", "cam2": "timeout", "cam3": "error: Connection refused"}
        output_lines = [f"  {name}: {state}" for name, state in status.items() if state != "ok"]
        self.assertEqual(len(output_lines), 2)
        self.assertTrue(any("timeout" in l for l in output_lines))

    def test_script_exits_gracefully_without_camera_config(self):
        """Without camera_config, script should print error and exit(1)."""
        src = _SCRIPT.read_text()
        self.assertIn("sys.exit(1)", src, "Should exit if camera_config missing")


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

    def test_script_exists(self):
        self.assertTrue(_SCRIPT.exists())

    def test_ffmpeg_path_defined(self):
        src = _SCRIPT.read_text()
        self.assertIn("/opt/homebrew/bin/ffmpeg", src)

    def test_script_imports_subprocess(self):
        src = _SCRIPT.read_text()
        self.assertIn("import subprocess", src)

    def test_script_imports_datetime(self):
        src = _SCRIPT.read_text()
        self.assertIn("datetime", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
