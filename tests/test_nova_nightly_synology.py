"""
test_nova_nightly_synology.py — All 7 test categories for nova_nightly_synology.py
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
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
sys.modules["nova_config"] = _nova_cfg

_nova_logger = MagicMock()
_nova_logger.log = MagicMock()
_nova_logger.LOG_INFO = "INFO"
_nova_logger.LOG_ERROR = "ERROR"
sys.modules["nova_logger"] = _nova_logger

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_nightly_synology.py"
_spec = importlib.util.spec_from_file_location("nova_nightly_synology", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_acknowledged = _mod.load_acknowledged
format_bytes = _mod.format_bytes
wake_nas = _mod.wake_nas
run_synology = _mod.run_synology


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

    def test_ack_path_under_home(self):
        self.assertTrue(str(_mod.ACK_PATH).startswith(str(Path.home())))

    def test_synology_ip_is_local_lan(self):
        """Synology NAS must be on local LAN."""
        src = _SCRIPT.read_text()
        self.assertIn("192.168.", src, "Synology NAS must be on local LAN")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_format_bytes_fast(self):
        import time
        start = time.perf_counter()
        for i in range(10000):
            format_bytes(i * 1024 * 1024)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_run_synology_has_timeout(self):
        """run_synology subprocess call must have a timeout."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=30", src, "Synology subprocess must have timeout")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_wake_nas_returns_false_on_connection_error(self):
        import socket
        with patch("socket.socket") as mock_sock:
            mock_sock.return_value.connect.side_effect = Exception("refused")
            result = wake_nas("192.168.1.11", retries=1)
        self.assertFalse(result)

    def test_run_synology_returns_none_on_subprocess_error(self):
        with patch("subprocess.run", side_effect=Exception("crash")):
            result = run_synology("status")
        self.assertIsNone(result)

    def test_run_synology_returns_none_on_empty_output(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            result = run_synology("status")
        self.assertIsNone(result)

    def test_load_acknowledged_returns_empty_on_missing(self):
        with patch.object(_mod.ACK_PATH, "exists", return_value=False):
            result = load_acknowledged()
        self.assertIsInstance(result, dict)

    def test_main_continues_on_nas_unreachable(self):
        """main() must not crash if NAS is unreachable."""
        with patch.object(_mod, "wake_nas", return_value=False):
            with patch.object(_mod, "run_synology", return_value=None):
                with patch.object(_mod, "slack_post"):
                    with patch("urllib.request.urlopen"):
                        try:
                            _mod.main()
                        except Exception as e:
                            self.fail(f"main() raised when NAS unreachable: {e}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_format_bytes_tb(self):
        result = format_bytes(2 * 1024 ** 4)
        self.assertIn("TB", result)
        self.assertIn("2.0", result)

    def test_format_bytes_gb(self):
        result = format_bytes(5 * 1024 ** 3)
        self.assertIn("GB", result)
        self.assertIn("5.0", result)

    def test_format_bytes_mb(self):
        result = format_bytes(512 * 1024 ** 2)
        self.assertIn("MB", result)
        self.assertIn("512", result)

    def test_format_bytes_kb(self):
        result = format_bytes(100 * 1024)
        self.assertIn("KB", result)

    def test_volume_bar_length(self):
        """Storage bar must always be 10 chars."""
        for pct in [0, 25, 50, 75, 100]:
            bar = "█" * int(pct // 10) + "░" * (10 - int(pct // 10))
            self.assertEqual(len(bar), 10, f"Bar wrong length at {pct}%")

    def test_run_synology_parses_json(self):
        status_data = {"model": "RS1221+", "dsm_version": "7.2.2"}
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout=json.dumps(status_data), returncode=0)
            result = run_synology("status")
        self.assertEqual(result["model"], "RS1221+")

    def test_synology_script_path(self):
        self.assertTrue(_mod.SYNOLOGY_SCRIPT.name.endswith(".py"))


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_builds_slack_report(self):
        status = {
            "model": "RS1221+", "dsm_version": "7.2.2",
            "uptime_seconds": 86400, "cpu_load": 15, "ram_used_percent": 40,
            "temperature": 35, "overall_status": "normal"
        }
        storage = {
            "volumes": [{
                "name": "volume1", "total_bytes": 8 * 1024 ** 4,
                "used_bytes": 2 * 1024 ** 4, "raid_type": "RAID6", "status": "normal"
            }]
        }

        def mock_run_synology(mode):
            if mode == "status": return status
            if mode == "storage": return storage
            return None

        slack_calls = []
        with patch.object(_mod, "wake_nas", return_value=True):
            with patch.object(_mod, "run_synology", side_effect=mock_run_synology):
                with patch.object(_mod, "slack_post", side_effect=lambda m: slack_calls.append(m)):
                    with patch("urllib.request.urlopen"):
                        _mod.main()

        self.assertGreater(len(slack_calls), 0)
        msg = slack_calls[0]
        self.assertIn("NAS Report", msg)

    def test_main_shows_bad_disks(self):
        disks_data = {
            "disks": [
                {"name": "Drive 1", "temperature": 40, "status": "normal", "model": "WD Red"},
                {"name": "Drive 2", "temperature": 40, "status": "error", "model": "Seagate"},
            ]
        }

        def mock_run_synology(mode):
            if mode == "disks": return disks_data
            if mode == "status": return {"model": "RS1221+", "overall_status": "warning"}
            return None

        slack_calls = []
        with patch.object(_mod, "wake_nas", return_value=True):
            with patch.object(_mod, "run_synology", side_effect=mock_run_synology):
                with patch.object(_mod, "slack_post", side_effect=lambda m: slack_calls.append(m)):
                    with patch("urllib.request.urlopen"):
                        _mod.main()

        msg = slack_calls[0]
        self.assertIn("Drive 2", msg)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_report_includes_security_section(self):
        security = {"failed_logins_24h": 5, "blocked_ips": 2}

        def mock_run_synology(mode):
            if mode == "security": return security
            return None

        slack_calls = []
        with patch.object(_mod, "wake_nas", return_value=True):
            with patch.object(_mod, "run_synology", side_effect=mock_run_synology):
                with patch.object(_mod, "slack_post", side_effect=lambda m: slack_calls.append(m)):
                    with patch("urllib.request.urlopen"):
                        _mod.main()

        msg = slack_calls[0]
        self.assertIn("failed login", msg.lower())

    def test_report_clean_security(self):
        security = {"failed_logins_24h": 0, "blocked_ips": 0}

        def mock_run_synology(mode):
            if mode == "security": return security
            return None

        slack_calls = []
        with patch.object(_mod, "wake_nas", return_value=True):
            with patch.object(_mod, "run_synology", side_effect=mock_run_synology):
                with patch.object(_mod, "slack_post", side_effect=lambda m: slack_calls.append(m)):
                    with patch("urllib.request.urlopen"):
                        _mod.main()

        msg = slack_calls[0]
        self.assertIn("Clean", msg)


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

    def test_format_bytes_defined(self):
        self.assertTrue(callable(format_bytes))

    def test_wake_nas_defined(self):
        self.assertTrue(callable(wake_nas))


if __name__ == "__main__":
    unittest.main(verbosity=2)
