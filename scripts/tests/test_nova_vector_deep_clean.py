#!/usr/bin/env python3
"""Tests for nova_vector_deep_clean.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

This engine MUTATES memories.source when run with --clean; --dry-run (the default) must write nothing.
Every asyncpg call is mocked — no live DB, no pool — and both the dry-run and clean paths are proven."""
import asyncio
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_vector_deep_clean.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="deep_clean_"))


def _load():
    spec = importlib.util.spec_from_file_location("deep_clean", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.notify = MagicMock()
    mod.LOG_DIR = TMP
    return mod


dc = _load()


class _Acquire:
    def __init__(self, conn): self.conn = conn
    async def __aenter__(self): return self.conn
    async def __aexit__(self, *a): return False


class _Pool:
    """asyncpg pool stand-in: fetch/fetchrow answer from queues keyed by a SQL needle; records executes."""
    def __init__(self, fetch_map=None, fetchrow=None):
        self.fetch_map = fetch_map or {}; self.fetchrow_val = fetchrow
        self.executes = []; self.closed = False
        conn = MagicMock()
        conn.fetch = AsyncMock(side_effect=self._fetch)
        conn.fetchrow = AsyncMock(side_effect=self._fetchrow)
        conn.execute = AsyncMock(side_effect=self._execute)
        self._conn = conn

    def acquire(self): return _Acquire(self._conn)
    async def close(self): self.closed = True

    async def _fetch(self, sql, *args):
        for needle, rows in self.fetch_map.items():
            if needle in sql:
                return rows
        return []

    async def _fetchrow(self, sql, *args):
        return self.fetchrow_val

    async def _execute(self, sql, *args):
        self.executes.append((sql, args))


def _vec(*first):
    """A 768-dim vector string starting with the given components."""
    arr = list(first) + [0.0] * (768 - len(first))
    return "[" + ",".join(str(x) for x in arr) + "]"


def _run(coro):
    return asyncio.run(coro)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password=", dc.DB_DSN)           # DSN is peer-auth, no inline secret

    def test_updates_are_parameterized(self):
        # the source value is always bound ($1/$2), never interpolated into the UPDATE text
        for m in re.finditer(r'execute\(\s*\n?\s*(f?)"[^"]*UPDATE memories', SRC):
            self.assertEqual(m.group(1), "", "f-string UPDATE")
        self.assertIn("UPDATE memories SET source = $1 WHERE id = $2", SRC)


class TestPerformance(unittest.TestCase):
    def test_cosine_similarity_on_10k_pairs(self):
        a = np.random.rand(768).astype(np.float32)
        bs = [np.random.rand(768).astype(np.float32) for _ in range(50)]
        t0 = time.perf_counter()
        for i in range(10_000):
            dc.cosine_similarity(a, bs[i % 50])
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_centroid_too_few_samples_returns_none(self):
        # RETRY GAP: compute_centroid — one query; fewer than 10 usable embeddings -> None, caller skips the vector
        pool = _Pool(fetch_map={"ORDER BY random()": [{"embedding": _vec(1.0)} for _ in range(5)]})
        self.assertIsNone(_run(dc.compute_centroid(pool, "research")))

    def test_main_dry_run_writes_nothing(self):
        pool = _Pool()
        with patch.object(dc.asyncpg, "create_pool", AsyncMock(return_value=pool)), \
             patch.object(dc, "compute_centroid", AsyncMock(return_value=np.ones(768, np.float32))), \
             patch.object(dc, "find_misfits", AsyncMock(return_value=[{"id": "m1", "source": "research",
                          "embedding": np.zeros(768, np.float32), "distance": 2.0}])), \
             patch.object(dc, "get_memory_text", AsyncMock(return_value="snip")), \
             patch.object(sys, "argv", ["x", "--vector", "research"]):
            _run(dc.main())
        self.assertEqual(pool.executes, [])                # default dry-run: memories untouched
        self.assertTrue(pool.closed)


class TestUnit(unittest.TestCase):
    def test_cosine_similarity_edges(self):
        v = np.array([1.0, 0.0, 0.0], np.float32)
        self.assertAlmostEqual(dc.cosine_similarity(v, v), 1.0, places=5)
        self.assertEqual(dc.cosine_similarity(v, np.zeros(3, np.float32)), 0.0)
        self.assertAlmostEqual(dc.cosine_similarity(v, np.array([0.0, 1.0, 0.0], np.float32)), 0.0, places=5)

    def test_parse_pgvector_forms(self):
        np.testing.assert_array_equal(dc.parse_pgvector("[1.0,2.0,3.0]"), np.array([1, 2, 3], np.float32))
        np.testing.assert_array_equal(dc.parse_pgvector([4.0, 5.0]), np.array([4, 5], np.float32))

    def test_centroid_is_normalized(self):
        pool = _Pool(fetch_map={"ORDER BY random()": [{"embedding": _vec(3.0, 4.0)} for _ in range(12)]})
        c = _run(dc.compute_centroid(pool, "research"))
        self.assertAlmostEqual(float(np.linalg.norm(c)), 1.0, places=4)


class TestIntegration(unittest.TestCase):
    def test_find_misfits_parses_and_filters_dims(self):
        pool = _Pool(fetch_map={"embedding <=> $1::vector": [
            {"id": "a", "source": "research", "embedding": _vec(1.0), "distance": 1.9},
            {"id": "b", "source": "research", "embedding": "[1.0,2.0]", "distance": 1.8},   # wrong dim -> dropped
        ]})
        misfits = _run(dc.find_misfits(pool, "research", np.ones(768, np.float32)))
        self.assertEqual([m["id"] for m in misfits], ["a"])
        self.assertEqual(misfits[0]["distance"], 1.9)

    def test_get_memory_text(self):
        self.assertEqual(_run(dc.get_memory_text(_Pool(fetchrow={"snippet": "hello"}), "id")), "hello")
        self.assertEqual(_run(dc.get_memory_text(_Pool(fetchrow=None), "id")), "")


class TestFunctional(unittest.TestCase):
    def test_clean_run_moves_a_misfit_to_general_knowledge(self):
        pool = _Pool(fetchrow={"snippet": "orphan memory"})
        orphan = {"id": "m1", "source": "research", "embedding": np.zeros(768, np.float32), "distance": 2.0}
        with patch.object(dc.asyncpg, "create_pool", AsyncMock(return_value=pool)), \
             patch.object(dc, "compute_centroid", AsyncMock(return_value=np.ones(768, np.float32))), \
             patch.object(dc, "find_misfits", AsyncMock(return_value=[orphan])), \
             patch.object(sys, "argv", ["x", "--vector", "research", "--clean"]):
            _run(dc.main())
        # zero-vector misfit has cosine 0.0 (<0.3) and no better centroid -> general_knowledge
        self.assertTrue(any("general_knowledge" in sql for sql, _ in pool.executes))
        self.assertTrue(dc.notify.called)

    def test_journal_output_written(self):
        pool = _Pool(fetchrow={"snippet": "confused memory"})
        orphan = {"id": "m1", "source": "research", "embedding": np.zeros(768, np.float32), "distance": 2.0}
        with patch.object(dc.asyncpg, "create_pool", AsyncMock(return_value=pool)), \
             patch.object(dc, "compute_centroid", AsyncMock(return_value=np.ones(768, np.float32))), \
             patch.object(dc, "find_misfits", AsyncMock(return_value=[orphan])), \
             patch.object(sys, "argv", ["x", "--vector", "research", "--journal"]):
            _run(dc.main())
        md = list(TMP.glob("deep_clean_journal_*.md"))
        self.assertTrue(md)
        self.assertIn("Great Memory Migration", md[0].read_text())
        self.assertEqual(pool.executes, [])               # journal alone is still a dry run


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--clean", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    asyncio.run(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_vector_deep_clean as m; print(m.MISFIT_PERCENTILE)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "5")


if __name__ == "__main__":
    unittest.main()
