#!/usr/bin/env python3
"""Tests for nova_unused_memories.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
PG is mocked at module load. The privacy gate (nova_config.is_private_source + INTERNAL_SOURCES) is the
module's reason to exist, so Security proves both the SQL and the Python layer drop private rows."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_unused_memories.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("unused_memories_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


um = _load()
# module-level stub: no PG from any test (extras kept so RealDictCursor resolves)
um.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=OSError("offline: pg stubbed")),
                                    extras=types.SimpleNamespace(RealDictCursor=object))


class _Cur:
    def __init__(self, rows=(), rowcount=0):
        self.rows = list(rows); self.sql = []; self.rowcount = rowcount
    def execute(self, sql, params=None): self.sql.append((sql, params))
    def fetchall(self): return self.rows
    def __enter__(self): return self
    def __exit__(self, *e): return False


class _Conn:
    def __init__(self, cur): self.cur = cur; self.commits = 0
    def cursor(self, **k): return self.cur
    def commit(self): self.commits += 1
    def __enter__(self): return self
    def __exit__(self, *e): return False


def _rows(*sources):
    return [{"id": i, "text": f"t{i}", "source": s, "metadata": {}, "created_at": i} for i, s in enumerate(sources)]


def _with(cur):
    return patch.object(um.psycopg2, "connect", return_value=_Conn(cur), side_effect=None)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", um.DSN)

    def test_private_and_internal_sources_never_returned(self):
        private = sorted(um.nova_config.PRIVATE_SOURCES)[0]
        rows = _rows("music", private, "work_email", "imessage_2019", "dream", "infrastructure", "history")
        with _with(_Cur(rows)):
            out = um.fetch_unused(10)
        self.assertEqual([r["source"] for r in out], ["music", "history"])

    def test_sql_excludes_blocked_list_and_local_only(self):
        cur = _Cur()
        with _with(cur):
            um.fetch_unused(5)
        sql, params = cur.sql[0]
        self.assertIn("source NOT IN %s", sql)
        self.assertIn("privacy IS DISTINCT FROM 'local-only'", sql)
        self.assertTrue(um.INTERNAL_SOURCES <= set(params[0]))
        self.assertTrue(set(um.nova_config.PRIVATE_SOURCES) <= set(params[0]))
        self.assertEqual(params[1], 20)                         # over-fetch n*4, never f-string SQL
        self.assertNotRegex(SRC, r"execute\(\s*f[\"']")


class TestPerformance(unittest.TestCase):
    def test_filter_10k_rows(self):
        rows = _rows(*(["music", "email", "dream", "history"] * 2500))
        t0 = time.perf_counter()
        with _with(_Cur(rows)):
            out = um.fetch_unused(100)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(out), 100)


class TestRetry(unittest.TestCase):
    def test_pg_down_raises_no_retry(self):
        # RETRY GAP: fetch_unused / mark_used — single connect, no retry; the error propagates to the caller
        # (art corner / dreams) which must then skip — it never silently returns unfiltered data
        with self.assertRaises(OSError):
            um.fetch_unused(10)
        self.assertEqual(um.mark_used([]), 0)                    # empty input short-circuits before PG


class TestUnit(unittest.TestCase):
    def test_fetch_caps_at_n(self):
        with _with(_Cur(_rows(*(["music"] * 50)))):
            self.assertEqual(len(um.fetch_unused(7)), 7)

    def test_mark_used_drops_falsy_ids(self):
        cur = _Cur(rowcount=2)
        with _with(cur):
            self.assertEqual(um.mark_used([1, None, 0, 2]), 2)
        self.assertEqual(cur.sql[0][1], ([1, 2],))
        self.assertIn("access_count = coalesce(access_count,0) + 1", cur.sql[0][0])


class TestIntegration(unittest.TestCase):
    def test_uses_the_single_authoritative_gate(self):
        self.assertIn("nova_config.is_private_source", SRC)
        self.assertNotIn("def is_private_source", SRC)
        self.assertTrue(um.nova_config.is_private_source("email"))

    def test_fetch_then_mark_advances_pool(self):
        with _with(_Cur(_rows("music", "history"))):
            ids = [r["id"] for r in um.fetch_unused(2)]
        cur = _Cur(rowcount=len(ids)); conn = _Conn(cur)
        with patch.object(um.psycopg2, "connect", return_value=conn, side_effect=None):
            self.assertEqual(um.mark_used(ids), 2)
        self.assertEqual(conn.commits, 1)


class TestFunctional(unittest.TestCase):
    def test_demo_reports_clean_pool(self):
        with _with(_Cur(_rows("music", "music", "email"))), patch("builtins.print") as pr:
            um.demo()
        self.assertIn("OK: 2 oldest-unused, zero private", pr.call_args[0][0])

    def test_demo_aborts_if_gate_leaks(self):
        # error path: if the Python gate were ever bypassed, demo() refuses loudly
        with _with(_Cur(_rows("email"))), patch.object(um, "fetch_unused", return_value=_rows("email")):
            with self.assertRaises(AssertionError):
                um.demo()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_demo(self):
        # no --help: __main__ runs demo() which queries the real memories DB, so import is the smoke test
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_unused_memories as m; print(callable(m.fetch_unused))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
