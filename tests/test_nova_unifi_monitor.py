"""
test_nova_unifi_monitor.py — All 7 test categories for nova_unifi_monitor.py
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
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_unifi_monitor.py"
_spec = importlib.util.spec_from_file_location("nova_unifi_monitor", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

find_problems = _mod.find_problems
format_status = _mod.format_status
format_health_report = _mod.format_health_report
find_bandwidth_hogs = _mod.find_bandwidth_hogs
_dpi_category_name = _mod._dpi_category_name
_load_json = _mod._load_json
_save_json = _mod._save_json
BANDWIDTH_HOG_THRESHOLD = _mod.BANDWIDTH_HOG_THRESHOLD


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA"]:
            self.assertNotIn(pat, src)

    def test_api_key_from_keychain(self):
        """UniFi API key must come from Keychain."""
        src = _SCRIPT.read_text()
        self.assertIn("find-generic-password", src, "API key must be loaded from Keychain")
        self.assertIn("nova-unifi-api-key", src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_udm_ip_is_local_lan(self):
        self.assertIn("192.168.", _mod.UDM_HOST)

    def test_ssl_context_for_self_signed(self):
        self.assertFalse(_mod.SSL_CTX.check_hostname)

    def test_state_files_under_home(self):
        for f in [_mod.STATE_FILE, _mod.KNOWN_DEVICES_FILE,
                  _mod.WAN_HISTORY_FILE, _mod.PRESENCE_FILE]:
            self.assertTrue(str(f).startswith(str(Path.home())),
                            f"{f} not under home directory")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_find_problems_fast(self):
        health = {"wan": {"status": "ok", "latency": 10}, "wlan": {"status": "ok"}}
        devices = [{"name": f"Dev{i}", "state": 1, "type": "usw",
                    "system-stats": {"cpu": "5", "mem": "30"},
                    "uplink": {"drops": 0}} for i in range(20)]
        clients = [{"signal": -60} for _ in range(100)]

        start = time.perf_counter()
        find_problems(health, devices, clients)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2)

    def test_find_bandwidth_hogs_fast(self):
        clients = [{"tx_bytes": i * 1024 * 1024, "rx_bytes": i * 1024 * 1024,
                    "hostname": f"device{i}"} for i in range(100)]
        start = time.perf_counter()
        find_bandwidth_hogs(clients)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_wan_history_bounded(self):
        """WAN history should be capped at 2000 entries."""
        src = _SCRIPT.read_text()
        self.assertIn("2000", src, "WAN history must be bounded to 2000 entries")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_api_get_returns_none_on_error(self):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(stdout="apikey123\n")
            with patch("urllib.request.urlopen", side_effect=Exception("refused")):
                result = _mod.api_get("stat/health")
        self.assertIsNone(result)

    def test_api_get_returns_none_on_missing_key(self):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(stdout="\n")  # empty key
            result = _mod.api_get("stat/health")
        self.assertIsNone(result)

    def test_vector_remember_silently_fails(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            try:
                _mod.vector_remember("test", {})
            except Exception as e:
                self.fail(f"vector_remember raised: {e}")

    def test_load_json_returns_empty_on_missing(self):
        result = _load_json(Path("/nonexistent/path.json"))
        self.assertIsInstance(result, (dict, list))

    def test_load_json_returns_empty_on_corrupt(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("{INVALID")
            tmp_path = Path(f.name)
        try:
            result = _load_json(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)
        self.assertIsInstance(result, (dict, list))


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_find_problems_empty_inputs(self):
        problems = find_problems({}, [], [])
        self.assertIsInstance(problems, list)
        self.assertEqual(len(problems), 0)

    def test_find_problems_detects_bad_health(self):
        health = {"wan": {"status": "error", "latency": 10}}
        problems = find_problems(health, [], [])
        self.assertGreater(len(problems), 0)
        self.assertEqual(problems[0]["severity"], "high")

    def test_find_problems_detects_high_latency(self):
        health = {"wan": {"status": "ok", "latency": 100}}  # >50ms threshold
        problems = find_problems(health, [], [])
        latency_probs = [p for p in problems if "latency" in p["message"].lower()]
        self.assertGreater(len(latency_probs), 0)

    def test_find_problems_detects_disconnected_device(self):
        devices = [{"name": "AP-Office", "state": 0, "type": "uap",
                    "system-stats": {}, "uplink": {}, "radio_table_stats": []}]
        problems = find_problems({}, devices, [])
        self.assertGreater(len(problems), 0)

    def test_find_problems_detects_poor_signal(self):
        clients = [{"signal": -85} for _ in range(5)]  # 5 clients with poor signal
        problems = find_problems({}, [], clients)
        signal_probs = [p for p in problems if "signal" in p["message"].lower()]
        self.assertGreater(len(signal_probs), 0)

    def test_find_bandwidth_hogs_detects_heavy_user(self):
        clients = [{
            "tx_bytes": 2 * 1024 * 1024 * 1024,  # 2GB
            "rx_bytes": 0,
            "hostname": "streaming-device"
        }]
        hogs = find_bandwidth_hogs(clients)
        self.assertGreater(len(hogs), 0)
        self.assertIn("streaming-device", hogs[0]["message"])

    def test_find_bandwidth_hogs_ignores_light_users(self):
        clients = [{"tx_bytes": 1024 * 1024, "rx_bytes": 1024 * 1024, "hostname": "phone"}]
        hogs = find_bandwidth_hogs(clients)
        self.assertEqual(len(hogs), 0)

    def test_format_status_returns_string(self):
        health = {"wan": {"status": "ok", "latency": 5}, "wlan": {"status": "ok"}}
        result = format_status(health)
        self.assertIsInstance(result, str)
        self.assertIn("ok", result)

    def test_format_status_handles_none(self):
        result = format_status(None)
        self.assertIsInstance(result, str)

    def test_dpi_category_name_known(self):
        self.assertEqual(_dpi_category_name(4), "Streaming Media")
        self.assertEqual(_dpi_category_name(8), "Games")
        self.assertEqual(_dpi_category_name(11), "Social Media")

    def test_dpi_category_name_unknown(self):
        result = _dpi_category_name(999)
        self.assertIn("999", result)

    def test_save_load_json_roundtrip(self):
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            tmp_path = Path(f.name)
        try:
            data = {"key": "value", "count": 42}
            _save_json(tmp_path, data)
            loaded = _load_json(tmp_path)
            self.assertEqual(loaded["key"], "value")
            self.assertEqual(loaded["count"], 42)
        finally:
            tmp_path.unlink(missing_ok=True)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_full_check_posts_on_problems(self):
        health = {"wan": {"status": "error", "latency": 200}, "wlan": {"status": "ok"}}
        slack_calls = []

        with patch.object(_mod, "get_health", return_value=health):
            with patch.object(_mod, "get_devices", return_value=[]):
                with patch.object(_mod, "get_clients", return_value=[]):
                    with patch.object(_mod, "slack_post",
                                      side_effect=lambda m, **kw: slack_calls.append(m)):
                        with patch.object(_mod, "vector_remember"):
                            _mod.full_check()

        self.assertGreater(len(slack_calls), 0)

    def test_full_check_no_slack_on_clean(self):
        health = {"wan": {"status": "ok", "latency": 5}, "wlan": {"status": "ok"},
                  "lan": {"status": "ok"}}
        slack_calls = []

        with patch.object(_mod, "get_health", return_value=health):
            with patch.object(_mod, "get_devices", return_value=[]):
                with patch.object(_mod, "get_clients", return_value=[]):
                    with patch.object(_mod, "slack_post",
                                      side_effect=lambda m, **kw: slack_calls.append(m)):
                        with patch.object(_mod, "vector_remember"):
                            _mod.full_check()

        self.assertEqual(len(slack_calls), 0)

    def test_full_check_stores_in_memory(self):
        health = {"wan": {"status": "ok", "latency": 5}, "wlan": {"status": "ok"}}
        memory_calls = []

        with patch.object(_mod, "get_health", return_value=health):
            with patch.object(_mod, "get_devices", return_value=[]):
                with patch.object(_mod, "get_clients", return_value=[]):
                    with patch.object(_mod, "slack_post"):
                        with patch.object(_mod, "vector_remember",
                                          side_effect=lambda t, m=None: memory_calls.append(t)):
                            _mod.full_check()

        self.assertGreater(len(memory_calls), 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_rogue_learn_saves_known_devices(self):
        """rogue_learn() should save client MACs to known devices file."""
        clients = [{"mac": "aa:bb:cc:dd:ee:ff", "hostname": "test-device", "ip": "192.168.1.100"}]
        with patch.object(_mod, "get_clients", return_value=clients):
            with patch.object(_mod, "_load_json", return_value={}):
                with patch.object(_mod, "_save_json") as mock_save:
                    _mod.rogue_learn()
        mock_save.assert_called_once()
        saved_data = mock_save.call_args[0][1]
        self.assertIn("aa:bb:cc:dd:ee:ff", saved_data)

    def test_rogue_check_detects_unknown_device(self):
        """rogue_check() should alert when an unknown device is found."""
        clients = [{"mac": "aa:bb:cc:dd:ee:ff", "hostname": "unknown-device", "ip": "192.168.1.200"}]
        known = {"11:22:33:44:55:66": {"name": "trusted-device"}}  # different MAC

        slack_calls = []
        with patch.object(_mod, "get_clients", return_value=clients):
            with patch.object(_mod, "get_devices", return_value=[]):
                with patch.object(_mod, "_load_json", return_value=known):
                    with patch.object(_mod, "slack_post",
                                      side_effect=lambda m, **kw: slack_calls.append(m)):
                        with patch.object(_mod, "vector_remember"):
                            _mod.rogue_check()

        self.assertGreater(len(slack_calls), 0, "Unknown device should trigger alert")

    def test_wan_log_saves_history(self):
        """wan_log() should append entry to WAN history."""
        health = {"wan": {"status": "ok", "latency": 5, "xput_down": 100, "xput_up": 20}}
        with patch.object(_mod, "get_health", return_value=health):
            with patch.object(_mod, "_load_json", return_value={"entries": [], "last_status": None}):
                with patch.object(_mod, "_save_json") as mock_save:
                    with patch.object(_mod, "slack_post"):
                        with patch.object(_mod, "vector_remember"):
                            entry = _mod.wan_log()

        self.assertIsNotNone(entry)
        self.assertEqual(entry["status"], "ok")


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

    def test_module_has_full_check(self):
        self.assertTrue(callable(_mod.full_check))

    def test_bandwidth_threshold_defined(self):
        self.assertGreater(BANDWIDTH_HOG_THRESHOLD, 0)

    def test_state_files_are_json(self):
        for f in [_mod.STATE_FILE, _mod.KNOWN_DEVICES_FILE, _mod.WAN_HISTORY_FILE]:
            self.assertTrue(str(f).endswith(".json"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
