#!/usr/bin/env python3
"""Tests for nova_ble_scan.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ble_scan.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(sys, "argv", ["nova_ble_scan.py"]):      # SCAN_S parses argv[1] at import; pytest's argv must not leak in
        spec.loader.exec_module(mod)
    return mod


ble = _load("ble_under_test", SCRIPT)

SCAN_OUT = "\n".join([
    "\x1b[0;92m[NEW]\x1b[0m Device AA:BB:CC:DD:EE:01 Amys-iPhone",
    "[CHG] Device AA:BB:CC:DD:EE:01 RSSI: -61",
    "[CHG] Device AA:BB:CC:DD:EE:01 ManufacturerData Key: 0x004c",
    "[NEW] Device aa:bb:cc:dd:ee:02 AA-BB-CC-DD-EE-02",            # name == MAC -> no name
    "[CHG] Device AA:BB:CC:DD:EE:02 Name: Tile Tracker",
    "[CHG] Device AA:BB:CC:DD:EE:02 RSSI: -88",
    "[CHG] Controller 00:11:22:33:44:55 Discovering: yes",
    "Discovery started",
])


def _proc(stdout="", returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=returncode)


def _run_main(scan_stdout=SCAN_OUT, psql_exc=None):
    """Run main() with bluetoothctl and psql both stubbed; return (rc, calls, stdout)."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[0] == "bluetoothctl":
            return _proc(scan_stdout)
        if psql_exc:
            raise psql_exc
        return _proc()

    with patch.object(ble.subprocess, "run", fake_run), redirect_stdout(io.StringIO()) as out:
        rc = ble.main()
    return rc, calls, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ble.DSN)

    def test_sql_values_are_escaped_and_psql_runs_without_a_shell(self):
        self.assertEqual(ble._sql_str("O'Brien'); DROP TABLE telemetry.bluetooth; --"),
                         "'O''Brien''); DROP TABLE telemetry.bluetooth; --'")
        self.assertEqual(ble._sql_str(None), "NULL")
        self.assertNotIn("shell=True", SRC)
        rc, calls, _ = _run_main(scan_stdout="[NEW] Device AA:BB:CC:DD:EE:09 Evil'); DROP TABLE x; --")
        sql = calls[-1][-1]
        self.assertIn("'Evil''); DROP TABLE x; --'", sql)   # quote doubled: the payload stays inside the literal
        self.assertEqual(calls[-1][:2], ["psql", ble.DSN])

    def test_only_write_is_the_bluetooth_table(self):
        writes = {m.group(0) for m in re.finditer(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+[\w.]+", SRC)}
        self.assertEqual(writes, {"INSERT INTO telemetry.bluetooth"})


class TestPerformance(unittest.TestCase):
    def test_parses_10k_scan_lines_quickly(self):
        lines = []
        for i in range(10_000):
            mac = f"AA:BB:{(i >> 8) & 0xFF:02X}:{i & 0xFF:02X}:00:01"
            lines.append(f"\x1b[0;92m[CHG]\x1b[0m Device {mac} RSSI: -{40 + i % 50}" if i % 2 else
                         f"[NEW] Device {mac} Sensor {i}")
        with patch.object(ble.subprocess, "run", lambda *a, **k: _proc("\n".join(lines))):
            t0 = time.perf_counter()
            devs = ble.scan()
            dt = time.perf_counter() - t0
        self.assertLess(dt, 2.0)
        self.assertEqual(len(devs), 10_000)
        t0 = time.perf_counter()
        for i in range(10_000):
            ble._sql_str(f"it's {i}")
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_scan_failure_fails_open(self):
        # RETRY GAP: scan()/bluetoothctl — one attempt; a crash or timeout yields {} and the run still exits 0
        def boom(*a, **k):
            raise subprocess.TimeoutExpired("bluetoothctl", 25)
        with patch.object(ble.subprocess, "run", boom), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ble.scan(), {})
            self.assertEqual(ble.main(), 0)
        self.assertIn("scan failed", out.getvalue())
        self.assertIn("0 BLE devices", out.getvalue())

    def test_insert_failure_is_one_shot_and_reports_nonzero(self):
        # RETRY GAP: main()/psql insert — single attempt; failure prints stderr and returns 1 without raising
        err = subprocess.CalledProcessError(1, "psql", stderr="connection refused")
        rc, calls, out = _run_main(psql_exc=err)
        self.assertEqual(rc, 1)
        self.assertEqual(sum(1 for c in calls if c[0] == "psql"), 1)
        self.assertIn("insert failed: connection refused", out)


class TestUnit(unittest.TestCase):
    def test_scan_parses_names_rssi_and_strips_ansi(self):
        with patch.object(ble.subprocess, "run", lambda *a, **k: _proc(SCAN_OUT)):
            devs = ble.scan()
        self.assertEqual(set(devs), {"AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"})
        self.assertEqual(devs["AA:BB:CC:DD:EE:01"], {"name": "Amys-iPhone", "rssi": -61})
        self.assertEqual(devs["AA:BB:CC:DD:EE:02"], {"name": "Tile Tracker", "rssi": -88})

    def test_property_updates_never_become_names(self):
        out = "\n".join(f"[CHG] Device AA:BB:CC:DD:EE:03 {p} x" for p in ble._PROP)
        with patch.object(ble.subprocess, "run", lambda *a, **k: _proc(out)):
            devs = ble.scan()
        self.assertEqual(devs["AA:BB:CC:DD:EE:03"], {"name": "", "rssi": None})

    def test_name_is_capped_at_80_chars_and_empty_output_is_empty(self):
        with patch.object(ble.subprocess, "run", lambda *a, **k: _proc("[NEW] Device AA:BB:CC:DD:EE:04 " + "n" * 200)):
            self.assertEqual(len(ble.scan()["AA:BB:CC:DD:EE:04"]["name"]), 80)
        with patch.object(ble.subprocess, "run", lambda *a, **k: _proc(None)):
            self.assertEqual(ble.scan(), {})

    def test_sql_str_edges(self):
        self.assertEqual(ble._sql_str(""), "''")
        self.assertEqual(ble._sql_str(0), "'0'")
        self.assertEqual(ble._sql_str("plain"), "'plain'")


class TestIntegration(unittest.TestCase):
    def test_scan_feeds_one_multirow_insert_tagged_with_this_observer(self):
        rc, calls, _ = _run_main()
        self.assertEqual([c[0] for c in calls], ["bluetoothctl", "psql"])
        self.assertEqual(calls[0], ["bluetoothctl", "--timeout", str(ble.SCAN_S), "scan", "on"])
        sql = calls[1][-1]
        self.assertTrue(sql.startswith("INSERT INTO telemetry.bluetooth (ts, device_mac, device_name, rssi, device_type, observer) VALUES "))
        self.assertIn(f"'ble_hci', '{ble.OBS}')", sql)
        self.assertIn("(now(), 'AA:BB:CC:DD:EE:01', 'Amys-iPhone', -61, 'ble_hci'", sql)
        self.assertIn("'Tile Tracker', -88", sql)
        self.assertEqual(sql.count("(now(),"), 2)
        self.assertIn("ON_ERROR_STOP=1", calls[1])

    def test_unnamed_and_unheard_devices_insert_nulls(self):
        rc, calls, _ = _run_main(scan_stdout="[NEW] Device AA:BB:CC:DD:EE:05 AA-BB-CC-DD-EE-05")
        self.assertIn("'AA:BB:CC:DD:EE:05', NULL, NULL, 'ble_hci'", calls[1][-1])


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_and_summarises(self):
        rc, calls, out = _run_main()
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), f"[ble_scan] {ble.OBS}: 2 BLE devices (2 named) in {ble.SCAN_S}s")

    def test_nothing_heard_means_no_db_round_trip(self):
        rc, calls, out = _run_main(scan_stdout="Discovery started\n")
        self.assertEqual(rc, 0)
        self.assertEqual([c[0] for c in calls], ["bluetoothctl"])
        self.assertIn("0 BLE devices (0 named)", out)

    def test_psql_error_path_returns_1(self):
        rc, _, out = _run_main(psql_exc=RuntimeError("psql missing"))
        self.assertEqual(rc, 1)
        self.assertIn("insert failed: psql missing", out)


class TestFrame(unittest.TestCase):
    def test_import_never_scans_or_writes(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_ble_scan; print(nova_ble_scan.SCAN_S)"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "25")          # default window, nothing scanned, nothing printed by main()

    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
