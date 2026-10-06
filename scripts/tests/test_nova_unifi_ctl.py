#!/usr/bin/env python3
"""Tests for nova_unifi_ctl.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_unifi_ctl.py"
SRC = SCRIPT.read_text()


def _poller_stub():
    u = types.ModuleType("nova_unifi_poller")
    u.CONTROLLER_BASE, u.SITE, u._api_key = "https://192.168.1.1", "default", "testkey"
    u._opener = mock.MagicMock()
    u._fetch_clients = mock.MagicMock(return_value=[])
    u._fetch_devices = mock.MagicMock(return_value=[])
    u._unifi_login = mock.MagicMock(return_value=True)
    return u


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"nova_unifi_poller": _poller_stub()}):   # restored after load; `u` stays bound
        spec.loader.exec_module(mod)
    return mod


C = _load("unifi_ctl_under_test", SCRIPT)
PHONE = {"mac": "aa:bb:cc:dd:ee:ff", "name": "Amys-iPhone", "hostname": "amys-iphone", "ip": "192.168.1.143",
         "is_wired": False, "essid": "DigitalNoise", "blocked": False}
UDM = {"mac": "70:a7:41:00:00:01", "name": "Dream Machine Pro"}
OK = {"meta": {"rc": "ok"}, "data": []}
REAL_POST = C._post   # the unpatched function, for TestUnit.test_post_request_shape


class _Base(unittest.TestCase):
    def setUp(self):
        C.u._fetch_clients = mock.MagicMock(return_value=[PHONE])
        C.u._fetch_devices = mock.MagicMock(return_value=[UDM])
        C.u._unifi_login = mock.MagicMock(return_value=True)
        self.post = mock.MagicMock(return_value=OK)
        p = mock.patch.object(C, "_post", self.post); p.start(); self.addCleanup(p.stop)


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("X-API-Key\": \"", SRC)                 # the key is read from the poller (fleet store), never literal

    def test_unifi_device_itself_is_refused_and_never_posted(self):
        r = C.block(UDM["mac"].upper(), "test")
        self.assertTrue(r.startswith("REFUSED"), r)
        self.assertIn("Dream Machine Pro", r)
        self.post.assert_not_called()

    def test_infrastructure_names_are_refused_case_insensitively(self):
        for name in ("nova-core3", "Hue Bridge", "UDM-Pro", "USW-Pro-48-PoE", "MAC-STUDIO", "slzb-06", "Rack 2 switch"):
            C.u._fetch_clients.return_value = [{**PHONE, "name": name, "ip": "192.168.1.200"}]
            self.assertTrue(C.block(PHONE["mac"]).startswith("REFUSED"), name)
        self.post.assert_not_called()

    def test_every_infra_ip_is_refused(self):
        for ip in sorted(C.INFRA_IPS):
            C.u._fetch_clients.return_value = [{**PHONE, "name": "mystery", "hostname": "", "ip": ip}]
            r = C.block(PHONE["mac"])
            self.assertIn(f"infrastructure address {ip}", r)
        self.assertEqual(len(C.INFRA_IPS), 14)
        self.post.assert_not_called()

    def test_login_failure_blocks_nothing(self):
        C.u._unifi_login.return_value = False
        self.assertEqual(C.block(PHONE["mac"]), "[error: UniFi login failed]")
        self.assertEqual(C.unblock(PHONE["mac"]), "[error: UniFi login failed]")
        self.post.assert_not_called()


class TestPerformance(_Base):
    def test_is_infrastructure_over_10k_clients(self):
        clients = [{"mac": f"02:00:00:{i >> 16 & 255:02x}:{i >> 8 & 255:02x}:{i & 255:02x}", "name": f"guest-{i}",
                    "hostname": "", "ip": f"10.9.{i >> 8 & 255}.{i & 255}"} for i in range(10_000)]
        C.u._fetch_devices.return_value = [UDM]
        t0 = time.perf_counter()
        hits = sum(1 for c in clients if C.is_infrastructure(c["mac"], c))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(hits, 0)


class TestRetry(_Base):
    def test_post_is_one_shot_and_raises(self):
        # RETRY GAP: _post()/_fetch_clients() — a controller error propagates; nothing is retried or silently blocked
        self.post.side_effect = OSError("controller down")
        with self.assertRaises(OSError):
            C.block(PHONE["mac"])
        self.assertEqual(self.post.call_count, 1)

    def test_udm_rc_error_is_reported_not_retried(self):
        self.post.return_value = {"meta": {"rc": "error", "msg": "api.err.Invalid"}}
        self.assertTrue(C.block(PHONE["mac"]).startswith("[error: UDM refused block-sta"))
        self.assertTrue(C.unblock(PHONE["mac"]).startswith("[error: UDM refused unblock-sta"))
        self.assertEqual(self.post.call_count, 2)


class TestUnit(_Base):
    def test_find_client_is_case_insensitive(self):
        self.assertEqual(C.find_client(PHONE["mac"].upper())["name"], "Amys-iPhone")
        self.assertIsNone(C.find_client("00:00:00:00:00:00"))
        C.u._fetch_clients.return_value = None
        self.assertIsNone(C.find_client(PHONE["mac"]))

    def test_is_infrastructure_reasons(self):
        self.assertIsNone(C.is_infrastructure(PHONE["mac"], PHONE))
        self.assertIsNone(C.is_infrastructure(PHONE["mac"], None))
        self.assertEqual(C.is_infrastructure(UDM["mac"], None), "UniFi device 'Dream Machine Pro'")
        self.assertIn("infrastructure name", C.is_infrastructure("x", {"name": "", "hostname": "synology-nas"}))
        C.u._fetch_devices.return_value = None
        self.assertIsNone(C.is_infrastructure("x", {"name": "tv", "ip": "192.168.1.201"}))

    def test_status_strings(self):
        self.assertEqual(C.status("00:00:00:00:00:00"), "00:00:00:00:00:00: not currently connected")
        s = C.status(PHONE["mac"])
        self.assertIn("Amys-iPhone ip=192.168.1.143 wired=False ssid=DigitalNoise blocked=False infra=no", s)
        C.u._fetch_clients.return_value = [{**PHONE, "ip": "192.168.1.2"}]
        self.assertIn("infra=infrastructure address 192.168.1.2", C.status(PHONE["mac"]))

    def test_post_request_shape(self):
        resp = mock.MagicMock(); resp.read.return_value = json.dumps(OK).encode()
        resp.__enter__.return_value = resp
        C.u._opener.open = mock.MagicMock(return_value=resp)
        out = REAL_POST({"cmd": "block-sta", "mac": "aa"})
        req = C.u._opener.open.call_args.args[0]
        self.assertEqual(out, OK)
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith("/cmd/stamgr"))
        self.assertEqual(req.get_header("X-api-key"), "testkey")
        self.assertEqual(json.loads(req.data), {"cmd": "block-sta", "mac": "aa"})
        resp.read.return_value = b""
        self.assertEqual(REAL_POST({"cmd": "x"}), {})


class TestIntegration(_Base):
    def test_cmd_url_is_built_from_the_pollers_session(self):
        self.assertEqual(C.CMD_URL, "https://192.168.1.1/proxy/network/api/s/default/cmd/stamgr")
        self.assertIn("import nova_unifi_poller as u", SRC)
        self.assertNotIn("_unifi_login()\n    ", SRC.split("def block")[0])     # login is the poller's, not reimplemented

    def test_block_then_status_reads_the_same_client_list(self):
        self.assertTrue(C.block(PHONE["mac"], "porch").startswith("quarantined"))
        self.assertTrue(C.status(PHONE["mac"]).startswith(PHONE["mac"]))
        self.assertEqual(C.u._fetch_clients.call_count, 2)


class TestFunctional(_Base):
    def test_block_golden_path(self):
        r = C.block(PHONE["mac"].upper(), "rogue scanner")
        self.post.assert_called_once_with({"cmd": "block-sta", "mac": PHONE["mac"]})
        self.assertIn("quarantined AA:BB:CC:DD:EE:FF = Amys-iPhone (192.168.1.143)", r)
        self.assertIn("Reason: rogue scanner", r)
        self.assertIn("Undo: unquarantine", r)

    def test_unblock_has_no_infra_gate_by_design(self):
        self.assertEqual(C.unblock(UDM["mac"]), f"unblocked {UDM['mac']} at the UDM Pro")
        self.post.assert_called_once_with({"cmd": "unblock-sta", "mac": UDM["mac"]})

    def test_unknown_client_is_still_blockable_but_named_unnamed(self):
        C.u._fetch_clients.return_value = []
        r = C.block("de:ad:be:ef:00:01")
        self.assertIn("= unnamed (no ip)", r)
        self.post.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main_and_no_args_is_usage(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        stub = ("import sys, types; u = types.ModuleType('nova_unifi_poller'); u.CONTROLLER_BASE='https://x'; u.SITE='s'; "
                "sys.modules['nova_unifi_poller'] = u; ")
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, "-c", stub + "import nova_unifi_ctl"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(r.stdout.strip(), "")
        r = subprocess.run([sys.executable, "-c", stub + "import runpy; runpy.run_path('nova_unifi_ctl.py', run_name='__main__')"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("usage: nova_unifi_ctl.py block|unblock|status", r.stderr)


if __name__ == "__main__":
    unittest.main()
