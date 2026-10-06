#!/usr/bin/env python3
"""Tests for nova_karr_daily_report.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2         # noqa: F401
import psycopg2.extras  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_karr_daily_report.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="karr_test_"))

import nova_notify  # noqa: E402,F401  (real module locked in before the load)


def _load():
    spec = importlib.util.spec_from_file_location("karr", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("psycopg2.connect", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


kr = _load()
kr.LOG_FILE = TMP / "karr_daily_report.log"
kr.notify = MagicMock(return_value=True)          # never reach Slack


def _hit(mac, subject="KARR", hh=13, rssi=-70):
    return {"observed_at": datetime(2026, 1, 2, hh, 5), "subject": subject, "observation": "o",
            "metadata": {"mac": mac, "rssi": rssi, "confidence": "probable"}}


def _conn(hits, persist=()):
    cur = MagicMock(); cur.fetchall.side_effect = [hits, list(persist)]
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn, cur


def _run(conn):
    with patch.object(kr.psycopg2, "connect", return_value=conn), redirect_stdout(io.StringIO()):
        kr.run()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", kr.DSN)

    def test_mac_list_is_bound_not_interpolated(self):
        evil = "aa' OR '1'='1"
        conn, cur = _conn([_hit(evil)])
        _run(conn)
        sql, params = cur.execute.call_args_list[1][0]
        self.assertNotIn(evil, sql)
        self.assertEqual(params, ([evil],))
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')

    def test_read_only(self):
        self.assertNotRegex(SRC, r"\b(INSERT INTO|UPDATE\s+\w+\s+SET|DELETE FROM)\b")


class TestPerformance(unittest.TestCase):
    def test_10k_hits_rollup_is_bounded(self):
        hits = [_hit(f"mac{i % 500}", hh=i % 24) for i in range(10_000)]
        persist = [{"device_mac": f"mac{i}", "days_seen": 3, "first_seen": datetime(2026, 1, 1)} for i in range(500)]
        conn, _ = _conn(hits, persist)
        kr.notify.reset_mock()
        t0 = time.perf_counter()
        _run(conn)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(kr.notify.call_args.kwargs["body"].count("RECURRING"), 10_000)


class TestRetry(unittest.TestCase):
    def test_notify_failure_fails_open(self):
        # RETRY GAP: run()/notify — one attempt; a Slack failure is logged, never raised
        conn, _ = _conn([])
        with patch.object(kr, "notify", MagicMock(side_effect=RuntimeError("slack down"))) as n:
            _run(conn)
        self.assertEqual(n.call_count, 1)
        self.assertIn("Notify failed: slack down", kr.LOG_FILE.read_text())

    def test_pg_down_posts_nothing(self):
        # RETRY GAP: run()/psycopg2.connect — no retry; the scheduler sees the error and no half-report is posted
        kr.notify.reset_mock()
        with patch.object(kr.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")) as c:
            with self.assertRaises(psycopg2.OperationalError):
                kr.run()
        self.assertEqual(c.call_count, 1)
        kr.notify.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_once_vs_recurring_tagging(self):
        conn, _ = _conn([_hit("m1"), _hit("m2")],
                        [{"device_mac": "m1", "days_seen": 4, "first_seen": datetime(2026, 1, 1)},
                         {"device_mac": "m2", "days_seen": 1, "first_seen": datetime(2026, 1, 2)}])
        kr.notify.reset_mock(); _run(conn)
        body = kr.notify.call_args.kwargs["body"]
        self.assertIn("m1 RSSI=-70 — RECURRING, 4 distinct days, first seen 01-01", body)
        self.assertIn("m2 RSSI=-70 — seen once so far", body)

    def test_missing_metadata_is_tolerated(self):
        h = _hit("x"); h["metadata"] = None
        conn, cur = _conn([h])
        kr.notify.reset_mock(); _run(conn)
        self.assertIn("? RSSI=? — seen once", kr.notify.call_args.kwargs["body"])
        self.assertEqual(cur.execute.call_args_list[1][0][1], ([],))

    def test_log_goes_to_redirected_file(self):
        with redirect_stdout(io.StringIO()):
            kr.log("unit-line")
        self.assertIn("unit-line", kr.LOG_FILE.read_text())


class TestIntegration(unittest.TestCase):
    def test_reads_the_right_tables_and_uses_shared_notify(self):
        self.assertIn("FROM shared_observations", SRC)
        self.assertIn("FROM vulnerable_ble_sightings", SRC)
        self.assertIn("from nova_notify import notify", SRC)
        self.assertIn("RealDictCursor", SRC)


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_rollup(self):
        conn, _ = _conn([_hit("m1", subject="KARR")],
                        [{"device_mac": "m1", "days_seen": 2, "first_seen": datetime(2026, 1, 1)}])
        kr.notify.reset_mock(); _run(conn)
        args, kw = kr.notify.call_args
        self.assertTrue(args[0].startswith("Vulnerable BLE Watchlist Report ("))
        self.assertEqual((kw["category"], kw["dedup_key"]), ("security", "karr-daily-report"))
        self.assertIn("1 watchlist detection(s)", kw["body"])
        conn.close.assert_called_once()

    def test_no_hits_posts_all_clear(self):
        conn, cur = _conn([])
        kr.notify.reset_mock(); _run(conn)
        self.assertIn("No watchlist matches", kr.notify.call_args.kwargs["body"])
        self.assertEqual(cur.execute.call_count, 1)           # persistence query skipped


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        # no --help: a bare run queries PG, so the smoke is an import
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_karr_daily_report"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
