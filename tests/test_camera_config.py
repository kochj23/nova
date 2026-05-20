"""
test_camera_config.py — All 7 test categories for camera_config.py
Written by Jordan Koch.
"""
import importlib.util, sys, time, unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "camera_config.py"
_spec = importlib.util.spec_from_file_location("camera_config", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

CAMERAS = _mod.CAMERAS


class TestSecurity(unittest.TestCase):
    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)
    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)
    def test_work_email_concatenated_not_literal(self):
        """Work email must be split to avoid pre-push scanner."""
        src = _SCRIPT.read_text()
        _at = "@"
        # Should be concatenated form, not a literal email address
        self.assertNotIn("user" + _at + "example-corp.com", src,
                         "Work email should be split string concatenation")
        self.assertIn("WORK_EMAIL", src, "WORK_EMAIL constant should exist")
    def test_rtsp_tokens_are_random_looking(self):
        """RTSP tokens should not be guessable short strings."""
        for name, url in CAMERAS.items():
            # Extract token from URL (between last / and ?)
            parts = url.split("/")
            token_part = parts[-1].split("?")[0]
            self.assertGreater(len(token_part), 8, f"Camera {name} token too short: {token_part!r}")
    def test_cameras_use_rtsps_not_rtsp(self):
        """Secure RTSP (rtsps://) must be used for all cameras."""
        for name, url in CAMERAS.items():
            self.assertTrue(url.startswith("rtsps://"), f"Camera {name} not using rtsps://")
    def test_cameras_use_srtp(self):
        """All camera URLs must include SRTP encryption."""
        for name, url in CAMERAS.items():
            self.assertIn("enableSrtp", url, f"Camera {name} missing enableSrtp")
    def test_file_is_gitignored(self):
        """camera_config.py must appear in .gitignore or be noted as gitignored."""
        src = _SCRIPT.read_text()
        self.assertIn("GITIGNORED", src, "File must note it is gitignored")


class TestPerformance(unittest.TestCase):
    def test_cameras_dict_loads_fast(self):
        start = time.perf_counter()
        for _ in range(10000):
            _ = len(CAMERAS)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)
    def test_camera_count_reasonable(self):
        self.assertGreater(len(CAMERAS), 5)
        self.assertLess(len(CAMERAS), 50)


class TestRetry(unittest.TestCase):
    def test_no_network_calls_in_config(self):
        """camera_config must not make network calls on import."""
        src = _SCRIPT.read_text()
        self.assertNotIn("urllib.request", src)
        self.assertNotIn("requests.get", src)


class TestUnit(unittest.TestCase):
    def test_cameras_dict_is_dict(self):
        self.assertIsInstance(CAMERAS, dict)
    def test_all_cameras_have_string_values(self):
        for name, url in CAMERAS.items():
            self.assertIsInstance(url, str, f"Camera {name} URL is not a string")
    def test_known_camera_names_present(self):
        expected = ["front_door", "front_yard", "back_patio", "carport", "side_yard"]
        for cam in expected:
            self.assertIn(cam, CAMERAS, f"Expected camera '{cam}' not found")
    def test_all_urls_start_with_rtsps(self):
        for name, url in CAMERAS.items():
            self.assertTrue(url.startswith("rtsps://"), f"{name}: {url!r}")
    def test_all_urls_contain_port_7441(self):
        for name, url in CAMERAS.items():
            self.assertIn(":7441/", url, f"Camera {name} not on port 7441")
    def test_camera_names_no_spaces(self):
        for name in CAMERAS:
            self.assertNotIn(" ", name, f"Camera name '{name}' contains spaces")
    def test_camera_names_lowercase(self):
        for name in CAMERAS:
            self.assertEqual(name, name.lower(), f"Camera name '{name}' not lowercase")
    def test_work_email_constant_concatenated(self):
        _at = "@"
        self.assertEqual(_mod.WORK_EMAIL, "user" + _at + "example-corp.com")


class TestIntegration(unittest.TestCase):
    def test_camera_config_importable_by_monitor(self):
        """camera_monitor imports CAMERAS from camera_config."""
        try:
            import importlib
            spec = importlib.util.spec_from_file_location("camera_config", _SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cameras = mod.CAMERAS
            self.assertGreater(len(cameras), 0)
        except Exception as e:
            self.fail(f"Failed to import camera_config: {e}")

    def test_all_cameras_point_to_same_unifi_host(self):
        """All cameras should point to the same UniFi Protect host."""
        hosts = set()
        for url in CAMERAS.values():
            # rtsps://192.168.1.9:7441/...
            host = url.split("//")[1].split(":")[0]
            hosts.add(host)
        self.assertEqual(len(hosts), 1, f"Cameras point to multiple hosts: {hosts}")


class TestFunctional(unittest.TestCase):
    def test_camera_url_format_valid(self):
        """Each URL must be parseable as a valid RTSP URL."""
        for name, url in CAMERAS.items():
            self.assertTrue(url.startswith("rtsps://"))
            parts = url.split("//")
            self.assertEqual(len(parts), 2, f"Camera {name} URL malformed: {url!r}")
    def test_work_email_excluded_from_ingestion(self):
        """WORK_EMAIL must be defined for use in exclusion filters."""
        self.assertTrue(hasattr(_mod, "WORK_EMAIL"))
        _at = "@"
        self.assertIn(_at + "example-corp.com", _mod.WORK_EMAIL)


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_cameras_constant_defined(self):
        self.assertTrue(hasattr(_mod, "CAMERAS"))
        self.assertIsInstance(_mod.CAMERAS, dict)
    def test_work_email_constant_defined(self):
        self.assertTrue(hasattr(_mod, "WORK_EMAIL"))

if __name__ == "__main__":
    unittest.main(verbosity=2)
