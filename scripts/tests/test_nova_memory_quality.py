#!/usr/bin/env python3
"""Tests for nova_memory_quality.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_memory_quality.py"
SRC = SCRIPT.read_text()
_TMP = tempfile.TemporaryDirectory()

import asyncpg  # noqa: E402  (imported inside run_audit; real module, only create_pool is patched)


def _load():
    spec = importlib.util.spec_from_file_location("nmq_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mq = _load()
# redirect the import-time FileHandler to a tempdir; stub the outbound notify
mq.logger.removeHandler(mq.fh); mq.fh.close()
mq.fh = logging.FileHandler(Path(_TMP.name) / "mq.log"); mq.logger.addHandler(mq.fh)
mq.logger.removeHandler(mq.sh)
mq.notify = MagicMock()

GOOD = "The Hubble telescope observed a distant galaxy cluster with remarkable clarity last week."


class _Conn:
    def __init__(self, batches, dupes=(), dupe_rows=()):
        self.batches = list(batches); self.dupes = list(dupes); self.dupe_rows = list(dupe_rows)
        self.executed = []

    async def fetchval(self, sql):
        return 3

    async def fetch(self, sql, *args):
        if "id > $1" in sql:
            return self.batches.pop(0) if self.batches else []
        if "GROUP BY text_hash" in sql:
            return self.dupes
        return self.dupe_rows

    async def execute(self, sql, *args):
        self.executed.append((sql, args))


def _pool(conn):
    pool = MagicMock()
    cm = MagicMock(); cm.__aenter__ = AsyncMock(return_value=conn); cm.__aexit__ = AsyncMock(return_value=False)
    pool.acquire.return_value = cm
    pool.close = AsyncMock()
    return pool


def _row(i, text, source="astronomy"):
    return {"id": f"m{i:04d}", "text": text, "source": source}


def _sample_conn():
    return _Conn([[_row(1, GOOD), _row(2, "hi"), _row(3, "spam " * 10), _row(4, "x" * 40, "quarantine:old")]],
                 dupes=[{"text_hash": "h1"}],
                 dupe_rows=[_row(5, GOOD), _row(6, GOOD)])


class _Base(unittest.TestCase):
    def setUp(self):
        mq.notify.reset_mock(side_effect=True)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn(":", mq.DB_DSN.split("@")[0].split("//")[1])

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'(execute|fetch|fetchval)\(\s*f["\']', SRC))
        self.assertIn("UPDATE memories SET source = $1 WHERE id = $2", SRC)

    def test_quarantine_never_deletes(self):
        self.assertIsNone(re.search(r"DELETE\s+FROM", SRC, re.I))


class TestPerformance(unittest.TestCase):
    def test_detectors_on_10k_memories(self):
        texts = [f"{GOOD} item {i}" for i in range(10_000)]
        t0 = time.perf_counter()
        for t in texts:
            mq.detect_repetitive(t); mq.detect_near_empty(t)
            mq.detect_misclassified(t, "astronomy"); mq.detect_transcription_artifact(t)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(_Base):
    def test_pool_failure_propagates_to_main_rc1(self):
        # RETRY GAP: run_audit()/asyncpg.create_pool — one attempt; main() turns the failure into rc=1
        with patch.object(asyncpg, "create_pool", AsyncMock(side_effect=OSError("pg down"))) as cp, \
             patch.object(sys, "argv", ["x"]):
            with self.assertRaises(SystemExit) as cm:
                mq.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(cp.call_count, 1)

    def test_scan_error_notifies_and_returns_1(self):
        conn = _Conn([]); conn.fetchval = AsyncMock(side_effect=RuntimeError("timeout"))
        pool = _pool(conn)
        with patch.object(asyncpg, "create_pool", AsyncMock(return_value=pool)):
            self.assertEqual(asyncio.run(mq.run_audit()), 1)
        self.assertEqual(mq.notify.call_args[0][0], "Memory Quality Audit FAILED")
        pool.close.assert_awaited()


class TestUnit(unittest.TestCase):
    def test_repetitive(self):
        self.assertTrue(mq.detect_repetitive("no no no no no"))
        # regression: repeated 2/3-word phrases were never detected (overlapping windows) — fixed 2026-10-05
        self.assertTrue(mq.detect_repetitive("thank you " * 6))
        self.assertTrue(mq.detect_repetitive("so " + "i mean like " * 5))
        self.assertFalse(mq.detect_repetitive("thank you " * 4))
        self.assertFalse(mq.detect_repetitive("a b"))
        self.assertFalse(mq.detect_repetitive(GOOD))

    def test_near_empty_and_misclassified(self):
        self.assertTrue(mq.detect_near_empty("   short   "))
        self.assertFalse(mq.detect_near_empty(GOOD))
        self.assertTrue(mq.detect_misclassified("recipe cookbook ingredient", "astronomy"))
        self.assertFalse(mq.detect_misclassified("recipe cookbook", "astronomy"))
        self.assertFalse(mq.detect_misclassified("recipe cookbook ingredient", "unknown_src"))

    def test_transcription_artifact(self):
        self.assertTrue(mq.detect_transcription_artifact("uh uh uh uh uh ok"))
        self.assertTrue(mq.detect_transcription_artifact("xkcdqz brrrrt pfffft mnbvcx a b zxcvbn qwrtp"))
        self.assertFalse(mq.detect_transcription_artifact("short one"))


class TestIntegration(_Base):
    def test_categorises_and_skips_quarantined(self):
        conn = _sample_conn()
        with patch.object(asyncpg, "create_pool", AsyncMock(return_value=_pool(conn))):
            self.assertEqual(asyncio.run(mq.run_audit(clean=False)), 0)
        body = mq.notify.call_args.kwargs["body"]
        self.assertIn("Scanned: 4 memories", body)
        self.assertIn("Near-empty chunks: 1", body)
        self.assertIn("Repetitive content: 1", body)
        self.assertIn("Duplicate hashes: 1", body)          # oldest kept, the second flagged
        self.assertIn("Dry-run", body)
        self.assertEqual(conn.executed, [])                   # dry-run writes nothing


class TestFunctional(_Base):
    def test_clean_mode_quarantines(self):
        conn = _sample_conn()
        with patch.object(asyncpg, "create_pool", AsyncMock(return_value=_pool(conn))), \
             patch.object(sys, "argv", ["x", "--clean"]):
            with self.assertRaises(SystemExit) as cm:
                mq.main()
        self.assertEqual(cm.exception.code, 0)
        ids = sorted(a[1] for _, a in conn.executed)
        self.assertEqual(ids, ["m0002", "m0003", "m0006"])
        self.assertTrue(all(a[0].startswith("quarantine:") for _, a in conn.executed))
        self.assertIn("Quarantined 3 entries", mq.notify.call_args.kwargs["body"])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--clean", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_memory_quality"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
