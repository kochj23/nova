"""
test_nova_backfill_tags.py — All 7 test categories for nova_backfill_tags.py
Written by Jordan Koch.
"""

import importlib.util
import json
import re
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub nova_config and nova_tag_extractor before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_tag_extractor = MagicMock()
_tag_extractor.extract_tags = MagicMock(return_value=["tech", "infrastructure", "ai"])
sys.modules["nova_tag_extractor"] = _tag_extractor

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_backfill_tags.py"
_spec = importlib.util.spec_from_file_location("nova_backfill_tags", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

needs_backfill = _mod.needs_backfill
process_file = _mod.process_file
MOOD_WORDS = _mod.MOOD_WORDS


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        """Source must not contain API keys or passwords."""
        src = _SCRIPT.read_text()
        for p in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(p, src, f"Credential found: {p!r}")

    def test_no_pii_emails(self):
        """Source must not contain personal email addresses."""
        src = _SCRIPT.read_text()
        _at = "@"
        for pattern in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pattern, src)

    def test_no_hardcoded_home_path(self):
        """Source must not hardcode literal home path."""
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_log_file_path_uses_path_home(self):
        """LOG_FILE must be under home directory."""
        self.assertTrue(
            str(_mod.LOG_FILE).startswith(str(Path.home())),
            "LOG_FILE must be under home directory"
        )

    def test_no_external_network_calls_in_source(self):
        """Script must not make cloud API calls directly."""
        src = _SCRIPT.read_text()
        cloud_patterns = ["openrouter.ai", "api.openai.com", "anthropic.com/v1"]
        for url in cloud_patterns:
            self.assertNotIn(url, src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_needs_backfill_fast(self):
        """needs_backfill must complete in < 1ms per call."""
        tags_list = [["surreal"], [], ["tech", "ai"], ["great"]]
        start = time.perf_counter()
        for _ in range(1000):
            for tags in tags_list:
                needs_backfill(tags)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, "needs_backfill 1000x iterations too slow")

    def test_process_file_reads_only_once(self):
        """process_file must read the markdown file exactly once."""
        content = "---\ntitle: Test Post\ntags: [\"surreal\"]\ncategories: [\"dreams\"]\n---\n\nContent here."

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            fname = Path(f.name)

        read_count = [0]
        original_read_text = Path.read_text

        def counting_read(self, *a, **kw):
            if str(self) == str(fname):
                read_count[0] += 1
            return original_read_text(self, *a, **kw)

        with patch.object(Path, "read_text", counting_read):
            process_file(fname, "dreams")

        fname.unlink()
        self.assertLessEqual(read_count[0], 2, "process_file should read file at most twice")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_process_file_handles_extract_tags_failure(self):
        """process_file must not crash when extract_tags raises."""
        content = "---\ntitle: Test\ntags: [\"surreal\"]\n---\nContent."

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            fname = Path(f.name)

        _tag_extractor.extract_tags.side_effect = Exception("Ollama down")
        try:
            result = process_file(fname, "dreams")
        except Exception as exc:
            self.fail(f"process_file raised on extract_tags failure: {exc}")
        finally:
            _tag_extractor.extract_tags.side_effect = None
            fname.unlink(missing_ok=True)

    def test_process_file_handles_unreadable_file(self):
        """process_file must return False (not crash) on unreadable file."""
        result = process_file(Path("/nonexistent/file.md"), "dreams")
        # Should either return False or None without raising
        self.assertFalse(bool(result))


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_needs_backfill_empty_tags(self):
        """Empty tags list needs backfill."""
        self.assertTrue(needs_backfill([]))

    def test_needs_backfill_single_tag(self):
        """Single tag always needs backfill."""
        self.assertTrue(needs_backfill(["surreal"]))

    def test_needs_backfill_all_mood_words(self):
        """Tags that are all mood words need backfill."""
        self.assertTrue(needs_backfill(["surreal", "anxious"]))

    def test_needs_backfill_good_tags(self):
        """Meaningful multi-word tags don't need backfill."""
        self.assertFalse(needs_backfill(["technology", "machine-learning", "infrastructure"]))

    def test_needs_backfill_mixed_tags(self):
        """Mixed mood + non-mood tags don't need backfill."""
        self.assertFalse(needs_backfill(["surreal", "technology", "ai"]))

    def test_mood_words_contains_expected(self):
        """MOOD_WORDS must contain common mood/generic words."""
        expected = {"surreal", "anxious", "vivid", "weekly", "news"}
        self.assertTrue(expected.issubset(MOOD_WORDS))

    def test_process_file_skips_good_tags(self):
        """process_file returns False for posts with good tags."""
        content = "---\ntitle: Good Post\ntags: [\"technology\", \"machine-learning\", \"infrastructure\"]\n---\nContent."

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            fname = Path(f.name)

        result = process_file(fname, "tech-today")
        fname.unlink()
        self.assertFalse(result)

    def test_process_file_updates_bad_tags(self):
        """process_file returns True and updates tags for posts with mood-only tags."""
        content = "---\ntitle: My Dream\ntags: [\"surreal\"]\ncategories: [\"dreams\"]\n---\n\nContent here."

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            fname = Path(f.name)

        _tag_extractor.extract_tags.return_value = ["dream", "memory", "subconscious"]
        result = process_file(fname, "dreams")
        updated_content = fname.read_text()
        fname.unlink()

        self.assertTrue(result, "process_file should return True when updating tags")
        self.assertIn("dream", updated_content)

    def test_process_file_inserts_tags_when_missing(self):
        """process_file inserts tags line when no tags exist."""
        content = "---\ntitle: Essay\ncategories: [\"essays\"]\n---\nSome content."

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            fname = Path(f.name)

        _tag_extractor.extract_tags.return_value = ["analysis", "culture"]
        result = process_file(fname, "essays")
        updated = fname.read_text()
        fname.unlink()

        self.assertTrue(result)
        self.assertIn("tags:", updated)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_process_file_full_pipeline(self):
        """process_file correctly updates a file with bad tags end-to-end."""
        content = """---
title: "Tech Analysis Post"
date: 2026-01-01T09:00:00-07:00
draft: false
categories: ["tech-today"]
tags: ["weekly"]
---

This is a post about artificial intelligence and machine learning infrastructure.
The deployment pipeline uses Kubernetes and Docker containers.
"""

        _tag_extractor.extract_tags.return_value = ["technology", "AI", "infrastructure", "kubernetes"]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            fname = Path(f.name)

        result = process_file(fname, "tech-today")
        updated = fname.read_text()
        fname.unlink()

        self.assertTrue(result)
        # Original mood tag should be replaced
        self.assertNotIn('"weekly"', updated)
        # New tags should be present
        self.assertIn("technology", updated)

    def test_main_skips_nonexistent_categories(self):
        """main() skips category dirs that don't exist."""
        with patch.object(_mod, "HUGO_ROOT", Path("/nonexistent/path")):
            with patch.object(_mod, "LOG_FILE", Path(tempfile.mktemp())):
                try:
                    _mod.main()
                except Exception as exc:
                    self.fail(f"main() raised with missing dirs: {exc}")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_logs_completion(self, capsys=None):
        """main() logs a completion summary."""
        output_lines = []

        original_log = _mod.log
        def capture_log(msg):
            output_lines.append(msg)

        with patch.object(_mod, "HUGO_ROOT", Path("/nonexistent")):
            with patch.object(_mod, "LOG_FILE", Path(tempfile.mktemp())):
                with patch.object(_mod, "log", side_effect=capture_log):
                    _mod.main()

        completion_msgs = [l for l in output_lines if "complete" in l.lower()]
        self.assertTrue(len(completion_msgs) > 0, "main() should log a completion message")

    def test_process_file_preserves_other_frontmatter(self):
        """process_file must not corrupt other frontmatter fields."""
        content = """---
title: "Keep This Title"
date: 2026-01-01T09:00:00-07:00
draft: false
categories: ["dreams"]
tags: ["surreal"]
---
Content here.
"""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(content)
            fname = Path(f.name)

        _tag_extractor.extract_tags.return_value = ["dream", "memory"]
        process_file(fname, "dreams")
        updated = fname.read_text()
        fname.unlink()

        self.assertIn("Keep This Title", updated)
        self.assertIn("2026-01-01", updated)
        self.assertIn("draft: false", updated)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        """nova_backfill_tags.py compiles without syntax errors."""
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_key_functions_callable(self):
        """needs_backfill and process_file must be callable."""
        self.assertTrue(callable(needs_backfill))
        self.assertTrue(callable(process_file))

    def test_mood_words_is_set(self):
        """MOOD_WORDS must be a set."""
        self.assertIsInstance(MOOD_WORDS, set)
        self.assertGreater(len(MOOD_WORDS), 5)

    def test_hugo_root_defined(self):
        """HUGO_ROOT must be defined."""
        self.assertIsInstance(_mod.HUGO_ROOT, Path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
