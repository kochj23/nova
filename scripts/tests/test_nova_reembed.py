#!/usr/bin/env python3
"""Tests for nova_reembed.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

nova_reembed is DESTRUCTIVE (nulls every embedding, drops/rebuilds HNSW indexes). Every PG cursor and every
Ollama call here is a recorder; the --dry-run path is proven to issue no DDL/DML."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_reembed.py"
SRC = SCRIPT.read_text()


def _load():
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    spec = importlib.util.spec_from_file_location("nreembed", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_notify": nn}), patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    mod.notify = MagicMock(return_value=True)
    return mod


re_ = _load()


class _Cur:
    def __init__(self, total=3, dims=768, rows=None):
        self.total, self.dims = total, dims
        self.rows = list(rows if rows is not None else [(i, f"memory {i}") for i in range(total)])
        self.sql = []; self.params = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split())); self.params.append(params); self._last = sql

    def fetchone(self):
        if "SELECT embedding" in self._last:
            return ([0.0] * self.dims,)
        return (self.total if "IS NULL" not in self._last else len(self.rows),)

    def fetchmany(self, n):
        out, self.rows = self.rows[:n], self.rows[n:]
        return out


def _main(argv, cur, embed=None):
    conn = MagicMock(); conn.cursor.return_value = cur
    re_.notify.reset_mock()
    with patch.object(sys, "argv", ["nova_reembed.py", *argv]), patch("psycopg2.connect", return_value=conn), \
         patch.object(re_, "embed", side_effect=embed or (lambda t, m: [0.1, 0.2])), redirect_stdout(io.StringIO()) as out:
        re_.main()
    return out.getvalue()


def _writes(cur):
    return [s for s in cur.sql if re.match(r"(UPDATE|ALTER|DROP|CREATE|DELETE|INSERT)\b", s)]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_dry_run_issues_no_writes(self):
        cur = _Cur()
        out = _main(["--dry-run"], cur)
        self.assertEqual(_writes(cur), [])
        re_.notify.assert_not_called()
        self.assertIn("Dry run — no changes made.", out)

    def test_embedding_update_is_parameterized(self):
        cur = _Cur(rows=[(1, "x'; DROP TABLE memories;--")], total=1)
        _main(["--resume", "--dims", "768"], cur)
        upd = [(s, p) for s, p in zip(cur.sql, cur.params) if s.startswith("UPDATE memories SET embedding = %s")]
        self.assertEqual(upd[0][1], ("[0.1,0.2]", 1))
        self.assertFalse(any("DROP TABLE" in s for s in cur.sql))

    def test_ollama_is_loopback(self):
        self.assertTrue(re_.OLLAMA_URL.startswith("http://127.0.0.1:"))


class TestPerformance(unittest.TestCase):
    def test_10k_rows_batched(self):
        cur = _Cur(total=10_000, dims=1024)
        t0 = time.perf_counter()
        _main(["--resume"], cur)
        self.assertLess(time.perf_counter() - t0, 10.0)
        self.assertEqual(sum(1 for s in cur.sql if s.startswith("UPDATE memories SET embedding = %s")), 10_000)


class TestRetry(unittest.TestCase):
    def test_embed_errors_are_counted_not_fatal(self):
        # RETRY GAP: embed() — one Ollama call per memory, no retry; a failed row is counted and left NULL
        # so a later --resume picks it up.
        calls = []
        def flaky(t, m):
            calls.append(t)
            if len(calls) % 2:
                raise OSError("ollama busy")
            return [1.0]
        cur = _Cur(total=4, dims=1024)
        out = _main(["--resume"], cur, embed=flaky)
        self.assertEqual(len(calls), 4)
        self.assertIn("2 errors", out)
        self.assertIn("Errors: 2", re_.notify.call_args[0][0] + (re_.notify.call_args.kwargs.get("body") or ""))

    def test_embed_raises_on_ollama_down(self):
        with patch.object(re_.urllib.request, "urlopen", side_effect=OSError("refused")) as uo:
            with self.assertRaises(OSError):
                re_.embed("x", "m")
        self.assertEqual(uo.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_embed_response_shapes(self):
        for payload, want in (({"embeddings": [[1, 2]]}, [1, 2]), ({"embedding": [3, 4]}, [3, 4])):
            r = MagicMock(); r.read.return_value = json.dumps(payload).encode()
            with patch.object(re_.urllib.request, "urlopen", return_value=r):
                self.assertEqual(re_.embed("t", "m"), want)

    def test_post_slack_strips_emoji_markup(self):
        re_.notify.reset_mock()
        re_.post_slack(":brain: *Re-embedding Started*\n• x")
        args, kw = re_.notify.call_args
        self.assertEqual(args[0], "Re-embedding Started")
        self.assertEqual((kw["body"], kw["category"], kw["dedup_key"]), ("• x", "memory_ingest", "reembed-status"))

    def test_partial_index_ddl_order(self):
        # regression: WHERE preceded WITH (...) in the partial-index DDL — a PostgreSQL syntax error
        cur = _Cur(total=1, dims=1024)
        _main(["--resume"], cur)
        partial = [s for s in cur.sql if s.startswith("CREATE INDEX memories_hnsw_")]
        self.assertEqual(len(partial), 5)
        for s in partial:
            self.assertLess(s.index("WITH ("), s.index("WHERE source"))


class TestIntegration(unittest.TestCase):
    def test_dimension_change_drops_and_alters_before_embedding(self):
        cur = _Cur(total=2, dims=768)
        _main(["--dims", "1024"], cur)
        w = _writes(cur)
        alter = w.index("ALTER TABLE memories ALTER COLUMN embedding TYPE vector(1024)")
        null_all = w.index("UPDATE memories SET embedding = NULL")
        self.assertTrue(all(s.startswith("DROP INDEX IF EXISTS") for s in w[:alter]))
        self.assertLess(alter, null_all)
        self.assertTrue(w[-1].startswith("CREATE INDEX memories_hnsw_health"))

    def test_uses_shared_truncation(self):
        self.assertIn("nova_config.truncate_at_boundary(text)", SRC)


class TestFunctional(unittest.TestCase):
    def test_resume_golden_path_notifies_start_and_complete(self):
        cur = _Cur(total=3, dims=1024)
        _main(["--resume"], cur)
        self.assertFalse(any(s == "UPDATE memories SET embedding = NULL" for s in cur.sql))   # resume never wipes
        titles = [c[0][0] for c in re_.notify.call_args_list]
        self.assertEqual(titles, ["Re-embedding Started", "Re-embedding Complete"])

    def test_pg_down_fails_before_any_change(self):
        with patch.object(sys, "argv", ["x"]), patch("psycopg2.connect", side_effect=psycopg2.OperationalError("down")):
            with self.assertRaises(psycopg2.OperationalError):
                re_.main()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            _load()


if __name__ == "__main__":
    unittest.main()
