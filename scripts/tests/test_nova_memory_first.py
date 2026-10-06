#!/usr/bin/env python3
"""Tests for nova_memory_first.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_memory_first.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_memory_first_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mf = _load()
mf._REDIS_AVAILABLE = False      # this copy never talks to a local Redis


def _ctx(payload):
    r = mock.MagicMock()
    r.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return r


def _mem(text, source="music", score=0.9):
    return {"text": text, "source": source, "score": score}


class _Server:
    """Fake memory server: routes /recall_batch, /recall, /search by URL."""
    def __init__(self, batch=None, recall=None, search=None, batch_fails=False):
        self.batch, self.rec, self.srch, self.batch_fails = batch or [], recall or [], search or [], batch_fails
        self.urls = []

    def __call__(self, req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        self.urls.append(url)
        if url.endswith("/recall_batch"):
            if self.batch_fails:
                raise OSError("batch down")
            return _ctx({"results": self.batch})
        if "/search?" in url:
            return _ctx({"results": self.srch})
        return _ctx({"memories": self.rec})


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_query_is_urlencoded_not_injected(self):
        srv = _Server()
        with mock.patch.object(mf.urllib.request, "urlopen", side_effect=srv):
            mf.recall("x&source=private_document", source="music")
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(srv.urls[0]).query)
        self.assertEqual(qs["source"], ["music"])
        self.assertEqual(qs["q"], ["x&source=private_document"])

    def test_cache_key_does_not_leak_query(self):
        k = mf._cache_key("my blood pressure secret", "health")
        self.assertNotIn("blood", k)
        self.assertRegex(k, r"^nova:memory:recall:[0-9a-f]{16}$")


class TestPerformance(unittest.TestCase):
    def test_classify_10k_queries(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            mf.classify_query(f"what raves did we go to in {i}?")
        self.assertLess(time.perf_counter() - t0, 15.0)

    def test_tight_timeouts_fit_gateway_budget(self):
        self.assertLessEqual(max(mf._HTTP_TIMEOUT, mf._BATCH_TIMEOUT, mf._SEARCH_TIMEOUT), 5.0)


class TestRetry(unittest.TestCase):
    def test_batch_failure_falls_back_to_individual_recalls(self):
        srv = _Server(recall=[_mem("solo")], batch_fails=True)
        qs = [{"q": "a", "source": "music"}, {"q": "b", "source": "email"}]
        with mock.patch.object(mf.urllib.request, "urlopen", side_effect=srv):
            out = mf.batch_recall(qs)
        self.assertEqual([r["memories"] for r in out], [[_mem("solo")], [_mem("solo")]])
        self.assertEqual(len(srv.urls), 3)                       # 1 failed batch + 2 fallback recalls

    def test_everything_down_fails_open(self):
        # RETRY GAP: recall / search — single attempt each with a 3s timeout; [] on failure
        with mock.patch.object(mf.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(mf.recall("q"), [])
            self.assertEqual(mf.search("q"), [])
            results, searched, labels = mf.memory_lookup("tell me about punk zines")
        self.assertEqual((results, searched), ([], []))
        self.assertIn("punk/hardcore", labels)


class TestUnit(unittest.TestCase):
    def test_classify_specific_and_default(self):
        s, labels, pref = mf.classify_query("what was my resting heart rate?")
        self.assertEqual((s, labels), (["apple_health", "health"], ["health"]))
        self.assertFalse(pref)
        self.assertEqual(mf.classify_query("zzqx"), (mf.DEFAULT_SOURCES, ["general"], False))
        self.assertTrue(mf.classify_query("who is Sam")[2])     # people -> prefer text search

    def test_classify_merges_without_duplicates(self):
        s, labels, _ = mf.classify_query("rave music at a punk show")
        self.assertEqual(len(s), len(set(s)))
        self.assertGreaterEqual(len(labels), 2)

    def test_format_result(self):
        self.assertEqual(mf.format_result(_mem("x" * 500, "music", 0.912), 1), "[1] (music (relevance: 0.91))\n" + "x" * 400)
        self.assertEqual(mf.format_result({"text": "t", "metadata": {"source": "m"}}, 2), "[2] (m)\nt")


class TestIntegration(unittest.TestCase):
    def test_lookup_batches_top4_sources_then_merges_broad(self):
        srv = _Server(batch=[{"memories": [_mem("rave flyer 2002"), _mem("rave flyer 2002")]},
                             {"memories": [_mem("scr list post", "email_archive")]}],
                      recall=[_mem("rave flyer 2002"), _mem("broad hit", "video")])
        with mock.patch.object(mf.urllib.request, "urlopen", side_effect=srv) as uo:
            results, searched, _ = mf.memory_lookup("what raves in 2002")
        batch_req = [c[0][0] for c in uo.call_args_list if not isinstance(c[0][0], str)][0]
        body = json.loads(batch_req.data)
        self.assertEqual([q["source"] for q in body["queries"]], ["music", "email_archive", "socal_rave", "music_history"])
        self.assertTrue(all(q["ef_search"] == 40 for q in body["queries"]))
        self.assertEqual([r["text"] for r in results], ["rave flyer 2002", "scr list post", "broad hit"])
        self.assertEqual(searched, ["music", "email_archive", "(all sources)"])


class TestFunctional(unittest.TestCase):
    def _main(self, argv, srv=None, rules=()):
        with mock.patch.object(sys, "argv", ["nova_memory_first.py"] + argv), \
                mock.patch.object(mf.urllib.request, "urlopen", side_effect=srv or _Server()), \
                mock.patch("nova_rules.get_active_rules", return_value=list(rules)), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            try:
                mf.main()
            except SystemExit as e:
                return out.getvalue(), e.code
        return out.getvalue(), None

    def test_found_prints_results_and_rules(self):
        out, _ = self._main(["what raves in 2002"], _Server(recall=[_mem("Together As One flyer")]),
                            rules=[{"topic": "global", "rule": "Check memory first"}])
        self.assertIn("MEMORY FOUND — 1 result(s)", out)
        self.assertIn("Together As One flyer", out)
        self.assertIn("- Check memory first", out)

    def test_nothing_found_and_usage(self):
        out, _ = self._main(["zzqx"])
        self.assertIn("NO MEMORIES FOUND for: zzqx", out)
        out, code = self._main([])
        self.assertEqual(code, 1)
        self.assertIn("Usage", out)

    def test_recency_question_routes_to_recent_memories_tool(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout="42 new")) as run:
            out, code = self._main(["how many memories were added in the past 48 hours"])
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args[0][0][-2:], ["--hours", "48"])
        self.assertIn("42 new", out)


class TestFrame(unittest.TestCase):
    def test_classify_cli_exits_zero(self):
        r = subprocess.run([sys.executable, str(PATH), "--classify", "my resting heart rate"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Classification: health", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_memory_first"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
