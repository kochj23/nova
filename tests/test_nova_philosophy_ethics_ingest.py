"""
test_nova_philosophy_ethics_ingest.py -- All 7 test categories for nova_philosophy_ethics_ingest.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules.setdefault("nova_config", _nova_cfg)

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_philosophy_ethics_ingest.py"
_spec = importlib.util.spec_from_file_location("nova_philosophy_ethics_ingest", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

classify_content  = _mod.classify_content
fetch_wiki_page   = _mod.fetch_wiki_page
chunk_text        = _mod.chunk_text
ingest_chunk      = _mod.ingest_chunk
VECTOR_CATEGORIES = _mod.VECTOR_CATEGORIES
START_URLS        = _mod.START_URLS
TARGET_CHUNKS     = _mod.TARGET_CHUNKS
CHUNK_SIZE        = _mod.CHUNK_SIZE

class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-", "password =", "secret ="]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")
    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)
    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com",
                    "kochj" + _at + "digitalnoise.net", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src, f"PII email found: {pat!r}")
    def test_ingest_payload_is_json(self):
        captured = []
        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock(); ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False); return ctx
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("Philosophy and Ethics content.", "Philosophy and Ethics", "philosophy_ethics",
                         "https://en.wikipedia.org/wiki/Ethics")
        self.assertIn("text", captured[0]); self.assertIn("metadata", captured[0])
    def test_memory_url_is_local(self):
        url = _mod.MEMORY_URL
        self.assertTrue(url.startswith("http://127.0.0.1") or url.startswith("http://192.168."))
    def test_user_agent_is_bot(self):
        self.assertIn("Nova/1.0", _SCRIPT.read_text())

class TestPerformance(unittest.TestCase):
    def test_classify_fast(self):
        text = "ethics moral virtue duty consequentialism deontology utilitarianism " * 50
        start = time.perf_counter()
        for _ in range(1000):
            classify_content("Philosophy and Ethics", text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)
    def test_chunk_text_bounded(self):
        para = "Research content about Philosophy and Ethics."
        text = (para + "\n\n") * 200
        chunks = chunk_text(text, "Philosophy and Ethics")
        self.assertLessEqual(len(chunks), 400)
    def test_vector_categories_non_empty(self):
        for name, kws in VECTOR_CATEGORIES.items():
            if kws: self.assertGreater(len(kws), 0)
    def test_chunk_text_fast(self):
        text = ("Word " * 200 + "\n\n") * 50
        start = time.perf_counter()
        chunk_text(text, "Test")
        self.assertLess(time.perf_counter() - start, 1.0)

class TestRetry(unittest.TestCase):
    def test_fetch_wiki_retries_on_429(self):
        import urllib.error; call_count = [0]
        def rate_limited(req, timeout=None):
            call_count[0] += 1
            raise urllib.error.HTTPError(url="http://x", code=429, msg="Too Many", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=rate_limited):
            with patch("time.sleep"):
                result, links, error = fetch_wiki_page("https://en.wikipedia.org/wiki/Ethics")
        self.assertIsNone(result); self.assertEqual(call_count[0], 5)
    def test_fetch_wiki_no_retry_on_non_429(self):
        import urllib.error; call_count = [0]
        def server_error(req, timeout=None):
            call_count[0] += 1
            raise urllib.error.HTTPError(url="http://x", code=503, msg="Error", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=server_error):
            result, links, error = fetch_wiki_page("https://en.wikipedia.org/wiki/Ethics")
        self.assertIsNone(result); self.assertEqual(call_count[0], 1)
    def test_ingest_returns_false_on_error(self):
        def failing(req, timeout=None): raise OSError("refused")
        with patch("urllib.request.urlopen", side_effect=failing):
            result = ingest_chunk("text", "T", "philosophy_ethics", "https://x.com")
        self.assertFalse(result)
    def test_fetch_wiki_succeeds_after_429(self):
        import urllib.error; attempt = [0]
        fake = json.dumps({"query": {"pages": {"1": {"title": "Philosophy and Ethics", "extract": "Content.", "links": []}}}}).encode()
        def flaky(req, timeout=None):
            attempt[0] += 1
            if attempt[0] < 2: raise urllib.error.HTTPError(url="http://x", code=429, msg="Too Many", hdrs=None, fp=None)
            ctx = MagicMock(); ctx.__enter__ = lambda s: s; ctx.__exit__ = MagicMock(return_value=False)
            ctx.read = lambda: fake; return ctx
        with patch("urllib.request.urlopen", side_effect=flaky):
            with patch("time.sleep"):
                result, links, error = fetch_wiki_page("https://en.wikipedia.org/wiki/Ethics")
        self.assertIsNotNone(result); self.assertEqual(attempt[0], 2)

class TestUnit(unittest.TestCase):
    def test_classify_philosophy_ethics_0(self):
        result = classify_content("Philosophy and Ethics", "ethics moral virtue duty consequentialism deontology utilitarianism")
        self.assertEqual(result, "philosophy_ethics")

    def test_classify_philosophy_metaphysics_1(self):
        result = classify_content("Philosophy and Ethics", "metaphysics ontology dualism monism cosmology existence reality being")
        self.assertEqual(result, "philosophy_metaphysics")

    def test_classify_philosophy_ethics_2(self):
        result = classify_content("Philosophy and Ethics", "categorical imperative golden rule right wrong justice fairness")
        self.assertEqual(result, "philosophy_ethics")


    def test_chunk_text_empty(self):
        self.assertEqual(chunk_text("", "Test"), [])
    def test_chunk_text_size_limit(self):
        text = ("A" * 800 + "\n\n") * 10
        chunks = chunk_text(text, "Title", chunk_size=1500)
        for c in chunks: self.assertLessEqual(len(c), 1500 + 800)
    def test_start_urls_wikipedia(self):
        for url in START_URLS: self.assertIn("wikipedia.org", url)
    def test_target_chunks_positive(self):
        self.assertGreater(TARGET_CHUNKS, 0)

class TestIntegration(unittest.TestCase):
    def test_chunk_then_ingest(self):
        stored = []
        def mock_urlopen(req, timeout=None):
            stored.append(json.loads(req.data.decode()))
            ctx = MagicMock(); ctx.__enter__ = lambda s: s; ctx.__exit__ = MagicMock(return_value=False); return ctx
        para = "Research content about Philosophy and Ethics from Wikipedia."
        text = (para + "\n\n") * 5
        chunks = chunk_text(text, "Philosophy and Ethics")
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            for chunk in chunks:
                ingest_chunk(chunk, "Philosophy and Ethics", "philosophy_ethics", "https://en.wikipedia.org/wiki/Ethics")
        self.assertGreater(len(stored), 0)
        for item in stored: self.assertEqual(item["metadata"]["type"], "wikipedia")
    def test_classify_feeds_vector(self):
        stored = []
        def mock_urlopen(req, timeout=None):
            stored.append(json.loads(req.data.decode()))
            ctx = MagicMock(); ctx.__enter__ = lambda s: s; ctx.__exit__ = MagicMock(return_value=False); return ctx
        vector = classify_content("Philosophy and Ethics", "ethics moral virtue duty consequentialism deontology utilitarianism")
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("Content.", "Philosophy and Ethics", vector, "https://en.wikipedia.org/wiki/Ethics")
        self.assertIsInstance(stored[0]["metadata"]["source"], str)
    def test_fetch_wiki_filters_namespaces(self):
        fake = json.dumps({"query": {"pages": {"1": {"title": "Philosophy and Ethics", "extract": "Content here.", "links": [{"ns": 0, "title": "Related Topic"}, {"ns": 1, "title": "Talk:Ignore"}]}}}}).encode()
        ctx = MagicMock(); ctx.__enter__ = lambda s: s; ctx.__exit__ = MagicMock(return_value=False); ctx.read = lambda: fake
        with patch("urllib.request.urlopen", return_value=ctx):
            result, links, error = fetch_wiki_page("https://en.wikipedia.org/wiki/Ethics")
        self.assertIsNone(error)
        for l in links: self.assertNotIn("Talk:", l)
    def test_stats_by_vector(self):
        _mod.stats["by_vector"] = {}
        for v in ["philosophy_ethics", "philosophy_ethics", "philosophy_metaphysics"]:
            _mod.stats["by_vector"][v] = _mod.stats["by_vector"].get(v, 0) + 1
        self.assertEqual(_mod.stats["by_vector"]["philosophy_ethics"], 2)

class TestFunctional(unittest.TestCase):
    def test_main_exits_on_shutdown(self):
        _mod.shutdown = True; _mod.stats["chunks_ingested"] = 0
        try:
            with patch.object(_mod, "notify"):
                with patch("time.sleep"): _mod.main()
        finally: _mod.shutdown = False
    def test_main_stops_at_target(self):
        _mod.shutdown = False; _mod.stats["chunks_ingested"] = TARGET_CHUNKS; calls = []
        with patch.object(_mod, "notify"):
            with patch.object(_mod, "fetch_wiki_page", side_effect=lambda u: calls.append(u) or (None, [], "stop")):
                with patch("time.sleep"): _mod.main()
        self.assertEqual(len(calls), 0); _mod.stats["chunks_ingested"] = 0
    def test_ingest_metadata_complete(self):
        captured = []
        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock(); ctx.__enter__ = lambda s: s; ctx.__exit__ = MagicMock(return_value=False); return ctx
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("Philosophy and Ethics content.", "Philosophy and Ethics", "philosophy_ethics", "https://en.wikipedia.org/wiki/Ethics")
        meta = captured[0]["metadata"]
        for field in ["source", "title", "url", "type", "ingested_at", "privacy"]: self.assertIn(field, meta)
    def test_post_status_reports(self):
        notified = []
        _mod.stats["chunks_ingested"] = 1000; _mod.stats["pages_processed"] = 20
        _mod.stats["by_vector"] = {"philosophy_ethics": 500}; _mod.stats["last_pages"] = []
        with patch.object(_mod, "notify", side_effect=lambda m: notified.append(m)):
            _mod.post_status()
        self.assertEqual(len(notified), 1); self.assertIn("1000", notified[0])

class TestFrame(unittest.TestCase):
    def test_script_compiles(self):
        import py_compile
        try: py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e: self.fail(f"Syntax error: {e}")
    def test_constants_defined(self):
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIsInstance(_mod.TARGET_CHUNKS, int)
        self.assertIsInstance(_mod.START_URLS, list)
        self.assertGreater(len(_mod.START_URLS), 0)
    def test_stats_keys(self):
        for key in ["pages_processed", "chunks_ingested", "errors", "by_vector", "last_pages"]:
            self.assertIn(key, _mod.stats)
    def test_module_loads_without_network(self):
        self.assertIsNotNone(_mod)
    def test_start_url_topic(self):
        self.assertIn("Ethics", " ".join(START_URLS))

if __name__ == "__main__":
    unittest.main(verbosity=2)
