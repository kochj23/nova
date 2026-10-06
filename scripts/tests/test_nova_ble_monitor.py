#!/usr/bin/env python3
"""Tests for nova_ble_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ble_monitor.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="ble_test_"))
INJECT = "x'); --injected"

PHONE, HP_OFFICE, HP_DEN = "AA:AA:AA:AA:AA:01", "BB:BB:BB:BB:BB:01", "BB:BB:BB:BB:BB:02"


class _Cur:
    def __init__(self, conn): self.conn = conn
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.conn.sql.append((sql, params))
    def fetchall(self): return self.conn.rows


class _Conn:
    closed = False
    def __init__(self, rows=()): self.rows = list(rows); self.sql = []; self.autocommit = False
    def cursor(self): return _Cur(self)


MAP_ROWS = [(PHONE, "Jordan iPhone", "phone", "jordan", None, False),
            (HP_OFFICE, "HomePod", "homepod", "house", "office", True),
            (HP_DEN, "HomePod", "homepod", "house", "den", True)]


def _load():
    """Import with PG answering the device-map query from a stub, the log file in a tempdir,
    and SIGINT/SIGTERM handlers restored afterwards (the module installs its own at import)."""
    old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    real_fh = logging.FileHandler
    try:
        with patch("psycopg2.connect", return_value=_Conn(MAP_ROWS)), \
             patch("logging.FileHandler", lambda *a, **k: real_fh(str(TMP / "ble.log"))), \
             patch("logging.basicConfig"):
            spec = importlib.util.spec_from_file_location("ble_mon", SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    mod.notify = MagicMock()
    mod.nova_config = types.SimpleNamespace(notify_local=MagicMock())
    return mod


bm = _load()


def _fresh_state():
    bm._last_presence.clear(); bm._alerted_macs.clear(); bm._watchlist_alerted.clear(); bm._battery_alerted.clear()
    bm.notify.reset_mock(); bm.nova_config.notify_local.reset_mock()
    conn = _Conn(); bm._conn = conn
    return conn


def _dev(mac, rssi=None, name="", battery=None, typ="ble_device"):
    return {"mac": mac, "name": name, "rssi": rssi, "battery": battery, "type": typ, "connected": False}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_mac_literals(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertEqual(re.findall(r"['\"](?:[0-9A-F]{2}:){5}[0-9A-F]{2}['\"]", SRC), [])   # MACs live in PG

    def test_value_sql_is_parameterized(self):
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))
        conn = _fresh_state()
        bm.check_unknown_devices([_dev("CC:00:00:00:00:01", -50, INJECT)])
        sql, params = conn.sql[0]
        self.assertNotIn("--injected", sql)
        self.assertIn("--injected", params[0])

    def test_watchlist_is_passive_only(self):
        self.assertNotIn("BleakClient", SRC)            # never connects to a device
        tail = SRC.split("VULNERABLE_BLE_WATCHLIST = [")[1].split("# ── Battery")[0]
        self.assertNotIn("connect", tail.replace("get_db()", ""))


class TestPerformance(unittest.TestCase):
    def test_fingerprints_and_classification_on_10k(self):
        t0 = time.perf_counter()
        fps = {bm.compute_ble_fingerprint(f"dev{i % 100}", [f"uuid{i % 7}"], [0x004C], None) for i in range(10_000)}
        for i in range(10_000):
            bm.classify_manufacturer({i % 0x1000: b""})
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertLessEqual(len(fps), 700)


class TestRetry(unittest.TestCase):
    def test_profiler_failure_fails_open(self):
        # RETRY GAP: scan_system_profiler/subprocess.run — one shot, [] on failure
        with patch.object(bm.subprocess, "run", side_effect=subprocess.TimeoutExpired("sp", 30)) as run:
            self.assertEqual(bm.scan_system_profiler(), [])
        self.assertEqual(run.call_count, 1)
        with patch.object(bm.subprocess, "run", return_value=MagicMock(returncode=1, stderr="boom")):
            self.assertEqual(bm.scan_system_profiler(), [])

    def test_db_insert_failure_resets_connection_and_keeps_going(self):
        # RETRY GAP: poll_cycle/insert_bluetooth — no retry; the cached conn is dropped for next cycle
        _fresh_state()

        async def no_ble():
            return []
        with patch.object(bm, "scan_system_profiler", return_value=[_dev("DD:00:00:00:00:01", -90)]), \
             patch.object(bm, "scan_ble", new=no_ble), \
             patch.object(bm, "insert_bluetooth", side_effect=RuntimeError("pg down")):
            asyncio.run(bm.poll_cycle())
        self.assertIsNone(bm._conn)

    def test_device_map_outage_degrades_to_empty(self):
        with patch.object(bm, "get_db", side_effect=RuntimeError("pg down")):
            self.assertEqual(bm._load_device_map(), ({}, {}))


class TestUnit(unittest.TestCase):
    def test_fingerprint_edges(self):
        self.assertIsNone(bm.compute_ble_fingerprint("", [], [], None))
        a = bm.compute_ble_fingerprint("Tile", ["B", "a"], [0x0842], -4)
        self.assertEqual(a, bm.compute_ble_fingerprint(" tile ", ["A", "b"], [0x0842], None))  # tx_power excluded
        self.assertEqual(len(a), 16)
        self.assertIsNone(bm.compute_cross_observer_fingerprint(None, None))
        self.assertEqual(bm.compute_cross_observer_fingerprint(["x"], [1]), bm.compute_cross_observer_fingerprint(["X"], [1]))

    def test_classify_manufacturer(self):
        self.assertEqual(bm.classify_manufacturer({}), ("unknown", "unknown"))
        self.assertEqual(bm.classify_manufacturer({0x004C: b"x"}), ("Apple", "phone/wearable"))
        self.assertEqual(bm.classify_manufacturer({0xBEEF: b""}), ("0xBEEF", "other"))

    def test_parse_bt_device(self):
        self.assertIsNone(bm._parse_bt_device("x", "notadict", True))
        self.assertIsNone(bm._parse_bt_device("x", {}, True))
        d = bm._parse_bt_device("Jordan's AirPods", {"device_address": "ee:00:00:00:00:01", "device_rssi": "-55",
                                                       "device_batteryLevelLeft": "80%"}, True)
        self.assertEqual((d["mac"], d["rssi"], d["battery"], d["type"]), ("EE:00:00:00:00:01", -55, 80, "headphones"))
        hp = bm._parse_bt_device("whatever", {"device_address": HP_DEN.lower()}, False)
        self.assertEqual((hp["type"], hp["name"]), ("homepod", "HomePod (den)"))


class TestIntegration(unittest.TestCase):
    def test_device_map_loaded_from_pg_table(self):
        self.assertIn("FROM telemetry.ble_device_map", SRC)
        self.assertEqual(bm.KNOWN_DEVICES[PHONE], ("Jordan iPhone", "phone", "jordan"))
        self.assertEqual(bm.HOMEPOD_ROOMS, {HP_OFFICE: "office", HP_DEN: "den"})

    def test_presence_writes_only_on_change(self):
        conn = _fresh_state()
        devs = [_dev(PHONE, -45), _dev(HP_DEN, -50)]
        bm.estimate_presence(devs); bm.estimate_presence(devs)
        inserts = [p for s, p in conn.sql if "telemetry.presence" in s]
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][:2], ("jordan", "den"))

    def test_bluetooth_rows_tagged_with_observer(self):
        _fresh_state()
        with patch.object(bm.psycopg2.extras, "execute_values") as ev:
            bm.insert_bluetooth([("m",) * 8])
            bm.insert_bluetooth([])          # empty is a no-op
        self.assertIn(f"'{bm.OBSERVER}'", ev.call_args.kwargs["template"])
        self.assertEqual(ev.call_count, 1)


class TestFunctional(unittest.TestCase):
    def test_poll_cycle_golden_path(self):
        _fresh_state()
        sp = [_dev(PHONE, -40, "Jordan iPhone", typ="phone"), _dev("FF:00:00:00:00:01", None, "Mouse", battery=10)]
        ble = [_dev("FF:00:00:00:00:01", -70, "Mouse"), _dev("CA:FE:00:00:00:01", -60, "KARR Alarm")]

        async def fake_ble():
            return ble
        with patch.object(bm, "scan_system_profiler", return_value=sp), patch.object(bm, "scan_ble", new=fake_ble), \
             patch.object(bm, "insert_bluetooth") as ins:
            asyncio.run(bm.poll_cycle())
        rows = ins.call_args.args[0]
        self.assertEqual(len(rows), 3)
        mouse = [r for r in rows if r[0] == "FF:00:00:00:00:01"][0]
        self.assertEqual((mouse[2], mouse[3]), (-70, 10))                  # rssi from BLE, battery from profiler
        titles = [c.args[0] for c in bm.notify.call_args_list]
        self.assertIn("Low Battery Alert", titles)
        self.assertIn("Possible KARR/SWDS vulnerable car alarm nearby", titles)
        bm.nova_config.notify_local.assert_called_once()

    def test_watchlist_pages_once_but_logs_every_sighting(self):
        conn = _fresh_state()
        d = [_dev("CA:FE:00:00:00:02", -70, "swds-unit")]
        bm.check_watchlist_devices(d); bm.check_watchlist_devices(d)
        self.assertEqual(bm.notify.call_count, 1)
        self.assertEqual(sum("vulnerable_ble_sightings" in s for s, _ in conn.sql), 2)

    def test_notify_failure_is_swallowed(self):
        _fresh_state()
        bm.notify.side_effect = RuntimeError("down")
        try:
            bm.check_batteries([_dev("AB:00:00:00:00:01", battery=5)])
        finally:
            bm.notify.side_effect = None
        self.assertIn("AB:00:00:00:00:01", bm._battery_alerted)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    asyncio.run(main())', SRC)
        code = ("import logging, psycopg2\n"
                "def _no(*a, **k): raise OSError('offline')\n"
                "psycopg2.connect = _no\n"
                "logging.FileHandler = lambda *a, **k: logging.NullHandler()\n"
                "import nova_ble_monitor as m\nprint(len(m.KNOWN_DEVICES))\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "0")


if __name__ == "__main__":
    unittest.main()
