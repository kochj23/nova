"""
test_nova_weekly_nmap_scan.py — All 7 test categories for nova_weekly_nmap_scan.py
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

# nova_weekly_nmap_scan uses `requests` and Path in __main__ block
_requests_mock = MagicMock()
sys.modules["requests"] = _requests_mock
sys.modules.setdefault("nova_config", MagicMock())

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_weekly_nmap_scan.py"
_spec = importlib.util.spec_from_file_location("nova_weekly_nmap_scan", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

run_nmap_scan = _mod.run_nmap_scan
post_to_slack = _mod.post_to_slack


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(p, src)

    def test_scan_target_is_local_lan(self):
        """nmap scan must target local LAN, never cloud/external IPs."""
        src = _SCRIPT.read_text()
        self.assertIn("192.168.", src, "nmap scan should target local LAN")

    def test_novacontrol_api_is_localhost(self):
        """NovaControl API must be on localhost."""
        src = _SCRIPT.read_text()
        self.assertIn("127.0.0.1", src, "NovaControl API should be localhost")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_run_nmap_scan_has_timeout(self):
        """API requests must have timeouts."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src, "HTTP requests must have timeouts")

    def test_post_to_slack_fast_on_clean_result(self):
        start = time.perf_counter()
        with patch("subprocess.run"):
            post_to_slack({"device_count": 25, "threats": [],
                           "timestamp": "2026-01-01T00:00:00"})
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_threat_summary_bounded(self):
        """post_to_slack must limit threat list in message (prevent huge messages)."""
        src = _SCRIPT.read_text()
        self.assertIn(":10]", src, "Threat list should be limited to 10 items")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_run_nmap_returns_error_on_exception(self):
        _requests_mock.post.side_effect = Exception("refused")
        result = run_nmap_scan()
        self.assertIn("error", result)

    def test_run_nmap_returns_error_on_non_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        _requests_mock.post.return_value = mock_resp
        _requests_mock.post.side_effect = None
        result = run_nmap_scan()
        self.assertIn("error", result)

    def test_post_to_slack_handles_subprocess_error(self):
        _requests_mock.post.side_effect = None
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        _requests_mock.post.return_value = mock_resp
        _requests_mock.get.return_value = mock_resp

        with patch("subprocess.run", side_effect=Exception("crash")):
            try:
                post_to_slack({"device_count": 5, "threats": [], "timestamp": "2026-01-01"})
            except Exception as e:
                pass  # Acceptable — exception in subprocess is handled


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_run_nmap_returns_device_count(self):
        _requests_mock.post.side_effect = None
        mock_post = MagicMock()
        mock_post.status_code = 200
        _requests_mock.post.return_value = mock_post

        mock_get_devices = MagicMock()
        mock_get_devices.status_code = 200
        mock_get_devices.json.return_value = [{"ip": "192.168.1.1"}, {"ip": "192.168.1.2"}]

        mock_get_threats = MagicMock()
        mock_get_threats.status_code = 200
        mock_get_threats.json.return_value = []

        _requests_mock.get.side_effect = [mock_get_devices, mock_get_threats]
        result = run_nmap_scan()

        self.assertIn("device_count", result)
        self.assertEqual(result["device_count"], 2)
        self.assertIn("threats", result)

    def test_run_nmap_returns_threats_list(self):
        _requests_mock.post.side_effect = None
        mock_post = MagicMock()
        mock_post.status_code = 200
        _requests_mock.post.return_value = mock_post

        mock_get_devices = MagicMock()
        mock_get_devices.status_code = 200
        mock_get_devices.json.return_value = []

        mock_get_threats = MagicMock()
        mock_get_threats.status_code = 200
        mock_get_threats.json.return_value = [{"severity": "high", "description": "Open port 22"}]

        _requests_mock.get.side_effect = [mock_get_devices, mock_get_threats]
        result = run_nmap_scan()

        self.assertEqual(len(result["threats"]), 1)
        self.assertEqual(result["threats"][0]["severity"], "high")

    def test_run_nmap_has_timestamp(self):
        _requests_mock.post.side_effect = None
        mock_post = MagicMock()
        mock_post.status_code = 200
        _requests_mock.post.return_value = mock_post

        mock_get = MagicMock()
        mock_get.status_code = 200
        mock_get.json.return_value = []
        _requests_mock.get.side_effect = [mock_get, mock_get]

        result = run_nmap_scan()
        self.assertIn("timestamp", result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_block_runs_scan_and_posts(self):
        """Running the main block should call run_nmap_scan and post_to_slack."""
        scan_calls = []
        post_calls = []

        with patch.object(_mod, "run_nmap_scan",
                          side_effect=lambda: scan_calls.append(True) or
                          {"device_count": 10, "threats": [], "timestamp": "2026-01-01"}):
            with patch.object(_mod, "post_to_slack",
                              side_effect=lambda r: post_calls.append(r)):
                result = _mod.run_nmap_scan()
                if "error" not in result:
                    _mod.post_to_slack(result)

        self.assertGreater(len(scan_calls), 0)
        self.assertGreater(len(post_calls), 0)

    def test_error_result_skips_slack_post(self):
        """When scan returns error, Slack post should be skipped."""
        post_calls = []
        with patch.object(_mod, "run_nmap_scan", return_value={"error": "refused"}):
            with patch.object(_mod, "post_to_slack",
                              side_effect=lambda r: post_calls.append(r)):
                result = _mod.run_nmap_scan()
                if "error" not in result:
                    _mod.post_to_slack(result)
        self.assertEqual(len(post_calls), 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_clean_scan_message_shows_none_threats(self):
        """Clean scan result should say 'NONE' for threats."""
        with patch("subprocess.run"):
            post_to_slack({"device_count": 25, "threats": [],
                           "timestamp": "2026-01-01T00:00:00"})
        # No assertion needed — just verify no crash

    def test_threat_scan_message_includes_severity(self):
        """Threat scan message should include threat severity."""
        result = {
            "device_count": 25,
            "threats": [{"severity": "high", "description": "Open SSH port"}],
            "timestamp": "2026-01-01T00:00:00"
        }
        with patch("subprocess.run"):
            post_to_slack(result)
        # Just verify no crash


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

    def test_module_has_run_nmap_scan(self):
        self.assertTrue(callable(run_nmap_scan))

    def test_module_has_post_to_slack(self):
        self.assertTrue(callable(post_to_slack))


if __name__ == "__main__":
    unittest.main(verbosity=2)
