#!/usr/bin/env python3
"""Tests for nova_backfill_date_sender.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_backfill_date_sender.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("bfds", SCRIPTS / "nova_backfill_date_sender.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bf = _load()
bf.notify = lambda *a, **k: None        # never post at load or in any test
bf.log = lambda *a, **k: None


class _Conn:
    def __init__(self, pool):
        self.pool = pool

    async def fetch(self, sql, *args):
        self.pool.fetch_sql.append((sql, args))
        return self.pool.batches.pop(0) if self.pool.batches else []

    async def executemany(self, sql, rows):
        self.pool.many.append((sql, list(rows)))

    async def execute(self, sql, *args):
        self.pool.exec.append((sql, args))


class _Acq:
    def __init__(self, pool):
        self.pool = pool

    async def __aenter__(self):
        return _Conn(self.pool)

    async def __aexit__(self, *a):
        return False


class _Pool:
    def __init__(self, batches):
        self.batches = list(batches); self.fetch_sql = []; self.many = []; self.exec = []; self.closed = False

    def acquire(self):
        return _Acq(self)

    async def close(self):
        self.closed = True


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(bf.DB_DSN, r":[^@/]+@")       # no password in the DSN

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r"(fetch|execute|executemany)\(\s*f[\"']", SRC))
        self.assertIn("WHERE id = ANY($1::text[])", SRC)

    def test_sender_is_length_capped(self):
        self.assertEqual(len(bf.parse_sender({"sender": "x" * 1000})), 255)


class TestPerformance(unittest.TestCase):
    def test_parse_10k_rows_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            bf.parse_sender({"from": f"a{i}@example.com"})
            bf.parse_date("no header here", {}, "notes")
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_header_scan_is_bounded_to_2000_chars(self):
        text = "x" * 5000 + "\nDate: Mon, 1 Jan 2024 10:00:00 +0000\n"
        self.assertIsNone(bf.parse_date(text, {}, "email_archive"))


class TestRetry(unittest.TestCase):
    def test_pool_failure_propagates_after_one_attempt(self):
        # RETRY GAP: main()/asyncpg.create_pool — one connect attempt; a one-shot nohup backfill that
        # aborts loudly before any write rather than half-applying.
        calls = []
        async def boom(*a, **k):
            calls.append(1); raise OSError("pg down")
        with patch.object(bf.asyncpg, "create_pool", side_effect=boom):
            with self.assertRaises(OSError):
                asyncio.run(bf.main())
        self.assertEqual(len(calls), 1)

    def test_bad_metadata_date_fails_open_to_header(self):
        d = bf.parse_date("Date: Tue, 2 Jan 2024 09:00:00 +0000", {"date": object()}, "email_archive")
        self.assertIsNotNone(d)


class TestUnit(unittest.TestCase):
    def test_parse_date_sources(self):
        self.assertEqual(bf.parse_date("", {"date": "2024-03-04"}, "x").year, 2024)
        self.assertEqual(bf.parse_date("", {"timestamp": "2023-01-02T03:04:05"}, "x").month, 1)
        self.assertEqual(bf.parse_date("Date: Wed, 5 Jun 2024 10:00:00 +0000", {}, "email_archive").day, 5)

    def test_parse_date_edges(self):
        self.assertIsNone(bf.parse_date("", {}, "email_archive"))
        self.assertIsNone(bf.parse_date("Date: 2024-01-01", None, "notes"))  # header only honoured for email
        self.assertIsNone(bf.parse_date("", None, ""))

    def test_parse_sender_edges(self):
        self.assertIsNone(bf.parse_sender(None))
        self.assertIsNone(bf.parse_sender({}))
        self.assertEqual(bf.parse_sender({"author": "  Ann  "}), "Ann")
        self.assertEqual(bf.parse_sender({"sender": "s", "from": "f"}), "s")


class TestIntegration(unittest.TestCase):
    def test_uses_shared_notify_and_memories_db(self):
        self.assertIn("from nova_notify import notify", SRC)
        self.assertTrue(bf.DB_DSN.endswith("/nova_memories"))

    def test_parsers_compose_into_update_tuples(self):
        rows = [{"id": "a", "text": "", "metadata": {"date": "2024-01-01", "sender": "x"}, "source": ""}]
        out = [(r["id"], bf.parse_date(r["text"], r["metadata"], r["source"]), bf.parse_sender(r["metadata"])) for r in rows]
        self.assertEqual(out[0][0], "a")
        self.assertEqual(out[0][2], "x")


class TestFunctional(unittest.TestCase):
    def test_main_backfills_and_marks_undated(self):
        rows = [
            {"id": "1", "text": "", "metadata": {"date": "2024-02-02", "sender": "bob"}, "source": "notes"},
            {"id": "2", "text": "nothing", "metadata": {}, "source": "notes"},
        ]
        pool = _Pool([rows])
        sent = []
        async def mk(*a, **k):
            return pool
        with patch.object(bf.asyncpg, "create_pool", side_effect=mk), \
             patch.object(bf, "notify", side_effect=lambda *a, **k: sent.append(a[0])):
            asyncio.run(bf.main())
        sqls = [s for s, _ in pool.many]
        self.assertTrue(any("extracted_date" in s for s in sqls))
        self.assertTrue(any("extracted_sender" in s for s in sqls))
        self.assertEqual(pool.exec[0][1][0], ["2"])           # undated row marked 1970
        self.assertTrue(pool.closed)
        self.assertEqual(len(sent), 2)
        self.assertIn("complete", sent[1])

    def test_main_empty_table_is_a_noop(self):
        pool = _Pool([])
        async def mk(*a, **k):
            return pool
        with patch.object(bf.asyncpg, "create_pool", side_effect=mk), patch.object(bf, "notify"):
            asyncio.run(bf.main())
        self.assertEqual(pool.many, [])
        self.assertTrue(pool.closed)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_backfill_date_sender"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
