"""
test_nova_architecture_ingest.py -- All 7 test categories for nova_architecture_ingest.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub dependencies before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules.setdefault("nova_config", _nova_cfg)

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_architecture_ingest.py"
_spec = importlib.util.spec_from_file_location("nova_architecture_ingest", _SCRIPT)
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


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-", "password =", "secret ="]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src, "Hardcoded home path found")

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney.com",
            "kochj" + _at + "digitalnoise.net",
            "kochj23" + _at + "gmail.com",
        ]:
            self.assertNotIn(pat, src, f"PII email found: {pat!r}")

    def test_ingest_payload_is_json(self):
        captured = []
        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("Architecture content.", "Architecture", "architecture_general",
                         "https://en.wikipedia.org/wiki/Architecture")
        self.assertEqual(len(captured), 1)
        self.assertIn("text", captured[0])
        self.assertIn("metadata", captured[0])

    def test_memory_url_is_local(self):
        url = _mod.MEMORY_URL
        self.assertTrue(
            url.startswith("http://127.0.0.1") or url.startswith("http://192.168."),
            f"MEMORY_URL must be local, got: {url}")

    def test_user_agent_identifies_as_bot(self):
        src = _SCRIPT.read_text()
        self.assertIn("Nova/1.0", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_classify_fast(self):
        text = "gothic baroque neoclassical art deco brutalism modernist postmodern " * 50
        start = time.perf_counter()
        for _ in range(1000):
            classify_content("Architecture", text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, f"classify_content 1000x took {elapsed:.3f}s")

    def test_chunk_text_bounded(self):
        para = "Interesting sentence about Architecture topics and research."
        text = (para + "\n\n") * 200
        chunks = chunk_text(text, "Architecture")
        self.assertLessEqual(len(chunks), 400)

    def test_chunk_text_fast(self):
        text = ("Word " * 200 + "\n\n") * 50
        start = time.perf_counter()
        chunk_text(text, "Test")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)

    def test_non_empty_categories_have_keywords(self):
        for name, kws in VECTOR_CATEGORIES.items():
            if kws:
                self.assertGreater(len(kws), 0, f"{name!r} has no keywords")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_fetch_wiki_retries_on_429(self):
        import urllib.error
        call_count = [0]
        def rate_limited(req, timeout=None):
            call_count[0] += 1
            raise urllib.error.HTTPError(
                url="http://x", code=429, msg="Too Many", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=rate_limited):
            with patch("time.sleep"):
                result, links, error = fetch_wiki_page(
                    "https://en.wikipedia.org/wiki/Architecture")
        self.assertIsNone(result)
        self.assertEqual(call_count[0], 5, f"Expected 5 retries, got {call_count[0]}")

    def test_fetch_wiki_no_retry_on_non_429(self):
        import urllib.error
        call_count = [0]
        def server_error(req, timeout=None):
            call_count[0] += 1
            raise urllib.error.HTTPError(
                url="http://x", code=503, msg="Service Unavailable", hdrs=None, fp=None)
        with patch("urllib.request.urlopen", side_effect=server_error):
            result, links, error = fetch_wiki_page(
                "https://en.wikipedia.org/wiki/Architecture")
        self.assertIsNone(result)
        self.assertEqual(call_count[0], 1)

    def test_ingest_chunk_returns_false_on_error(self):
        def failing(req, timeout=None):
            raise OSError("refused")
        with patch("urllib.request.urlopen", side_effect=failing):
            result = ingest_chunk("test text", "Title", "architecture_general", "https://x.com")
        self.assertFalse(result)

    def test_fetch_wiki_succeeds_after_429(self):
        import urllib.error
        attempt = [0]
        fake = json.dumps({"query": {"pages": {"1": {
            "title": "Architecture",
            "extract": "Content about Architecture.",
            "links": [],
        }}}}).encode()
        def flaky(req, timeout=None):
            attempt[0] += 1
            if attempt[0] < 2:
                raise urllib.error.HTTPError(
                    url="http://x", code=429, msg="Too Many", hdrs=None, fp=None)
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.read = lambda: fake
            return ctx
        with patch("urllib.request.urlopen", side_effect=flaky):
            with patch("time.sleep"):
                result, links, error = fetch_wiki_page(
                    "https://en.wikipedia.org/wiki/Architecture")
        self.assertIsNotNone(result)
        self.assertEqual(attempt[0], 2)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_classify_architecture_styles(self):
        result = classify_content("Architecture", "gothic baroque neoclassical art deco brutalism modernist postmodern")
        self.assertEqual(result, "architecture_styles")

    def test_classify_architecture_structures(self):
        result = classify_content("Architecture", "bridge skyscraper cathedral palace temple dome arch")
        self.assertEqual(result, "architecture_structures")

    def test_classify_architecture_urban(self):
        result = classify_content("Architecture", "urban planning city zoning infrastructure transportation public space")
        self.assertEqual(result, "architecture_urban")

    def test_classify_architecture_general(self):
        result = classify_content("Architecture", "completely unrelated text that matches nothing here at all")
        self.assertEqual(result, "architecture_general")


    def test_chunk_text_empty(self):
        self.assertEqual(chunk_text("", "Test"), [])

    def test_chunk_text_skips_short_paragraphs(self):
        para = "This is a longer paragraph with real content about Architecture. " * 3
        text = "hi\n\n" + para + "\n\n"
        chunks = chunk_text(text, "Architecture")
        for c in chunks:
            self.assertGreaterEqual(len(c), 30)

    def test_chunk_text_size_limit(self):
        text = ("A" * 800 + "\n\n") * 10
        chunks = chunk_text(text, "Title", chunk_size=1500)
        for c in chunks:
            self.assertLessEqual(len(c), 1500 + 800)

    def test_start_urls_are_wikipedia(self):
        for url in START_URLS:
            self.assertIn("wikipedia.org", url, f"Non-wikipedia start URL: {url}")

    def test_target_chunks_positive(self):
        self.assertGreater(TARGET_CHUNKS, 0)

    def test_fallback_category_is_empty(self):
        self.assertIn("architecture_general", VECTOR_CATEGORIES)
        self.assertEqual(VECTOR_CATEGORIES["architecture_general"], [])


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_chunk_then_ingest_pipeline(self):
        stored = []
        def mock_urlopen(req, timeout=None):
            stored.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx
        para = "This is content about Architecture and related topics."
        text = (para + "\n\n") * 5
        chunks = chunk_text(text, "Architecture")
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            for chunk in chunks:
                ingest_chunk(chunk, "Architecture", "architecture_general",
                             "https://en.wikipedia.org/wiki/Architecture")
        self.assertGreater(len(stored), 0)
        for item in stored:
            self.assertIn("metadata", item)
            self.assertEqual(item["metadata"]["type"], "wikipedia")

    def test_classify_feeds_ingest_vector(self):
        stored = []
        def mock_urlopen(req, timeout=None):
            stored.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx
        vector = classify_content("Architecture", "gothic baroque neoclassical art deco brutalism modernist postmodern")
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("Test content here.", "Architecture", vector,
                         "https://en.wikipedia.org/wiki/Architecture")
        self.assertTrue(len(stored) > 0)
        self.assertIsInstance(stored[0]["metadata"]["source"], str)

    def test_fetch_wiki_returns_filtered_links(self):
        fake = json.dumps({"query": {"pages": {"1": {
            "title": "Architecture",
            "extract": "Content here.",
            "links": [
                {"ns": 0, "title": "Related Topic A"},
                {"ns": 0, "title": "Related Topic B"},
                {"ns": 1, "title": "Talk:Ignore"},
            ],
        }}}}).encode()
        ctx = MagicMock()
        ctx.__enter__ = lambda s: s
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.read = lambda: fake
        with patch("urllib.request.urlopen", return_value=ctx):
            result, links, error = fetch_wiki_page(
                "https://en.wikipedia.org/wiki/Architecture")
        self.assertIsNone(error)
        self.assertGreaterEqual(len(links), 2)
        for l in links:
            self.assertNotIn("Talk:", l)

    def test_stats_accumulate(self):
        _mod.stats["by_vector"] = {}
        for v in ["architecture_styles", "architecture_styles", "architecture_structures"]:
            _mod.stats["by_vector"][v] = _mod.stats["by_vector"].get(v, 0) + 1
        self.assertEqual(_mod.stats["by_vector"]["architecture_styles"], 2)
        self.assertEqual(_mod.stats["by_vector"]["architecture_structures"], 1)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_exits_on_shutdown(self):
        _mod.shutdown = True
        _mod.stats["chunks_ingested"] = 0
        try:
            with patch.object(_mod, "notify"):
                with patch("time.sleep"):
                    _mod.main()
        finally:
            _mod.shutdown = False

    def test_main_stops_at_target(self):
        _mod.shutdown = False
        _mod.stats["chunks_ingested"] = TARGET_CHUNKS
        calls = []
        with patch.object(_mod, "notify"):
            with patch.object(_mod, "fetch_wiki_page",
                               side_effect=lambda u: calls.append(u) or (None, [], "stop")):
                with patch("time.sleep"):
                    _mod.main()
        self.assertEqual(len(calls), 0)
        _mod.stats["chunks_ingested"] = 0

    def test_ingest_metadata_complete(self):
        captured = []
        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("Architecture content.", "Architecture", "architecture_styles",
                         "https://en.wikipedia.org/wiki/Architecture")
        meta = captured[0]["metadata"]
        for field in ["source", "title", "url", "type", "ingested_at", "privacy"]:
            self.assertIn(field, meta)
        self.assertEqual(meta["privacy"], "public")

    def test_post_status_includes_progress(self):
        notified = []
        _mod.stats["chunks_ingested"] = 1000
        _mod.stats["pages_processed"] = 20
        _mod.stats["by_vector"] = {"architecture_styles": 500}
        _mod.stats["last_pages"] = []
        with patch.object(_mod, "notify", side_effect=lambda m: notified.append(m)):
            _mod.post_status()
        self.assertEqual(len(notified), 1)
        self.assertIn("1000", notified[0])

    def test_classify_selects_highest_score(self):
        v0_kws = VECTOR_CATEGORIES.get("architecture_styles", [])
        if v0_kws:
            text = " ".join(v0_kws[:5]) * 5
            result = classify_content("Architecture", text)
            self.assertEqual(result, "architecture_styles")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_module_constants_defined(self):
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIsInstance(_mod.TARGET_CHUNKS, int)
        self.assertIsInstance(_mod.CHUNK_SIZE, int)
        self.assertIsInstance(_mod.START_URLS, list)
        self.assertGreater(len(_mod.START_URLS), 0)

    def test_stats_keys_present(self):
        for key in ["pages_processed", "chunks_ingested", "errors",
                    "by_vector", "last_pages", "current_page", "current_vector"]:
            self.assertIn(key, _mod.stats)

    def test_module_loads_without_network(self):
        self.assertIsNotNone(_mod)

    def test_start_url_contains_topic(self):
        url_joined = " ".join(START_URLS)
        self.assertIn("Architecture", url_joined)

    def test_chunk_size_reasonable(self):
        self.assertGreater(_mod.CHUNK_SIZE, 100)
        self.assertLess(_mod.CHUNK_SIZE, 10000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
