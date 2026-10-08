#!/usr/bin/env python3
"""Seven-category tests for nova_unifi_ctl.py, focused on the 2026-10-08 changes: the P2 comms
guard (household devices are never blocked) and the retry/backoff on the UDM POST.
Never talks to the UDM: nova_unifi_poller is stubbed and _opener is a mock.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
os.environ["NOVA_GUARDS_NO_SLACK"] = "1"
SCRIPT = SCRIPTS / "nova_unifi_ctl.py"

import nova_safety_guards as G  # noqa: E402


def _poller_stub():
    u = types.ModuleType("nova_unifi_poller")
    u.CONTROLLER_BASE, u.SITE, u._api_key = "https://192.168.1.1", "default", "k"
    u._opener = mock.MagicMock()
    u._fetch_clients = mock.MagicMock(return_value=[])
    u._fetch_devices = mock.MagicMock(return_value=[])
    u._unifi_login = mock.MagicMock(return_value=True)
    return u


def _load():
    spec = importlib.util.spec_from_file_location("unifi_ctl_7cat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {"nova_unifi_poller": _poller_stub()}):
        spec.loader.exec_module(mod)
    return mod


C = _load()
STRANGER = {"mac": "02:11:22:33:44:55", "name": "esp-unknown", "hostname": "esp", "ip": "192.168.1.150"}


def _resp(obj):
    r = mock.MagicMock()
    r.__enter__.return_value.read.return_value = json.dumps(obj).encode()
    return r


class _Base(unittest.TestCase):
    def setUp(self):
        C.u._fetch_clients = mock.MagicMock(return_value=[STRANGER])
        C.u._fetch_devices = mock.MagicMock(return_value=[])
        C.u._unifi_login = mock.MagicMock(return_value=True)
        C.u._opener = mock.MagicMock()
        C.u._opener.open.return_value = _resp({"meta": {"rc": "ok"}})
        self.owner = mock.MagicMock(return_value=(None, None))
        self.rb = mock.MagicMock()
        for p in (mock.patch.object(G, "_household_owner", self.owner),
                  mock.patch.object(G, "report_block", self.rb),
                  mock.patch.object(G, "_ops_cursor", return_value=mock.MagicMock()),
                  mock.patch.object(C.time, "sleep")):
            p.start()
            self.addCleanup(p.stop)


class TestSecurity(_Base):
    def test_household_names_refused_and_reported(self):
        for name in ("Amys-MacBook", "Amys-iPad", "Apple Watch", "kochj-phone", "AirPods Pro"):
            C.u._fetch_clients.return_value = [dict(STRANGER, name=name)]
            r = C.block(STRANGER["mac"])
            self.assertTrue(r.startswith("REFUSED: COMMS GUARD"), (name, r))
        C.u._opener.open.assert_not_called()
        self.assertEqual(self.rb.call_count, 5)
        self.assertEqual(self.rb.call_args.kwargs["guard"], "comms")

    def test_guard_exception_fails_closed(self):
        with mock.patch.object(G, "comms_guard", side_effect=RuntimeError("boom")):
            r = C.block(STRANGER["mac"])
        self.assertIn("comms guard unavailable", r)
        C.u._opener.open.assert_not_called()

    def test_api_key_sent_as_header_not_in_url(self):
        C._post({"cmd": "block-sta", "mac": "x"})
        req = C.u._opener.open.call_args.args[0]
        self.assertEqual(req.get_header("X-api-key"), "k")
        self.assertNotIn("k", req.full_url.split("/")[-1])

    def test_mac_lowercased_in_payload(self):
        C.block(STRANGER["mac"].upper())
        body = json.loads(C.u._opener.open.call_args.args[0].data)
        self.assertEqual(body, {"cmd": "block-sta", "mac": STRANGER["mac"]})


class TestPerformance(_Base):
    def test_block_refusal_is_fast(self):
        C.u._fetch_clients.return_value = [dict(STRANGER, name="Amys-iPhone")]
        t0 = time.perf_counter()
        for _ in range(200):
            C.block(STRANGER["mac"])
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_retry_is_bounded(self):
        C.u._opener.open.side_effect = OSError("down")
        with self.assertRaises(OSError):
            C._post({"cmd": "block-sta", "mac": "x"})
        self.assertEqual(C.u._opener.open.call_count, 3)


class TestRetry(_Base):
    def test_post_retries_transient_then_succeeds(self):
        C.u._opener.open.side_effect = [OSError("reset"), _resp({"meta": {"rc": "ok"}})]
        self.assertEqual(C._post({"cmd": "unblock-sta", "mac": "x"}), {"meta": {"rc": "ok"}})
        self.assertEqual(C.u._opener.open.call_count, 2)
        C.time.sleep.assert_called_once()

    def test_backoff_grows_and_error_surfaces(self):
        C.u._opener.open.side_effect = OSError("down")
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            with self.assertRaises(OSError):
                C.block(STRANGER["mac"])
        delays = [c.args[0] for c in C.time.sleep.call_args_list]
        self.assertEqual(len(delays), 2)
        self.assertLess(delays[0], delays[1])
        self.assertIn("attempt 1/3", err.getvalue())

    def test_bad_json_retried(self):
        bad = mock.MagicMock()
        bad.__enter__.return_value.read.return_value = b"<html>502</html>"
        C.u._opener.open.side_effect = [bad, _resp({"meta": {"rc": "ok"}})]
        self.assertEqual(C._post({})["meta"]["rc"], "ok")

    def test_refused_block_never_hits_network_even_with_retries(self):
        self.owner.return_value = ("amy", None)
        C.block(STRANGER["mac"])
        C.u._opener.open.assert_not_called()


class TestUnit(_Base):
    def test_is_infrastructure_none_for_stranger(self):
        self.assertIsNone(C.is_infrastructure(STRANGER["mac"], STRANGER))

    def test_find_client_missing(self):
        self.assertIsNone(C.find_client("ff:ff:ff:ff:ff:ff"))

    def test_status_unknown(self):
        self.assertIn("not currently connected", C.status("ff:ff:ff:ff:ff:ff"))


class TestIntegration(_Base):
    def test_block_passes_mac_and_names_to_comms_guard(self):
        with mock.patch.object(G, "comms_guard", return_value=(True, "ok")) as cg:
            C.block(STRANGER["mac"])
        kw = cg.call_args.kwargs
        self.assertEqual(kw["macs"], [STRANGER["mac"]])
        self.assertEqual(kw["names"], ["esp-unknown", "esp"])

    def test_owner_lookup_flows_through_real_comms_guard(self):
        self.owner.return_value = ("jordan", None)
        r = C.block(STRANGER["mac"])
        self.assertIn("belongs to jordan", r)
        self.owner.assert_called_with(STRANGER["mac"], None)


class TestFunctional(_Base):
    def test_stranger_block_golden_path(self):
        r = C.block(STRANGER["mac"], reason="port scan")
        self.assertTrue(r.startswith(f"quarantined {STRANGER['mac']}"), r)
        self.assertIn("Reason: port scan", r)
        self.assertIn("Undo: unquarantine", r)

    def test_unblock_golden_path(self):
        self.assertTrue(C.unblock(STRANGER["mac"]).startswith("unblocked"))

    def test_udm_rejects(self):
        C.u._opener.open.return_value = _resp({"meta": {"rc": "error"}})
        self.assertTrue(C.block(STRANGER["mac"]).startswith("[error: UDM refused"))


class TestFrame(unittest.TestCase):
    def test_module_loads_and_exposes_api(self):
        for fn in ("block", "unblock", "status", "find_client", "is_infrastructure", "_post"):
            self.assertTrue(callable(getattr(C, fn)))
        self.assertIn("/cmd/stamgr", C.CMD_URL)

    def test_cli_usage_exits_nonzero(self):
        code = ("import sys, types; u = types.ModuleType('nova_unifi_poller'); "
                "u.CONTROLLER_BASE='https://x'; u.SITE='default'; sys.modules['nova_unifi_poller'] = u; "
                f"sys.argv=['x']; import runpy; runpy.run_path({str(SCRIPT)!r}, run_name='__main__')")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("usage", r.stderr)

if __name__ == "__main__":
    unittest.main()
