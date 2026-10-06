#!/usr/bin/env python3
"""Tests for nova_spatial_query.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import types
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_spatial_query.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sq = _load("sq_mod", SCRIPT)


class _Cur:
    def __init__(self, rows):
        self.rows = rows; self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False; self.autocommit = False

    def cursor(self, **kw):
        return self.cur

    def close(self):
        self.closed = True


def _row(src="scanner", mi=0.8, d="NE", text="[KMA367] structure fire reported on Olive Ave"):
    return {"source": src, "text": text, "created_at": datetime(2026, 10, 5, 14, 30), "mi": mi, "dir": d}


def _answer(text, rows, **kw):
    cur = _Cur(rows)
    with patch.object(sq.psycopg2, "connect", return_value=_Conn(cur)) as c:
        out = sq.answer(text, **kw)
    return out, cur, c


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", sq.MEM_DSN)

    def test_sql_interpolates_only_numeric_values(self):
        # the one query uses %d/%f formatting: only numbers can ever reach the SQL text, never user strings
        m = re.search(r'"\s*%\s*\(hours, radius\)\)', SRC)
        self.assertIsNotNone(m)
        self.assertNotIn("%s", SRC.split("cur.execute(")[1].split("rows = ")[0])
        out, cur, _ = _answer("anything near me within 2 miles' or 1=1 --", [])
        self.assertIn("<= 2.000000", cur.sql[0])
        self.assertNotIn("1=1", cur.sql[0])

    def test_read_only(self):
        self.assertIsNone(re.search(r"\b(INSERT|UPDATE|DELETE)\b", SRC))

    def test_non_numeric_hours_cannot_reach_sql(self):
        with self.assertRaises(TypeError):
            with patch.object(sq.psycopg2, "connect", return_value=_Conn(_Cur([]))):
                sq.answer("what's near me", hours="1; select 1")


class TestPerformance(unittest.TestCase):
    def test_is_spatial_on_10k_messages(self):
        msgs = [f"message {i} about something unrelated to location" for i in range(10_000)]
        t0 = time.perf_counter()
        n = sum(sq.is_spatial(m) for m in msgs)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(n, 0)


class TestRetry(unittest.TestCase):
    def test_pg_failure_propagates_no_retry(self):
        # RETRY GAP: answer()/psycopg2.connect — one attempt; a connection error escapes to the
        # gateway caller, which is responsible for falling through to normal chat.
        attempts = []

        def boom(*a, **k):
            attempts.append(1); raise OSError("pg down")
        with patch.object(sq.psycopg2, "connect", side_effect=boom):
            with self.assertRaises(OSError):
                sq.answer("anything near me?")
        self.assertEqual(len(attempts), 1)

    def test_non_spatial_text_never_touches_pg(self):
        with patch.object(sq.psycopg2, "connect") as c:
            self.assertIsNone(sq.answer("how are you today"))
            self.assertIsNone(sq.answer(""))
            self.assertIsNone(sq.answer(None))
        c.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_is_spatial(self):
        for t in ("what's near me", "anything nearby?", "how close is that fire", "within 2 miles",
                  "anything on the scanner", "is it walkable", "3 blocks away", "what's going on in the neighborhood"):
            self.assertTrue(sq.is_spatial(t), t)
        for t in ("what time is it", "", None, "tell me about Burbank history"):
            self.assertFalse(sq.is_spatial(t), t)

    def test_radius_parsing(self):
        _, cur, _ = _answer("anything within 5 miles", [])
        self.assertIn("<= 5.000000", cur.sql[0])
        _, cur, _ = _answer("anything within 10 blocks", [])
        self.assertIn("<= 0.600000", cur.sql[0])
        _, cur, _ = _answer("anything near me", [], hours=12)
        self.assertIn("interval '12 hours'", cur.sql[0]); self.assertIn("<= 3.000000", cur.sql[0])

    def test_empty_rows_quiet_message(self):
        out, _, _ = _answer("anything nearby", [])
        self.assertTrue(out.startswith("Nothing on the police/fire scanners within 3 mi"))
        self.assertIn("last 6h", out)

    def test_line_rendering_strips_bracket_prefix_and_truncates(self):
        out, _, _ = _answer("what's near me", [_row(text="[hdr] " + "x" * 300)])
        self.assertNotIn("[hdr]", out)
        self.assertIn("x" * 95, out); self.assertNotIn("x" * 96, out)
        self.assertIn("\U0001F693", out)
        out, _, _ = _answer("what's near me", [_row(src="fire", d=None)])
        self.assertIn("\U0001F692 ~0.8 mi  ", out)


class TestIntegration(unittest.TestCase):
    def test_reads_scanner_and_fire_memories_with_geo_metadata(self):
        _, cur, c = _answer("near me", [])
        c.assert_called_once_with(sq.MEM_DSN)
        self.assertIn("FROM memories WHERE source IN ('scanner','fire')", cur.sql[0])
        self.assertIn("metadata->'geo'->>'nearest_mi'", cur.sql[0])
        self.assertIn("nova_memories", sq.MEM_DSN)

    def test_closest_only_vs_list(self):
        rows = [_row(mi=0.3), _row(mi=1.1, text="traffic collision")]
        out, _, _ = _answer("what's the closest thing", rows)
        self.assertTrue(out.startswith("Closest recent activity:"))
        self.assertNotIn("traffic collision", out)
        out, _, _ = _answer("anything near me", rows)
        self.assertIn("(2 located)", out); self.assertIn("traffic collision", out)


class TestFunctional(unittest.TestCase):
    def test_golden_path_lists_at_most_eight_and_closes_connection(self):
        rows = [_row(mi=i / 10, text=f"call {i}") for i in range(12)]
        cur = _Cur(rows); conn = _Conn(cur)
        with patch.object(sq.psycopg2, "connect", return_value=conn):
            out = sq.answer("anything happening near me?")
        self.assertIn("(12 located)", out)
        self.assertEqual(out.count("\n"), 8)
        self.assertTrue(conn.closed); self.assertTrue(conn.autocommit)

    def test_cli_prints_not_spatial_without_touching_pg(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "hello", "there"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "(not a spatial question)")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_spatial_query"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
