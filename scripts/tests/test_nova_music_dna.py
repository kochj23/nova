#!/usr/bin/env python3
"""Tests for nova_music_dna.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
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
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_music_dna.py"
SRC = SCRIPT.read_text()


def _stub_modules():
    cfg = types.ModuleType("nova_config")
    cfg.NOVA_HOST = "127.0.0.1"
    cfg.post_both = MagicMock()
    nn = types.ModuleType("nova_notify")
    nn.notify = MagicMock()
    return {"nova_config": cfg, "nova_notify": nn}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()):      # binds the stubbed nova_config/notify at import; restored after
        spec.loader.exec_module(mod)
    return mod


md = _load("music_dna_under_test", SCRIPT)
md.notify = MagicMock()                                  # module-level stub: no Slack ever


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _rows(*pairs):
    return [{"source": s, "text": t, "similarity": sim} for s, t, sim in pairs]


def _search(query, rows, embed=None, connect_exc=None):
    """Run search_music_vectors fully offline; returns (connections, fake_conn, urlopen mock)."""
    conn = types.SimpleNamespace(fetch=AsyncMock(return_value=rows), close=AsyncMock())
    uo = MagicMock(return_value=_Resp({"embedding": embed or [0.1, 0.2, 0.3]}))
    connect = AsyncMock(return_value=conn, side_effect=connect_exc)
    with patch.object(md.urllib.request, "urlopen", uo), patch.object(md.asyncpg, "connect", connect):
        out = asyncio.run(md.search_music_vectors(query))
    return out, conn, uo


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", md.DB_DSN)

    def test_sql_is_parameterized_and_query_text_never_enters_sql(self):
        self.assertIsNone(re.search(r'fetch\(\s*f"', SRC))
        inj = "x'); DROP TABLE memories; --"
        _, conn, _ = _search(inj, [])
        sql, *params = conn.fetch.call_args[0]
        self.assertNotIn("DROP", sql)
        self.assertIn("$1::vector", sql)
        self.assertEqual(params[1], md.MUSIC_SOURCES)            # source filter is a bound array, not interpolated
        self.assertEqual(params[2], md.TOP_K)

    def test_only_reads_memories(self):
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", SRC))


class TestPerformance(unittest.TestCase):
    def test_genre_heuristics_fast_on_10k_queries(self):
        queries = [f"{w} track {i}" for i, w in enumerate(["coltrane", "slayer", "aphex twin", "black flag", "nothing"] * 2000)]
        t0 = time.perf_counter()
        for q in queries:
            fam = md.detect_query_genre(q)
            md.is_cross_genre(md.MUSIC_SOURCES[len(q) % len(md.MUSIC_SOURCES)], fam)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_format_results_is_linear_and_truncates(self):
        conns = [md.Connection("jazz", "t" * 200, 0.5, 0.5, False) for _ in range(10_000)]
        t0 = time.perf_counter()
        out = md.format_results("q", conns)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertIn("Cross-genre connections: 0/10000", out)


class TestRetry(unittest.TestCase):
    def test_embedding_fetch_is_one_shot(self):
        # RETRY GAP: get_embedding — a single urlopen; an embed-server outage raises straight to the caller
        uo = MagicMock(side_effect=OSError("embed down"))
        with patch.object(md.urllib.request, "urlopen", uo):
            with self.assertRaises(OSError):
                md.get_embedding("q")
        self.assertEqual(uo.call_count, 1)

    def test_db_connect_is_one_shot_and_propagates(self):
        # RETRY GAP: search_music_vectors/asyncpg.connect — no retry, no fallback result
        with self.assertRaises(ConnectionError):
            _search("q", [], connect_exc=ConnectionError("pg down"))

    def test_connection_closed_even_when_fetch_fails(self):
        conn = types.SimpleNamespace(fetch=AsyncMock(side_effect=RuntimeError("boom")), close=AsyncMock())
        with patch.object(md.urllib.request, "urlopen", MagicMock(return_value=_Resp({"embedding": [1.0]}))), \
             patch.object(md.asyncpg, "connect", AsyncMock(return_value=conn)):
            with self.assertRaises(RuntimeError):
                asyncio.run(md.search_music_vectors("q"))
        conn.close.assert_awaited_once()


class TestUnit(unittest.TestCase):
    def test_detect_query_genre(self):
        self.assertEqual(md.detect_query_genre(""), {"general"})
        self.assertEqual(md.detect_query_genre("nothing musical here"), {"general"})
        self.assertEqual(md.detect_query_genre("Black Flag nervous breakdown"), {"punk"})
        self.assertEqual(md.detect_query_genre("Coltrane plays techno"), {"jazz", "edm"})
        self.assertIn("rave", md.detect_query_genre("socal warehouse rave"))

    def test_is_cross_genre(self):
        self.assertFalse(md.is_cross_genre("hardcore_punk", {"punk"}))
        self.assertTrue(md.is_cross_genre("jazz_history", {"punk"}))
        self.assertTrue(md.is_cross_genre("unknown_source", {"punk"}))     # unknown = interesting
        self.assertFalse(md.is_cross_genre("edm", {"rave"}))               # edm is in both edm and rave families

    def test_every_music_source_belongs_to_a_family(self):
        all_fam = set().union(*md.GENRE_FAMILIES.values())
        self.assertEqual(set(md.MUSIC_SOURCES) - all_fam, set())
        self.assertEqual(len(md.MUSIC_SOURCES), len(set(md.MUSIC_SOURCES)))

    def test_format_results_edges(self):
        out = md.format_results("q", [])
        self.assertIn('Music DNA: "q"', out)
        self.assertIn("Cross-genre connections: 0/0", out)
        c = md.Connection("jazz", "txt", 0.1234, 0.2734, True)
        out = md.format_results("q", [c])
        self.assertIn("***  1. [jazz] (sim: 0.123) [+boost]", out)

    def test_get_embedding_posts_json(self):
        uo = MagicMock(return_value=_Resp({"embedding": [0.5]}))
        with patch.object(md.urllib.request, "urlopen", uo):
            self.assertEqual(md.get_embedding("hello"), [0.5])
        req = uo.call_args[0][0]
        self.assertEqual(json.loads(req.data), {"text": "hello"})
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.endswith(":18790/embed"))


class TestIntegration(unittest.TestCase):
    def test_embed_url_comes_from_nova_config_host(self):
        self.assertEqual(md.EMBED_URL, "http://127.0.0.1:18790/embed")
        self.assertIn("nova_memories", md.DB_DSN)

    def test_cross_genre_boost_floats_to_top_and_truncates_text(self):
        rows = _rows(("hardcore_punk", "same family " + "x" * 300, 0.90), ("jazz_history", "cross family", 0.80))
        out, conn, _ = _search("Black Flag", rows)
        self.assertEqual([c.source for c in out], ["jazz_history", "hardcore_punk"])
        self.assertAlmostEqual(out[0].boosted_score, 0.80 + md.CROSS_GENRE_BOOST)
        self.assertEqual(len(out[1].text), 200)
        self.assertEqual(conn.fetch.call_args[0][1], "[0.1,0.2,0.3]")   # embedding serialized as a pgvector literal

    def test_display_cap(self):
        rows = _rows(*[("jazz", f"t{i}", 0.5) for i in range(30)])
        out, _, _ = _search("jazz", rows)
        self.assertEqual(len(out), md.DISPLAY_K)


class TestFunctional(unittest.TestCase):
    def test_find_connections_notifies_when_asked(self):
        rows = _rows(("jazz_history", "cross", 0.7), ("punk", "home", 0.9))
        conn = types.SimpleNamespace(fetch=AsyncMock(return_value=rows), close=AsyncMock())
        md.notify = MagicMock()
        with patch.object(md.urllib.request, "urlopen", MagicMock(return_value=_Resp({"embedding": [1.0]}))), \
             patch.object(md.asyncpg, "connect", AsyncMock(return_value=conn)):
            text = asyncio.run(md.find_connections("Black Flag", notify_slack=True))
        self.assertIn("Cross-genre connections: 1/2", text)
        md.notify.assert_called_once()
        self.assertEqual(md.notify.call_args[1]["body"], "1 cross-genre hits found.")
        self.assertEqual(md.notify.call_args[1]["category"], "media")

    def test_find_connections_silent_by_default(self):
        md.notify = MagicMock()
        conn = types.SimpleNamespace(fetch=AsyncMock(return_value=[]), close=AsyncMock())
        with patch.object(md.urllib.request, "urlopen", MagicMock(return_value=_Resp({"embedding": [1.0]}))), \
             patch.object(md.asyncpg, "connect", AsyncMock(return_value=conn)):
            asyncio.run(md.find_connections("q"))
        md.notify.assert_not_called()

    def test_main_golden_path_prints_results(self):
        conn = types.SimpleNamespace(fetch=AsyncMock(return_value=_rows(("jazz", "t", 0.5))), close=AsyncMock())
        buf = io.StringIO()
        with patch.object(sys, "argv", ["nova_music_dna.py", "Miles", "Davis"]), \
             patch.object(md.urllib.request, "urlopen", MagicMock(return_value=_Resp({"embedding": [1.0]}))), \
             patch.object(md.asyncpg, "connect", AsyncMock(return_value=conn)), redirect_stdout(buf):
            asyncio.run(md.main())
        self.assertIn('Searching 80 music vectors for: "Miles Davis"', buf.getvalue())
        self.assertIn("[jazz] (sim: 0.500)", buf.getvalue())

    def test_main_without_query_exits_1_before_any_io(self):
        uo = MagicMock(); connect = AsyncMock()
        with patch.object(sys, "argv", ["nova_music_dna.py"]), patch.object(md.urllib.request, "urlopen", uo), \
             patch.object(md.asyncpg, "connect", connect), redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as cm:
                asyncio.run(md.main())
        self.assertEqual(cm.exception.code, 1)
        uo.assert_not_called(); connect.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    asyncio.run(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_music_dna"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_no_args_prints_usage_and_exits_1(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage: nova_music_dna.py <query>", r.stdout)


if __name__ == "__main__":
    unittest.main()
