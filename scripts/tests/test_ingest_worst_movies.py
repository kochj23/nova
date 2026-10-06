#!/usr/bin/env python3
"""Tests for ingest_worst_movies.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). All HTTP (script fetch + vector store) and the notify bus are
mocked; HOME is redirected to a tempdir for load so the real ~/.openclaw/logs is never touched.
Fully offline. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

# Redirect HOME so the module-level logging FileHandler writes into a throwaway dir, not real logs.
_TMP_HOME = tempfile.mkdtemp(prefix="iwm-home-")
_OLD_HOME = os.environ.get("HOME")
os.environ["HOME"] = _TMP_HOME


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


iwm = _load("ingest_worst_movies_t", SCRIPTS / "ingest_worst_movies.py")
if _OLD_HOME is not None:
    os.environ["HOME"] = _OLD_HOME
SRC = (SCRIPTS / "ingest_worst_movies.py").read_text()
iwm.notify = mock.MagicMock()   # neutralize the notification bus


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_log_path_under_openclaw_and_vector_from_config(self):
        self.assertIn('".openclaw/logs/ingest_worst_movies_scripts.log"', SRC)
        self.assertIn("VECTOR_URL = nova_config.VECTOR_URL", SRC)


class TestPerformance(unittest.TestCase):
    def test_chunk_10k_words(self):
        text = "word " * 10_000
        t0 = time.perf_counter()
        chunks = iwm.chunk_text(text, 500)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(chunks), 20)


class TestRetry(unittest.TestCase):
    def test_fetch_url_failure_returns_none(self):
        # RETRY GAP: fetch_url()/urlopen — single attempt; HTTP/URL errors return None (caller tries next source)
        import urllib.error
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("dns")) as u:
            self.assertIsNone(iwm.fetch_url("https://x"))
        self.assertEqual(u.call_count, 1)

    def test_store_chunk_failure_returns_false(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("mem down")):
            self.assertFalse(iwm.store_chunk("t", "comedy", "Cats", 2019, 1, 1))


class TestUnit(unittest.TestCase):
    def test_imsdb_slug_variants(self):
        slugs = iwm.title_to_imsdb_slugs("The Last Airbender (2010)")
        self.assertIn("The-Last-Airbender", slugs)
        self.assertIn("Last-Airbender", slugs)      # The- prefix stripped
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_springfield_url_prefix_variants(self):
        urls = iwm.get_springfield_urls("The Emoji Movie")
        self.assertTrue(any(u.endswith("the-emoji-movie") for u in urls))
        self.assertTrue(any(u.endswith("=emoji-movie") for u in urls))

    def test_chunk_skips_tiny_trailing(self):
        self.assertEqual(iwm.chunk_text("hi there", 500), [])   # <50 chars dropped
        self.assertEqual(len(iwm.chunk_text("w " * 60, 500)), 1)

    def test_screenplaysio_url_lowercased(self):
        self.assertEqual(iwm.get_screenplaysio_url("Gigli (2003)"), "https://www.screenplays.io/screenplay/gigli")


class TestIntegration(unittest.TestCase):
    def test_imsdb_extractor_needs_substantial_text(self):
        html = '<td class="scrtext"><pre>' + ("INT. ROOM - DAY\n" * 500) + "</pre></td>"
        self.assertIsNotNone(iwm.extract_imsdb_script(html))
        self.assertIsNone(iwm.extract_imsdb_script('<td class="scrtext">tiny</td>'))

    def test_springfield_extractor(self):
        html = '<div class="scrolling-script-container">' + ("dialogue line here. " * 300) + "</div>"
        text = iwm.extract_springfield_script(html)
        self.assertIsNotNone(text)
        self.assertIn("dialogue line", text)

    def test_store_chunk_payload_shape(self):
        cap = {}
        def urlopen(req, timeout=None):
            cap["url"] = req.full_url; cap["body"] = json.loads(req.data)
            return mock.MagicMock(__enter__=lambda s: s, __exit__=lambda *a: False)
        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            ok = iwm.store_chunk("the text", "horror", "BloodRayne", 2005, 2, 4)
        self.assertTrue(ok)
        self.assertEqual(cap["body"]["source"], "horror")
        self.assertEqual(cap["body"]["metadata"]["chunk"], "2/4")
        self.assertEqual(cap["body"]["metadata"]["type"], "movie_script")


class TestFunctional(unittest.TestCase):
    def test_process_movie_ingests_chunks(self):
        with mock.patch.object(iwm, "fetch_script", return_value="word " * 1200), \
             mock.patch.object(iwm, "store_chunk", return_value=True) as store:
            res = iwm.process_movie({"title": "Cats", "year": 2019, "genre": "comedy"})
        self.assertEqual(res["status"], "ingested")
        self.assertEqual(res["chunks"], store.call_count)
        self.assertGreater(res["chunks"], 0)

    def test_process_movie_not_found(self):
        with mock.patch.object(iwm, "fetch_script", return_value=None), \
             mock.patch.object(iwm, "store_chunk") as store:
            res = iwm.process_movie({"title": "Unknown Flop", "year": 2000, "genre": "drama"})
        self.assertEqual(res["status"], "not_found")
        store.assert_not_called()

    def test_main_summarizes_and_notifies(self):
        def proc(m):
            return {"title": m["title"], "status": "ingested", "chunks": 3, "failed": 0}
        with mock.patch.object(iwm, "MOVIES", [{"title": "Cats", "year": 2019, "genre": "comedy"}]), \
             mock.patch.object(iwm, "process_movie", side_effect=proc), \
             mock.patch.object(iwm, "notify") as notify:
            iwm.main()
        titles = [c.args[0] for c in notify.call_args_list]
        self.assertIn("Worst Movies Script Ingest starting", titles)
        self.assertIn("Worst Movies Script Ingest Complete", titles)

    def test_main_contains_crash_in_one_movie(self):
        with mock.patch.object(iwm, "MOVIES", [{"title": "Boom", "year": 2000, "genre": "x"}]), \
             mock.patch.object(iwm, "process_movie", side_effect=RuntimeError("kaboom")), \
             mock.patch.object(iwm, "notify"):
            iwm.main()   # must not raise; error is captured into results


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        home = tempfile.mkdtemp(prefix="iwm-frame-")
        r = subprocess.run([sys.executable, "-c", "import ingest_worst_movies"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30,
                           env={**os.environ, "HOME": home, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("WORST MOVIES SCRIPT INGEST", r.stdout)


if __name__ == "__main__":
    unittest.main()
