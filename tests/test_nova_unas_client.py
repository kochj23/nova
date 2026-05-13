"""
test_nova_unas_client.py — All 7 test categories for nova_unas_client.py
Written by Jordan Koch.
"""
import importlib.util, json, sys, time, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import urllib.error

sys.modules["nova_config"] = MagicMock()

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_unas_client.py"
_spec = importlib.util.spec_from_file_location("nova_unas_client", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

UNASClient = _mod.UNASClient
UNASError = _mod.UNASError
_load_api_key = _mod._load_api_key
_request = _mod._request


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_", "Jkoogie"]:
            self.assertNotIn(pat, src)
    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)
    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)
    def test_api_key_from_keychain(self):
        src = _SCRIPT.read_text()
        self.assertIn("security", src)
        self.assertIn("find-generic-password", src)
    def test_ssl_cert_verification_disabled_for_self_signed(self):
        """UNAS has a self-signed cert — verify_mode is CERT_NONE."""
        src = _SCRIPT.read_text()
        self.assertIn("CERT_NONE", src)
    def test_unas_host_is_local_ip(self):
        self.assertIn("192.168.", _mod.UNAS_HOST)
    def test_api_key_used_in_header_not_url(self):
        """API key must go in X-API-Key header, not URL query param."""
        src = _SCRIPT.read_text()
        self.assertIn("X-API-Key", src)
        self.assertNotIn("?api_key=", src)


class TestPerformance(unittest.TestCase):
    def test_default_timeout_reasonable(self):
        self.assertGreater(_mod.DEFAULT_TIMEOUT, 0)
        self.assertLessEqual(_mod.DEFAULT_TIMEOUT, 30)
    def test_max_retries_defined(self):
        self.assertGreater(_mod.MAX_RETRIES, 0)
        self.assertLessEqual(_mod.MAX_RETRIES, 5)
    def test_retry_delay_reasonable(self):
        self.assertGreater(_mod.RETRY_DELAY, 0)
        self.assertLessEqual(_mod.RETRY_DELAY, 10)
    def test_request_backoff_is_exponential(self):
        """Retry delay should multiply by attempt number."""
        src = _SCRIPT.read_text()
        self.assertIn("RETRY_DELAY * attempt", src)


class TestRetry(unittest.TestCase):
    def test_request_retries_on_url_error(self):
        call_count = [0]
        def failing_urlopen(req, **kwargs):
            call_count[0] += 1
            raise urllib.error.URLError("connection refused")
        with patch("urllib.request.urlopen", side_effect=failing_urlopen):
            with patch.object(_mod, "_load_api_key", return_value="test_api_key"):
                with patch("time.sleep"):
                    with self.assertRaises(UNASError):
                        _request("/api/system")
        self.assertEqual(call_count[0], _mod.MAX_RETRIES)

    def test_request_no_retry_on_401(self):
        """401 auth error must not retry (key is wrong)."""
        call_count = [0]
        def auth_fail(req, **kwargs):
            call_count[0] += 1
            raise urllib.error.HTTPError(url="/api/system", code=401, msg="Unauthorized", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=auth_fail):
            with patch.object(_mod, "_load_api_key", return_value="bad_key"):
                with self.assertRaises(UNASError) as ctx:
                    _request("/api/system")
        self.assertEqual(call_count[0], 1)
        self.assertIn("401", str(ctx.exception))

    def test_request_no_retry_on_404(self):
        call_count = [0]
        def not_found(req, **kwargs):
            call_count[0] += 1
            raise urllib.error.HTTPError(url="/api/bogus", code=404, msg="Not Found", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=not_found):
            with patch.object(_mod, "_load_api_key", return_value="test_key"):
                with self.assertRaises(UNASError):
                    _request("/api/bogus")
        self.assertEqual(call_count[0], 1)

    def test_load_api_key_returns_none_when_keychain_empty(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="")
            result = _load_api_key()
        self.assertIsNone(result)

    def test_request_raises_unas_error_when_no_key(self):
        with patch.object(_mod, "_load_api_key", return_value=None):
            with self.assertRaises(UNASError) as ctx:
                _request("/api/system")
        self.assertIn("not found in Keychain", str(ctx.exception))


class TestUnit(unittest.TestCase):
    def test_load_api_key_reads_keychain(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="test_api_key_123\n")
            result = _load_api_key()
        self.assertEqual(result, "test_api_key_123")

    def test_request_builds_correct_url(self):
        built_urls = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"hardware": {"shortname": "UNASPRO8"}}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        def capture_urlopen(req, **kwargs):
            built_urls.append(req.full_url)
            return mock_resp
        with patch("urllib.request.urlopen", side_effect=capture_urlopen):
            with patch.object(_mod, "_load_api_key", return_value="test_key"):
                _request("/api/system")
        self.assertTrue(any("/api/system" in url for url in built_urls))

    def test_request_with_query_params(self):
        built_urls = []
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"{}"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        def capture(req, **kwargs):
            built_urls.append(req.full_url)
            return mock_resp
        with patch("urllib.request.urlopen", side_effect=capture):
            with patch.object(_mod, "_load_api_key", return_value="test_key"):
                _request("/api/path", params={"type": "detail"})
        self.assertTrue(any("type=detail" in url for url in built_urls))

    def test_health_snapshot_structure(self):
        client = UNASClient()
        mock_system = {"hardware": {"shortname": "UNASPRO8"}, "name": "UNAS Pro 8",
                       "mac": "aa:bb:cc:dd", "deviceState": "online"}
        mock_storage = {"status": "healthy", "totalQuota": 16e12, "usage": {"sharedDrives": 4e12, "system": 0.5e12}}
        mock_shares = [{"id": "1", "name": "videos", "status": "active", "usage": 2e12,
                        "encryptionStatus": None, "quota": None}]
        with patch.object(client, "system_info", return_value=mock_system):
            with patch.object(client, "storage_summary", return_value=mock_storage):
                with patch.object(client, "shared_drives", return_value=mock_shares):
                    snapshot = client.health_snapshot()
        self.assertIn("device", snapshot)
        self.assertIn("storage", snapshot)
        self.assertIn("shares", snapshot)
        self.assertIn("timestamp", snapshot)

    def test_ping_returns_bool(self):
        client = UNASClient()
        with patch.object(client, "system_info", return_value={"hardware": {"shortname": "UNASPRO8"}}):
            self.assertTrue(client.ping())
        with patch.object(client, "system_info", side_effect=UNASError("failed")):
            self.assertFalse(client.ping())


class TestIntegration(unittest.TestCase):
    def test_health_snapshot_computes_used_pct(self):
        client = UNASClient()
        mock_storage = {"status": "healthy", "totalQuota": 10e12, "usage": {"sharedDrives": 5e12, "system": 0}}
        with patch.object(client, "system_info", return_value={"hardware": {}}):
            with patch.object(client, "storage_summary", return_value=mock_storage):
                with patch.object(client, "shared_drives", return_value=[]):
                    snapshot = client.health_snapshot()
        self.assertEqual(snapshot["storage"]["used_pct"], 50.0)

    def test_health_snapshot_handles_zero_total(self):
        client = UNASClient()
        with patch.object(client, "system_info", return_value={"hardware": {}}):
            with patch.object(client, "storage_summary", return_value={"totalQuota": 0, "usage": {}}):
                with patch.object(client, "shared_drives", return_value=[]):
                    snapshot = client.health_snapshot()
        self.assertEqual(snapshot["storage"]["used_pct"], 0)


class TestFunctional(unittest.TestCase):
    def test_shared_drives_returns_list(self):
        client = UNASClient()
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"data": [
            {"id": "1", "name": "videos", "status": "active", "usage": 1e12}
        ]}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(_mod, "_load_api_key", return_value="test_key"):
                result = client.shared_drives()
        self.assertIsInstance(result, list)


class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.UNAS_HOST, str)
        self.assertIsInstance(_mod.DEFAULT_TIMEOUT, int)
        self.assertIsInstance(_mod.MAX_RETRIES, int)
        self.assertIsInstance(_mod.RETRY_DELAY, int)
    def test_classes_and_errors_defined(self):
        self.assertTrue(hasattr(_mod, "UNASClient"))
        self.assertTrue(hasattr(_mod, "UNASError"))
        client = UNASClient()
        for m in ("system_info", "storage_summary", "storage_basic",
                  "shared_drives", "shared_drive", "health_snapshot", "ping"):
            self.assertTrue(hasattr(client, m), f"Missing: {m}")

if __name__ == "__main__":
    unittest.main(verbosity=2)
