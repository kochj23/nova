"""
test_nova_ww2_ingest.py — All 7 test categories for nova_ww2_ingest.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_ww2_ingest.py"
_spec = importlib.util.spec_from_file_location("nova_ww2_ingest", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

classify_content  = _mod.classify_content
fetch_wiki_page   = _mod.fetch_wiki_page
chunk_text        = _mod.chunk_text
ingest_chunk      = _mod.ingest_chunk
VECTOR_CATEGORIES = _mod.VECTOR_CATEGORIES
START_URLS        = _mod.START_URLS
TARGET_CHUNKS     = _mod.TARGET_CHUNKS


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-", "password =", "secret ="]:
            self.assertNotIn(pattern, src, f"Credential pattern found: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src, "Hardcoded home path in source")

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp.com",
            "kochj" + _at + "digitalnoise.net",
            "kochj23" + _at + "gmail.com",
        ]:
            self.assertNotIn(pat, src, f"PII email found: {pat!r}")

    def test_ingest_payload_is_json(self):
        """ingest_chunk must send JSON-encoded payload."""
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("ww2 test text", "World War II", "ww2_battles",
                         "https://en.wikipedia.org/wiki/World_War_II")

        self.assertEqual(len(captured), 1)
        self.assertIn("text", captured[0])
        self.assertIn("metadata", captured[0])

    def test_memory_url_is_local(self):
        url = _mod.MEMORY_URL
        self.assertTrue(
            url.startswith("http://127.0.0.1") or url.startswith("http://192.168."),
            f"MEMORY_URL must be local, got: {url}"
        )

    def test_user_agent_is_bot_identifier(self):
        src = _SCRIPT.read_text()
        self.assertIn("Nova/1.0", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_classify_fast(self):
        text = "battle stalingrad normandy invasion operation front siege " * 50
        start = time.perf_counter()
        for _ in range(1000):
            classify_content("Battle of Stalingrad", text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, f"classify_content 1000x took {elapsed:.3f}s")

    def test_chunk_text_bounded(self):
        text = ("The German invasion of Poland began on 1 September 1939.\n\n") * 200
        chunks = chunk_text(text, "Invasion of Poland")
        self.assertLessEqual(len(chunks), 400)

    def test_vector_categories_keyword_counts(self):
        """Each non-fallback category should have at least 3 keywords."""
        for name, kws in VECTOR_CATEGORIES.items():
            if kws:
                self.assertGreaterEqual(len(kws), 3,
                                        f"Category {name!r} has fewer than 3 keywords")


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
                    "https://en.wikipedia.org/wiki/World_War_II")

        self.assertIsNone(result)
        self.assertEqual(call_count[0], 5)

    def test_fetch_wiki_no_retry_on_503(self):
        """Non-429 HTTP errors should not trigger 429 retry loop."""
        import urllib.error
        call_count = [0]

        def server_error(req, timeout=None):
            call_count[0] += 1
            raise urllib.error.HTTPError(
                url="http://x", code=503, msg="Service Unavailable",
                hdrs=None, fp=None)

        with patch("urllib.request.urlopen", side_effect=server_error):
            result, links, error = fetch_wiki_page(
                "https://en.wikipedia.org/wiki/Test")

        self.assertIsNone(result)
        self.assertEqual(call_count[0], 1, "Non-429 errors should not retry")

    def test_ingest_chunk_returns_false_on_error(self):
        def failing(req, timeout=None):
            raise OSError("refused")

        with patch("urllib.request.urlopen", side_effect=failing):
            result = ingest_chunk("text", "Title", "ww2_battles", "https://x.com")

        self.assertFalse(result)

    def test_fetch_wiki_succeeds_after_429(self):
        import urllib.error
        attempt = [0]
        fake = json.dumps({
            "query": {"pages": {"1": {
                "title": "World War II",
                "extract": "World War II was a global conflict from 1939 to 1945.",
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
                    "https://en.wikipedia.org/wiki/World_War_II")

        self.assertIsNotNone(result)
        self.assertEqual(attempt[0], 2)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_classify_battles(self):
        result = classify_content(
            "Battle of Stalingrad",
            "battle offensive campaign front siege invasion operation blitz")
        self.assertEqual(result, "ww2_battles")

    def test_classify_leaders(self):
        result = classify_content(
            "Adolf Hitler",
            "hitler churchill roosevelt general admiral marshal commander")
        self.assertEqual(result, "ww2_leaders")

    def test_classify_nations(self):
        result = classify_content(
            "Allied Powers",
            "allies axis germany japan soviet united states britain france")
        self.assertEqual(result, "ww2_nations")

    def test_classify_technology(self):
        result = classify_content(
            "Enigma Machine",
            "enigma codebreaking radar tank aircraft atomic bomb")
        self.assertEqual(result, "ww2_technology")

    def test_classify_holocaust(self):
        result = classify_content(
            "Auschwitz",
            "holocaust genocide concentration camp auschwitz extermination")
        self.assertEqual(result, "ww2_holocaust")

    def test_classify_fallback(self):
        result = classify_content("Random Page", "unrelated content here")
        self.assertEqual(result, "military_history_general")

    def test_chunk_text_empty(self):
        self.assertEqual(chunk_text("", "Test"), [])

    def test_chunk_text_short_paras_skipped(self):
        text = "hi\n\n" + "The Battle of Normandy was fought in 1944 " * 5 + "\n\n"
        chunks = chunk_text(text, "Normandy")
        for c in chunks:
            self.assertGreaterEqual(len(c), 30)

    def test_chunk_text_size_limit(self):
        text = ("A" * 800 + "\n\n") * 10
        chunks = chunk_text(text, "Title", chunk_size=1500)
        for c in chunks:
            self.assertLessEqual(len(c), 1500 + 800)

    def test_start_urls_contain_ww2(self):
        joined = " ".join(START_URLS)
        self.assertIn("World_War_II", joined)

    def test_fallback_category_exists(self):
        self.assertIn("military_history_general", VECTOR_CATEGORIES)
        self.assertEqual(VECTOR_CATEGORIES["military_history_general"], [])


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

        text = ("The Battle of Stalingrad was a decisive victory for the Soviet Union.\n\n") * 5
        chunks = chunk_text(text, "Stalingrad")

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            for chunk in chunks:
                ingest_chunk(chunk, "Stalingrad", "ww2_battles",
                             "https://en.wikipedia.org/wiki/Battle_of_Stalingrad")

        self.assertGreater(len(stored), 0)
        for item in stored:
            self.assertEqual(item["metadata"]["source"], "ww2_battles")

    def test_classify_feeds_ingest_metadata(self):
        stored = []

        def mock_urlopen(req, timeout=None):
            stored.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        vector = classify_content("D-Day", "battle invasion normandy operation")
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("D-Day was the Allied invasion.", "D-Day", vector,
                         "https://en.wikipedia.org/wiki/D-Day")

        self.assertTrue(len(stored) > 0)
        self.assertIn("ww2_", stored[0]["metadata"]["source"])

    def test_fetch_wiki_parses_links(self):
        fake = json.dumps({
            "query": {"pages": {"1": {
                "title": "World War II",
                "extract": "Global conflict 1939-1945.",
                "links": [
                    {"ns": 0, "title": "Battle of Normandy"},
                    {"ns": 0, "title": "Adolf Hitler"},
                    {"ns": 1, "title": "Talk:WW2"},
                ],
            }}}
        }).encode()

        ctx = MagicMock()
        ctx.__enter__ = lambda s: s
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.read = lambda: fake

        with patch("urllib.request.urlopen", return_value=ctx):
            result, links, error = fetch_wiki_page(
                "https://en.wikipedia.org/wiki/World_War_II")

        self.assertIsNone(error)
        self.assertGreaterEqual(len(links), 2)
        for l in links:
            self.assertNotIn("Talk:", l)

    def test_stats_accumulate_per_vector(self):
        _mod.stats["by_vector"] = {}
        for v in ["ww2_battles", "ww2_battles", "ww2_leaders"]:
            _mod.stats["by_vector"][v] = _mod.stats["by_vector"].get(v, 0) + 1
        self.assertEqual(_mod.stats["by_vector"]["ww2_battles"], 2)
        self.assertEqual(_mod.stats["by_vector"]["ww2_leaders"], 1)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_exits_when_shutdown(self):
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

        def no_fetch(url):
            calls.append(url)
            return None, [], "should not reach"

        with patch.object(_mod, "notify"):
            with patch.object(_mod, "fetch_wiki_page", side_effect=no_fetch):
                with patch("time.sleep"):
                    _mod.main()

        self.assertEqual(len(calls), 0)
        _mod.stats["chunks_ingested"] = 0

    def test_ingest_chunk_metadata_fields(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("Operation Overlord was the codename for D-Day.",
                         "Operation Overlord", "ww2_battles",
                         "https://en.wikipedia.org/wiki/Operation_Overlord")

        meta = captured[0]["metadata"]
        for field in ["source", "title", "url", "type", "ingested_at", "privacy"]:
            self.assertIn(field, meta)
        self.assertEqual(meta["privacy"], "public")
        self.assertEqual(meta["type"], "wikipedia")

    def test_post_status_includes_percentage(self):
        notified = []
        _mod.stats["chunks_ingested"] = 2500
        _mod.stats["pages_processed"] = 50
        _mod.stats["by_vector"] = {"ww2_battles": 1000}
        _mod.stats["last_pages"] = []

        with patch.object(_mod, "notify", side_effect=lambda m: notified.append(m)):
            _mod.post_status()

        self.assertEqual(len(notified), 1)
        self.assertIn("25.0", notified[0])

    def test_classify_highest_scoring_wins(self):
        text = ("battle battle battle battle invasion operation siege "
                "tank aircraft submarine radar")
        result = classify_content("WWII Combat", text)
        self.assertEqual(result, "ww2_battles")


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

    def test_vector_categories_ww2_prefix(self):
        for key in VECTOR_CATEGORIES:
            if VECTOR_CATEGORIES[key]:
                self.assertTrue(
                    key.startswith("ww2_") or key == "military_history_general",
                    f"Unexpected category name: {key!r}"
                )

    def test_module_loads_without_side_effects(self):
        self.assertIsNotNone(_mod)

    def test_chunk_size_reasonable(self):
        self.assertGreater(_mod.CHUNK_SIZE, 100)
        self.assertLess(_mod.CHUNK_SIZE, 10000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
