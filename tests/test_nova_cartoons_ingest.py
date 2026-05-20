"""
test_nova_cartoons_ingest.py -- All 7 test categories for nova_cartoons_ingest.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_cartoons_ingest.py"
_spec = importlib.util.spec_from_file_location("nova_cartoons_ingest", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

fetch_wiki_page = _mod.fetch_wiki_page
chunk_text      = _mod.chunk_text
ingest_chunk    = _mod.ingest_chunk
SHOWS           = _mod.SHOWS
TARGET_CHUNKS   = _mod.TARGET_CHUNKS


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-", "password =", "secret ="]:
            self.assertNotIn(pattern, src, f"Credential: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp.com",
            "kochj" + _at + "digitalnoise.net",
            "kochj23" + _at + "gmail.com",
        ]:
            self.assertNotIn(pat, src, f"PII: {pat!r}")

    def test_ingest_payload_json(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("ThunderCats Ho! content.", "ThunderCats", "thundercats",
                         "https://en.wikipedia.org/wiki/ThunderCats")

        self.assertTrue(len(captured) > 0)
        self.assertIn("text", captured[0])

    def test_memory_url_is_local(self):
        url = _mod.MEMORY_URL
        self.assertTrue(
            url.startswith("http://127.0.0.1") or url.startswith("http://192.168."),
            f"MEMORY_URL must be local: {url}"
        )


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_chunk_text_fast(self):
        text = ("ThunderCats content paragraph.\n\n") * 200
        start = time.perf_counter()
        chunk_text(text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)

    def test_shows_count(self):
        self.assertGreaterEqual(len(SHOWS), 3)

    def test_target_chunks_is_large(self):
        self.assertGreaterEqual(TARGET_CHUNKS, 5000)


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
                    "https://en.wikipedia.org/wiki/ThunderCats")

        self.assertIsNone(result)
        self.assertGreaterEqual(call_count[0], 5)

    def test_ingest_returns_false_on_error(self):
        def failing(req, timeout=None):
            raise OSError("refused")

        with patch("urllib.request.urlopen", side_effect=failing):
            result = ingest_chunk("text", "Title", "thundercats", "https://x.com")

        self.assertFalse(result)

    def test_fetch_wiki_retries_on_generic_error(self):
        call_count = [0]

        def generic_error(req, timeout=None):
            call_count[0] += 1
            raise OSError("network error")

        with patch("urllib.request.urlopen", side_effect=generic_error):
            with patch("time.sleep"):
                result, links, error = fetch_wiki_page(
                    "https://en.wikipedia.org/wiki/He-Man")

        self.assertIsNone(result)
        self.assertGreaterEqual(call_count[0], 5)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_shows_are_tuples(self):
        for show in SHOWS:
            self.assertIsInstance(show, tuple)
            self.assertEqual(len(show), 3)

    def test_shows_include_thundercats(self):
        names = [s[0] for s in SHOWS]
        self.assertIn("ThunderCats", names)

    def test_shows_have_wikipedia_urls(self):
        for name, source, url in SHOWS:
            self.assertIn("wikipedia.org", url)

    def test_chunk_text_empty(self):
        self.assertEqual(chunk_text(""), [])

    def test_chunk_text_skips_short(self):
        text = "ok\n\n" + ("ThunderCats is a long show " * 10) + "\n\n"
        chunks = chunk_text(text)
        for c in chunks:
            self.assertGreaterEqual(len(c), 40)

    def test_stats_keys(self):
        for key in ["total_ingested", "pages_processed", "errors"]:
            self.assertIn(key, _mod.stats)


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

        text = ("ThunderCats is an animated television series from 1985.\n\n") * 5
        chunks = chunk_text(text)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            for chunk in chunks:
                ingest_chunk(chunk, "ThunderCats", "thundercats",
                             "https://en.wikipedia.org/wiki/ThunderCats")

        self.assertGreater(len(stored), 0)

    def test_fetch_wiki_returns_links(self):
        fake = json.dumps({
            "query": {"pages": {"1": {
                "title": "ThunderCats",
                "extract": "ThunderCats is a cartoon from 1985.",
                "links": [
                    {"ns": 0, "title": "Lion-O"},
                    {"ns": 1, "title": "Talk:ThunderCats"},
                ],
            }}}
        }).encode()

        ctx = MagicMock()
        ctx.__enter__ = lambda s: s
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.read = lambda: fake

        with patch("urllib.request.urlopen", return_value=ctx):
            result, links, error = fetch_wiki_page(
                "https://en.wikipedia.org/wiki/ThunderCats")

        self.assertIsNone(error)
        for l in links:
            self.assertNotIn("Talk:", l)

    def test_ingest_uses_show_source(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("He-Man and the Masters of the Universe content.",
                         "He-Man", "he_man", "https://en.wikipedia.org/wiki/He-Man")

        self.assertTrue(len(captured) > 0)
        self.assertIn("source", captured[0])


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_exits_on_shutdown(self):
        _mod.shutdown = True
        _mod.stats["total_ingested"] = 0
        try:
            with patch.object(_mod, "notify"):
                with patch("time.sleep"):
                    _mod.main()
        finally:
            _mod.shutdown = False

    def test_main_stops_at_target(self):
        _mod.shutdown = False
        _mod.stats["total_ingested"] = TARGET_CHUNKS
        calls = []

        with patch.object(_mod, "notify"):
            with patch.object(_mod, "fetch_wiki_page",
                               side_effect=lambda u: calls.append(u) or (None, [], "stop")):
                with patch("time.sleep"):
                    _mod.main()

        self.assertEqual(len(calls), 0)
        _mod.stats["total_ingested"] = 0

    def test_ingest_metadata_complete(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("Voltron content.", "Voltron", "voltron",
                         "https://en.wikipedia.org/wiki/Voltron")

        meta = captured[0]["metadata"]
        for field in ["source", "title", "url", "type", "privacy", "ingested_at"]:
            self.assertIn(field, meta)


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

    def test_module_constants(self):
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIsInstance(_mod.TARGET_CHUNKS, int)
        self.assertIsInstance(_mod.SHOWS, list)
        self.assertGreater(len(_mod.SHOWS), 0)

    def test_module_loads_without_network(self):
        self.assertIsNotNone(_mod)

    def test_log_file_uses_home(self):
        self.assertIn(str(Path.home()), str(_mod.LOG_FILE))

    def test_user_agent_is_bot(self):
        self.assertIn("Nova/1.0", _mod.USER_AGENT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
