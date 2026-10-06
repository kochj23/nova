#!/usr/bin/env python3
"""Tests for nova_unifi_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). nova_config/nova_notify are stubbed at load; the UniFi API, Keychain
and all HTTP are mocked; state files are redirected to a tempdir. No controller, no Keychain, no
network. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

_FAKE_CFG = types.SimpleNamespace(VECTOR_URL="http://memory.test/remember", SLACK_PHOTOS="C", JORDAN_DM="D")
_FAKE_NOTIFY = types.ModuleType("nova_notify")
_FAKE_NOTIFY.notify = mock.MagicMock()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"nova_config": _FAKE_CFG, "nova_notify": _FAKE_NOTIFY}):
        spec.loader.exec_module(mod)
    return mod


uc = _load("nova_unifi_monitor_t", SCRIPTS / "nova_unifi_monitor.py")
SRC = (SCRIPTS / "nova_unifi_monitor.py").read_text()


class _StateDir:
    def __enter__(self):
        self.td = Path(tempfile.mkdtemp())
        self._p = [
            mock.patch.object(uc, "STATE_DIR", self.td),
            mock.patch.object(uc, "STATE_FILE", self.td / "state.json"),
            mock.patch.object(uc, "KNOWN_DEVICES_FILE", self.td / "known.json"),
        ]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *a):
        for p in self._p:
            p.stop()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_api_key_from_secrets_or_keychain(self):
        self.assertIn('nova_secrets.get_secret("nova-unifi-api-key")', SRC)
        self.assertIn('"security", "find-generic-password"', SRC)

    def test_api_get_no_key_skips_network(self):
        with mock.patch.object(uc, "get_api_key", return_value=None), \
             mock.patch.object(uc.urllib.request, "urlopen") as u:
            self.assertIsNone(uc.api_get("stat/sta"))
        u.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_find_problems_1000_clients(self):
        clients = [{"mac": f"m{i}", "signal": -85} for i in range(1000)]
        t0 = time.perf_counter()
        probs = uc.find_problems({}, [], clients)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertTrue(any(p["category"] == "clients" for p in probs))


class TestRetry(unittest.TestCase):
    def test_api_get_failure_returns_none(self):
        # RETRY GAP: api_get()/urlopen — single attempt; any error returns None, caller degrades
        with mock.patch.object(uc, "get_api_key", return_value="k"), \
             mock.patch.object(uc.urllib.request, "urlopen", side_effect=OSError("udm down")) as u, \
             mock.patch.object(uc, "log"):
            self.assertIsNone(uc.api_get("stat/sta"))
        self.assertEqual(u.call_count, 1)

    def test_vector_remember_swallows_errors(self):
        with mock.patch.object(uc.urllib.request, "urlopen", side_effect=OSError("mem down")):
            uc.vector_remember("x")   # no raise

    def test_load_json_corrupt_returns_empty(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"; p.write_text("{not json")
            with mock.patch.object(uc, "log"):
                self.assertEqual(uc._load_json(p), {})


class TestUnit(unittest.TestCase):
    def test_find_problems_flags_down_device(self):
        probs = uc.find_problems({}, [{"name": "AP-Garage", "state": 0}], [])
        self.assertTrue(any("not connected" in p["message"] for p in probs))

    def test_find_problems_skips_intentionally_offline(self):
        off = next(iter(uc.INTENTIONALLY_OFFLINE))
        probs = uc.find_problems({}, [{"name": off, "state": 10}], [])
        self.assertEqual(probs, [])

    def test_find_bandwidth_hogs(self):
        big = uc.BANDWIDTH_HOG_THRESHOLD + 1
        hogs = uc.find_bandwidth_hogs([{"hostname": "roku", "tx_bytes": big, "rx_bytes": 0},
                                       {"hostname": "lamp", "tx_bytes": 10, "rx_bytes": 10}])
        self.assertEqual(len(hogs), 1)
        self.assertIn("roku", hogs[0]["message"])

    def test_dpi_category_name(self):
        self.assertEqual(uc._dpi_category_name(4), "Streaming Media")
        self.assertEqual(uc._dpi_category_name(999), "Category 999")

    def test_format_status(self):
        self.assertEqual(uc.format_status(None), "Unable to reach UDM Pro")
        self.assertIn("wan: ok", uc.format_status({"wan": {"status": "ok"}}))


class TestIntegration(unittest.TestCase):
    def test_slack_post_routes_to_notify(self):
        _FAKE_NOTIFY.notify.reset_mock()
        uc.slack_post("*A Title*\nbody line", level="warning", category="network", dedup_key="k")
        _FAKE_NOTIFY.notify.assert_called_once()
        self.assertEqual(_FAKE_NOTIFY.notify.call_args[0][0], "A Title")
        self.assertEqual(_FAKE_NOTIFY.notify.call_args[1]["level"], "warning")

    def test_rogue_learn_then_check(self):
        clients = [{"mac": "AA:BB", "hostname": "known-tv", "ip": "192.168.1.50"}]
        with _StateDir():
            with mock.patch.object(uc, "get_clients", return_value=clients), mock.patch("builtins.print"), \
                 mock.patch.object(uc, "log"):
                uc.rogue_learn()
            # now an unknown device appears -> rogue_check should alert
            clients2 = clients + [{"mac": "FF:EE", "hostname": "mystery", "ip": "192.168.1.99"}]
            _FAKE_NOTIFY.notify.reset_mock()
            with mock.patch.object(uc, "get_clients", return_value=clients2), \
                 mock.patch.object(uc, "get_devices", return_value=[]), \
                 mock.patch.object(uc, "vector_remember"), mock.patch("builtins.print"), \
                 mock.patch.object(uc, "log"):
                uc.rogue_check()
        _FAKE_NOTIFY.notify.assert_called_once()
        self.assertIn("Rogue", _FAKE_NOTIFY.notify.call_args[0][0])


class TestFunctional(unittest.TestCase):
    def test_full_check_posts_new_problems(self):
        _FAKE_NOTIFY.notify.reset_mock()
        with _StateDir():
            with mock.patch.object(uc, "get_health", return_value={"wan": {"status": "ok", "latency": 5}}), \
                 mock.patch.object(uc, "get_devices", return_value=[{"name": "AP1", "state": 0}]), \
                 mock.patch.object(uc, "get_clients", return_value=[]), \
                 mock.patch.object(uc, "vector_remember") as vr, mock.patch.object(uc, "log"):
                uc.full_check()
        _FAKE_NOTIFY.notify.assert_called_once()
        vr.assert_called_once()

    def test_full_check_unreachable_alerts_critical(self):
        _FAKE_NOTIFY.notify.reset_mock()
        with _StateDir():
            with mock.patch.object(uc, "get_health", return_value=None), \
                 mock.patch.object(uc, "get_devices", return_value=[]), \
                 mock.patch.object(uc, "get_clients", return_value=[]), mock.patch.object(uc, "log"):
                uc.full_check()
        self.assertEqual(_FAKE_NOTIFY.notify.call_args[1]["level"], "critical")

    def test_full_check_unchanged_does_not_repage(self):
        _FAKE_NOTIFY.notify.reset_mock()
        with _StateDir():
            health = {"wan": {"status": "ok", "latency": 5}}
            devices = [{"name": "AP1", "state": 0}]
            common = dict(get_health=mock.DEFAULT)
            with mock.patch.object(uc, "get_health", return_value=health), \
                 mock.patch.object(uc, "get_devices", return_value=devices), \
                 mock.patch.object(uc, "get_clients", return_value=[]), \
                 mock.patch.object(uc, "vector_remember"), mock.patch.object(uc, "log"):
                uc.full_check()      # first run posts
                _FAKE_NOTIFY.notify.reset_mock()
                uc.full_check()      # same problem -> no re-page
        _FAKE_NOTIFY.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c",
                            "import sys, unittest.mock as m; "
                            "import types; "
                            "nc=types.SimpleNamespace(VECTOR_URL='x'); "
                            "nn=types.ModuleType('nova_notify'); nn.notify=lambda *a, **k: None; "
                            "sys.modules['nova_config']=nc; sys.modules['nova_notify']=nn; "
                            "import nova_unifi_monitor"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Running UniFi network health check", r.stdout)


if __name__ == "__main__":
    unittest.main()
