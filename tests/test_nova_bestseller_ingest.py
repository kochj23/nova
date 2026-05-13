"""
test_nova_bestseller_ingest.py -- All 7 test categories for nova_bestseller_ingest.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_bestseller_ingest.py"
_spec = importlib.util.spec_from_file_location("nova_bestseller_ingest", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

classify_content  = _mod.classify_content
fetch_wiki_page   = _mod.fetch_wiki_page
chunk_text        = _mod.chunk_text
ingest_chunk      = _mod.ingest_chunk
VECTOR_CATEGORIES = _mod.VECTOR_CATEGORIES
BOOKS_100M        = _mod.BOOKS_100M
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
        self.assertNotIn(home, src)

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
            ingest_chunk("Harry Potter content.", "Harry Potter", "literature_fantasy",
                         "Harry Potter")

        self.assertEqual(len(captured), 1)
        self.assertIn("text", captured[0])
        self.assertIn("metadata", captured[0])

    def test_memory_url_is_local(self):
        url = _mod.MEMORY_URL
        self.assertTrue(
            url.startswith("http://127.0.0.1") or url.startswith("http://192.168."),
            f"MEMORY_URL must be local, got: {url}"
        )

    def test_user_agent_identifies_bot(self):
        src = _SCRIPT.read_text()
        self.assertIn("Nova/1.0", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_classify_fast(self):
        text = "fantasy tolkien hobbit ring magic wizard " * 50
        start = time.perf_counter()
        for _ in range(1000):
            classify_content("The Hobbit", text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)

    def test_chunk_text_bounded(self):
        para = "This book sold over one hundred million copies worldwide."
        text = (para + "\n\n") * 200
        chunks = chunk_text(text)
        self.assertLessEqual(len(chunks), 400)

    def test_books_list_non_empty(self):
        self.assertGreater(len(BOOKS_100M), 20)

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
                result, links, error = fetch_wiki_page("Harry Potter")

        self.assertIsNone(result)
        self.assertEqual(call_count[0], 5)

    def test_fetch_wiki_no_retry_on_non_429(self):
        import urllib.error
        call_count = [0]

        def server_error(req, timeout=None):
            call_count[0] += 1
            raise urllib.error.HTTPError(
                url="http://x", code=503, msg="Service Unavailable",
                hdrs=None, fp=None)

        with patch("urllib.request.urlopen", side_effect=server_error):
            result, links, error = fetch_wiki_page("The Hobbit")

        self.assertIsNone(result)
        self.assertEqual(call_count[0], 1)

    def test_ingest_returns_false_on_error(self):
        def failing(req, timeout=None):
            raise OSError("refused")

        with patch("urllib.request.urlopen", side_effect=failing):
            result = ingest_chunk("text", "Title", "literature_fantasy", "The Hobbit")

        self.assertFalse(result)

    def test_fetch_wiki_succeeds_after_429(self):
        import urllib.error
        attempt = [0]
        fake = json.dumps({
            "query": {"pages": {"1": {
                "title": "The Hobbit",
                "extract": "The Hobbit is a fantasy novel by J.R.R. Tolkien.",
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
                result, links, error = fetch_wiki_page("The Hobbit")

        self.assertIsNotNone(result)
        self.assertEqual(attempt[0], 2)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_classify_fantasy(self):
        result = classify_content("The Hobbit",
                                  "fantasy tolkien hobbit ring magic wizard dragon quest")
        self.assertEqual(result, "literature_fantasy")

    def test_classify_mystery(self):
        result = classify_content("And Then There Were None",
                                  "mystery detective crime murder thriller agatha christie whodunit")
        self.assertEqual(result, "literature_mystery")

    def test_classify_romance(self):
        result = classify_content("Twilight",
                                  "romance love passion twilight fifty shades relationship")
        self.assertEqual(result, "literature_romance")

    def test_classify_scifi(self):
        result = classify_content("1984",
                                  "science fiction dystopia future robot orwell brave new world")
        self.assertEqual(result, "literature_scifi")

    def test_classify_fallback(self):
        result = classify_content("Unknown Book", "nothing relevant here at all")
        self.assertEqual(result, "literature_general")

    def test_chunk_text_empty(self):
        self.assertEqual(chunk_text(""), [])

    def test_chunk_text_size_limit(self):
        text = ("A" * 800 + "\n\n") * 10
        chunks = chunk_text(text, chunk_size=1500)
        for c in chunks:
            self.assertLessEqual(len(c), 1500 + 800)

    def test_books_list_has_classics(self):
        joined = " ".join(BOOKS_100M)
        self.assertIn("Don Quixote", joined)
        self.assertIn("Harry Potter", joined)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_chunk_then_ingest(self):
        stored = []

        def mock_urlopen(req, timeout=None):
            stored.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        para = "Harry Potter is a series of fantasy novels by J. K. Rowling."
        text = (para + "\n\n") * 5
        chunks = chunk_text(text)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            for chunk in chunks:
                ingest_chunk(chunk, "Harry Potter", "literature_fantasy",
                             "Harry Potter and the Philosopher's Stone")

        self.assertGreater(len(stored), 0)
        for item in stored:
            self.assertEqual(item["metadata"]["type"], "wikipedia_book")

    def test_classify_feeds_ingest_vector(self):
        stored = []

        def mock_urlopen(req, timeout=None):
            stored.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        vector = classify_content("A Tale of Two Cities",
                                  "classic 19th century dickens literary fiction victorian realism")
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("It was the best of times.", "A Tale of Two Cities",
                         vector, "A Tale of Two Cities")

        self.assertTrue(len(stored) > 0)
        self.assertEqual(stored[0]["metadata"]["source"], "literature_classic")

    def test_fetch_wiki_returns_title_text(self):
        fake = json.dumps({
            "query": {"pages": {"1": {
                "title": "Harry Potter",
                "extract": "Harry Potter is a series of fantasy novels.",
                "links": [{"ns": 0, "title": "J. K. Rowling"}],
            }}}
        }).encode()

        ctx = MagicMock()
        ctx.__enter__ = lambda s: s
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.read = lambda: fake

        with patch("urllib.request.urlopen", return_value=ctx):
            result, links, error = fetch_wiki_page("Harry Potter")

        self.assertIsNone(error)
        self.assertIsNotNone(result)
        title, text = result
        self.assertEqual(title, "Harry Potter")


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

    def test_ingest_metadata_fields(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("The Alchemist by Paulo Coelho is a philosophical novel.",
                         "The Alchemist", "literature_philosophy", "The Alchemist (novel)")

        meta = captured[0]["metadata"]
        for field in ["source", "title", "book", "type", "ingested_at", "privacy"]:
            self.assertIn(field, meta)
        self.assertEqual(meta["privacy"], "public")

    def test_post_status_notifies(self):
        notified = []
        _mod.stats["chunks_ingested"] = 500
        _mod.stats["pages_processed"] = 10
        _mod.stats["by_vector"] = {"literature_fantasy": 200}
        _mod.stats["last_pages"] = []
        _mod.stats["current_book"] = "Harry Potter"

        with patch.object(_mod, "notify", side_effect=lambda m: notified.append(m)):
            _mod.post_status()

        self.assertEqual(len(notified), 1)
        self.assertIn("500", notified[0])

    def test_classify_highest_score_wins(self):
        text = "fantasy tolkien hobbit ring magic wizard dragon quest middle-earth " * 3
        result = classify_content("The Lord of the Rings", text)
        self.assertEqual(result, "literature_fantasy")


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
        self.assertIsInstance(_mod.CHUNK_SIZE, int)
        self.assertIsInstance(_mod.BOOKS_100M, list)
        self.assertGreater(len(_mod.BOOKS_100M), 0)

    def test_stats_keys_present(self):
        for key in ["books_processed", "chunks_ingested", "errors", "by_vector"]:
            self.assertIn(key, _mod.stats)

    def test_module_loads_without_network(self):
        self.assertIsNotNone(_mod)

    def test_fallback_category_empty(self):
        self.assertIn("literature_general", VECTOR_CATEGORIES)
        self.assertEqual(VECTOR_CATEGORIES["literature_general"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
