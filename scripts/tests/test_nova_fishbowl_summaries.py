#!/usr/bin/env python3
"""Tests for nova_fishbowl_summaries.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_fishbowl_summaries.py"
SRC = SCRIPT.read_text()


def _stubs():
    nj = types.ModuleType("nova_journal"); nj.call_openrouter = MagicMock(return_value="A dossier.")
    nv = types.ModuleType("nova_voice"); nv.system_prompt = lambda s: "VOICE " + s
    return {"nova_journal": nj, "nova_voice": nv}


def _load():
    spec = importlib.util.spec_from_file_location("nfishsum", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stubs()), patch("psycopg2.connect", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    mod.nova_config = types.SimpleNamespace(post_both=MagicMock(), SLACK_FEED="C_FEED")
    return mod


fs = _load()


class _Cur:
    """Scripted cursor: `script` maps an SQL substring to the rows its fetch returns."""
    def __init__(self, script=None):
        self.script = script or {}; self.sql = []; self.params = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _rows(self):
        for k, v in self.script.items():
            if k in self._last:
                return v(self.params[-1]) if callable(v) else v
        return []

    def fetchall(self): return self._rows()
    def fetchone(self):
        r = self._rows(); return r[0] if r else None


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_gather_only_interpolates_placeholders(self):
        cur = _Cur()
        fs.gather(cur, ["o'malley'; DROP TABLE memories;--", "x"])
        self.assertNotIn("DROP", cur.sql[0])
        self.assertEqual(cur.sql[0].count("text ILIKE %s"), 2)
        self.assertEqual(cur.params[0][0], "%o'malley'; DROP TABLE memories;--%")

    def test_dossier_memory_is_private(self):
        import urllib.request as ur
        with patch.object(ur, "urlopen") as uo:
            fs.remember_dossier("X", "s")
        body = json.loads(uo.call_args[0][0].data)
        self.assertEqual(body["metadata"]["privacy"], "private")

    def test_known_facts_reach_the_prompt(self):
        fs.nj.call_openrouter.reset_mock()
        fs.summarize("Mookie", ["@m"], ["mem"])
        self.assertIn("GROUND-TRUTH", fs.nj.call_openrouter.call_args[0][0])


class TestPerformance(unittest.TestCase):
    def test_source_links_on_10k_rows(self):
        rows = [(i, {"type": "fishbowl_stream", "url": f"https://y/{i % 50}", "channel": "c", "title": "t"})
                for i in range(10_000)]
        t0 = time.perf_counter()
        out = fs.source_links(rows, limit=100)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(out.count("\n") + 1, 50)            # deduped by url


class TestRetry(unittest.TestCase):
    def test_remember_dossier_fails_open(self):
        # RETRY GAP: remember_dossier — one POST to the memory server; failure is logged only
        import urllib.request as ur
        with patch.object(ur, "urlopen", side_effect=OSError("down")) as uo, redirect_stdout(io.StringIO()) as b:
            fs.remember_dossier("X", "s")
        self.assertEqual(uo.call_count, 1)
        self.assertIn("remember dossier X", b.getvalue())

    def test_extract_names_empty_llm(self):
        # RETRY GAP: extract_names/call_openrouter — one call; empty reply -> no names
        fs.nj.call_openrouter = MagicMock(return_value="")
        self.assertEqual(fs.extract_names("blob", "c"), [])
        self.assertEqual(fs.nj.call_openrouter.call_count, 1)

    def test_slack_failure_swallowed(self):
        fs.nova_config.post_both.side_effect = RuntimeError("slack down")
        with redirect_stdout(io.StringIO()) as b:
            fs.slack("x")
        fs.nova_config.post_both.side_effect = None
        self.assertIn("slack: slack down", b.getvalue())


class TestUnit(unittest.TestCase):
    def test_source_links_reddit_and_skips(self):
        rows = [(1, {"type": "reddit", "post_id": "t3_abc", "subreddit": "watches", "title": "x" * 200}),
                (2, {"type": "reddit", "post_id": "", "subreddit": "w"}), (3, None)]
        out = fs.source_links(rows)
        self.assertIn("(https://www.reddit.com/r/watches/comments/abc/)", out)
        self.assertEqual(out.count("- ["), 1)
        self.assertIn("…", out)

    def test_extract_names_filters(self):
        fs.nj.call_openrouter = MagicMock(return_value="Nicholas, A, " + "x" * 60 + "\nMookie")
        self.assertEqual(fs.extract_names("b", "c"), ["Nicholas", "Mookie"])


class TestIntegration(unittest.TestCase):
    def test_discover_guests_skips_cast_and_marks_scanned(self):
        fs.nj.call_openrouter = MagicMock(return_value="Watch Nicholas, New Guy")
        mem = _Cur({"DISTINCT metadata": [("vid1", "@chan")], "part'='transcript": [("t" * 300,)]})
        ops = _Cur({"fishbowl_scanned WHERE": []})
        with redirect_stdout(io.StringIO()):
            fs.discover_guests(mem, ops)
        inserts = [p for s, p in zip(ops.sql, ops.params) if "INSERT INTO fishbowl_people" in s]
        self.assertEqual(inserts, [("New Guy", "New Guy")])
        self.assertTrue(any("INSERT INTO fishbowl_scanned" in s for s in ops.sql))

    def test_reads_memories_db_writes_ops_db(self):
        self.assertIn("dbname=nova_memories", fs.MEM_DSN)
        self.assertIn("dbname=nova_ops", fs.OPS_DSN)


class TestFunctional(unittest.TestCase):
    def _main(self, mems, dossier="Fresh dossier."):
        fs.nj.call_openrouter = MagicMock(return_value=dossier)
        fs.nova_config.post_both.reset_mock()
        mem = _Cur({"DISTINCT metadata": [], "FROM memories WHERE source='fishbowl' AND (": mems})
        ops = _Cur({"kind='guest'": [], "SELECT signature": [("It's Hard",)]})
        conns = [MagicMock(cursor=MagicMock(return_value=mem)), MagicMock(cursor=MagicMock(return_value=ops))]
        with patch.object(fs.psycopg2, "connect", side_effect=conns), redirect_stdout(io.StringIO()):
            fs.main()
        return ops

    def test_refreshes_every_dossier_without_vector_or_slack_spam(self):
        with patch.object(fs, "remember_dossier") as rd:
            ops = self._main([("Nicholas said things",)])
        ups = [p for s, p in zip(ops.sql, ops.params) if "summary=EXCLUDED.summary" in s]
        self.assertEqual(len(ups), len(fs.PEOPLE))
        self.assertEqual(ups[0][3], "Fresh dossier.")
        rd.assert_not_called()
        fs.nova_config.post_both.assert_not_called()

    def test_no_memories_posts_one_hourglass(self):
        ops = self._main([])
        self.assertFalse(any("summary=EXCLUDED.summary" in s for s in ops.sql))
        fs.nova_config.post_both.assert_called_once()
        self.assertIn("Fishbowl dossiers", fs.nova_config.post_both.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_import_smoke_with_stubbed_siblings(self):
        code = ("import sys, types; sys.path.insert(0, sys.argv[1])\n"
                "for n in ('nova_journal', 'nova_voice'): sys.modules[n] = types.ModuleType(n)\n"
                "import nova_fishbowl_summaries as f; print(len(f.PEOPLE))\n")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(int(r.stdout.strip()), len(fs.PEOPLE))

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(psycopg2, "connect", side_effect=AssertionError("import must not connect")):
            _load()


if __name__ == "__main__":
    unittest.main()
