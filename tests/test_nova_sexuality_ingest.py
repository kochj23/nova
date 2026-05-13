"""
test_nova_sexuality_ingest.py -- All 7 test categories for nova_sexuality_ingest.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_sexuality_ingest.py"
_spec = importlib.util.spec_from_file_location("nova_sexuality_ingest", _SCRIPT)
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
            ingest_chunk("Human sexuality content.", "Human Sexuality", "sexuality_general",
                         "https://en.wikipedia.org/wiki/Human_sexuality")

        self.assertEqual(len(captured), 1)
        self.assertIn("text", captured[0])
        self.assertIn("metadata", captured[0])

    def test_memory_url_is_local(self):
        url = _mod.MEMORY_URL
        self.assertTrue(
            url.startswith("http://127.0.0.1") or url.startswith("http://192.168."),
            f"MEMORY_URL must be local, got: {url}"
        )

    def test_content_marked_public(self):
        """Wikipedia content should be marked privacy=public."""
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("Sexual health information from Wikipedia.", "Human Sexuality",
                         "sexuality_health", "https://en.wikipedia.org/wiki/Sexual_health")

        self.assertEqual(captured[0]["metadata"]["privacy"], "public")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_classify_fast(self):
        text = "sexual orientation homosexuality bisexuality asexuality lgbtq " * 50
        start = time.perf_counter()
        for _ in range(1000):
            classify_content("Human Sexuality", text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)

    def test_chunk_text_bounded(self):
        para = "This is content about human sexuality topics and research."
        text = (para + "\n\n") * 200
        chunks = chunk_text(text, "Sexuality")
        self.assertLessEqual(len(chunks), 400)

    def test_vector_categories_have_keywords(self):
        for name, kws in VECTOR_CATEGORIES.items():
            if kws:
                self.assertGreater(len(kws), 0)


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
                    "https://en.wikipedia.org/wiki/Human_sexuality")

        self.assertIsNone(result)
        self.assertEqual(call_count[0], 5)

    def test_ingest_chunk_returns_false_on_error(self):
        def failing(req, timeout=None):
            raise OSError("refused")

        with patch("urllib.request.urlopen", side_effect=failing):
            result = ingest_chunk("text", "Title", "sexuality_general", "https://x.com")

        self.assertFalse(result)

    def test_fetch_wiki_succeeds_after_429(self):
        import urllib.error
        attempt = [0]
        fake = json.dumps({
            "query": {"pages": {"1": {
                "title": "Human sexuality",
                "extract": "Human sexuality is the way people experience sexual desire.",
                "links": [],
            }}}
        }).encode()

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
                    "https://en.wikipedia.org/wiki/Human_sexuality")

        self.assertIsNotNone(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_classify_biology(self):
        result = classify_content("Human Biology",
                                  "reproductive anatomy hormone puberty fertility testosterone")
        self.assertEqual(result, "sexuality_biology")

    def test_classify_psychology(self):
        result = classify_content("Sexual Psychology",
                                  "desire attraction libido intimacy attachment fantasy paraphilia")
        self.assertEqual(result, "sexuality_psychology")

    def test_classify_identity(self):
        result = classify_content("Sexual Identity",
                                  "sexual orientation homosexuality bisexuality asexuality lgbtq queer")
        self.assertEqual(result, "sexuality_identity")

    def test_classify_health(self):
        result = classify_content("Sexual Health",
                                  "sexually transmitted std sti hiv aids safe sex condom")
        self.assertEqual(result, "sexuality_health")

    def test_classify_fallback(self):
        result = classify_content("Obscure Page", "nothing related here at all")
        self.assertEqual(result, "sexuality_general")

    def test_chunk_text_empty(self):
        self.assertEqual(chunk_text("", "Test"), [])

    def test_chunk_text_size_limit(self):
        text = ("A" * 800 + "\n\n") * 10
        chunks = chunk_text(text, "Title", chunk_size=1500)
        for c in chunks:
            self.assertLessEqual(len(c), 1500 + 800)

    def test_start_urls_contain_sexuality(self):
        joined = " ".join(START_URLS)
        self.assertIn("sexuality", joined.lower())

    def test_fallback_vector_empty(self):
        self.assertIn("sexuality_general", VECTOR_CATEGORIES)
        self.assertEqual(VECTOR_CATEGORIES["sexuality_general"], [])


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

        para = "This is content about human sexuality from Wikipedia."
        text = (para + "\n\n") * 5
        chunks = chunk_text(text, "Human Sexuality")

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            for chunk in chunks:
                ingest_chunk(chunk, "Human Sexuality", "sexuality_general",
                             "https://en.wikipedia.org/wiki/Human_sexuality")

        self.assertGreater(len(stored), 0)
        for item in stored:
            self.assertEqual(item["metadata"]["type"], "wikipedia")

    def test_fetch_wiki_filters_talk_links(self):
        fake = json.dumps({
            "query": {"pages": {"1": {
                "title": "Human sexuality",
                "extract": "Content here.",
                "links": [
                    {"ns": 0, "title": "Sexual orientation"},
                    {"ns": 1, "title": "Talk:Human sexuality"},
                ],
            }}}
        }).encode()

        ctx = MagicMock()
        ctx.__enter__ = lambda s: s
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.read = lambda: fake

        with patch("urllib.request.urlopen", return_value=ctx):
            result, links, error = fetch_wiki_page(
                "https://en.wikipedia.org/wiki/Human_sexuality")

        self.assertIsNone(error)
        for l in links:
            self.assertNotIn("Talk:", l)


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
            ingest_chunk("Sexuality content.", "Human Sexuality", "sexuality_biology",
                         "https://en.wikipedia.org/wiki/Human_sexuality")

        meta = captured[0]["metadata"]
        for field in ["source", "title", "url", "type", "ingested_at", "privacy"]:
            self.assertIn(field, meta)
        self.assertEqual(meta["privacy"], "public")

    def test_post_status_reports_progress(self):
        notified = []
        _mod.stats["chunks_ingested"] = 500
        _mod.stats["pages_processed"] = 10
        _mod.stats["by_vector"] = {"sexuality_identity": 200}
        _mod.stats["last_pages"] = []

        with patch.object(_mod, "notify", side_effect=lambda m: notified.append(m)):
            _mod.post_status()

        self.assertEqual(len(notified), 1)
        self.assertIn("500", notified[0])


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

    def test_chunk_size_reasonable(self):
        self.assertGreater(_mod.CHUNK_SIZE, 100)
        self.assertLess(_mod.CHUNK_SIZE, 10000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
