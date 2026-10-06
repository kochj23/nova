#!/usr/bin/env python3
"""Tests for nova_replication_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_replication_monitor.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("replication_monitor_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rm = _load()
_PATCHES = []


def setUpModule():
    p = patch.object(rm.psycopg2, "connect", side_effect=AssertionError("unmocked PG")); p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


def _conn(recs=None, exc=None):
    cur = MagicMock()
    if exc:
        cur.execute.side_effect = exc
    cur.fetchall.return_value = recs or []
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


REC = ("192.168.1.2", "nova-core", "streaming", timedelta(milliseconds=12), timedelta(milliseconds=15),
       None, "async")


def _main(argv, conn):
    with patch.object(rm.psycopg2, "connect", return_value=conn) as c, patch.object(sys, "argv", ["x", *argv]), \
            patch.object(rm.psycopg2.extras, "execute_batch") as eb, redirect_stdout(io.StringIO()) as out:
        rm.main()
    return c, eb, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_insert_values_are_placeholders(self):
        conn, cur = _conn()
        with patch.object(rm.psycopg2.extras, "execute_batch") as eb:
            rm.write_rows(conn, [rm._row(note="x'); SELECT pg_sleep(9);--")])
        sql, values = eb.call_args.args[1:]
        self.assertNotIn("pg_sleep", sql)
        self.assertEqual(sql.count("%s"), len(rm.COLUMNS))
        self.assertIn("pg_sleep", values[0][-1])

    def test_partition_bounds_parameterized(self):
        conn, cur = _conn()
        rm.ensure_partition(conn, datetime(2026, 12, 15, tzinfo=timezone.utc))
        sql, params = cur.execute.call_args.args
        self.assertIn("replication_health_202612", sql)
        self.assertEqual(params[1], datetime(2027, 1, 1, tzinfo=timezone.utc))   # December rolls the year


class TestPerformance(unittest.TestCase):
    def test_collect_10k_standbys_fast(self):
        conn, _ = _conn([REC] * 10_000)
        t0 = time.perf_counter()
        rows = rm.collect(conn)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(rows), 10_000)


class TestRetry(unittest.TestCase):
    def test_query_failure_writes_sentinel_not_crash(self):
        # RETRY GAP: collect() — one query; failure becomes a sentinel row (cron re-runs next minute)
        conn, cur = _conn(exc=RuntimeError("conn reset"))
        with redirect_stdout(io.StringIO()):
            rows = rm.collect(conn)
        self.assertEqual(cur.execute.call_count, 1)
        self.assertEqual(rows[0]["note"], "query error: conn reset")

    def test_connect_failure_returns_cleanly(self):
        with patch.object(rm.psycopg2, "connect", side_effect=Exception("refused")), patch.object(sys, "argv", ["x"]), \
                redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(rm.main())
        self.assertIn("DB connect failed", out.getvalue())

    def test_insert_failure_returns_zero(self):
        conn, _ = _conn()
        with patch.object(rm.psycopg2.extras, "execute_batch", side_effect=RuntimeError("x")), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(rm.write_rows(conn, [rm._row()]), 0)


class TestUnit(unittest.TestCase):
    def test_ms(self):
        self.assertIsNone(rm._ms(None))
        self.assertEqual(rm._ms(timedelta(seconds=1.5)), 1500.0)
        self.assertIsNone(rm._ms("not an interval"))

    def test_row_ignores_unknown_keys(self):
        r = rm._row(state="x", bogus=1)
        self.assertEqual(set(r), set(rm.COLUMNS))
        self.assertEqual(r["state"], "x")
        self.assertEqual(rm.write_rows(MagicMock(), []), 0)


class TestIntegration(unittest.TestCase):
    def test_collect_then_write_shape(self):
        conn, cur = _conn([REC])
        rows = rm.collect(conn)
        self.assertEqual((rows[0]["client_addr"], rows[0]["write_lag_ms"], rows[0]["replay_lag_ms"]),
                         ("192.168.1.2", 12.0, None))
        with patch.object(rm.psycopg2.extras, "execute_batch") as eb:
            self.assertEqual(rm.write_rows(conn, rows), 1)
        self.assertIn("INSERT INTO telemetry.replication_health", eb.call_args.args[1])
        self.assertIn("pg_stat_replication", cur.execute.call_args_list[0].args[0])


class TestFunctional(unittest.TestCase):
    def test_golden_path_inserts(self):
        conn, _ = _conn([REC])
        c, eb, out = _main([], conn)
        self.assertIn("inserted 1 row(s)", out)
        conn.close.assert_called_once()
        self.assertTrue(conn.autocommit)

    def test_dry_run_never_writes(self):
        conn, _ = _conn([])
        c, eb, out = _main(["--dry-run"], conn)
        eb.assert_not_called()
        self.assertIn("DRY RUN — would insert 1 row(s)", out)
        self.assertIn("no standby connected", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(PATH), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_replication_monitor"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
