#!/usr/bin/env python3
"""Tests for nova_backup_ingest.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bi = _load("nova_backup_ingest_t", SCRIPTS / "nova_backup_ingest.py")
SRC = (SCRIPTS / "nova_backup_ingest.py").read_text()
CREATED = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _conn(rows=()):
    cur = MagicMock()
    cur.fetchall.return_value = list(rows)
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def _main(argv, connect):
    buf = io.StringIO()
    with patch.object(sys, "argv", ["nova_backup_ingest.py", *argv]), \
            patch.object(bi.psycopg2, "connect", connect), redirect_stdout(buf):
        bi.main()
    return buf.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password=", SRC)

    def test_upsert_is_parameterized(self):
        self.assertIn("%(ts)s", bi.UPSERT)
        self.assertNotRegex(bi.UPSERT, r"\{")
        # the only f-string in fetch_memories interpolates the fixed LIMIT keyword, never a value
        self.assertIn('limit = "" if all_rows else "LIMIT 1"', SRC)


class TestPerformance(unittest.TestCase):
    def test_to_row_10k_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            bi.to_row(i, CREATED, {"rc": "0", "files": "1,234", "errors": 0}, "")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_memory_query_failure_fails_open(self):
        # RETRY GAP: fetch_memories/psycopg2.connect — one attempt, main() logs and writes nothing
        connect = MagicMock(side_effect=RuntimeError("pg down"))
        out = _main([], connect)
        self.assertEqual(connect.call_count, 1)
        self.assertIn("memory query failed: pg down", out)

    def test_write_failure_fails_open(self):
        # RETRY GAP: write_rows/psycopg2.connect(OPS_DSN) — one attempt, logged not raised
        mem_conn, _ = _conn([("m1", CREATED, {"rc": 0}, "")])
        connect = MagicMock(side_effect=[mem_conn, RuntimeError("ops down")])
        out = _main([], connect)
        self.assertEqual(connect.call_count, 2)
        self.assertIn("ingest failed: ops down", out)


class TestUnit(unittest.TestCase):
    def test_int_coercion(self):
        self.assertIsNone(bi._int(None))
        self.assertEqual(bi._int(True), 1)
        self.assertEqual(bi._int(3.9), 3)
        self.assertEqual(bi._int("1,234 files"), 1234)
        self.assertIsNone(bi._int("n/a"))

    def test_parse_date_formats_and_fallback(self):
        self.assertEqual(bi._parse_date({"date": "2026-02-03 04:05:06"}, None),
                         datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc))
        self.assertEqual(bi._parse_date({"date": "2026-02-03"}, None).day, 3)
        self.assertEqual(bi._parse_date({"date": "garbage"}, CREATED), CREATED)
        self.assertEqual(bi._parse_date(None, CREATED), CREATED)

    def test_from_text_legacy_parse(self):
        t = "[a: rc=0, transferred=5 (1M), errors=1] [b: transferred=7, errors=2]"
        self.assertEqual(bi._from_text(t, "files"), 12)
        self.assertEqual(bi._from_text(t, "errors"), 3)
        self.assertIsNone(bi._from_text("", "files"))
        self.assertIsNone(bi._from_text(t, "bytes"))

    def test_to_row_derives_ok(self):
        self.assertTrue(bi.to_row(1, CREATED, {"rc": 0, "errors": 0}, "")["ok"])
        self.assertFalse(bi.to_row(1, CREATED, {"rc": 0}, "errors=2")["ok"])
        self.assertFalse(bi.to_row(1, CREATED, {"rc": 0, "ok": False}, "")["ok"])
        self.assertEqual(bi.to_row(9, CREATED, None, None)["mem_id"], "9")


class TestIntegration(unittest.TestCase):
    def test_reads_memories_writes_telemetry(self):
        self.assertIn("dbname=nova_memories", bi.MEM_DSN)
        self.assertIn("dbname=nova_ops", bi.OPS_DSN)
        self.assertIn("telemetry.backup_runs", bi.UPSERT)

    def test_to_row_feeds_upsert_keys(self):
        row = bi.to_row("x", CREATED, {"rc": 0}, "")
        self.assertEqual(set(re.findall(r"%\((\w+)\)s", bi.UPSERT)), set(row))


class TestFunctional(unittest.TestCase):
    def test_golden_path_upserts_latest(self):
        mem_conn, mem_cur = _conn([("m1", CREATED, {"rc": 0, "files": 10, "errors": 0}, "")])
        ops_conn, ops_cur = _conn()
        with patch.object(bi.psycopg2.extras, "execute_batch") as eb:
            out = _main([], MagicMock(side_effect=[mem_conn, ops_conn]))
        self.assertIn("LIMIT 1", mem_cur.execute.call_args.args[0])
        rows = eb.call_args.args[2]
        self.assertEqual(rows[0]["files"], 10)
        self.assertIn("upserted 1 row(s)", out)

    def test_dry_run_never_opens_ops(self):
        mem_conn, _ = _conn([("m1", CREATED, {"rc": 0}, "")])
        connect = MagicMock(side_effect=[mem_conn])
        out = _main(["--dry-run", "--all"], connect)
        self.assertEqual(connect.call_count, 1)
        self.assertIn("DRY RUN", out)

    def test_no_memories(self):
        mem_conn, _ = _conn([])
        out = _main([], MagicMock(side_effect=[mem_conn]))
        self.assertIn("nothing to ingest", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_backup_ingest.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(bi.main))


if __name__ == "__main__":
    unittest.main()
