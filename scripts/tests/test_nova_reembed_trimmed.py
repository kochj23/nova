#!/usr/bin/env python3
"""Tests for nova_reembed_trimmed.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_reembed_trimmed.py"
SRC = SCRIPT.read_text()
try:
    import psycopg2  # noqa: F401
except ImportError:
    pass


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rt = _load("reembed_under_test", SCRIPT)


class _Cur:
    """Cursor stub: the id list answers the backup JOIN; `texts` answers per-id text lookups."""
    def __init__(self, ids, texts):
        self.ids, self.texts = list(ids), dict(texts); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split()); self.sql.append((s, params))
        if s.startswith("SELECT b.id"):
            self._last = [(i,) for i in self.ids]
        elif s.startswith("SELECT text"):
            t = self.texts.get(params[0]); self._last = (t,) if t is not None else None

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.commits = 0; self.closed = False

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _resp(vec):
    r = MagicMock(); r.read.return_value = json.dumps({"embeddings": [vec]}).encode()
    return r


def _run(ids, texts, embed=None, urlopen=None):
    cur = _Cur(ids, texts); conn = _Conn(cur)
    connect = MagicMock(return_value=conn)
    ctx = [patch.object(rt, "psycopg2", types.SimpleNamespace(connect=connect)), redirect_stdout(io.StringIO())]
    if urlopen is not None:
        ctx.append(patch.object(rt.urllib.request, "urlopen", urlopen))
    else:
        ctx.append(patch.object(rt, "embed", embed or (lambda t: [0.1, 0.2])))
    out = None
    with ctx[0], ctx[1] as o, ctx[2]:
        rt.main(); out = o.getvalue()
    return conn, out, connect


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", rt.DSN)

    def test_sql_is_parameterized_and_only_updates_embedding(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(0) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+[\w.]+(?:\s+SET\s+\w+)?", SRC)}
        self.assertEqual(writes, {"UPDATE memories SET embedding"})
        evil = "1'; DELETE FROM memories; --"
        conn, _, _ = _run([evil], {evil: "text"})
        sql, params = conn.cur.ran("UPDATE memories")[0]
        self.assertNotIn("DELETE", sql); self.assertEqual(params[1], evil)

    def test_embed_node_is_on_the_private_lan(self):
        self.assertTrue(rt.EMBED_URL.startswith("http://192.168."))
        self.assertIn(":11434/api/embed", rt.EMBED_URL)


class TestPerformance(unittest.TestCase):
    def test_10k_rows_reembed_under_2s_with_periodic_commits(self):
        ids = list(range(10_000)); texts = {i: f"memory {i}" for i in ids}
        t0 = time.perf_counter()
        conn, out, _ = _run(ids, texts)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(conn.commits, 10_000 // rt.BATCH_COMMIT + 1)      # every BATCH_COMMIT rows + final
        self.assertIn("DONE: 10000/10000 re-embedded, 0 errors", out)


class TestRetry(unittest.TestCase):
    def test_embed_failure_is_one_shot_per_row_and_fails_open(self):
        # RETRY GAP: embed()/urllib.request.urlopen — no retry; a failing row is counted as an error, skipped,
        # and the loop continues, so a flaky embed node degrades the run instead of aborting it.
        calls = []

        def flaky(req, timeout=None):
            calls.append(1)
            if len(calls) == 2:
                raise OSError("embed node down")
            return _resp([0.5])
        conn, out, _ = _run([1, 2, 3], {1: "a", 2: "b", 3: "c"}, urlopen=flaky)
        self.assertEqual(len(calls), 3)                                  # one attempt per row, never two
        self.assertEqual(len(conn.cur.ran("UPDATE memories")), 2)
        self.assertIn("err id=2: embed node down", out)
        self.assertIn("DONE: 2/3 re-embedded, 1 errors", out)
        self.assertTrue(conn.closed)

    def test_error_log_is_capped_at_ten_lines(self):
        ids = list(range(25))
        conn, out, _ = _run(ids, {i: "t" for i in ids}, urlopen=MagicMock(side_effect=OSError("x")))
        self.assertEqual(out.count("[reembed] err id="), 10)
        self.assertIn("DONE: 0/25 re-embedded, 25 errors", out)


class TestUnit(unittest.TestCase):
    def test_embed_truncates_to_6000_chars_and_posts_json(self):
        with patch.object(rt.urllib.request, "urlopen", return_value=_resp([1.0, 2.0])) as u:
            self.assertEqual(rt.embed("x" * 9000), [1.0, 2.0])
        req = u.call_args[0][0]
        body = json.loads(req.data)
        self.assertEqual(len(body["input"]), 6000)
        self.assertEqual(body["model"], "nomic-embed-text")
        self.assertEqual(req.full_url, rt.EMBED_URL)
        self.assertEqual(u.call_args[1]["timeout"], 45)

    def test_empty_or_missing_text_rows_are_skipped(self):
        conn, out, _ = _run([1, 2, 3], {1: "", 3: "real"})
        self.assertEqual(len(conn.cur.ran("UPDATE memories")), 1)
        self.assertEqual(conn.cur.ran("UPDATE memories")[0][1][1], 3)
        self.assertIn("DONE: 1/3", out)

    def test_no_rows_is_a_clean_noop(self):
        conn, out, _ = _run([], {})
        self.assertIn("[reembed] 0 trimmed memories", out)
        self.assertEqual(conn.commits, 1); self.assertTrue(conn.closed)


class TestIntegration(unittest.TestCase):
    def test_reads_the_553_backup_join_and_writes_vector_cast(self):
        conn, _, connect = _run([7], {7: "trimmed text"}, embed=lambda t: [0.25, 0.75])
        connect.assert_called_once_with(rt.DSN)
        self.assertIn("nova_memories", rt.DSN)
        self.assertEqual(conn.cur.sql[0][0], "SELECT b.id FROM memory_cruft_backup_553 b JOIN memories m ON m.id = b.id ORDER BY b.id")
        sql, params = conn.cur.ran("UPDATE memories")[0]
        self.assertEqual(sql, "UPDATE memories SET embedding = %s::vector WHERE id = %s")
        self.assertEqual(params, ("[0.25, 0.75]", 7))


class TestFunctional(unittest.TestCase):
    def test_golden_path_reembeds_every_row_and_reports(self):
        ids = list(range(1, rt.BATCH_COMMIT + 2))
        seen = []

        def emb(t):
            seen.append(t); return [len(t)]
        conn, out, _ = _run(ids, {i: f"m{i}" for i in ids}, embed=emb)
        self.assertEqual(len(seen), len(ids))
        self.assertIn(f"[reembed] {len(ids)} trimmed memories to re-embed via .10", out)
        self.assertIn(f"[reembed] {rt.BATCH_COMMIT}/{len(ids)} ({rt.BATCH_COMMIT} ok, 0 err)", out)
        self.assertIn(f"DONE: {len(ids)}/{len(ids)} re-embedded, 0 errors", out)
        self.assertEqual(conn.commits, 2); self.assertTrue(conn.closed)

    def test_pg_outage_escapes_before_any_embed_call(self):
        embed = MagicMock()
        with patch.object(rt, "psycopg2", types.SimpleNamespace(connect=MagicMock(side_effect=RuntimeError("pg down")))), \
             patch.object(rt, "embed", embed):
            with self.assertRaises(RuntimeError):
                rt.main()
        embed.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no argparse: --help would open PG and start re-embedding, so the smoke is an import
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_reembed_trimmed"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
