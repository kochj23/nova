#!/usr/bin/env python3
"""Tests for nova_pg_stat_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_pg_stat_poller.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_pg_stat_poller_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ps = _load()
ROWS = [("nova_ops",) + tuple(range(12)), ("nova_memories",) + tuple(range(12))]


def _conn(rows=ROWS, fail=None, one=None):
    cur = MagicMock()
    if fail:
        cur.execute.side_effect = fail
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = one
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def _main(argv, conn):
    with patch.object(sys, "argv", ["x", *argv]), patch.object(ps.psycopg2, "connect", return_value=conn) as c, \
         patch.object(ps.psycopg2.extras, "execute_batch") as eb, redirect_stdout(io.StringIO()) as out:
        ps.main()
    return c, eb, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")

    def test_insert_uses_placeholders_and_fixed_columns(self):
        conn, cur = _conn()
        with patch.object(ps.psycopg2.extras, "execute_batch") as eb:
            ps.write_rows(conn, ROWS)
        sql = eb.call_args[0][1]
        self.assertEqual(sql.count("%s"), len(ps.COLUMNS))
        self.assertTrue(all(re.fullmatch(r"[a-z_]+", c) for c in ps.COLUMNS))
        self.assertIn("INSERT INTO telemetry.pg_stat_db", sql)


class TestPerformance(unittest.TestCase):
    def test_write_10k_rows_single_batch(self):
        rows = [(f"db{i}",) + tuple(range(12)) for i in range(10_000)]
        conn, _ = _conn()
        with patch.object(ps.psycopg2.extras, "execute_batch") as eb:
            t0 = time.perf_counter()
            n = ps.write_rows(conn, rows)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual((n, eb.call_count), (10_000, 1))


class TestRetry(unittest.TestCase):
    def test_connect_failure_fails_open(self):
        # RETRY GAP: main/psycopg2.connect — one attempt per run (next cron tick is the retry)
        with patch.object(sys, "argv", ["x"]), patch.object(ps.psycopg2, "connect", side_effect=OSError("down")) as c, \
             redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(ps.main())
        self.assertEqual(c.call_count, 1)
        self.assertIn("DB connect failed", out.getvalue())

    def test_query_and_insert_failures_write_nothing(self):
        conn, _ = _conn(fail=RuntimeError("boom"))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ps.collect(conn), [])
            with patch.object(ps.psycopg2.extras, "execute_batch", side_effect=RuntimeError("x")):
                self.assertEqual(ps.write_rows(conn, ROWS), 0)


class TestUnit(unittest.TestCase):
    def test_write_empty(self):
        conn, _ = _conn()
        self.assertEqual(ps.write_rows(conn, []), 0)
        conn.cursor.assert_not_called()

    def test_self_check(self):
        with redirect_stdout(io.StringIO()):
            self.assertTrue(ps.self_check(_conn(one=(3, "now"))[0]))
            self.assertFalse(ps.self_check(_conn(one=(0, None))[0]))
            self.assertFalse(ps.self_check(_conn(fail=RuntimeError("x"))[0]))


class TestIntegration(unittest.TestCase):
    def test_collect_skips_templates(self):
        conn, cur = _conn()
        self.assertEqual(ps.collect(conn), ROWS)
        sql = cur.execute.call_args[0][0]
        self.assertIn("FROM pg_stat_database", sql)
        self.assertIn("NOT IN ('template0', 'template1')", sql)
        self.assertIn(", ".join(ps.COLUMNS), sql)


class TestFunctional(unittest.TestCase):
    def test_golden_path_inserts(self):
        conn, _ = _conn()
        c, eb, out = _main([], conn)
        self.assertEqual(c.call_args[0][0], ps.DB_DSN)
        self.assertEqual(eb.call_args[0][2], ROWS)
        self.assertIn("inserted 2 row(s)", out)
        conn.close.assert_called_once()

    def test_dry_run_writes_nothing(self):
        conn, _ = _conn()
        _, eb, out = _main(["--dry-run"], conn)
        eb.assert_not_called()
        self.assertIn("DRY RUN — would insert 2", out)

    def test_self_check_exit_code(self):
        conn, _ = _conn(one=(0, None))
        with self.assertRaises(SystemExit) as cm:
            _main(["--self-check"], conn)
        self.assertEqual(cm.exception.code, 1)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--self-check", r.stdout)


if __name__ == "__main__":
    unittest.main()
