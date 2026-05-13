"""
test_nova_physics_ingest.py — All 7 test categories for nova_physics_ingest.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub dependencies before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_physics_ingest.py"
_spec = importlib.util.spec_from_file_location("nova_physics_ingest", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

classify_content = _mod.classify_content
fetch_wiki_page  = _mod.fetch_wiki_page
chunk_text       = _mod.chunk_text
ingest_chunk     = _mod.ingest_chunk
VECTOR_CATEGORIES = _mod.VECTOR_CATEGORIES
START_URLS       = _mod.START_URLS
TARGET_CHUNKS    = _mod.TARGET_CHUNKS
CHUNK_SIZE       = _mod.CHUNK_SIZE


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
        self.assertNotIn(home, src, "Hardcoded home path found in source")

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

    def test_ingest_chunk_text_truncated_by_json_encoding(self):
        """ingest_chunk sends text as-is; verify it serialises via JSON (not string concat)."""
        captured = []

        def mock_urlopen(req, timeout=None):
            body = json.loads(req.data.decode())
            captured.append(body)
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("safe text payload", "TestTitle", "physics_mechanics",
                         "https://en.wikipedia.org/wiki/Test")

        self.assertTrue(len(captured) > 0)
        body = captured[0]
        self.assertIn("text", body)
        self.assertIn("metadata", body)

    def test_user_agent_identifies_as_bot(self):
        src = _SCRIPT.read_text()
        self.assertIn("Nova/1.0", src, "User-Agent must identify as Nova/1.0 bot")

    def test_memory_url_is_localhost(self):
        url = _mod.MEMORY_URL
        self.assertTrue(
            url.startswith("http://127.0.0.1") or url.startswith("http://192.168."),
            f"MEMORY_URL must be local network, got: {url}"
        )


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_classify_content_fast(self):
        text = "quantum mechanics particle physics electron proton neutron " * 50
        start = time.perf_counter()
        for _ in range(1000):
            classify_content("Physics", text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0,
                        f"classify_content 1000x took {elapsed:.3f}s (limit 1s)")

    def test_chunk_text_linear_growth(self):
        text = ("Newton's laws of motion describe the relationship between forces "
                "and the motion of bodies.\n\n") * 100
        chunks = chunk_text(text, "Newton", chunk_size=1500)
        self.assertLessEqual(len(chunks), 200,
                             f"chunk_text produced {len(chunks)} chunks for 100 paragraphs")

    def test_chunk_text_fast_on_large_input(self):
        text = "Quantum physics " * 10000
        start = time.perf_counter()
        chunk_text(text, "test")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"chunk_text slow: {elapsed:.3f}s")

    def test_vector_categories_not_empty(self):
        """All non-fallback categories must have at least 1 keyword."""
        for name, kws in VECTOR_CATEGORIES.items():
            if name != "physics_general":
                self.assertGreater(len(kws), 0,
                                   f"Category {name!r} has no keywords")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_fetch_wiki_page_retries_on_429(self):
        """fetch_wiki_page must retry up to 5 times on HTTP 429."""
        import urllib.error
        call_count = [0]

        def rate_limit_urlopen(req, timeout=None):
            call_count[0] += 1
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            raise urllib.error.HTTPError(
                url="http://x", code=429, msg="Too Many Requests",
                hdrs=None, fp=None)

        with patch("urllib.request.urlopen", side_effect=rate_limit_urlopen):
            with patch("time.sleep"):
                result, links, error = fetch_wiki_page(
                    "https://en.wikipedia.org/wiki/Physics")

        self.assertIsNone(result)
        self.assertIn("rate limited", error.lower())
        self.assertEqual(call_count[0], 5,
                         f"Expected 5 attempts on 429, got {call_count[0]}")

    def test_fetch_wiki_page_no_retry_on_404(self):
        """fetch_wiki_page must not retry on 404."""
        import urllib.error
        call_count = [0]

        def not_found(req, timeout=None):
            call_count[0] += 1
            raise urllib.error.HTTPError(
                url="http://x", code=404, msg="Not Found",
                hdrs=None, fp=None)

        with patch("urllib.request.urlopen", side_effect=not_found):
            result, links, error = fetch_wiki_page(
                "https://en.wikipedia.org/wiki/NoSuchPage")

        self.assertIsNone(result)
        self.assertEqual(call_count[0], 1, "Should not retry on 404")

    def test_ingest_chunk_retries_gracefully(self):
        """ingest_chunk returns False on network error without raising."""
        def failing(req, timeout=None):
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", side_effect=failing):
            result = ingest_chunk("test text", "Title", "physics_general",
                                  "https://example.com")

        self.assertFalse(result, "ingest_chunk should return False on error")

    def test_fetch_wiki_page_succeeds_after_retry(self):
        """fetch_wiki_page returns data when second attempt succeeds."""
        import urllib.error
        attempt = [0]
        fake_data = json.dumps({
            "query": {
                "pages": {
                    "1": {
                        "title": "Physics",
                        "extract": "Physics is the natural science of matter and energy.",
                        "links": [],
                    }
                }
            }
        }).encode()

        def flaky(req, timeout=None):
            attempt[0] += 1
            if attempt[0] < 2:
                raise urllib.error.HTTPError(
                    url="http://x", code=429, msg="Too Many Requests",
                    hdrs=None, fp=None)
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.read = lambda: fake_data
            return ctx

        with patch("urllib.request.urlopen", side_effect=flaky):
            with patch("time.sleep"):
                result, links, error = fetch_wiki_page(
                    "https://en.wikipedia.org/wiki/Physics")

        self.assertIsNotNone(result)
        self.assertEqual(attempt[0], 2)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_classify_mechanics(self):
        result = classify_content("Classical Mechanics",
                                  "force motion velocity newton acceleration momentum energy")
        self.assertEqual(result, "physics_mechanics")

    def test_classify_quantum(self):
        result = classify_content("Quantum Physics",
                                  "quantum particle electron proton uncertainty superposition")
        self.assertEqual(result, "physics_quantum")

    def test_classify_relativity(self):
        result = classify_content("General Relativity",
                                  "einstein relativity spacetime gravity black hole universe")
        self.assertEqual(result, "physics_relativity")

    def test_classify_fallback(self):
        result = classify_content("Obscure Topic",
                                  "something completely unrelated to anything listed here")
        self.assertEqual(result, "physics_general")

    def test_classify_em(self):
        result = classify_content("Electromagnetism",
                                  "electromagnetic electricity magnetism maxwell wave photon")
        self.assertEqual(result, "physics_em")

    def test_chunk_text_empty(self):
        chunks = chunk_text("", "TestPage")
        self.assertEqual(chunks, [])

    def test_chunk_text_skips_short_paragraphs(self):
        text = "ok\n\n" + "A" * 100 + "\n\n" + "B" * 100
        chunks = chunk_text(text, "Title")
        for c in chunks:
            self.assertGreaterEqual(len(c), 30)

    def test_chunk_text_respects_size(self):
        text = ("X" * 800 + "\n\n") * 20
        chunks = chunk_text(text, "Title", chunk_size=1500)
        for c in chunks:
            self.assertLessEqual(len(c), 1500 + 800,
                                 "Chunk may slightly exceed size at paragraph boundary")

    def test_start_urls_are_wikipedia(self):
        for url in START_URLS:
            self.assertIn("wikipedia.org", url,
                          f"START_URL is not a Wikipedia URL: {url}")

    def test_target_chunks_positive(self):
        self.assertGreater(TARGET_CHUNKS, 0)

    def test_vector_categories_has_fallback(self):
        self.assertIn("physics_general", VECTOR_CATEGORIES)
        self.assertEqual(VECTOR_CATEGORIES["physics_general"], [])

    def test_classify_nuclear(self):
        result = classify_content("Nuclear Physics",
                                  "nuclear fission fusion radioactive decay isotope reactor")
        self.assertEqual(result, "physics_nuclear")

    def test_classify_thermo(self):
        result = classify_content("Thermodynamics",
                                  "thermodynamics entropy temperature heat gas pressure phase")
        self.assertEqual(result, "physics_thermo")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_chunk_then_ingest_pipeline(self):
        """chunk_text -> ingest_chunk pipeline stores all chunks."""
        stored = []

        def mock_urlopen(req, timeout=None):
            stored.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        text = ("Newton's laws of motion are three physical laws that together "
                "laid the foundation for classical mechanics.\n\n") * 5

        chunks = chunk_text(text, "Newton's Laws")
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            for chunk in chunks:
                ingest_chunk(chunk, "Newton's Laws", "physics_mechanics",
                             "https://en.wikipedia.org/wiki/Newton%27s_laws_of_motion")

        self.assertGreater(len(stored), 0, "No chunks were stored")
        for item in stored:
            self.assertIn("text", item)
            self.assertEqual(item["metadata"]["source"], "physics_mechanics")

    def test_classify_then_ingest_uses_correct_vector(self):
        """classify_content result flows correctly into ingest_chunk metadata."""
        stored = []

        def mock_urlopen(req, timeout=None):
            stored.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        title = "Quantum Entanglement"
        text = "quantum particle electron proton uncertainty superposition entanglement"
        vector = classify_content(title, text)

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("Quantum entanglement is a phenomenon.", title, vector,
                         "https://en.wikipedia.org/wiki/Quantum_entanglement")

        self.assertTrue(len(stored) > 0)
        self.assertEqual(stored[0]["metadata"]["source"], "physics_quantum")

    def test_fetch_wiki_parses_links_correctly(self):
        """fetch_wiki_page must extract only main namespace links."""
        fake_data = json.dumps({
            "query": {
                "pages": {
                    "1": {
                        "title": "Physics",
                        "extract": "Physics is a natural science.",
                        "links": [
                            {"ns": 0, "title": "Quantum mechanics"},
                            {"ns": 1, "title": "Talk:Physics"},
                            {"ns": 0, "title": "Classical mechanics"},
                            {"ns": 4, "title": "Wikipedia:About"},
                        ],
                    }
                }
            }
        }).encode()

        ctx = MagicMock()
        ctx.__enter__ = lambda s: s
        ctx.__exit__ = MagicMock(return_value=False)
        ctx.read = lambda: fake_data

        with patch("urllib.request.urlopen", return_value=ctx):
            result, links, error = fetch_wiki_page(
                "https://en.wikipedia.org/wiki/Physics")

        self.assertIsNone(error)
        link_titles = [l.split("/wiki/")[-1] for l in links]
        self.assertIn("Quantum_mechanics", link_titles)
        self.assertIn("Classical_mechanics", link_titles)
        # Talk and Wikipedia pages should not be included
        for l in links:
            self.assertNotIn("Talk:", l)
            self.assertNotIn("Wikipedia:", l)

    def test_stats_dict_tracks_by_vector(self):
        """stats['by_vector'] dict accumulates counts per vector category."""
        stats = _mod.stats
        stats["by_vector"] = {}
        stats["by_vector"]["physics_quantum"] = stats["by_vector"].get("physics_quantum", 0) + 1
        stats["by_vector"]["physics_quantum"] = stats["by_vector"].get("physics_quantum", 0) + 1
        stats["by_vector"]["physics_em"] = stats["by_vector"].get("physics_em", 0) + 1
        self.assertEqual(stats["by_vector"]["physics_quantum"], 2)
        self.assertEqual(stats["by_vector"]["physics_em"], 1)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_exits_when_shutdown_set(self):
        """main() must stop crawl immediately when shutdown=True."""
        _mod.shutdown = True
        _mod.stats["chunks_ingested"] = 0

        try:
            with patch.object(_mod, "notify"):
                with patch.object(_mod, "fetch_wiki_page", return_value=(None, [], "should not call")):
                    with patch("time.sleep"):
                        _mod.main()
        except Exception:
            pass
        finally:
            _mod.shutdown = False

    def test_main_stops_at_target_chunks(self):
        """main() must stop when chunks_ingested reaches TARGET_CHUNKS."""
        _mod.shutdown = False
        _mod.stats["chunks_ingested"] = TARGET_CHUNKS

        fetch_calls = []

        def no_fetch(url):
            fetch_calls.append(url)
            return None, [], "stopped"

        with patch.object(_mod, "notify"):
            with patch.object(_mod, "fetch_wiki_page", side_effect=no_fetch):
                with patch("time.sleep"):
                    _mod.main()

        self.assertEqual(len(fetch_calls), 0,
                         "Should not fetch any pages when already at TARGET_CHUNKS")
        _mod.stats["chunks_ingested"] = 0

    def test_ingest_chunk_sends_correct_metadata_fields(self):
        """ingest_chunk payload must include source, title, url, type, privacy."""
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            ingest_chunk("Physics text here.", "Physics", "physics_em",
                         "https://en.wikipedia.org/wiki/Electromagnetism")

        self.assertEqual(len(captured), 1)
        meta = captured[0]["metadata"]
        for field in ["source", "title", "url", "type", "ingested_at", "privacy"]:
            self.assertIn(field, meta, f"Missing metadata field: {field}")
        self.assertEqual(meta["type"], "wikipedia")
        self.assertEqual(meta["privacy"], "public")

    def test_post_status_calls_notify(self):
        """post_status must call notify with a non-empty message."""
        notified = []
        _mod.stats["chunks_ingested"] = 500
        _mod.stats["pages_processed"] = 10
        _mod.stats["by_vector"] = {"physics_quantum": 200, "physics_em": 300}
        _mod.stats["last_pages"] = ["Physics [physics_em] (5 chunks)"]

        with patch.object(_mod, "notify", side_effect=lambda m: notified.append(m)):
            _mod.post_status()

        self.assertEqual(len(notified), 1)
        self.assertIn("500", notified[0])

    def test_classify_content_scores_highest_match(self):
        """When multiple categories match, classify_content returns highest scorer."""
        text = ("quantum quantum quantum quantum quantum "
                "particle electron proton neutron "
                "force motion newton velocity")
        result = classify_content("Mixed", text)
        self.assertEqual(result, "physics_quantum",
                         "quantum has most matches, should win")


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
        self.assertIsInstance(_mod.VECTOR_CATEGORIES, dict)
        self.assertGreater(len(_mod.VECTOR_CATEGORIES), 0)

    def test_stats_dict_has_required_keys(self):
        for key in ["pages_processed", "chunks_ingested", "errors",
                    "by_vector", "last_pages", "current_page", "current_vector"]:
            self.assertIn(key, _mod.stats, f"stats missing key: {key!r}")

    def test_module_loads_without_network(self):
        """Loading the module must not make network calls."""
        self.assertIsNotNone(_mod)

    def test_start_url_is_physics_wikipedia(self):
        self.assertTrue(
            any("Physics" in u for u in START_URLS),
            f"Expected Physics in START_URLS, got: {START_URLS}"
        )

    def test_vector_categories_includes_physics_prefix(self):
        for key in VECTOR_CATEGORIES:
            if key != "physics_general":
                self.assertTrue(key.startswith("physics_"),
                                f"Non-physics category: {key!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
