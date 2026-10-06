#!/usr/bin/env python3
"""Tests for nova_unas_disk_health.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_unas_disk_health.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ud = _load("ud_mod", SCRIPT)

TSV = ("VOL\tpool\t1000000\t400000\t600000\t40%\n"
       "RAID\tmd1\tactive\tok\n"
       "RAID\tmd2\tactive\tdegraded\n"
       "DISK\tsda\tWD Red Plus\tPASSED\t38\t12000\t0\n"
       "DISK\tsdb\t\t\t\t\t\n"
       "garbage line\n")


def _run(out):
    return types.SimpleNamespace(stdout=out, returncode=0)


class _Cur:
    def __init__(self):
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False; self.autocommit = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn("BatchMode=yes", SRC)      # key auth only, never an interactive password prompt

    def test_values_are_bound_not_interpolated(self):
        # the f-string only builds column names/placeholders from a fixed tuple; row values go via params
        cur = _Cur()
        with patch.object(ud.subprocess, "run", return_value=_run("DISK\tsda\tWD'; select 1; --\tPASSED\t38\t1\t0\n")), \
             patch.object(ud.psycopg2, "connect", return_value=_Conn(cur)), redirect_stdout(io.StringIO()):
            ud.main()
        sql, params = cur.sql[0]
        self.assertNotIn("select 1", sql)
        self.assertIn("WD'; select 1; --", params)
        self.assertEqual(sql.count("%s"), 14)

    def test_only_write_is_storage_metrics(self):
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"telemetry.storage_metrics"})


class TestPerformance(unittest.TestCase):
    def test_collect_parses_10k_lines_under_bound(self):
        big = "".join(f"DISK\tsd{i}\tModel {i}\tPASSED\t{30 + i % 10}\t{i}\t0\n" for i in range(10_000))
        with patch.object(ud.subprocess, "run", return_value=_run(big)):
            t0 = time.perf_counter()
            rows = ud.collect()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(rows), 10_000)


class TestRetry(unittest.TestCase):
    def test_ssh_timeout_escapes_main(self):
        # RETRY GAP: collect()/subprocess.run — a single SSH attempt; a timeout propagates (launchd re-runs
        # the job next interval) and nothing is written to PG.
        attempts = []

        def boom(*a, **k):
            attempts.append(1); raise subprocess.TimeoutExpired("ssh", 120)
        with patch.object(ud.subprocess, "run", side_effect=boom), patch.object(ud.psycopg2, "connect") as pg:
            with self.assertRaises(subprocess.TimeoutExpired):
                ud.main()
        self.assertEqual(len(attempts), 1)
        pg.assert_not_called()

    def test_empty_ssh_output_fails_closed_with_rc1(self):
        with patch.object(ud.subprocess, "run", return_value=_run("")), patch.object(ud.psycopg2, "connect") as pg, \
             redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ud.main(), 1)
        pg.assert_not_called()
        self.assertIn("no rows collected", err.getvalue())


class TestUnit(unittest.TestCase):
    def test_collect_parses_all_kinds(self):
        with patch.object(ud.subprocess, "run", return_value=_run(TSV)) as r:
            rows = ud.collect()
        self.assertEqual(r.call_args[0][0][:2], ["ssh", "-o"])
        self.assertEqual(r.call_args[1]["timeout"], 120)
        kinds = [x["component_type"] for x in rows]
        self.assertEqual(kinds, ["volume", "raid", "raid", "disk", "disk"])
        vol = rows[0]
        self.assertEqual((vol["total_bytes"], vol["used_pct"], vol["healthy"]), (1000000, 40.0, True))
        self.assertTrue(rows[1]["healthy"]); self.assertFalse(rows[2]["healthy"])
        self.assertEqual(json.loads(rows[2]["extra"]), {"degraded": True})
        sda = rows[3]
        self.assertEqual((sda["component_name"], sda["temp_c"], sda["healthy"], sda["smart_status"]), ("WD Red Plus", 38.0, True, "PASSED"))
        self.assertEqual(json.loads(sda["extra"])["power_on_hours"], "12000")
        sdb = rows[4]
        self.assertEqual((sdb["component_name"], sdb["temp_c"], sdb["healthy"], sdb["status"]), ("sdb", None, None, None))

    def test_volume_over_90_pct_unhealthy_and_short_rows_skipped(self):
        with patch.object(ud.subprocess, "run", return_value=_run("VOL\tpool\t10\t9\t1\t95%\nVOL\tshort\nRAID\tmd0\nDISK\tsda\n")):
            rows = ud.collect()
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["healthy"])

    def test_raid_inactive_is_unhealthy_even_if_not_degraded(self):
        with patch.object(ud.subprocess, "run", return_value=_run("RAID\tmd0\tinactive\tok\n")):
            self.assertFalse(ud.collect()[0]["healthy"])


class TestIntegration(unittest.TestCase):
    def test_remote_script_targets_the_pool_and_smart_sources(self):
        self.assertIn(ud.POOL, ud.REMOTE)
        for needle in ("/proc/mdstat", "smartctl", "df -B1"):
            self.assertIn(needle, ud.REMOTE)
        self.assertEqual(ud.SSH[-1], f"root@{ud.HOST}")
        self.assertIn("nova_ops", ud.DSN)

    def test_rows_get_source_and_host_then_insert_columns_match(self):
        cur = _Cur()
        with patch.object(ud.subprocess, "run", return_value=_run(TSV)), \
             patch.object(ud.psycopg2, "connect", return_value=_Conn(cur)), redirect_stdout(io.StringIO()):
            ud.main()
        sql, params = cur.sql[0]
        self.assertIn("INSERT INTO telemetry.storage_metrics (ts,source,host,component_type", sql)
        self.assertEqual(params[0], "unas-ssh"); self.assertEqual(params[1], ud.HOST)
        self.assertEqual(len(params), 14)


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_every_row_and_reports(self):
        cur = _Cur(); conn = _Conn(cur)
        with patch.object(ud.subprocess, "run", return_value=_run(TSV)), \
             patch.object(ud.psycopg2, "connect", return_value=conn), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ud.main(), 0)
        self.assertEqual(len(cur.sql), 5)
        self.assertTrue(conn.autocommit); self.assertTrue(conn.closed)
        self.assertIn("wrote 5 rows (2 disks)", out.getvalue())

    def test_pg_down_raises_after_collect(self):
        with patch.object(ud.subprocess, "run", return_value=_run(TSV)), \
             patch.object(ud.psycopg2, "connect", side_effect=OSError("pg down")):
            with self.assertRaises(OSError):
                ud.main()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_unas_disk_health"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
