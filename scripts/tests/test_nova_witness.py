#!/usr/bin/env python3
"""Tests for nova_witness.py — the 7 house categories (Security, Performance, Retry, Unit,
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
PATH = SCRIPTS / "nova_witness.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("witness_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wt = _load()
_PATCHES = []


def setUpModule():
    p = patch.object(wt, "_conn", side_effect=AssertionError("unmocked PG")); p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


def _conn(row=None):
    cur = MagicMock(); cur.fetchone.return_value = row
    conn = MagicMock(); conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


CARD = dict(witness="memory-health", claim="memory server answers", green_means="HTTP 200 + count",
            injected_fault="stopped the service", observed_red="HTTP 503 at 00:32Z",
            observed_green_on_removal="HTTP 200 at 00:35Z", recorded_by="claude")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_witness_lookup_and_upsert_parameterized(self):
        evil = "w'); SELECT pg_sleep(9);--"
        conn, cur = _conn(None)
        wt.witness_state(evil, conn=conn)
        sql, params = cur.execute.call_args.args
        self.assertNotIn(evil, sql)
        self.assertEqual(params, (evil,))
        conn, cur = _conn()
        wt.record_proven_red(**{**CARD, "witness": evil}, conn=conn)
        sql, params = cur.execute.call_args.args
        self.assertNotIn(evil, sql)
        self.assertEqual(params[0], evil)


class TestPerformance(unittest.TestCase):
    def test_check_grain_10k_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            wt.check_grain(bool(i % 2), "HTTP 200" if i % 3 else "", i % 20)
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestRetry(unittest.TestCase):
    def test_db_error_propagates_and_closes_owned_conn(self):
        # RETRY GAP: witness_state() — one query, no retry; an error propagates but the owned conn is closed
        conn, cur = _conn()
        cur.execute.side_effect = RuntimeError("pg down")
        with patch.object(wt, "_conn", return_value=conn):
            with self.assertRaises(RuntimeError):
                wt.witness_state("w")
        self.assertEqual(cur.execute.call_count, 1)
        conn.close.assert_called_once()


class TestUnit(unittest.TestCase):
    def test_demo(self):
        with redirect_stdout(io.StringIO()) as out:
            wt._demo()
        self.assertIn("5/5 PASSED", out.getvalue())

    def test_check_grain_edges(self):
        self.assertEqual(wt.check_grain(True, "body", None), (True, "body"))
        self.assertFalse(wt.check_grain(True, "   ", 50)[0])
        self.assertEqual(wt.check_grain(True, "", 50, require_evidence=False), (True, ""))
        self.assertIn("< 100ms floor", wt.check_grain(True, "x", 99, min_ms=100)[1])

    def test_witness_state_buckets(self):
        now = datetime.now(timezone.utc)
        self.assertEqual(wt.witness_state("w", conn=_conn(None)[0])[0], "red")
        self.assertEqual(wt.witness_state("w", conn=_conn((None, 30))[0])[0], "red")
        self.assertEqual(wt.witness_state("w", conn=_conn((now - timedelta(days=2), 30))[0])[0], "green")
        st, why = wt.witness_state("w", conn=_conn((now - timedelta(days=40), None))[0])
        self.assertEqual(st, "yellow")
        self.assertIn("window 30d", why)

    def test_clock_skew_never_negative(self):
        st, why = wt.witness_state("w", conn=_conn((datetime.now(timezone.utc) + timedelta(seconds=5), 30))[0])
        self.assertEqual(st, "green")
        self.assertIn("0d ago", why)


class TestIntegration(unittest.TestCase):
    def test_record_ensures_schema_then_upserts_and_commits(self):
        conn, cur = _conn()
        wt.record_proven_red(**CARD, freshness_days=7, conn=conn)
        sqls = [c.args[0] for c in cur.execute.call_args_list]
        self.assertIn("CREATE TABLE IF NOT EXISTS telemetry.witness_proven_red", sqls[0])
        self.assertIn("ON CONFLICT (witness) DO UPDATE", sqls[1])
        self.assertEqual(cur.execute.call_args.args[1][3], 7)
        self.assertGreaterEqual(conn.commit.call_count, 2)
        conn.close.assert_not_called()          # caller-owned connection stays open


class TestFunctional(unittest.TestCase):
    def test_vague_card_refused_before_any_db_touch(self):
        with patch.object(wt, "_conn") as c:
            with self.assertRaises(ValueError) as cm:
                wt.record_proven_red(**{**CARD, "injected_fault": "  ", "recorded_by": None})
        c.assert_not_called()
        self.assertIn("injected_fault", str(cm.exception))
        self.assertIn("recorded_by", str(cm.exception))

    def test_record_then_state_green(self):
        conn, cur = _conn((datetime.now(timezone.utc), 30))
        with patch.object(wt, "_conn", return_value=conn):
            wt.record_proven_red(**CARD)
            self.assertEqual(wt.witness_state("memory-health")[0], "green")
        self.assertEqual(conn.close.call_count, 2)


class TestFrame(unittest.TestCase):
    def test_demo_exits_zero(self):
        r = subprocess.run([sys.executable, str(PATH), "--demo"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("PASSED", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_witness"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
