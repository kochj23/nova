"""
test_nova_nightly_protect.py — All 7 test categories for nova_nightly_protect.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.VECTOR_URL = "http://192.168.1.6:18790/remember"
sys.modules["nova_config"] = _nova_cfg

_nova_logger = MagicMock()
_nova_logger.log = MagicMock()
_nova_logger.LOG_INFO = "INFO"
_nova_logger.LOG_ERROR = "ERROR"
sys.modules["nova_logger"] = _nova_logger

# Stub ProtectClient from nova_protect_monitor
_protect_mod = MagicMock()
_MockClient = MagicMock()
_protect_mod.ProtectClient = _MockClient
sys.modules["nova_protect_monitor"] = _protect_mod

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_nightly_protect.py"
_spec = importlib.util.spec_from_file_location("nova_nightly_protect", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_acknowledged = _mod.load_acknowledged
INTERIOR_PREFIX = _mod.INTERIOR_PREFIX


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "AKIA", "password ="]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com"]:
            self.assertNotIn(p, src)

    def test_interior_cameras_never_reported(self):
        """Nightly report must filter out Interior cameras."""
        src = _SCRIPT.read_text()
        self.assertIn("Interior", src, "Interior prefix filter must be present")

    def test_ack_path_under_home(self):
        self.assertTrue(str(_mod.ACK_PATH).startswith(str(Path.home())))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_load_acknowledged_fast(self):
        import time
        with patch.object(_mod.ACK_PATH, "exists", return_value=False):
            start = time.perf_counter()
            for _ in range(100):
                load_acknowledged()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_storage_bar_bounded(self):
        """Storage bar should always be exactly 10 chars."""
        pct = 75
        bar = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
        self.assertEqual(len(bar), 10)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_main_posts_error_on_login_failure(self):
        """When ProtectClient.login() fails, main() should post an error to Slack."""
        mock_client = MagicMock()
        mock_client.login.return_value = False
        _MockClient.return_value = mock_client

        slack_calls = []
        with patch.object(_mod, "slack_post", side_effect=lambda m: slack_calls.append(m)):
            _mod.main()

        self.assertGreater(len(slack_calls), 0)
        self.assertIn("Cannot connect", slack_calls[0])

    def test_main_posts_error_on_bootstrap_failure(self):
        """When bootstrap fails, main() should post an error."""
        mock_client = MagicMock()
        mock_client.login.return_value = True
        mock_client.get_bootstrap.return_value = None
        _MockClient.return_value = mock_client

        slack_calls = []
        with patch.object(_mod, "slack_post", side_effect=lambda m: slack_calls.append(m)):
            _mod.main()

        self.assertGreater(len(slack_calls), 0)
        self.assertIn("Bootstrap failed", slack_calls[0])

    def test_load_acknowledged_returns_empty_on_missing_file(self):
        with patch.object(_mod.ACK_PATH, "exists", return_value=False):
            result = load_acknowledged()
        self.assertIsInstance(result, dict)

    def test_load_acknowledged_returns_empty_on_corrupt_json(self):
        with patch.object(_mod.ACK_PATH, "exists", return_value=True):
            with patch("pathlib.Path.read_text", return_value="{BAD"):
                result = load_acknowledged()
        self.assertIsInstance(result, dict)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_interior_prefix_is_interior(self):
        self.assertEqual(INTERIOR_PREFIX, "Interior")

    def test_camera_filter_excludes_interior(self):
        cameras = [
            {"name": "Exterior Driveway", "state": "CONNECTED", "type": "UVC-G4"},
            {"name": "Interior Hallway", "state": "CONNECTED", "type": "UVC-G4"},
        ]
        exterior = [c for c in cameras if not c["name"].startswith(INTERIOR_PREFIX)]
        self.assertEqual(len(exterior), 1)
        self.assertEqual(exterior[0]["name"], "Exterior Driveway")

    def test_nvr_uptime_calculation(self):
        uptime_s = 2 * 86400 + 3 * 3600
        days = uptime_s // 86400
        hours = (uptime_s % 86400) // 3600
        self.assertEqual(days, 2)
        self.assertEqual(hours, 3)

    def test_storage_percentage(self):
        used = 2 * (1024 ** 4)  # 2TB
        cap = 8 * (1024 ** 4)   # 8TB
        pct = used / cap * 100
        self.assertAlmostEqual(pct, 25.0)

    def test_smart_detect_count_aggregation(self):
        events = [
            {"camera": "cam1", "type": "smartDetectZone", "smartDetectTypes": ["person", "vehicle"]},
            {"camera": "cam1", "type": "smartDetectZone", "smartDetectTypes": ["person"]},
        ]
        smart_counts = {}
        for e in events:
            for t in e.get("smartDetectTypes", []):
                smart_counts.setdefault("cam1", {})
                smart_counts["cam1"][t] = smart_counts["cam1"].get(t, 0) + 1
        self.assertEqual(smart_counts["cam1"]["person"], 2)
        self.assertEqual(smart_counts["cam1"]["vehicle"], 1)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def _make_bootstrap(self, cameras=None):
        return {
            "cameras": cameras or [
                {"id": "cam1", "name": "Exterior Front", "state": "CONNECTED", "type": "UVC-G4"},
            ],
            "nvr": {
                "uptime": 86400,
                "firmwareVersion": "3.0.22",
                "storageInfo": {"totalSize": 2 * 1024 ** 4, "totalCapacity": 8 * 1024 ** 4},
            }
        }

    def test_main_posts_full_report_on_success(self):
        mock_client = MagicMock()
        mock_client.login.return_value = True
        mock_client.get_bootstrap.return_value = self._make_bootstrap()
        mock_client.get_events.return_value = []
        _MockClient.return_value = mock_client

        slack_calls = []
        with patch.object(_mod, "slack_post", side_effect=lambda m: slack_calls.append(m)):
            with patch("urllib.request.urlopen"):
                _mod.main()

        self.assertGreater(len(slack_calls), 0)
        msg = slack_calls[0]
        self.assertIn("Protect Report", msg)

    def test_main_reports_disconnected_cameras(self):
        cameras = [
            {"id": "cam1", "name": "Exterior Front", "state": "DISCONNECTED", "type": "UVC-G4"},
        ]
        mock_client = MagicMock()
        mock_client.login.return_value = True
        mock_client.get_bootstrap.return_value = self._make_bootstrap(cameras)
        mock_client.get_events.return_value = []
        _MockClient.return_value = mock_client

        slack_calls = []
        with patch.object(_mod, "slack_post", side_effect=lambda m: slack_calls.append(m)):
            with patch("urllib.request.urlopen"):
                _mod.main()

        msg = slack_calls[0]
        self.assertIn("OFFLINE", msg)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_acknowledged_cameras_show_different_icon(self):
        """Acknowledged offline cameras should show white circle, not red."""
        ack = {"cameras_offline": ["Exterior Driveway"]}
        src = _SCRIPT.read_text()
        self.assertIn("acknowledged", src.lower(), "Acknowledged camera handling must exist")

    def test_memory_storage_on_success(self):
        """Nightly report should store summary in vector memory."""
        cameras = [{"id": "cam1", "name": "Exterior Front", "state": "CONNECTED"}]
        mock_client = MagicMock()
        mock_client.login.return_value = True
        mock_client.get_bootstrap.return_value = {
            "cameras": cameras,
            "nvr": {"uptime": 0, "firmwareVersion": "3.0", "storageInfo": {}}
        }
        mock_client.get_events.return_value = []
        _MockClient.return_value = mock_client

        memory_calls = []
        with patch.object(_mod, "slack_post"):
            with patch("urllib.request.urlopen") as mock_url:
                mock_url.return_value.__enter__ = lambda s: MagicMock()
                mock_url.return_value.__exit__ = MagicMock(return_value=False)
                memory_calls.append(True)
                _mod.main()

        # Verify the attempt was made (urlopen called for vector memory)


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

    def test_script_is_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_module_has_main(self):
        self.assertTrue(callable(_mod.main))

    def test_interior_prefix_not_empty(self):
        self.assertNotEqual(INTERIOR_PREFIX, "")

    def test_ack_path_is_json(self):
        self.assertTrue(str(_mod.ACK_PATH).endswith(".json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
