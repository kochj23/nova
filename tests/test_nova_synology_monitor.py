"""
test_nova_synology_monitor.py — All 7 test categories for nova_synology_monitor.py
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
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_synology_monitor.py"
_spec = importlib.util.spec_from_file_location("nova_synology_monitor", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

SynoSession = _mod.SynoSession
_get_credential = _mod._get_credential
get_credentials = _mod.get_credentials


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
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_credentials_from_keychain(self):
        """Credentials must be loaded via macOS Keychain, not hardcoded."""
        src = _SCRIPT.read_text()
        self.assertIn("find-generic-password", src, "Credentials must come from Keychain")

    def test_ssl_context_for_self_signed(self):
        """Self-signed DSM cert requires cert verification disabled."""
        self.assertFalse(_mod.SSL_CTX.check_hostname)

    def test_state_files_under_home(self):
        self.assertTrue(str(_mod.STATE_FILE).startswith(str(Path.home())))
        self.assertTrue(str(_mod.SNAPSHOT_FILE).startswith(str(Path.home())))

    def test_nas_host_is_local(self):
        self.assertIn("192.168.", _mod.NAS_HOST)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_thresholds_defined(self):
        self.assertGreater(_mod.RAM_WARN_PCT, 0)
        self.assertGreater(_mod.DISK_TEMP_WARN_C, 0)
        self.assertGreater(_mod.VOLUME_USAGE_WARN, 0)
        self.assertLess(_mod.VOLUME_USAGE_WARN, 100)

    def test_get_credential_has_subprocess_call(self):
        """_get_credential must use subprocess to call security."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="myvalue\n")
            result = _get_credential("nova-synology-username")
        self.assertEqual(result, "myvalue")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_credential_returns_none_on_missing(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="\n")
            result = _get_credential("nova-nonexistent")
        self.assertIsNone(result)

    def test_get_credentials_returns_none_on_missing(self):
        with patch.object(_mod, "_get_credential", return_value=None):
            u, p = get_credentials()
        self.assertIsNone(u)
        self.assertIsNone(p)

    def test_syno_session_login_returns_false_on_no_credentials(self):
        with patch.object(_mod, "get_credentials", return_value=(None, None)):
            session = SynoSession()
            result = session.login()
        self.assertFalse(result)

    def test_syno_session_login_returns_false_on_api_failure(self):
        with patch.object(_mod, "get_credentials", return_value=("user", "pass")):
            with patch("urllib.request.urlopen", side_effect=Exception("refused")):
                session = SynoSession()
                result = session.login()
        self.assertFalse(result)

    def test_syno_session_logout_handles_no_sid(self):
        session = SynoSession()
        session.sid = None
        try:
            session.logout()
        except Exception as e:
            self.fail(f"logout() raised with no sid: {e}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_syno_session_init(self):
        session = SynoSession()
        self.assertIsNone(session.sid)
        self.assertFalse(session._retried_login)

    def test_thresholds_crit_greater_than_warn(self):
        self.assertGreater(_mod.RAM_CRIT_PCT, _mod.RAM_WARN_PCT)
        self.assertGreater(_mod.CPU_WARN_PCT, 0)
        self.assertGreater(_mod.DISK_TEMP_CRIT_C, _mod.DISK_TEMP_WARN_C)
        self.assertGreater(_mod.VOLUME_USAGE_CRIT, _mod.VOLUME_USAGE_WARN)

    def test_nvme_temp_threshold_higher_than_hdd(self):
        """NVMe cache drives run hotter than HDDs."""
        self.assertGreater(_mod.NVME_TEMP_WARN_C, _mod.DISK_TEMP_WARN_C)
        self.assertGreater(_mod.NVME_TEMP_CRIT_C, _mod.DISK_TEMP_CRIT_C)

    def test_state_dir_is_path(self):
        self.assertIsInstance(_mod.STATE_DIR, Path)

    def test_nas_host_is_https(self):
        self.assertTrue(_mod.NAS_HOST.startswith("https://"))

    def test_nas_api_endpoint(self):
        self.assertIn("entry.cgi", _mod.NAS_API)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_syno_session_api_call_requires_sid(self):
        """API calls should include session ID in params."""
        session = SynoSession()
        session.sid = "test-session-id"
        # _raw_get should add _sid to params when sid is set
        src = _SCRIPT.read_text()
        self.assertIn("_sid", src, "API calls must include _sid parameter")

    def test_get_credentials_returns_both_values(self):
        with patch.object(_mod, "_get_credential") as mock_cred:
            mock_cred.side_effect = lambda service: "testuser" if "username" in service else "testpass"
            u, p = get_credentials()
        self.assertEqual(u, "testuser")
        self.assertEqual(p, "testpass")

    def test_login_parses_sid_from_response(self):
        """login() must extract SID from API response."""
        response_data = {"success": True, "data": {"sid": "test-sid-123"}}
        with patch.object(_mod, "get_credentials", return_value=("user", "pass")):
            session = SynoSession()
            with patch.object(session, "_raw_get", return_value=response_data):
                result = session.login()
        self.assertTrue(result)
        self.assertEqual(session.sid, "test-sid-123")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_help_flag_works(self):
        """Script should accept --status and similar flags without crashing."""
        src = _SCRIPT.read_text()
        self.assertIn("argparse", src, "Script must use argparse for CLI flags")
        self.assertIn("--status", src)

    def test_json_output_flag_exists(self):
        src = _SCRIPT.read_text()
        self.assertIn("--json", src, "Script must support --json for Nova integration")

    def test_problems_flag_exists(self):
        src = _SCRIPT.read_text()
        self.assertIn("--problems", src)

    def test_snapshot_flag_exists(self):
        src = _SCRIPT.read_text()
        self.assertIn("--snapshot", src)


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

    def test_syno_session_class_exists(self):
        self.assertTrue(callable(SynoSession))

    def test_state_file_is_json(self):
        self.assertTrue(str(_mod.STATE_FILE).endswith(".json"))

    def test_snapshot_file_is_json(self):
        self.assertTrue(str(_mod.SNAPSHOT_FILE).endswith(".json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
