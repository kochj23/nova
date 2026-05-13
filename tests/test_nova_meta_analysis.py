"""
test_nova_meta_analysis.py — All 7 test categories for nova_meta_analysis.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# Stubs
_nova_cfg = MagicMock()
_nova_cfg.openrouter_api_key = MagicMock(return_value=None)
sys.modules["nova_config"] = _nova_cfg

_tag_extractor = MagicMock()
_tag_extractor.extract_tags = MagicMock(return_value=["meta", "analysis", "reflection"])
sys.modules["nova_tag_extractor"] = _tag_extractor

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_meta_analysis.py"
_spec = importlib.util.spec_from_file_location("nova_meta_analysis", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_should_run = _mod._should_run
_collect_month_posts = _mod._collect_month_posts
_analyze_patterns = _mod._analyze_patterns
_generate_meta_analysis = _mod._generate_meta_analysis
main = _mod.main


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

    def test_openrouter_key_from_function(self):
        """OpenRouter API key must come from nova_config function, not hardcoded."""
        src = _SCRIPT.read_text()
        self.assertIn("openrouter_api_key()", src)
        self.assertNotIn("sk-or-", src)

    def test_ollama_is_local_fallback(self):
        """Ollama URL must be localhost for offline fallback."""
        self.assertIn("127.0.0.1", _mod.OLLAMA_URL)

    def test_log_file_path_uses_home(self):
        self.assertTrue(str(_mod.LOG_FILE).startswith(str(Path.home())))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_analyze_patterns_fast(self):
        """_analyze_patterns must complete in < 100ms for 50 posts."""
        posts = [
            {"category": "dreams", "title": f"Post {i}", "tags": ["dream", "memory"],
             "body": "Content " * 20}
            for i in range(50)
        ]
        start = time.perf_counter()
        _analyze_patterns(posts)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_collect_month_posts_fast_no_hugo(self):
        """_collect_month_posts must return quickly when HUGO_ROOT missing."""
        with patch.object(_mod, "HUGO_ROOT", Path("/nonexistent")):
            start = time.perf_counter()
            result = _collect_month_posts()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)
        self.assertEqual(result, [])


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_generate_uses_ollama_when_no_openrouter_key(self):
        """_generate_meta_analysis falls back to Ollama when no API key."""
        _nova_cfg.openrouter_api_key.return_value = None
        posts = [{"category": "dreams", "title": "Test", "tags": [], "body": "content"}]
        patterns = {"total_posts": 1, "by_category": {}, "top_tags": [],
                    "recurring_words": [], "most_active_category": "dreams"}

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"response": "Generated analysis text"}).encode()

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _generate_meta_analysis(posts, patterns, "May 2026")

        self.assertIsNotNone(result)

    def test_generate_returns_none_when_both_fail(self):
        """_generate_meta_analysis returns None when both LLMs fail."""
        _nova_cfg.openrouter_api_key.return_value = None
        posts = [{"category": "dreams", "title": "T", "tags": [], "body": "c"}]
        patterns = {"total_posts": 1, "by_category": {}, "top_tags": [],
                    "recurring_words": [], "most_active_category": "dreams"}

        with patch("urllib.request.urlopen", side_effect=Exception("all LLMs down")):
            result = _generate_meta_analysis(posts, patterns, "May 2026")

        self.assertIsNone(result)

    def test_main_handles_generation_failure(self):
        """main() does not crash when analysis generation fails."""
        with patch.object(_mod, "_should_run", return_value=True):
            with patch.object(_mod, "_collect_month_posts", return_value=[
                {"cat": "x"} for _ in range(10)
            ]):
                with patch.object(_mod, "_analyze_patterns", return_value={
                    "total_posts": 10, "by_category": {}, "top_tags": [],
                    "recurring_words": [], "most_active_category": "dreams"
                }):
                    with patch.object(_mod, "_generate_meta_analysis", return_value=None):
                        try:
                            main()
                        except Exception as exc:
                            self.fail(f"main() raised on generation failure: {exc}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_should_run_false_on_non_sunday(self):
        """_should_run returns False on any day that isn't Sunday."""
        today = date.today()
        if today.weekday() != 6:  # Not Sunday
            self.assertFalse(_should_run())

    def test_should_run_false_after_recent_run(self):
        """_should_run returns False if ran in last 25 days."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            state = {"last_run": datetime.now().isoformat()}
            json.dump(state, f)
            fname = Path(f.name)

        with patch.object(_mod, "STATE_FILE", fname):
            result = _should_run()

        fname.unlink()
        self.assertFalse(result)

    def test_analyze_patterns_counts_categories(self):
        """_analyze_patterns counts posts per category correctly."""
        posts = [
            {"category": "dreams", "title": "D1", "tags": [], "body": "content"},
            {"category": "dreams", "title": "D2", "tags": [], "body": "content"},
            {"category": "essays", "title": "E1", "tags": [], "body": "content"},
        ]
        result = _analyze_patterns(posts)
        self.assertEqual(result["by_category"]["dreams"], 2)
        self.assertEqual(result["by_category"]["essays"], 1)
        self.assertEqual(result["total_posts"], 3)

    def test_analyze_patterns_finds_recurring_words(self):
        """_analyze_patterns identifies recurring words across posts."""
        posts = [
            {"category": "tech-today", "title": "AI Post",
             "tags": [], "body": "artificial intelligence machine learning deep learning"},
            {"category": "tech-today", "title": "ML Post",
             "tags": [], "body": "machine learning neural networks deep learning"},
        ]
        result = _analyze_patterns(posts)
        word_dict = dict(result["recurring_words"])
        # "learning" appears twice
        self.assertIn("learning", word_dict)
        self.assertGreaterEqual(word_dict["learning"], 2)

    def test_analyze_patterns_most_active(self):
        """_analyze_patterns identifies the most active category."""
        posts = [
            {"category": "dreams", "title": f"D{i}", "tags": [], "body": "c"}
            for i in range(5)
        ] + [
            {"category": "essays", "title": "E1", "tags": [], "body": "c"}
        ]
        result = _analyze_patterns(posts)
        self.assertEqual(result["most_active_category"], "dreams")

    def test_collect_month_posts_returns_list(self):
        """_collect_month_posts always returns a list."""
        with patch.object(_mod, "HUGO_ROOT", Path("/nonexistent")):
            result = _collect_month_posts()
        self.assertIsInstance(result, list)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_collect_month_posts_reads_recent_posts(self):
        """_collect_month_posts reads markdown files from last 30 days."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hugo_root = Path(tmpdir)
            (hugo_root / "content" / "dreams").mkdir(parents=True)

            recent = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-07:00")
            post_content = f"""---
title: "Recent Dream Post"
date: {recent}
tags: ["dream", "memory"]
---
Dream content here.
"""
            (hugo_root / "content" / "dreams" / "2026-05-01-dream.md").write_text(post_content)

            with patch.object(_mod, "HUGO_ROOT", hugo_root):
                posts = _collect_month_posts()

        self.assertTrue(any(p["title"] == "Recent Dream Post" for p in posts))

    def test_collect_month_posts_excludes_old_posts(self):
        """_collect_month_posts excludes posts older than 30 days."""
        with tempfile.TemporaryDirectory() as tmpdir:
            hugo_root = Path(tmpdir)
            (hugo_root / "content" / "dreams").mkdir(parents=True)

            old = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M:%S-07:00")
            post_content = f"""---
title: "Old Dream Post"
date: {old}
tags: ["dream"]
---
Old content.
"""
            (hugo_root / "content" / "dreams" / "old-dream.md").write_text(post_content)

            with patch.object(_mod, "HUGO_ROOT", hugo_root):
                posts = _collect_month_posts()

        self.assertFalse(any(p["title"] == "Old Dream Post" for p in posts))


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_skips_when_not_first_sunday(self):
        """main() skips analysis when _should_run returns False."""
        with patch.object(_mod, "_should_run", return_value=False):
            with patch.object(_mod, "_collect_month_posts") as mock_collect:
                main()
        mock_collect.assert_not_called()

    def test_main_skips_when_too_few_posts(self):
        """main() skips when fewer than 5 posts found."""
        with patch.object(_mod, "_should_run", return_value=True):
            with patch.object(_mod, "_collect_month_posts", return_value=[
                {"category": "dreams", "title": f"Post {i}"}
                for i in range(3)  # Only 3 posts
            ]):
                with patch.object(_mod, "_generate_meta_analysis") as mock_gen:
                    main()
        mock_gen.assert_not_called()


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
        for fn in [_should_run, _collect_month_posts, _analyze_patterns,
                   _generate_meta_analysis, main]:
            self.assertTrue(callable(fn))

    def test_content_out_path_defined(self):
        self.assertIsInstance(_mod.CONTENT_OUT, Path)

    def test_state_file_path_uses_home(self):
        self.assertTrue(str(_mod.STATE_FILE).startswith(str(Path.home())))


if __name__ == "__main__":
    unittest.main(verbosity=2)
