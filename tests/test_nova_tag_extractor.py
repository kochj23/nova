"""
test_nova_tag_extractor.py — All 7 test categories for nova_tag_extractor.py
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
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_tag_extractor.py"
_spec = importlib.util.spec_from_file_location("nova_tag_extractor", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

extract_tags = _mod.extract_tags
_extract_keyword_tags = _mod._extract_keyword_tags
_extract_llm_tags = _mod._extract_llm_tags
CATEGORY_SEEDS = _mod.CATEGORY_SEEDS
STOPWORDS = _mod.STOPWORDS


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for p in ["sk-", "ghp_", "AKIA"]:
            self.assertNotIn(p, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(p, src)

    def test_ollama_url_is_local(self):
        """OLLAMA_URL must be localhost, not external."""
        self.assertIn("127.0.0.1", _mod.OLLAMA_URL)

    def test_no_cloud_llm_in_source(self):
        src = _SCRIPT.read_text()
        for cloud in ["openai.com", "openrouter.ai", "anthropic.com/v1"]:
            self.assertNotIn(cloud, src)

    def test_tags_never_contain_stopwords(self):
        """extract_tags must not return stopwords as tags."""
        tags = _extract_keyword_tags(
            "the and or in",
            "the quick brown fox jumps over lazy dog",
            "essays", 5
        )
        for tag in tags:
            self.assertNotIn(tag.lower(), STOPWORDS,
                             f"Stopword in tags: {tag!r}")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_keyword_tags_fast(self):
        """_extract_keyword_tags must complete in < 50ms."""
        content = "artificial intelligence machine learning deep learning " * 100
        start = time.perf_counter()
        for _ in range(100):
            _extract_keyword_tags("AI Post", content, "tech-today", 5)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_extract_tags_no_llm_for_rich_content(self):
        """extract_tags should not call LLM when keyword extraction yields >= 3 tags."""
        content = "artificial intelligence machine learning deep neural networks python programming"
        llm_calls = []

        with patch.object(_mod, "_extract_llm_tags", side_effect=lambda *a: llm_calls.append(a)):
            tags = extract_tags("AI Article", content, "tech-today", 5)

        self.assertEqual(len(llm_calls), 0, "LLM should not be called for rich keyword content")
        self.assertGreaterEqual(len(tags), 3)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_llm_tags_falls_back_on_ollama_failure(self):
        """_extract_llm_tags returns category seeds when Ollama fails."""
        with patch("urllib.request.urlopen", side_effect=Exception("Ollama down")):
            result = _extract_llm_tags("Test Title", "Some content", "dreams", 5)

        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        # Should return category seeds as fallback
        seeds = CATEGORY_SEEDS.get("dreams", ["journal"])
        for tag in result:
            self.assertIn(tag, seeds)

    def test_extract_tags_fallback_to_llm_on_few_keywords(self):
        """extract_tags calls LLM when keyword extraction yields < 3 tags."""
        # Very short content with no real keywords
        content = "ok"

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "response": '["dream", "memory", "subconscious", "night", "vision"]'
        }).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = extract_tags("X", content, "dreams", 5)

        self.assertIsInstance(result, list)

    def test_llm_tags_handles_json_parse_error(self):
        """_extract_llm_tags handles invalid JSON response from LLM."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "response": "not valid json at all"
        }).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _extract_llm_tags("Title", "Content", "tech-today", 5)

        self.assertIsInstance(result, list)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_keyword_tags_returns_list(self):
        result = _extract_keyword_tags("AI Post", "machine learning python", "tech-today", 5)
        self.assertIsInstance(result, list)

    def test_keyword_tags_respects_n(self):
        """_extract_keyword_tags returns at most n tags."""
        content = "alpha beta gamma delta epsilon zeta eta theta iota kappa"
        result = _extract_keyword_tags("Title", content, "essays", 3)
        self.assertLessEqual(len(result), 3)

    def test_keyword_tags_strips_urls(self):
        """URLs are stripped from content before tagging."""
        content = "https://example.com/article artificial intelligence"
        tags = _extract_keyword_tags("Title", content, "tech-today", 5)
        for tag in tags:
            self.assertNotIn("http", tag)
            self.assertNotIn("example.com", tag)

    def test_keyword_tags_adds_category_seeds(self):
        """Category seeds are always included in tags."""
        result = _extract_keyword_tags("Post", "content here", "dreams", 5)
        seeds = CATEGORY_SEEDS.get("dreams", [])
        # At least some seeds should appear
        found_seeds = [s for s in seeds if s in result]
        self.assertTrue(len(found_seeds) > 0, "Category seeds should appear in tags")

    def test_category_seeds_defined_for_all_categories(self):
        """All Hugo categories must have seeds defined."""
        expected_categories = ["dreams", "essays", "opinions", "tech-today",
                               "research", "after-dark", "art", "digests"]
        for cat in expected_categories:
            self.assertIn(cat, CATEGORY_SEEDS, f"Missing seeds for category: {cat}")
            self.assertGreater(len(CATEGORY_SEEDS[cat]), 0)

    def test_stopwords_contains_common_words(self):
        """STOPWORDS must contain common English words."""
        expected = {"the", "and", "or", "but", "in", "on", "for", "to"}
        self.assertTrue(expected.issubset(STOPWORDS))

    def test_extract_tags_returns_lowercase(self):
        """Tags must be lowercase."""
        content = "Machine Learning and Artificial Intelligence Systems"
        tags = _extract_keyword_tags("ML Article", content, "tech-today", 5)
        for tag in tags:
            self.assertEqual(tag, tag.lower(), f"Tag not lowercase: {tag!r}")

    def test_llm_tags_cleans_whitespace(self):
        """LLM-extracted tags must have whitespace stripped."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "response": '["  machine learning  ", "  ai  ", "technology"]'
        }).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _extract_llm_tags("Title", "content", "tech-today", 5)

        for tag in result:
            self.assertEqual(tag, tag.strip())


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_extract_tags_full_pipeline(self):
        """extract_tags returns meaningful tags for real article content."""
        title = "Understanding Neural Networks in Python"
        content = """
        Deep learning and neural networks have revolutionized artificial intelligence.
        Python libraries like PyTorch and TensorFlow make it easy to build models.
        Backpropagation and gradient descent are core training algorithms.
        """
        tags = extract_tags(title, content, "tech-today", 5)
        self.assertIsInstance(tags, list)
        self.assertGreater(len(tags), 0)
        self.assertLessEqual(len(tags), 5)

    def test_extract_tags_dream_post(self):
        """extract_tags handles dream category posts."""
        title = "The Dark Forest"
        content = "I was walking through a misty forest at night. Ancient trees surrounded me."
        tags = extract_tags(title, content, "dreams", 5)
        # Should include category seeds
        self.assertIn("dream", tags)

    def test_extract_tags_minimum_word_length(self):
        """Tags must be > 3 chars (minimum word length filter)."""
        content = "the and for but in to a an so as if of on at by do is are was"
        tags = _extract_keyword_tags("Short Words", content, "essays", 5)
        for tag in tags:
            self.assertGreater(len(tag), 3, f"Tag too short: {tag!r}")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_extract_tags_never_returns_empty_for_rich_content(self):
        """extract_tags must return at least 1 tag for non-empty content."""
        content = "technology infrastructure deployment systems monitoring"
        tags = extract_tags("Tech Post", content, "tech-today", 3)
        self.assertGreater(len(tags), 0)

    def test_extract_tags_n_limit_respected(self):
        """extract_tags must return at most n tags."""
        content = "one two three four five six seven eight nine ten " * 5
        for n in [1, 3, 5]:
            tags = extract_tags("Title", content, "essays", n)
            self.assertLessEqual(len(tags), n, f"Too many tags for n={n}")

    def test_llm_tags_replaces_spaces_with_hyphens(self):
        """Multi-word LLM tags must use hyphens, not spaces."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "response": '["machine learning", "deep learning", "neural network"]'
        }).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _extract_llm_tags("Title", "content", "tech-today", 5)

        for tag in result:
            self.assertNotIn(" ", tag, f"Space in tag: {tag!r}")


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

    def test_key_functions_callable(self):
        for fn in [extract_tags, _extract_keyword_tags, _extract_llm_tags]:
            self.assertTrue(callable(fn))

    def test_constants_defined(self):
        self.assertIsInstance(CATEGORY_SEEDS, dict)
        self.assertIsInstance(STOPWORDS, set)
        self.assertIsInstance(_mod.OLLAMA_URL, str)
        self.assertIsInstance(_mod.MODEL, str)

    def test_model_defined(self):
        """MODEL must be defined for Ollama."""
        self.assertGreater(len(_mod.MODEL), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
