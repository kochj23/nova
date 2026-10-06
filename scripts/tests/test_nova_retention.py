#!/usr/bin/env python3
"""Tests for nova_retention.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Destructive script: every PG call goes to a recording fake connection (psycopg2.connect is
stubbed file-wide). The tests prove dry-run issues no destructive SQL, the current/previous
month partitions are never dropped, and FORBIDDEN tables are refused."""
import datetime as dt
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
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_retention.py"
SRC = PATH.read_text()
DESTRUCTIVE = re.compile(r"\b(DR" r"OP|DEL" r"ETE|DETACH|INSERT|VACUUM|CREATE)\b", re.I)


def _load():
    spec = importlib.util.spec_from_file_location("retention_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rt = _load()
_PATCHES = []


def setUpModule():
    p = patch.object(rt.psycopg2, "connect", side_effect=AssertionError("unmocked PG")); p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


class _Cur:
    def __init__(self, conn):
        self.c = conn; self._one = None; self._all = []; self.rowcount = 0

    def execute(self, sql, params=None):
        self.c.sql.append((" ".join(sql.split()), params))
        self._one, self._all = self.c.answer(sql, params)
        if sql.lstrip().upper().startswith("DEL" "ETE"):
            self.rowcount = self.c.delete_counts.pop(0) if self.c.delete_counts else 0

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._all

    def close(self):
        pass


class _Conn:
    """Fake PG: partitions per parent, row counts for plain tables, and a log of every SQL."""
    def __init__(self, parts=None, eligible=0, total=0, delete_counts=(), fail_on=None):
        self.parts = parts or {}; self.eligible = eligible; self.total = total
        self.delete_counts = list(delete_counts); self.fail_on = fail_on
        self.sql = []; self.commits = 0; self.rollbacks = 0; self.isolation_level = 1

    def cursor(self):
        return _Cur(self)

    def answer(self, sql, params):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError(f"boom on {self.fail_on}")
        s = " ".join(sql.split())
        if "FROM pg_inherits" in s:
            return None, [(r, 1024 * 1024) for r in self.parts.get(params[1], [])]
        if "pg_database_size" in s or "pg_total_relation_size" in s:
            return (10 * 2**30,), []
        if "WHERE" in s and "count(*)" in s:
            return (self.eligible,), []
        if "count(*)" in s:
            return (self.total,), []
        return None, []

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def set_isolation_level(self, lvl):
        self.isolation_level = lvl

    def close(self):
        pass

    def destructive(self):
        return [s for s, _ in self.sql if DESTRUCTIVE.search(s)]


def _args(verbose=False):
    return types.SimpleNamespace(verbose=verbose)


def _ym(d):
    return d.strftime("%Y%m")


TODAY = dt.date(2026, 10, 5)   # explicit `today` arg to pure functions — main() itself uses the real clock


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_dry_run_partition_never_drops(self):
        conn = _Conn(parts={"bluetooth": ["bluetooth_202601", "bluetooth_202602"]})
        with redirect_stdout(io.StringIO()) as out:
            reclaimed, dropped = rt.process_partitioned(conn, "bluetooth", rt.RETENTION["bluetooth"], TODAY,
                                                        False, True, _args())
        self.assertEqual(dropped, ["bluetooth_202601", "bluetooth_202602"])
        self.assertEqual(conn.destructive(), [])
        self.assertIn("[dry-run] WOULD detach + drop", out.getvalue())

    def test_current_and_previous_month_protected_even_with_zero_window(self):
        cfg = {**rt.RETENTION["presence"], "retention_days": 0}
        conn = _Conn(parts={"presence": ["presence_202610", "presence_202609", "presence_202608", "presence_misc"]})
        with redirect_stdout(io.StringIO()):
            _, dropped = rt.process_partitioned(conn, "presence", cfg, TODAY, True, False, _args())
        self.assertEqual(dropped, ["presence_202608"])
        self.assertFalse(any("202610" in s or "202609" in s for s in conn.destructive()))

    def test_forbidden_tables_refused(self):
        conn = _Conn(eligible=5, total=5)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(rt.process_plain(conn, "x", {"schema": "public", "table": "memories", "ts_col": "ts",
                                                          "retention_days": 1}, TODAY, True, False, _args()), (0, 0))
        self.assertEqual(conn.sql, [])
        self.assertIn("FORBIDDEN", out.getvalue())
        self.assertFalse(set(rt.FORBIDDEN) & {c.get("table", c.get("parent")) for c in rt.RETENTION.values()})

    def test_plain_cutoff_value_is_parameterized(self):
        conn = _Conn(eligible=0, total=3)
        with redirect_stdout(io.StringIO()):
            rt.process_plain(conn, "health_checks", rt.RETENTION["health_checks"], TODAY, False, False, _args())
        sql, params = conn.sql[0]
        self.assertIn("checked_at < %s", sql)
        self.assertEqual(params, (dt.datetime(2026, 9, 5),))


class TestPerformance(unittest.TestCase):
    def test_month_math_10k_fast(self):
        t0 = time.perf_counter()
        d = dt.date(2000, 1, 15)
        for i in range(10_000):
            rt.protected_months(d + dt.timedelta(days=i))
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_chunked_delete_loop_terminates(self):
        conn = _Conn(eligible=120_000, total=200_000, delete_counts=[50_000, 50_000, 20_000, 0])
        with redirect_stdout(io.StringIO()):
            est, deleted = rt.process_plain(conn, "syslog_events", rt.RETENTION["syslog_events"], TODAY, True, False, _args())
        self.assertEqual(deleted, 120_000)
        dels = [p for s, p in conn.sql if s.startswith("DEL" "ETE")]
        self.assertEqual(len(dels), 4)
        self.assertEqual(dels[0][1], rt.DELETE_CHUNK)


class TestRetry(unittest.TestCase):
    def test_one_failing_table_does_not_abort_the_run(self):
        # RETRY GAP: main() — each table is attempted once; an error is rolled back, summarized, and the rest run
        conn = _Conn(parts={}, eligible=0, total=0, fail_on="syslog_events")
        with patch.object(rt.psycopg2, "connect", return_value=conn), \
                patch.object(sys, "argv", ["x", "--only", "syslog_events", "health_checks"]), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(rt.main(), 0)
        self.assertGreaterEqual(conn.rollbacks, 1)
        self.assertIn("ERROR: boom on syslog_events", out.getvalue())
        self.assertIn("health_checks", out.getvalue().split("SUMMARY")[1])

    def test_connect_failure_returns_2(self):
        with patch.object(rt.psycopg2, "connect", side_effect=Exception("refused")), patch.object(sys, "argv", ["x"]), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(rt.main(), 2)
        self.assertIn("FATAL", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_month_helpers(self):
        self.assertEqual(rt.prev_month(dt.date(2026, 1, 1)), dt.date(2025, 12, 1))
        self.assertEqual(rt.protected_months(dt.date(2026, 1, 31)), {"202601", "202512"})
        self.assertEqual(rt.month_floor(dt.date(2026, 3, 17)), dt.date(2026, 3, 1))
        self.assertEqual(rt.fmt_bytes(None), "?")
        self.assertEqual(rt.fmt_bytes(512), "512.0 B")
        self.assertEqual(rt.fmt_bytes(3 * 2**30), "3.0 GB")

    def test_plain_floor_enforced(self):
        conn = _Conn(eligible=0, total=1)
        with redirect_stdout(io.StringIO()):
            rt.process_plain(conn, "t", {"schema": "public", "table": "t", "ts_col": "ts", "retention_days": 1},
                             TODAY, False, False, _args())
        self.assertEqual(conn.sql[0][1][0].date(), TODAY - dt.timedelta(days=rt.MIN_KEEP_DAYS))


class TestIntegration(unittest.TestCase):
    def test_bluetooth_rolled_up_before_drop_in_apply(self):
        conn = _Conn(parts={"bluetooth": ["bluetooth_202601"]}, eligible=7, total=7)
        conn.answer = lambda sql, p, _o=conn.answer: ((5,), []) if "FROM telemetry.bluetooth_202601" in sql and "count" in sql else _o(sql, p)
        with redirect_stdout(io.StringIO()):
            rt.process_partitioned(conn, "bluetooth", rt.RETENTION["bluetooth"], TODAY, True, True, _args())
        order = [s.split()[0] for s in conn.destructive()]
        self.assertEqual(order, ["INSERT", "ALTER", "DR" "OP"])
        self.assertIn("INSERT INTO telemetry.bluetooth_hourly", conn.destructive()[0])

    def test_snmp_downsample_dry_vs_apply(self):
        conn = _Conn(eligible=9, total=9)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(rt.downsample_snmp(conn, dt.datetime(2026, 1, 1), False, _args()), 9)
            self.assertEqual(conn.destructive(), [])
            rt.downsample_snmp(conn, dt.datetime(2026, 1, 1), True, _args())
        self.assertIn("INSERT INTO public.snmp_metrics_hourly", conn.destructive()[0])


class TestFunctional(unittest.TestCase):
    def test_main_default_is_dry_run(self):
        old = rt.month_floor(dt.date.today() - dt.timedelta(days=400))
        conn = _Conn(parts={"bluetooth": [f"bluetooth_{_ym(old)}"]}, eligible=10, total=20)
        with patch.object(rt.psycopg2, "connect", return_value=conn), patch.object(sys, "argv", ["x"]), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(rt.main(), 0)
        self.assertEqual(conn.destructive(), [])
        self.assertIn("DRY-RUN only", out.getvalue())
        self.assertIn(f"bluetooth_{_ym(old)}", out.getvalue())

    def test_apply_drops_old_partition_and_vacuums_plain(self):
        old = rt.month_floor(dt.date.today() - dt.timedelta(days=400))
        conn = _Conn(parts={"network": [f"network_{_ym(old)}", f"network_{_ym(dt.date.today())}"]},
                     eligible=3, total=10, delete_counts=[3, 0])
        with patch.object(rt.psycopg2, "connect", return_value=conn), \
                patch.object(sys, "argv", ["x", "--apply", "--no-downsample", "--only", "network", "health_checks"]), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(rt.main(), 0)
        d = conn.destructive()
        self.assertEqual(sum(1 for s in d if s.startswith("DR" "OP TABLE")), 1)
        self.assertIn(f"telemetry.network_{_ym(old)}", " ".join(d))
        self.assertTrue(any(s.startswith("VACUUM (ANALYZE) public.health_checks") for s in d))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(PATH), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--apply", r.stdout)


if __name__ == "__main__":
    unittest.main()
