#!/usr/bin/env python3
"""Tests for nova_storage_metrics.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
The Synology session and UNAS client (which read Keychain creds) are replaced via patch.dict(sys.modules)
stubs, and psycopg2.connect is mocked: no NAS, no Keychain, no PG."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_storage_metrics.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("storage_metrics_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sm = _load()
sm.log = MagicMock()
sm.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=OSError("offline: pg stubbed")),
                                    extras=sm.psycopg2.extras)

SYSINFO = {"model": "RS1221+", "firmware_ver": "DSM 7.2", "sys_temp": 44, "sys_tempwarn": False, "up_time": "3d"}
UTIL = {"cpu": {"user_load": 3, "system_load": 2, "other_load": 1, "1min_load": 10, "5min_load": 8, "15min_load": 6},
        "memory": {"memory_size": 1000, "avail_real": 250, "real_usage": 70},
        "network": [{"device": "eth0", "rx": 1, "tx": 1}, {"device": "total", "rx": 500, "tx": 700}],
        "disk": {"total": {"read_byte": 11, "write_byte": 22}}}
STORAGE = {"volumes": [{"id": "volume_1", "status": "normal", "fs_type": "btrfs", "size": {"total": "1000", "used": "250"}}],
           "storagePools": [{"id": "reuse_1", "status": "background_scrubbing", "desc": "Pool 1", "disks": ["d1"]}],
           "disks": [{"id": "sata1", "status": "normal", "smart_status": "normal", "temp": 36, "size_total": "4000",
                      "remain_life": {"value": -1}, "model": " WD ", "serial": "S1"},
                     {"id": "sata2", "status": "crashed", "smart_status": "failing", "temp": 50}]}
UPS = {"model": "APC", "status": "Online", "battery_charge": "100", "battery_runtime": "1200", "load": "12.5"}


def _syno_stub(fail=None):
    mod = types.ModuleType("nova_synology_monitor")
    class SynoSession:
        def __enter__(self): return "session"
        def __exit__(self, *e): return False
    mod.SynoSession = SynoSession
    def mk(val, name):
        def f(session):
            if fail == name:
                raise RuntimeError(f"{name} 500")
            return val
        return f
    mod.get_system_info, mod.get_utilization = mk(SYSINFO, "sys"), mk(UTIL, "util")
    mod.get_storage, mod.get_ups = mk(STORAGE, "storage"), mk(UPS, "ups")
    return mod


def _unas_stub(snap=None, exc=None):
    mod = types.ModuleType("nova_unas_client")
    class UNASError(Exception): pass
    class UNASClient:
        def health_snapshot(self):
            if exc:
                raise exc
            return snap
    mod.UNASClient, mod.UNASError = UNASClient, UNASError
    return mod


SNAP = {"storage": {"total_bytes": 100, "used_bytes": 40, "free_bytes": 60, "used_pct": 40.0, "status": "healthy"},
        "device": {"model": "UNAS Pro 8", "state": "connected"},
        "shares": [{"name": "nas", "id": "s1", "used_bytes": 30, "status": "active"},
                   {"name": "old", "id": "s2", "status": "deleting"}]}


def _mods(**kw):
    return patch.dict(sys.modules, {"nova_synology_monitor": _syno_stub(kw.get("fail")),
                                    "nova_unas_client": _unas_stub(kw.get("snap", SNAP), kw.get("unas_exc"))})


def _by(rows, ctype):
    return [r for r in rows if r["component_type"] == ctype]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn("SynoSession", SRC)                   # creds reused from the Keychain-backed monitors
        self.assertNotRegex(SRC, r"(?i)(passwd|password)\s*=")

    def test_sql_values_are_parameterized(self):
        # the only f-strings in SQL are identifiers (fixed column list, YYYYMM suffix); values use %s
        cur = MagicMock()
        conn = MagicMock(); conn.cursor.return_value.__enter__.return_value = cur
        sm.ensure_partition(conn, datetime(2026, 12, 15, tzinfo=timezone.utc))
        sql, params = cur.execute.call_args[0]
        self.assertIn("storage_metrics_202612", sql)
        self.assertEqual(params[1].year, 2027)               # December rolls over to January
        self.assertIn("FROM (%s) TO (%s)", sql)


class TestPerformance(unittest.TestCase):
    def test_10k_disks_to_rows(self):
        storage = {"disks": [{"id": f"d{i}", "status": "normal", "smart_status": "normal", "temp": 30} for i in range(10_000)]}
        t0 = time.perf_counter()
        rows = sm._syno_disk_rows(storage)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(rows), 10_000)


class TestRetry(unittest.TestCase):
    def test_one_failing_call_does_not_block_the_rest(self):
        # RETRY GAP: collect_synology/_safe — each DSM call is one attempt; a failure drops only that slice
        with _mods(fail="storage"):
            rows = sm.collect_synology()
        self.assertEqual([r["component_type"] for r in rows], ["system", "ups"])

    def test_unas_down_and_db_down_fail_open(self):
        # RETRY GAP: collect_unas / write_rows — single attempt; failures return [] / 0, never raise
        with _mods(unas_exc=ConnectionError("unas down")):
            self.assertEqual(sm.collect_unas(), [])
        self.assertEqual(sm.write_rows([sm._row("x", "h", "system", "s")]), 0)


class TestUnit(unittest.TestCase):
    def test_row_and_to_int(self):
        r = sm._row("s", "h", "disk", "d1", temp_c=40, bogus=1)
        self.assertEqual(list(r), sm.COLUMNS)
        self.assertEqual((r["temp_c"], r["used_pct"]), (40, None))
        self.assertNotIn("bogus", r)
        self.assertEqual((sm._to_int("7"), sm._to_int("x", -1), sm._to_int(None)), (7, -1, None))

    def test_system_row_math(self):
        r = sm._syno_system_row(SYSINFO, UTIL)[0]
        self.assertEqual((r["cpu_pct"], r["ram_pct"], r["net_rx_bps"], r["disk_write_bps"], r["healthy"]),
                         (6, 75.0, 500, 22, True))
        r = sm._syno_system_row(None, {"network": [{"device": "eth0"}]})[0]
        self.assertIsNone(r["net_rx_bps"])                   # no 'total' entry -> unknown, not zero

    def test_disk_pool_volume_ups_rows(self):
        disks = sm._syno_disk_rows(STORAGE)
        self.assertEqual([(d["healthy"], d["remain_life_pct"], d["component_name"]) for d in disks],
                         [(True, None, "WD"), (False, None, None)])
        self.assertTrue(sm._syno_pool_rows(STORAGE)[0]["healthy"])
        v = sm._syno_volume_rows(STORAGE)[0]
        self.assertEqual((v["free_bytes"], v["used_pct"]), (750, 25.0))
        self.assertEqual(sm._syno_ups_rows({"status": "status_unknown"}), [])
        self.assertEqual(sm._syno_ups_rows(UPS)[0]["ups_runtime_s"], 1200)


class TestIntegration(unittest.TestCase):
    def test_collect_both_devices(self):
        with _mods():
            syno, unas = sm.collect_synology(), sm.collect_unas()
        self.assertEqual(sorted({r["component_type"] for r in syno}), ["disk", "pool", "system", "ups", "volume"])
        self.assertEqual([(r["component_id"], r["healthy"]) for r in _by(unas, "share")], [("nas", True), ("old", False)])
        self.assertTrue(_by(unas, "system")[0]["healthy"])


class TestFunctional(unittest.TestCase):
    def test_main_inserts_all_rows(self):
        cur = MagicMock()
        conn = MagicMock(); conn.cursor.return_value.__enter__.return_value = cur
        with _mods(), patch.object(sm.psycopg2, "connect", return_value=conn, side_effect=None), \
             patch.object(sm.psycopg2.extras, "execute_batch") as eb, patch.object(sys, "argv", ["x"]):
            sm.main()
        sql, values = eb.call_args[0][1], eb.call_args[0][2]
        self.assertTrue(sql.startswith("INSERT INTO telemetry.storage_metrics (ts, source"))
        self.assertEqual(len(values), 9)                     # 1 sys + 1 vol + 1 pool + 2 disks + 1 ups + 1 unas + 2 shares
        self.assertTrue(all(len(v) == len(sm.COLUMNS) for v in values))

    def test_dry_run_never_connects(self):
        with _mods(), patch.object(sys, "argv", ["x", "--dry-run", "--unas"]):
            sm.psycopg2.connect.reset_mock()
            sm.main()
        sm.psycopg2.connect.assert_not_called()

    def test_rows_but_no_insert_exits_1(self):
        with _mods(), patch.object(sys, "argv", ["x", "--unas"]), self.assertRaises(SystemExit) as e:
            sm.main()
        self.assertEqual(e.exception.code, 1)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(sm.main))


if __name__ == "__main__":
    unittest.main()
