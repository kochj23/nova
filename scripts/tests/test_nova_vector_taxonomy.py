#!/usr/bin/env python3
"""Tests for nova_vector_taxonomy.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). asyncpg and the notification bus are mocked; no DB is touched.
Written by Jordan Koch (via Claude)."""
import asyncio
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
from unittest.mock import AsyncMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_vector_taxonomy.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_vector_taxonomy_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vt = _load()


class _Pool:
    def __init__(self, sources, counts=None, fail_on=None):
        self.sources, self.counts, self.fail_on = sources, counts or {}, fail_on
        self.updates, self.closed = [], False

    def acquire(self):
        pool = self

        class _A:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *a):
                return False
        return _A()

    async def fetch(self, sql, *a):
        self.fetch_sql = sql
        return [{"source": s} for s in self.sources]

    async def execute(self, sql, *args):
        if self.fail_on and args[1] == self.fail_on:
            raise RuntimeError("lock timeout")
        self.updates.append((sql, args))
        return f"UPDATE {self.counts.get(args[1], 0)}"

    async def close(self):
        self.closed = True


def _run(pool):
    with patch.object(vt.asyncpg, "create_pool", AsyncMock(return_value=pool)) as cp, \
         patch.object(vt, "notify") as n, redirect_stdout(io.StringIO()) as out:
        asyncio.run(vt.main())
    return cp, n, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")
        self.assertNotRegex(vt.DB_DSN, r":[^@/]+@")  # no password in the DSN

    def test_update_is_parameterized(self):
        pool = _Pool(["x'; --"], {"x'; --": 1})
        _run(pool)
        sql, args = pool.updates[0]
        self.assertEqual(sql, "UPDATE memories SET category = $1::ltree WHERE source = $2 AND category IS NULL")
        self.assertEqual(args, ("uncategorized", "x'; --"))

    def test_private_sources_classified_personal(self):
        for s in ("email_archive", "imessage", "financial_documents", "apple_health", "work_internal"):
            self.assertTrue(vt.SOURCE_TAXONOMY[s].startswith("personal."), s)


class TestPerformance(unittest.TestCase):
    def test_many_sources_fast(self):
        srcs = [f"src{i}" for i in range(5000)]
        pool = _Pool(srcs, {s: 1 for s in srcs})
        t0 = time.perf_counter()
        _, _, out = _run(pool)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(len(pool.updates), 5000)
        self.assertIn("5000/5000 sources done", out)


class TestRetry(unittest.TestCase):
    def test_update_failure_still_closes_pool(self):
        # RETRY GAP: main/conn.execute — one UPDATE per source, no retry; the pool is always closed
        pool = _Pool(["music", "occult"], fail_on="occult")
        with self.assertRaises(RuntimeError):
            _run(pool)
        self.assertTrue(pool.closed)
        self.assertEqual(len(pool.updates), 1)


class TestUnit(unittest.TestCase):
    def test_taxonomy_paths_are_valid_ltree(self):
        for src, path in vt.SOURCE_TAXONOMY.items():
            self.assertRegex(path, r"^[a-z_]+(\.[a-z0-9_]+)*$", src)

    def test_known_mappings(self):
        self.assertEqual(vt.SOURCE_TAXONOMY["ww2_pacific"], "history.military.ww2.pacific")
        self.assertEqual(vt.SOURCE_TAXONOMY["general_knowledge"], "uncategorized")


class TestIntegration(unittest.TestCase):
    def test_reads_only_uncategorized_rows_from_memories(self):
        pool = _Pool(["jazz_theory"], {"jazz_theory": 3})
        cp, _, _ = _run(pool)
        self.assertEqual(pool.fetch_sql, "SELECT DISTINCT source FROM memories WHERE category IS NULL AND source IS NOT NULL")
        self.assertEqual(cp.call_args[0][0], vt.DB_DSN)
        self.assertTrue(vt.DB_DSN.endswith("/nova_memories"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_counts_and_notifies(self):
        pool = _Pool(["jazz_theory", "unknown_src", "occult"], {"jazz_theory": 10, "occult": 5})
        _, n, out = _run(pool)
        self.assertEqual([a for _, a in pool.updates],
                         [("music.jazz.theory", "jazz_theory"), ("uncategorized", "unknown_src"),
                          ("philosophy.occult", "occult")])
        self.assertEqual(n.call_args_list[0][0][0], "nova_vector_taxonomy starting")
        self.assertIn("Rows categorized: 15", n.call_args.kwargs["body"])
        self.assertIn("Sources processed: 3", n.call_args.kwargs["body"])
        self.assertTrue(pool.closed)

    def test_nothing_to_do(self):
        pool = _Pool([])
        _, n, _ = _run(pool)
        self.assertEqual(pool.updates, [])
        self.assertIn("Rows categorized: 0", n.call_args.kwargs["body"])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_vector_taxonomy; print('ok')"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
