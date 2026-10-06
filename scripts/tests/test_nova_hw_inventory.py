#!/usr/bin/env python3
"""Tests for nova_hw_inventory.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_hw_inventory.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="hw-test-"))

import psycopg2  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hw = _load("hw_inventory_under_test", SCRIPT)

LINUX_OUT = """===HOST===
nova-core
===USB===
Bus 001 Device 001: ID 1d6b:0002 Linux Foundation 2.0 root hub
Bus 001 Device 003: ID 10c4:ea60 Silicon Labs CP210x UART Bridge
Bus 001 Device 004: ID 1a86:7523 QinHeng Electronics CH340 serial converter
===SERIAL===
ttyUSB0
ttyUSB1
===BYID===
lrwxrwxrwx 1 root root 13 Jan 1 usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle-if00-port0 -> ../../ttyUSB0
lrwxrwxrwx 1 root root 13 Jan 1 usb-Heltec_LoRa_V3-if00 -> ../../ttyUSB1
===HCI===
hci0:	Type: Primary  Bus: USB
	BD Address: AA:BB  ACL MTU: 1021:8  SCO MTU: 64:1
	UP RUNNING
"""
MAC_OUT = "===HOST===\nmac-studio\n===SERIAL===\ncu.usbmodem1101\n===USB===\nUSB3.1 Hub:\nStudio Display:\nMagic Keyboard:\n"


class _Cur:
    def __init__(self, hosts=()):
        self.hosts, self.sql, self.params = list(hosts), [], []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split())); self.params.append(params)

    def fetchall(self):
        return self.hosts


class _Conn:
    def __init__(self, cur):
        self.cur, self.closed, self.autocommit = cur, False, False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _runner(table):
    calls = []

    def _run(cmd, ip=None, timeout=30):
        calls.append((cmd, ip, timeout))
        v = table.get(ip, (-1, ""))
        return v
    _run.calls = calls
    return _run


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", hw.DSN)

    def test_collection_is_argv_only(self):
        self.assertNotIn("shell=True", SRC)
        run = MagicMock(return_value=types.SimpleNamespace(returncode=0, stdout="x"))
        with patch.object(hw.subprocess, "run", run):
            hw._run("echo '===HOST==='; hostname", ip="192.168.1.2")
            hw._run("echo local", ip="192.168.1.6")
        remote, local = run.call_args_list[0][0][0], run.call_args_list[1][0][0]
        self.assertEqual(remote[:3], ["ssh", "-o", "BatchMode=yes"]); self.assertEqual(remote[-2:], ["kochj@192.168.1.2", "echo '===HOST==='; hostname"])
        self.assertEqual(local, ["/bin/sh", "-c", "echo local"])

    def test_sql_parameterized_and_values_truncated(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        long_name = "Z" * 500
        out = f"===HOST===\nh\n===USB===\nBus 1 Device 2: ID 0403:6001 {long_name}\n===SERIAL===\n===BYID===\n===HCI===\n"
        cur = _Cur([("h", "10.0.0.9", "linux")]); conn = _Conn(cur)
        with patch.object(hw, "_run", _runner({"10.0.0.9": (0, out)})), patch.object(hw.psycopg2, "connect", return_value=conn), \
             redirect_stdout(io.StringIO()):
            hw.main()
        ins = [p for s, p in zip(cur.sql, cur.params) if s.startswith("INSERT INTO hardware_inventory (")]
        self.assertEqual(len(ins[0][2]), 160)
        self.assertTrue(all("%s" in s for s in cur.sql if s.startswith(("INSERT", "DELETE"))))


class TestPerformance(unittest.TestCase):
    def test_collect_parses_10k_usb_lines_fast(self):
        usb = "\n".join(f"Bus 001 Device {i:03d}: ID {i % 65536:04x}:{i:04x} Vendor Device {i}" for i in range(10_000))
        out = f"===HOST===\nh\n===USB===\n{usb}\n===SERIAL===\n===BYID===\n===HCI===\n"
        with patch.object(hw, "_run", _runner({"10.0.0.1": (0, out)})):
            t0 = time.perf_counter()
            items, reachable = hw.collect("h", "10.0.0.1", "linux")
            self.assertLess(time.perf_counter() - t0, 1.5)
        self.assertTrue(reachable); self.assertEqual(len(items), 10_000)


class TestRetry(unittest.TestCase):
    def test_run_fails_open_and_unreachable_is_recorded_not_fabricated(self):
        # RETRY GAP: _run()/subprocess.run — one ssh per host; failure returns (-1, "") and collect() reports unreachable
        run = MagicMock(side_effect=subprocess.TimeoutExpired("ssh", 40))
        with patch.object(hw.subprocess, "run", run):
            self.assertEqual(hw.collect("h", "10.0.0.2", "linux"), ([], False))
        self.assertEqual(run.call_count, 1)
        with patch.object(hw, "_run", _runner({"10.0.0.2": (0, "garbage without markers")})):
            self.assertEqual(hw.collect("h", "10.0.0.2", "macos"), ([], False))

    def test_main_survives_a_collect_exception(self):
        cur = _Cur([("h", "10.0.0.3", "linux")]); conn = _Conn(cur)
        with patch.object(hw, "collect", MagicMock(side_effect=RuntimeError("boom"))) as c, \
             patch.object(hw.psycopg2, "connect", return_value=conn), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(hw.main(), 0)
        self.assertEqual(c.call_count, 1)
        self.assertIn("error boom", out.getvalue())
        self.assertEqual(cur.params[-1], ("h", False, 0, 0, 0, 0))


class TestUnit(unittest.TestCase):
    def test_section_handles_empty_blocks(self):
        blob = "===A===\n===B===\nline\n===C===\n"
        self.assertEqual(hw._section(blob, "A"), "")
        self.assertEqual(hw._section(blob, "B"), "line")
        self.assertEqual(hw._section(blob, "Z"), "")

    def test_linux_collect_classifies_devices(self):
        with patch.object(hw, "_run", _runner({"10.0.0.4": (0, LINUX_OUT)})):
            items, ok = hw.collect("nova-core", "10.0.0.4", "linux")
        cats = [(i[0], i[1]) for i in items]
        self.assertNotIn("usb", [c for c, n in cats if "root hub" in n])
        self.assertIn(("usb", "Silicon Labs CP210x UART Bridge"), cats)
        self.assertIn(("zigbee", "Zigbee controller on ttyUSB0"), cats)
        self.assertIn(("lora", "LoRa/ESP dev board on ttyUSB1"), cats)
        self.assertIn(("bluetooth", "hci0"), cats)
        self.assertEqual([i for i in items if i[0] == "bluetooth"][0][3], "up")
        self.assertEqual([i for i in items if i[0] == "usb"][0][4], "CP210x")      # bridge chip hint

    def test_macos_collect(self):
        with patch.object(hw, "_run", _runner({"192.168.1.6": (0, MAC_OUT)})):
            items, ok = hw.collect("mac-studio", "192.168.1.6", "macos")
        self.assertEqual(items[0], ("serial", "cu.usbmodem1101", "", "present", ""))
        self.assertEqual([i[1] for i in items if i[0] == "usb"], ["USB3.1 Hub", "Studio Display", "Magic Keyboard"])
        self.assertEqual(items[-1][0], "bluetooth")


class TestIntegration(unittest.TestCase):
    def test_hosts_come_from_cinc_node_configs_and_rows_land_in_hardware_inventory(self):
        cur = _Cur([("nova-core", "10.0.0.4", "linux")]); conn = _Conn(cur)
        with patch.object(hw.psycopg2, "connect", return_value=conn):
            self.assertEqual(hw.get_hosts(), [("nova-core", "10.0.0.4", "linux")])
        self.assertIn("FROM cinc_node_configs", cur.sql[0]); self.assertTrue(conn.closed)
        self.assertIn("dbname=nova_ops", hw.DSN)
        cur = _Cur([("nova-core", "10.0.0.4", "linux")]); conn = _Conn(cur)
        with patch.object(hw, "_run", _runner({"10.0.0.4": (0, LINUX_OUT)})), patch.object(hw.psycopg2, "connect", return_value=conn), \
             redirect_stdout(io.StringIO()):
            hw.main()
        self.assertTrue(cur.sql[0].startswith("CREATE TABLE IF NOT EXISTS hardware_inventory"))
        self.assertEqual(cur.params[cur.sql.index("DELETE FROM hardware_inventory WHERE host_name=%s")], ("nova-core",))
        self.assertEqual(cur.params[-1], ("nova-core", True, 2, 0, 1, 1))         # usb, serial, bluetooth, lora
        self.assertIn("ON CONFLICT (host_name) DO UPDATE", cur.sql[-1])


class TestFunctional(unittest.TestCase):
    def test_golden_path_two_hosts(self):
        cur = _Cur([("mac-studio", "192.168.1.6", "macos"), ("nova-core", "10.0.0.4", "linux")]); conn = _Conn(cur)
        runner = _runner({"192.168.1.6": (0, MAC_OUT), "10.0.0.4": (0, LINUX_OUT)})
        with patch.object(hw, "_run", runner), patch.object(hw.psycopg2, "connect", return_value=conn), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(hw.main(), 0)
        self.assertEqual([c[1] for c in runner.calls], ["192.168.1.6", "10.0.0.4"])
        self.assertIn("mac-studio: ok — usb=3 serial=1 bt=1 lora=0", out.getvalue())
        self.assertIn("nova-core: ok — usb=2 serial=0 bt=1 lora=1", out.getvalue())
        self.assertTrue(conn.closed)

    def test_unreachable_host_writes_no_inventory_rows(self):
        cur = _Cur([("dead", "10.0.0.99", "linux")]); conn = _Conn(cur)
        with patch.object(hw, "_run", _runner({})), patch.object(hw.psycopg2, "connect", return_value=conn), redirect_stdout(io.StringIO()) as out:
            hw.main()
        self.assertFalse(any(s.startswith(("DELETE", "INSERT INTO hardware_inventory (")) for s in cur.sql))
        self.assertEqual(cur.params[-1], ("dead", False, 0, 0, 0, 0))
        self.assertIn("dead: UNREACHABLE", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("from unittest.mock import MagicMock; import psycopg2, subprocess\n"
                "psycopg2.connect = MagicMock(side_effect=AssertionError('PG touched'))\n"
                "subprocess.run = MagicMock(side_effect=AssertionError('ssh spawned'))\n"
                "import nova_hw_inventory; print(sorted(nova_hw_inventory.LOCAL_IPS))")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "['192.168.1.6']")


if __name__ == "__main__":
    unittest.main()
