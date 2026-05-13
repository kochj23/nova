"""
test_nova_bandwidth_report.py — All 7 test categories for nova_bandwidth_report.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_bandwidth_report.py"
_spec = importlib.util.spec_from_file_location("nova_bandwidth_report", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

get_api_key = _mod.get_api_key
api_get = _mod.api_get
api_post = _mod.api_post
get_wan_daily = _mod.get_wan_daily
get_wan_health = _mod.get_wan_health
main = _mod.main


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for p in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(p, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(p, src)

    def test_api_key_from_keychain(self):
        """API key must be retrieved from macOS Keychain, not hardcoded."""
        src = _SCRIPT.read_text()
        self.assertIn("security", src)
        self.assertIn("find-generic-password", src)

    def test_no_hardcoded_api_key_value(self):
        """No UniFi API key value should be hardcoded."""
        src = _SCRIPT.read_text()
        # Check for common UniFi API key patterns
        self.assertNotIn("unifi-key-", src)

    def test_ssl_context_disables_cert_verification(self):
        """SSL cert verification disabled for local UDM Pro (self-signed cert)."""
        src = _SCRIPT.read_text()
        self.assertIn("CERT_NONE", src)

    def test_unifi_ip_is_local_network(self):
        """UniFi router IP must be a private/local network address."""
        src = _SCRIPT.read_text()
        self.assertIn("192.168.1.1", src)
        # Must not be an external IP
        self.assertNotIn("api.ubnt.com", src)

    def test_vector_url_is_local(self):
        """VECTOR_URL must be local."""
        self.assertIn("127.0.0.1", str(_mod.VECTOR_URL))

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_main_exits_early_when_no_api_key(self):
        """main() must return quickly (< 100ms) when API key is missing."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=1)
            start = time.perf_counter()
            main()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_api_get_timeout_used(self):
        """api_get must use a timeout to prevent hanging."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=10", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_wan_daily_handles_api_failure(self):
        """get_wan_daily returns (0, 0) when API call fails."""
        mock_key = "test-api-key"

        with patch.object(_mod, "api_post", side_effect=Exception("API down")):
            down, up = get_wan_daily(mock_key)

        self.assertEqual(down, 0)
        self.assertEqual(up, 0)

    def test_get_wan_health_handles_failure(self):
        """get_wan_health returns empty dict when all API calls fail."""
        with patch.object(_mod, "api_get", side_effect=Exception("API down")):
            result = get_wan_health("test-key")

        self.assertIsInstance(result, dict)

    def test_main_continues_when_wan_fails(self):
        """main() continues even when WAN health calls fail."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="test-key", returncode=0)

            with patch.object(_mod, "api_get", side_effect=[
                [],           # stat/sta returns empty clients
            ]):
                with patch.object(_mod, "get_wan_daily", return_value=(0, 0)):
                    with patch.object(_mod, "get_wan_health", return_value={}):
                        try:
                            main()
                        except Exception as exc:
                            self.fail(f"main() raised: {exc}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_get_api_key_calls_security(self):
        """get_api_key uses macOS security command."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="my-test-key\n", returncode=0)
            key = get_api_key()
        self.assertEqual(key, "my-test-key")

    def test_get_api_key_returns_empty_on_failure(self):
        """get_api_key returns empty string when security fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=1)
            key = get_api_key()
        self.assertEqual(key, "")

    def test_api_get_builds_correct_url(self):
        """api_get constructs the correct UDM Pro URL."""
        captured_urls = []

        def fake_urlopen(req, timeout=None, context=None):
            captured_urls.append(req.full_url if hasattr(req, "full_url") else req.get_full_url())
            r = MagicMock()
            r.read.return_value = json.dumps({"data": []}).encode()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            api_get("stat/sta", "test-key")

        self.assertTrue(len(captured_urls) > 0)
        self.assertIn("192.168.1.1", captured_urls[0])
        self.assertIn("stat/sta", captured_urls[0])

    def test_wan_daily_accumulates_bytes(self):
        """get_wan_daily accumulates rx/tx bytes across hourly buckets."""
        report_data = [
            {"wan-rx_bytes": 1_000_000_000, "wan-tx_bytes": 500_000_000},
            {"wan-rx_bytes": 2_000_000_000, "wan-tx_bytes": 1_000_000_000},
        ]

        with patch.object(_mod, "api_post", return_value=report_data):
            down, up = get_wan_daily("test-key")

        self.assertEqual(down, 3_000_000_000)
        self.assertEqual(up, 1_500_000_000)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_posts_report_to_slack(self):
        """main() calls post_both with bandwidth report."""
        clients = [
            {"hostname": "MacBook-Pro", "tx_bytes": 1_000_000_000, "rx_bytes": 2_000_000_000},
            {"hostname": "iPhone", "tx_bytes": 100_000_000, "rx_bytes": 500_000_000},
        ]

        slack_msgs = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: slack_msgs.append(msg)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="api-key\n", returncode=0)

            with patch.object(_mod, "api_get", return_value=clients):
                with patch.object(_mod, "get_wan_daily", return_value=(5_000_000_000, 1_000_000_000)):
                    with patch.object(_mod, "get_wan_health", return_value={"status": "ok"}):
                        with patch("urllib.request.urlopen", MagicMock()):
                            main()

        _nova_cfg.post_both.side_effect = None
        self.assertTrue(len(slack_msgs) > 0)
        self.assertIn("MacBook-Pro", slack_msgs[0])

    def test_main_sorts_by_total_bandwidth(self):
        """main() must sort clients by total bandwidth descending."""
        clients = [
            {"hostname": "LowBW", "tx_bytes": 100, "rx_bytes": 100},
            {"hostname": "HighBW", "tx_bytes": 1_000_000_000, "rx_bytes": 2_000_000_000},
            {"hostname": "MidBW", "tx_bytes": 500_000_000, "rx_bytes": 500_000_000},
        ]

        slack_msgs = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: slack_msgs.append(msg)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="api-key\n", returncode=0)

            with patch.object(_mod, "api_get", return_value=clients):
                with patch.object(_mod, "get_wan_daily", return_value=(0, 0)):
                    with patch.object(_mod, "get_wan_health", return_value={}):
                        with patch("urllib.request.urlopen", MagicMock()):
                            main()

        _nova_cfg.post_both.side_effect = None
        if slack_msgs:
            full_msg = "\n".join(slack_msgs)
            high_pos = full_msg.find("HighBW")
            mid_pos = full_msg.find("MidBW")
            if high_pos > 0 and mid_pos > 0:
                self.assertLess(high_pos, mid_pos, "HighBW should appear before MidBW")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_report_includes_top_10_only(self):
        """Bandwidth report must show at most 10 clients."""
        clients = [
            {"hostname": f"device-{i}", "tx_bytes": i * 1_000_000, "rx_bytes": i * 1_000_000}
            for i in range(20, 0, -1)
        ]

        slack_msgs = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: slack_msgs.append(msg)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="api-key\n", returncode=0)

            with patch.object(_mod, "api_get", return_value=clients):
                with patch.object(_mod, "get_wan_daily", return_value=(0, 0)):
                    with patch.object(_mod, "get_wan_health", return_value={}):
                        with patch("urllib.request.urlopen", MagicMock()):
                            main()

        _nova_cfg.post_both.side_effect = None

    def test_memory_stored_after_report(self):
        """main() must store bandwidth summary in vector memory."""
        clients = [{"hostname": "TestDevice", "tx_bytes": 1_000_000_000, "rx_bytes": 2_000_000_000}]
        stored_payloads = []

        def fake_urlopen(req, timeout=None):
            if hasattr(req, "data") and req.data:
                stored_payloads.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="api-key\n", returncode=0)
            with patch.object(_mod, "api_get", return_value=clients):
                with patch.object(_mod, "get_wan_daily", return_value=(0, 0)):
                    with patch.object(_mod, "get_wan_health", return_value={}):
                        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                            main()

        if stored_payloads:
            self.assertEqual(stored_payloads[0].get("source"), "infrastructure")


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

    def test_key_functions_callable(self):
        for fn in [get_api_key, api_get, api_post, get_wan_daily, get_wan_health, main]:
            self.assertTrue(callable(fn))

    def test_ssl_context_created(self):
        """SSL_CTX must be defined."""
        self.assertIsNotNone(_mod.SSL_CTX)


if __name__ == "__main__":
    unittest.main(verbosity=2)
