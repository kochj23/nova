"""
test_nova_memory_first.py — All 7 test categories for nova_memory_first.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Load module under test — no nova_config import at module level
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_memory_first.py"
_spec = importlib.util.spec_from_file_location("nova_memory_first", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

classify_query = _mod.classify_query
recall = _mod.recall
search = _mod.search
batch_recall = _mod.batch_recall
format_result = _mod.format_result
memory_lookup = _mod.memory_lookup
filter_echoes = _mod.filter_echoes if hasattr(_mod, "filter_echoes") else None
SOURCE_RULES = _mod.SOURCE_RULES
DEFAULT_SOURCES = _mod.DEFAULT_SOURCES


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-live", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src, "Hardcoded home path found")

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii_patterns = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney" + ".com",
            "kochj" + _at + "digitalnoise.net",
        ]
        for pattern in pii_patterns:
            self.assertNotIn(pattern, src, f"PII found: {pattern!r}")

    def test_memory_server_uses_local_ip(self):
        """Recall/search must use LAN IP, not internet endpoints."""
        self.assertNotIn("openai.com", _mod.RECALL_URL)
        self.assertNotIn("anthropic.com", _mod.RECALL_URL)

    def test_format_result_truncates_text(self):
        """format_result() must truncate text to 400 chars to prevent info leakage in logs."""
        item = {"text": "x" * 600, "source": "email_archive", "score": 0.9}
        result = format_result(item, 1)
        # The formatted result should not contain 600 x's
        text_section = result.split("\n", 1)[1] if "\n" in result else result
        self.assertLessEqual(len(text_section), 450,
                             "format_result() must truncate text to 400 chars")

    def test_query_never_sent_to_cloud(self):
        """recall() must only call local memory server, never external APIs."""
        captured_urls = []

        def fake_urlopen(url, timeout=None):
            captured_urls.append(str(url))
            raise OSError("simulated failure")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            recall("what is Jordan's health data?")

        for url in captured_urls:
            self.assertNotIn("openai.com", url)
            self.assertNotIn("openrouter.ai", url)
            self.assertNotIn("anthropic.com", url)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_classify_query_fast(self):
        """classify_query() must classify 1000 queries in < 500ms."""
        queries = [
            "what raves were in LA in 2002?",
            "what was my blood pressure last week?",
            "tell me about Sam's email",
            "who is Jason Cox?",
            "review this Swift code",
            "what's on my calendar?",
            "tell me about the Ukrainian war",
        ]
        start = time.perf_counter()
        for _ in range(143):  # ~1000 total
            for q in queries:
                classify_query(q)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5,
                        f"classify_query 1000x took {elapsed:.3f}s (limit 500ms)")

    def test_source_rules_not_too_many(self):
        """SOURCE_RULES must not grow unboundedly (>50 would be too slow)."""
        self.assertLessEqual(len(SOURCE_RULES), 50,
                             "Too many SOURCE_RULES — classification will be slow")

    def test_recall_count_is_bounded(self):
        """RECALL_COUNT must be bounded to prevent O(n) memory retrieval."""
        self.assertLessEqual(_mod.RECALL_COUNT, 20,
                             "RECALL_COUNT must be bounded (<=20 to prevent overload)")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_recall_returns_empty_on_failure(self):
        """recall() must return [] on network failure, not raise."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("memory server down")
            result = recall("test query")
        self.assertEqual(result, [], "recall() must return [] on failure")

    def test_search_returns_empty_on_failure(self):
        """search() must return [] on network failure."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("search server down")
            result = search("test query")
        self.assertEqual(result, [], "search() must return [] on failure")

    def test_batch_recall_falls_back_to_individual_on_failure(self):
        """batch_recall() must fall back to individual recall() calls on HTTP failure."""
        individual_calls = []
        original_recall = _mod.recall

        def tracking_recall(query, source=None, n=8):
            individual_calls.append((query, source))
            return []

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("batch endpoint down")
            with patch.object(_mod, "recall", side_effect=tracking_recall):
                queries = [
                    {"q": "what raves?", "source": "socal_rave"},
                    {"q": "blood pressure", "source": "apple_health"},
                ]
                result = batch_recall(queries)

        # Should have fallen back to individual recalls
        self.assertEqual(len(individual_calls), 2,
                         "batch_recall() should fall back to 2 individual recall() calls")

    def test_memory_lookup_never_raises(self):
        """memory_lookup() must never raise — always return a tuple."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("all services down")
            with patch.object(_mod, "batch_recall", return_value=[]):
                with patch.object(_mod, "recall", return_value=[]):
                    with patch.object(_mod, "search", return_value=[]):
                        try:
                            result = memory_lookup("what is my health status?")
                        except Exception as e:
                            self.fail(f"memory_lookup() raised unexpectedly: {e}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_classify_rave_query(self):
        sources, labels, prefer_search = classify_query("what raves happened in LA in 1999?")
        # Should match music/rave rule
        self.assertTrue(any("rave" in l.lower() or "music" in l.lower() for l in labels),
                        f"Rave query should match music/rave rule, got: {labels}")

    def test_classify_health_query(self):
        sources, labels, prefer_search = classify_query("what was my blood pressure last week?")
        self.assertTrue(any("health" in l.lower() for l in labels),
                        f"Health query should match health rule, got: {labels}")

    def test_classify_email_query(self):
        sources, labels, prefer_search = classify_query("what email did Sam send me?")
        self.assertTrue(
            any("email" in l.lower() for l in labels) or
            any("email" in s for s in sources),
            f"Email query should match email rule, got: {labels}, {sources}"
        )

    def test_classify_people_query_prefers_search(self):
        sources, labels, prefer_search = classify_query("who is Jason Cox?")
        # People queries should set prefer_search=True
        self.assertTrue(prefer_search,
                        "People queries should use prefer_search=True (text search over vector)")

    def test_classify_unknown_returns_defaults(self):
        sources, labels, prefer_search = classify_query("xyzzy frobnicator quux")
        self.assertEqual(sources, DEFAULT_SOURCES, "Unknown queries should use DEFAULT_SOURCES")
        self.assertEqual(labels, ["general"])
        self.assertFalse(prefer_search)

    def test_classify_multiple_matches_merges_sources(self):
        """A query matching multiple rules should get sources from all matched rules."""
        sources, labels, _ = classify_query("email about a rave event")
        # Should match both email and music/rave
        self.assertGreater(len(labels), 1,
                           "Query matching email AND rave should have multiple labels")

    def test_format_result_includes_source_and_index(self):
        item = {"text": "Important memory about Jordan.", "source": "email_archive", "score": 0.87}
        result = format_result(item, 3)
        self.assertIn("[3]", result)
        self.assertIn("email_archive", result)
        self.assertIn("Jordan", result)

    def test_format_result_handles_missing_score(self):
        item = {"text": "Memory without score.", "source": "music"}
        result = format_result(item, 1)
        self.assertIn("Memory without score", result)
        self.assertIn("music", result)

    def test_recall_sends_correct_params(self):
        """recall() must send q, n, and source params."""
        captured_urls = []

        def fake_urlopen(url, timeout=None):
            captured_urls.append(url)
            raise OSError("test")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            recall("blood pressure", source="apple_health", n=5)

        self.assertTrue(len(captured_urls) > 0)
        url = captured_urls[0]
        self.assertIn("q=", url)
        self.assertIn("n=5", url)
        self.assertIn("apple_health", url)

    def test_search_sends_correct_params(self):
        captured_urls = []

        def fake_urlopen(url, timeout=None):
            captured_urls.append(url)
            raise OSError("test")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            search("Jason Cox", source="email_archive", n=3)

        self.assertTrue(len(captured_urls) > 0)
        url = captured_urls[0]
        self.assertIn("q=", url)
        self.assertIn("n=3", url)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_memory_lookup_deduplicates_results(self):
        """memory_lookup() must not return the same text in duplicate entries."""
        duplicate_memory = [
            {"text": "Jordan went to a rave in 1999", "source": "socal_rave", "score": 0.9}
        ] * 5

        with patch.object(_mod, "batch_recall", return_value=[
            {"query": "test", "memories": duplicate_memory}
        ]):
            with patch.object(_mod, "recall", return_value=duplicate_memory):
                with patch.object(_mod, "search", return_value=[]):
                    results, _, _ = memory_lookup("rave 1999")

        # After dedup, should have at most 1 copy of this memory
        texts = [r.get("text", "") for r in results]
        jordan_rave_count = sum(1 for t in texts if "Jordan went to a rave" in t)
        self.assertEqual(jordan_rave_count, 1,
                         "Duplicate memories must be deduplicated")

    def test_classify_then_recall_pipeline(self):
        """classify_query -> recall pipeline must run without error."""
        sources, labels, prefer_search = classify_query("what was my blood pressure?")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("server down")
            result = recall("blood pressure", source=sources[0] if sources else None)

        self.assertEqual(result, [])

    def test_memory_lookup_returns_triple(self):
        """memory_lookup() must return a 3-tuple: (results, sources_searched, labels)."""
        with patch.object(_mod, "batch_recall", return_value=[]):
            with patch.object(_mod, "recall", return_value=[]):
                with patch.object(_mod, "search", return_value=[]):
                    result = memory_lookup("test query here")

        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 3)
        results, sources_searched, labels = result
        self.assertIsInstance(results, list)
        self.assertIsInstance(sources_searched, list)
        self.assertIsInstance(labels, list)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_classify_mode_prints_output(self):
        """--classify mode must print classification results."""
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--classify", "what raves were in LA?"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(_SCRIPT.parent)},
        )
        self.assertEqual(result.returncode, 0, f"--classify failed: {result.stderr[:300]}")
        self.assertIn("Query:", result.stdout)
        self.assertIn("Sources:", result.stdout)

    def test_no_args_prints_usage(self):
        """Running with no args must print usage and exit 1."""
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(_SCRIPT.parent)},
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Usage", result.stdout + result.stderr)

    def test_all_source_rules_have_required_keys(self):
        """Every rule in SOURCE_RULES must have patterns, sources, and label keys."""
        for rule in SOURCE_RULES:
            self.assertIn("patterns", rule, f"Rule missing 'patterns': {rule}")
            self.assertIn("sources", rule, f"Rule missing 'sources': {rule}")
            self.assertIn("label", rule, f"Rule missing 'label': {rule}")
            self.assertIsInstance(rule["patterns"], list)
            self.assertIsInstance(rule["sources"], list)
            self.assertGreater(len(rule["patterns"]), 0)
            self.assertGreater(len(rule["sources"]), 0)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_source_rules_non_empty(self):
        self.assertGreater(len(SOURCE_RULES), 10, "SOURCE_RULES must have at least 10 entries")

    def test_default_sources_non_empty(self):
        self.assertGreater(len(DEFAULT_SOURCES), 5, "DEFAULT_SOURCES must have at least 5 entries")

    def test_recall_count_defined(self):
        self.assertIsInstance(_mod.RECALL_COUNT, int)
        self.assertGreater(_mod.RECALL_COUNT, 0)

    def test_recall_url_and_search_url_defined(self):
        self.assertIn("http", _mod.RECALL_URL)
        self.assertIn("http", _mod.SEARCH_URL)

    def test_classify_query_returns_triple(self):
        sources, labels, prefer_search = classify_query("hello world test query")
        self.assertIsInstance(sources, list)
        self.assertIsInstance(labels, list)
        self.assertIsInstance(prefer_search, bool)

    def test_format_result_returns_string(self):
        item = {"text": "Test memory", "source": "test"}
        result = format_result(item, 1)
        self.assertIsInstance(result, str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
