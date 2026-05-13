"""
test_nova_cross_linker.py — All 7 test categories for nova_cross_linker.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_cross_linker.py"
_spec = importlib.util.spec_from_file_location("nova_cross_linker", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_recall = _mod._recall
_get_published_posts = _mod._get_published_posts
find_related = _mod.find_related
_title_overlap = _mod._title_overlap
format_related_frontmatter = _mod.format_related_frontmatter
CATEGORY_URLS = _mod.CATEGORY_URLS
CATEGORY_EMOJI = _mod.CATEGORY_EMOJI


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

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_memory_server_is_local(self):
        """MEMORY_SERVER must be localhost."""
        self.assertIn("127.0.0.1", _mod.MEMORY_SERVER)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_recall_fast_on_failure(self):
        """_recall returns [] quickly when memory server unreachable."""
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            start = time.perf_counter()
            result = _recall("test query")
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)
        self.assertEqual(result, [])

    def test_title_overlap_fast(self):
        """_title_overlap must be < 1ms per call."""
        start = time.perf_counter()
        for _ in range(10000):
            _title_overlap("text with machine learning words", "Machine Learning in Python")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_find_related_bounded_results(self):
        """find_related must return at most max_results items."""
        with patch.object(_mod, "_recall", return_value=[
            {"text": f"memory {i}", "score": 0.9}
            for i in range(100)
        ]):
            with patch.object(_mod, "_get_published_posts", return_value={
                f"key{i}": {
                    "url": f"/dreams/post-{i}/",
                    "title": f"Post {i}",
                    "category": "dreams" if i % 2 == 0 else "essays",
                    "slug": f"post-{i}",
                }
                for i in range(100)
            }):
                result = find_related("test text", "tech-today", "current-slug", max_results=4)

        self.assertLessEqual(len(result), 4)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_recall_handles_json_error(self):
        """_recall returns [] on JSON decode error."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json"

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _recall("test")
        self.assertEqual(result, [])

    def test_find_related_handles_recall_failure(self):
        """find_related returns [] when memory server is down."""
        with patch.object(_mod, "_recall", return_value=[]):
            with patch.object(_mod, "_get_published_posts", return_value={}):
                result = find_related("text", "dreams", "slug")
        self.assertEqual(result, [])


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_title_overlap_detects_common_words(self):
        """_title_overlap returns True when >= 2 significant title words appear in text."""
        result = _title_overlap(
            "machine learning and neural networks are transforming tech",
            "Neural Networks in Machine Learning"
        )
        self.assertTrue(result)

    def test_title_overlap_false_for_no_common(self):
        """_title_overlap returns False when titles share no significant words."""
        result = _title_overlap(
            "cooking recipes for autumn season",
            "Quantum Physics Experiments"
        )
        self.assertFalse(result)

    def test_title_overlap_ignores_stopwords(self):
        """_title_overlap must not match on stopwords like 'with', 'from'."""
        result = _title_overlap(
            "with from that this have been",
            "With From That This Have Been"
        )
        # stopwords filtered — no significant words match
        self.assertFalse(result)

    def test_format_related_frontmatter_empty(self):
        """format_related_frontmatter returns '' for empty list."""
        result = format_related_frontmatter([])
        self.assertEqual(result, "")

    def test_format_related_frontmatter_structure(self):
        """format_related_frontmatter returns valid YAML-like block."""
        related = [
            {"url": "/dreams/post-1/", "title": "A Dream Post", "category": "🌙 Dreams"},
            {"url": "/essays/essay-1/", "title": "An Essay", "category": "📝 Essays"},
        ]
        result = format_related_frontmatter(related)
        self.assertIn("related:", result)
        self.assertIn("/dreams/post-1/", result)
        self.assertIn("A Dream Post", result)

    def test_format_related_frontmatter_escapes_quotes(self):
        """format_related_frontmatter must escape quotes in titles."""
        related = [{"url": "/test/", "title": 'Post with "quotes"', "category": "essays"}]
        result = format_related_frontmatter(related)
        self.assertNotIn('Post with "quotes"', result)  # raw quotes must be escaped

    def test_category_urls_defined(self):
        """CATEGORY_URLS must map all Hugo content categories."""
        expected = ["dreams", "essays", "opinions", "tech-today", "after-dark"]
        for cat in expected:
            self.assertIn(cat, CATEGORY_URLS)

    def test_category_emoji_defined(self):
        """CATEGORY_EMOJI must be defined for standard categories."""
        self.assertIn("dreams", CATEGORY_EMOJI)
        self.assertIn("essays", CATEGORY_EMOJI)

    def test_recall_returns_list_response(self):
        """_recall handles list response from memory server."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps([
            {"text": "test memory", "score": 0.9}
        ]).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _recall("test")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["text"], "test memory")

    def test_recall_handles_dict_response(self):
        """_recall handles dict with 'memories' key response."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "memories": [{"text": "memory", "score": 0.8}]
        }).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _recall("test")

        self.assertEqual(len(result), 1)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_get_published_posts_reads_markdown(self):
        """_get_published_posts reads Hugo content and returns lookup dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hugo_root = Path(tmpdir)
            (hugo_root / "content" / "dreams").mkdir(parents=True)

            post_content = """---
title: "Test Dream Post"
date: 2026-01-01
---
This is the dream content about machines and learning.
"""
            (hugo_root / "content" / "dreams" / "test-dream.md").write_text(post_content)

            with patch.object(_mod, "HUGO_ROOT", hugo_root):
                posts = _get_published_posts()

        self.assertGreater(len(posts), 0)
        # Verify structure
        for key, post in posts.items():
            self.assertIn("url", post)
            self.assertIn("title", post)
            self.assertIn("category", post)

    def test_find_related_cross_category_only(self):
        """find_related must only return posts from other categories."""
        published = {
            "content dreams": {"url": "/dreams/d1/", "title": "Dream Post",
                               "category": "dreams", "slug": "d1"},
            "content essays": {"url": "/essays/e1/", "title": "Essay Post",
                               "category": "essays", "slug": "e1"},
        }
        memories = [
            {"text": "content dreams", "score": 0.9},
            {"text": "content essays", "score": 0.85},
        ]

        with patch.object(_mod, "_recall", return_value=memories):
            with patch.object(_mod, "_get_published_posts", return_value=published):
                result = find_related("test content", "dreams", "current-slug")

        # Should not include dreams posts (same category)
        for r in result:
            self.assertNotIn("dreams", r.get("url", ""))


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_find_related_skips_current_slug(self):
        """find_related must not include the current post in results."""
        published = {
            "current post content": {
                "url": "/dreams/current-post/",
                "title": "Current Post",
                "category": "dreams",
                "slug": "current-post",
            },
            "other content essays": {
                "url": "/essays/other/",
                "title": "Other Post",
                "category": "essays",
                "slug": "other",
            },
        }
        memories = [
            {"text": "current post content", "score": 0.95},
            {"text": "other content essays", "score": 0.85},
        ]

        with patch.object(_mod, "_recall", return_value=memories):
            with patch.object(_mod, "_get_published_posts", return_value=published):
                result = find_related("text", "dreams", "current-post")

        # current-post should not appear in results
        slugs = [r["url"] for r in result]
        self.assertNotIn("/dreams/current-post/", slugs)

    def test_find_related_filters_low_scores(self):
        """find_related excludes memories below min_score."""
        published = {
            "low score memory": {"url": "/essays/low/", "title": "Low Score",
                                  "category": "essays", "slug": "low"},
        }
        memories = [
            {"text": "low score memory", "score": 0.5},  # Below 0.72 threshold
        ]

        with patch.object(_mod, "_recall", return_value=memories):
            with patch.object(_mod, "_get_published_posts", return_value=published):
                result = find_related("test", "dreams", "current", min_score=0.72)

        self.assertEqual(len(result), 0)


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
        for fn in [_recall, _get_published_posts, find_related,
                   _title_overlap, format_related_frontmatter]:
            self.assertTrue(callable(fn))

    def test_hugo_root_defined(self):
        self.assertIsInstance(_mod.HUGO_ROOT, Path)

    def test_memory_server_defined(self):
        self.assertIsInstance(_mod.MEMORY_SERVER, str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
