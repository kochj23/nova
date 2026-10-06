#!/usr/bin/env python3
"""Tests for nova_meshtastic_alert.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

No radio is ever keyed: meshtastic is a stub module (patched per test, keys restored), device discovery
and socket reachability are mocked, and time.sleep is a no-op."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_meshtastic_alert.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ma = _load("nova_meshtastic_alert_t", SCRIPT)
_REAL_FIND = ma.find_device


class _Base(unittest.TestCase):
    def setUp(self):
        self.iface = MagicMock()
        self.SerialInterface = MagicMock(return_value=self.iface)
        si = types.ModuleType("meshtastic.serial_interface")
        si.SerialInterface = self.SerialInterface
        pkg = types.ModuleType("meshtastic")
        pkg.serial_interface = si
        p = patch.dict(sys.modules, {"meshtastic": pkg, "meshtastic.serial_interface": si})
        p.start()
        self.addCleanup(p.stop)
        for p in (patch.object(ma.time, "sleep"),
                  patch.object(ma, "find_device", return_value="/dev/cu.usbmodemTEST")):
            p.start()
            self.addCleanup(p.stop)
        self.out = io.StringIO()
        r = redirect_stdout(self.out)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_watch_without_alert_flag_never_transmits(self):
        with patch.object(ma, "reachable", return_value=False), patch.object(ma, "send") as send:
            self.assertEqual(ma.watch(alert=False), 1)
        send.assert_not_called()

    def test_payload_capped_to_airtime_budget(self):
        ma.send("x" * 5000)
        self.assertEqual(len(self.iface.sendText.call_args.args[0]), ma.MAX_CHARS)


class TestPerformance(_Base):
    def test_watch_with_all_checks_fast(self):
        with patch.object(ma, "reachable", return_value=True):
            t0 = time.perf_counter()
            for _ in range(10_000):
                ma.watch(alert=False)
            self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(_Base):
    def test_send_failure_is_one_shot_and_closes_port(self):
        # RETRY GAP: send/sendText — one attempt (LoRa airtime is precious); failure returns False, port closed
        self.iface.sendText.side_effect = OSError("serial busy")
        self.assertFalse(ma.send("alert"))
        self.assertEqual(self.iface.sendText.call_count, 1)
        self.iface.close.assert_called_once()
        self.assertIn("send FAILED", self.out.getvalue())

    def test_open_failure_fails_open(self):
        self.SerialInterface.side_effect = OSError("no port")
        self.assertFalse(ma.send("alert"))
        self.iface.close.assert_not_called()


class TestUnit(_Base):
    def test_no_device(self):
        with patch.object(ma, "find_device", return_value=None):
            self.assertFalse(ma.send("x"))
            self.assertEqual(ma.self_test(), 2)
        self.SerialInterface.assert_not_called()

    def test_find_device_patterns(self):
        fake = {"/dev/ttyUSB*": ["/dev/ttyUSB1", "/dev/ttyUSB0"]}
        with patch.object(ma.glob, "glob", side_effect=lambda p: fake.get(p, [])):
            self.assertEqual(_REAL_FIND(), "/dev/ttyUSB0")
        with patch.object(ma.glob, "glob", return_value=[]):
            self.assertIsNone(_REAL_FIND())

    def test_reachable(self):
        cm = MagicMock()
        with patch("socket.create_connection", return_value=cm):
            self.assertTrue(ma.reachable("h", 1))
        with patch("socket.create_connection", side_effect=OSError("refused")):
            self.assertFalse(ma.reachable("h", 1))


class TestIntegration(_Base):
    def test_watch_names_down_services_and_sends(self):
        with patch.object(ma, "reachable", side_effect=lambda h, p: p != 5432):
            self.assertEqual(ma.watch(alert=True), 1)
        sent = self.iface.sendText.call_args.args[0]
        self.assertTrue(sent.startswith("NOVA CRITICAL: postgres unreachable"))
        self.assertEqual(self.SerialInterface.call_args.kwargs["devPath"], "/dev/cu.usbmodemTEST")


class TestFunctional(_Base):
    def test_self_test_golden_path(self):
        self.assertEqual(ma.self_test(), 0)
        self.assertIn("out-of-band path OK", self.iface.sendText.call_args.args[0])
        self.assertIn("SELF-TEST PASSED", self.out.getvalue())

    def test_all_green_watch_sends_nothing(self):
        with patch.object(ma, "reachable", return_value=True):
            self.assertEqual(ma.watch(alert=True), 0)
        self.SerialInterface.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--self-test", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertNotIn("import meshtastic", SRC.split("def send")[0])   # radio lib is lazy-imported


if __name__ == "__main__":
    unittest.main()
