#!/usr/bin/env python3
"""Tests for nova_disk_forecast.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
psycopg2.connect / execute_batch are mocked with a SQL-dispatching fake cursor; nothing hits PG."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_disk_forecast.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_disk_forecast_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


df = _load()
df.log = lambda m: None
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _days(*pcts):
    return [(T0 + timedelta(days=i), p) for i, p in enumerate(pcts)]


class _Cur:
    def __init__(self, data, fail=()):
        self.data, self.fail, self.sql = data, fail, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append(sql)
        for f in self.fail:
            if f in sql:
                raise RuntimeError(f"{f} missing")
        self._last = next((v for k, v in self.data.items() if k in sql), [])

    def fetchall(self):
        return self._last


class _Conn:
    def __init__(self, data=None, fail=()):
        self.cur = _Cur(data or {}, fail)
        self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


DATA = {
    "telemetry.storage_metrics": [("nas", "vol1", T0, 50), ("nas", "vol1", T0 + timedelta(days=10), 60)],
    "snmp_metrics": [("sw", "10.0.0.2", "disk_storage_used.1", T0, 40), ("sw", "10.0.0.2", "disk_storage_size.1", T0, 100),
                     ("sw", "10.0.0.2", "disk_storage_used.1", T0 + timedelta(days=1), 41),
                     ("sw", "10.0.0.2", "disk_storage_size.1", T0 + timedelta(days=1), 100),
                     ("sw", "10.0.0.2", "garbage", T0, 1)],
    "FROM node_status": [("studio", "192.168.1.6", 70.0)],
    "SELECT host, used_pct, ts FROM telemetry.disk_forecast": [],
}


class TestSecurity(unittest.TestCase):
    def test_no_credentials(self):
        self.assertIsNone(re.search(r"(password|token|secret)\s*=\s*['\"]", SRC, re.I))

    def test_only_constants_are_interpolated_into_sql(self):
        # every %-format into SQL uses the module constant, never user input; inserts are %s-parameterized
        self.assertEqual(set(re.findall(r'"""\s*%\s*(\w+)', SRC)), {"LOOKBACK_DAYS"})
        self.assertIsInstance(df.LOOKBACK_DAYS, int)
        self.assertIn('ph = ", ".join(["%s"] * len(COLUMNS))', SRC)

    def test_dry_run_never_writes(self):
        conn = _Conn(DATA)
        with patch.object(df.psycopg2, "connect", return_value=conn), \
             patch.object(df.psycopg2.extras, "execute_batch") as eb, patch.object(sys, "argv", ["x", "--dry-run"]):
            df.main()
        eb.assert_not_called()
        self.assertTrue(conn.closed)


class TestPerformance(unittest.TestCase):
    def test_ols_on_10k_points_fast(self):
        series = [(T0 + timedelta(hours=i), 10 + i * 0.001) for i in range(10_000)]
        t0 = time.perf_counter()
        r = df._forecast_row("s", "h", "v", series, 95.0)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertAlmostEqual(r["growth_pct_per_day"], 0.024, places=4)


class TestRetry(unittest.TestCase):
    def test_each_source_fails_open_independently(self):
        # RETRY GAP: from_storage_metrics/from_snmp/node_status_snapshots — one query each; failure -> []
        conn = _Conn(DATA, fail=("telemetry.storage_metrics", "snmp_metrics"))
        self.assertEqual(df.from_storage_metrics(conn, 95), [])
        self.assertEqual(df.from_snmp(conn, 95), [])
        self.assertEqual(len(df.node_status_snapshots(conn, 95)), 1)

    def test_connect_failure_and_insert_failure_are_safe(self):
        with patch.object(df.psycopg2, "connect", side_effect=df.psycopg2.OperationalError("down")) as c, \
             patch.object(sys, "argv", ["x"]):
            self.assertIsNone(df.main())
        self.assertEqual(c.call_count, 1)
        with patch.object(df.psycopg2.extras, "execute_batch", side_effect=RuntimeError("x")):
            self.assertEqual(df.write_rows(_Conn(), [df._row("s", "h", "v")]), 0)


class TestUnit(unittest.TestCase):
    def test_ols_slope_edges(self):
        self.assertIsNone(df._ols_slope([(0, 1)]))
        self.assertIsNone(df._ols_slope([(1, 1), (1, 5)]))
        self.assertAlmostEqual(df._ols_slope([(0, 0), (1, 2), (2, 4)]), 2.0)

    def test_forecast_branches(self):
        self.assertIn("insufficient", df._forecast_row("s", "h", "v", _days(50), 95)["note"])
        self.assertEqual(df._forecast_row("s", "h", "v", _days(50, 60), 95)["days_until_full"], 3.5)
        self.assertEqual(df._forecast_row("s", "h", "v", _days(96, 97), 95)["days_until_full"], 0.0)
        self.assertIn("flat", df._forecast_row("s", "h", "v", _days(60, 59), 95)["note"])
        self.assertIn("negligible", df._forecast_row("s", "h", "v", _days(10, 10.0002), 95)["note"])
        self.assertIn("degenerate", df._forecast_row("s", "h", "v", [(T0, 1), (T0, 2)], 95)["note"])

    def test_used_pct_clamped(self):
        self.assertEqual(df._forecast_row("s", "h", "v", _days(101, 102), 110)["used_pct"], 100.0)


class TestIntegration(unittest.TestCase):
    def test_snmp_used_over_size_series(self):
        rows = df.from_snmp(_Conn(DATA), 95)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["host"], rows[0]["volume"], rows[0]["used_pct"]), ("sw", "hrStorage.1", 41.0))

    def test_node_status_uses_prior_snapshots(self):
        data = dict(DATA)
        data["SELECT host, used_pct, ts FROM telemetry.disk_forecast"] = [("studio", 60.0, df.NOW - timedelta(days=10))]
        (row,) = df.node_status_snapshots(_Conn(data), 95)
        self.assertEqual(row["n_points"], 2)
        self.assertAlmostEqual(row["growth_pct_per_day"], 1.0, places=3)

    def test_partition_named_for_month(self):
        conn = _Conn()
        df.ensure_partition(conn, datetime(2026, 12, 15, tzinfo=timezone.utc))
        self.assertIn("disk_forecast_202612", conn.cur.sql[-1])


class TestFunctional(unittest.TestCase):
    def test_main_writes_all_sources(self):
        conn = _Conn(DATA)
        with patch.object(df.psycopg2, "connect", return_value=conn), \
             patch.object(df.psycopg2.extras, "execute_batch") as eb, patch.object(sys, "argv", ["x", "--target", "90"]):
            df.main()
        sql, values = eb.call_args[0][1], eb.call_args[0][2]
        self.assertIn("INSERT INTO telemetry.disk_forecast", sql)
        self.assertEqual(sorted(v[1] for v in values), ["node_status", "snmp", "storage_metrics"])
        self.assertTrue(all(v[7] == 90.0 for v in values))
        self.assertTrue(conn.closed)


class TestFrame(unittest.TestCase):
    def test_help_and_import(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--target", r.stdout)
        r = subprocess.run([sys.executable, "-c", "import nova_disk_forecast"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))


if __name__ == "__main__":
    unittest.main()
