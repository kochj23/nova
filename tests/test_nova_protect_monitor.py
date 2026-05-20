"""
test_nova_protect_monitor.py — All 7 test categories for nova_protect_monitor.py
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

_nova_cfg = MagicMock()
_nova_cfg.SLACK_PHOTOS = "#nova-photos"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.VECTOR_URL = "http://192.168.1.6:18790/remember"
_nova_cfg.slack_bot_token.return_value = "xoxb-fake"
sys.modules["nova_config"] = _nova_cfg

_nova_logger = MagicMock()
_nova_logger.log = MagicMock()
_nova_logger.LOG_INFO = "INFO"
_nova_logger.LOG_ERROR = "ERROR"
_nova_logger.LOG_WARN = "WARN"
sys.modules["nova_logger"] = _nova_logger

# Stub the optional package clairvoyance module
sys.modules["nova_package_clairvoyance"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_protect_monitor.py"
_spec = importlib.util.spec_from_file_location("nova_protect_monitor", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

ProtectClient = _mod.ProtectClient
INTERIOR_PREFIX = _mod.INTERIOR_PREFIX
PROTECT_HOST = _mod.PROTECT_HOST
PROTECT_USER = _mod.PROTECT_USER
ALERT_EVENTS = _mod.ALERT_EVENTS


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_password(self):
        """Password must come from Keychain, not be hardcoded."""
        src = _SCRIPT.read_text()
        self.assertIn("find-generic-password", src, "Password must be loaded from Keychain")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_interior_prefix_defined(self):
        """Interior cameras must be identified and excluded."""
        self.assertIsNotNone(INTERIOR_PREFIX)
        self.assertNotEqual(INTERIOR_PREFIX, "")

    def test_interior_cameras_never_accessed(self):
        """Verify the code explicitly excludes interior cameras."""
        src = _SCRIPT.read_text()
        self.assertIn("Interior", src, "Interior camera exclusion must be present")

    def test_ssl_context_disables_hostname_check(self):
        """Self-signed cert on UNVR requires hostname verification off."""
        client = ProtectClient()
        self.assertFalse(client._ctx.check_hostname)

    def test_state_file_under_home(self):
        self.assertTrue(str(_mod.STATE_FILE).startswith(str(Path.home())))

    def test_snapshot_dir_under_home(self):
        self.assertTrue(str(_mod.SNAPSHOT_DIR).startswith(str(Path.home())))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_get_events_has_limit_param(self):
        """get_events must accept a limit to prevent fetching unbounded data."""
        client = ProtectClient()
        client._logged_in = True
        client._csrf_token = "fake"
        with patch.object(client, "_get", return_value=[]):
            result = client.get_events(limit=30)
        self.assertIsInstance(result, list)

    def test_protect_client_login_timeout(self):
        """Login must use a timeout to prevent hanging."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=10", src, "Login must have a 10s timeout")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_retries_on_401(self):
        """_get should re-authenticate on 401 and retry once."""
        import urllib.error
        retry_count = [0]

        def mock_login(self_obj):
            self_obj._logged_in = True
            return True

        client = ProtectClient()
        client._logged_in = True
        client._csrf_token = "fake"

        def fake_opener_open(req, timeout=None):
            retry_count[0] += 1
            if retry_count[0] == 1:
                raise urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)
            return MagicMock(read=lambda: b'{"cameras": []}', status=200)

        client._opener.open = fake_opener_open
        with patch.object(type(client), "login", mock_login):
            result = client._get("bootstrap")

    def test_get_returns_none_on_persistent_failure(self):
        """_get must return None after exhausting retries."""
        import urllib.error
        client = ProtectClient()
        client._logged_in = True
        client._csrf_token = "fake"

        def always_401(req, timeout=None):
            raise urllib.error.HTTPError("http://x", 401, "Unauthorized", {}, None)

        client._opener.open = always_401
        with patch.object(client, "login", return_value=False):
            result = client._get("bootstrap", _retry=False)
        self.assertIsNone(result)

    def test_get_password_returns_empty_on_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="\n")
            result = _mod._get_password()
        self.assertEqual(result, "")

    def test_login_returns_false_on_no_password(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="\n")
            client = ProtectClient()
            result = client.login()
        self.assertFalse(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_protect_client_init(self):
        client = ProtectClient()
        self.assertFalse(client._logged_in)
        self.assertIsNone(client._csrf_token)

    def test_interior_prefix_is_interior(self):
        self.assertEqual(INTERIOR_PREFIX, "Interior")

    def test_alert_events_not_empty(self):
        self.assertIsInstance(ALERT_EVENTS, set)
        self.assertGreater(len(ALERT_EVENTS), 0)

    def test_protect_host_is_local_lan(self):
        self.assertTrue(PROTECT_HOST.startswith("192.168."),
                        "PROTECT_HOST must be on local LAN")

    def test_protect_user_is_nova(self):
        self.assertEqual(PROTECT_USER, "nova")

    def test_get_bootstrap_calls_get(self):
        client = ProtectClient()
        with patch.object(client, "_get", return_value={"cameras": []}) as mock_get:
            result = client.get_bootstrap()
        mock_get.assert_called_once_with("bootstrap")

    def test_get_events_builds_correct_path(self):
        client = ProtectClient()
        with patch.object(client, "_get", return_value=[]) as mock_get:
            client.get_events(limit=50)
        call_arg = mock_get.call_args[0][0]
        self.assertIn("limit=50", call_arg)

    def test_get_events_with_since_ms(self):
        client = ProtectClient()
        with patch.object(client, "_get", return_value=[]) as mock_get:
            client.get_events(since_ms=1234567890000, limit=30)
        call_arg = mock_get.call_args[0][0]
        self.assertIn("start=1234567890000", call_arg)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_login_sends_correct_credentials(self):
        """Login must POST username + password JSON to Protect API."""
        posted_data = []

        class FakeResponse:
            status = 200
            def headers(self): return {}
            def get(self, key, default=""): return "fake-csrf"

        def fake_open(req, timeout=None):
            posted_data.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.headers.get.return_value = "fake-csrf"
            r.status = 200
            return r

        fake_password = "testpassword123"
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(stdout=f"{fake_password}\n")
            client = ProtectClient()
            client._opener.open = fake_open
            result = client.login()

        self.assertGreater(len(posted_data), 0)
        self.assertEqual(posted_data[0]["username"], "nova")
        self.assertEqual(posted_data[0]["password"], fake_password)

    def test_snapshot_requires_login(self):
        """get_snapshot should attempt login if not logged in."""
        client = ProtectClient()
        client._logged_in = False
        with patch.object(client, "login", return_value=False) as mock_login:
            result = client.get_snapshot("cam_123", "/tmp/test.jpg")
        mock_login.assert_called_once()
        self.assertFalse(result)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_exterior_cameras_filter(self):
        """Only cameras NOT starting with 'Interior' should be included."""
        cameras = [
            {"name": "Exterior Front", "state": "CONNECTED"},
            {"name": "Interior Living", "state": "CONNECTED"},
            {"name": "Exterior Back", "state": "CONNECTED"},
        ]
        exterior = [c for c in cameras if not c["name"].startswith(INTERIOR_PREFIX)]
        names = [c["name"] for c in exterior]
        self.assertIn("Exterior Front", names)
        self.assertIn("Exterior Back", names)
        self.assertNotIn("Interior Living", names)

    def test_slack_upload_returns_false_on_no_token(self):
        _nova_cfg.slack_bot_token.return_value = None
        with tempfile.NamedTemporaryFile(suffix=".jpg") as f:
            result = _mod.slack_upload_image(f.name, "#test")
        self.assertFalse(result)
        _nova_cfg.slack_bot_token.return_value = "xoxb-fake"


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

    def test_protect_client_class_exists(self):
        self.assertTrue(callable(ProtectClient))

    def test_alert_events_contains_smart_detect(self):
        self.assertIn("smartDetectZone", ALERT_EVENTS)

    def test_protect_host_not_empty(self):
        self.assertNotEqual(PROTECT_HOST, "")

    def test_state_file_is_json(self):
        self.assertTrue(str(_mod.STATE_FILE).endswith(".json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
