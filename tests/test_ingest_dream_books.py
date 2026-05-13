"""
test_ingest_dream_books.py — All 7 test categories for ingest_dream_books.py
Written by Jordan Koch.
"""

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

import importlib.util
_SCRIPT = Path(__file__).parent.parent / "scripts" / "ingest_dream_books.py"
_spec = importlib.util.spec_from_file_location("ingest_dream_books", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
# Patch 'dict | None' union type syntax for Python < 3.10 compatibility
# by pre-patching builtins to allow the newer syntax via __future__ annotations
import sys as _sys
_orig_getattr = None
try:
    _spec.loader.exec_module(_mod)
except TypeError:
    # Python < 3.10: `dict | None` not supported. Load via source with annotation fix.
    src = _SCRIPT.read_text()
    src_fixed = "from __future__ import annotations\n" + src
    exec(compile(src_fixed, str(_SCRIPT), "exec"), _mod.__dict__)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-", "password ="]:
            self.assertNotIn(pattern, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_memory_server_is_localhost(self):
        self.assertIn("127.0.0.1", _mod.MEMORY_SERVER)

    def test_payload_json_encoded(self):
        src = _SCRIPT.read_text()
        self.assertIn("json.dumps", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_batch_delay_defined(self):
        self.assertIsNotNone(_mod.BATCH_DELAY)

    def test_books_list_bounded(self):
        self.assertGreater(len(_mod.BOOKS), 0)
        self.assertLessEqual(len(_mod.BOOKS), 100)

    def test_send_memory_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=10", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_send_memory_returns_none_on_failure(self):
        """send_memory() must return None on network failure, not crash."""
        def fail(*args, **kwargs):
            raise OSError("connection refused")

        with patch("urllib.request.urlopen", side_effect=fail):
            result = _mod.send_memory("Test fact about dreams.", {})
        self.assertIsNone(result)

    def test_errors_counted(self):
        """main() must count errors."""
        src = _SCRIPT.read_text()
        self.assertIn("errors", src)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_books_have_required_fields(self):
        """Every book must have title, author, year, and memories."""
        for book in _mod.BOOKS:
            self.assertIn("title", book)
            self.assertIn("author", book)
            self.assertIn("year", book)
            self.assertIn("memories", book)
            self.assertIsInstance(book["memories"], list)
            self.assertGreater(len(book["memories"]), 0)

    def test_books_years_are_ints(self):
        for book in _mod.BOOKS:
            self.assertIsInstance(book["year"], int)

    def test_send_memory_posts_to_remember(self):
        posted = []

        def capture(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=capture):
            _mod.send_memory("Test dream book fact.", {"title": "Test Book"})

        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["source"], _mod.SOURCE)
        self.assertIn("Test dream book fact.", posted[0]["text"])

    def test_source_constant(self):
        self.assertEqual(_mod.SOURCE, "dream_books")

    def test_log_function_exists(self):
        self.assertTrue(callable(_mod.log))


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_ingests_all_book_memories(self):
        """main() must attempt to ingest memories for all books."""
        posted_count = [0]

        def count_posts(req, timeout=None):
            posted_count[0] += 1
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=count_posts):
            with patch("time.sleep"):
                _mod.main()

        expected = sum(len(b["memories"]) for b in _mod.BOOKS)
        self.assertEqual(posted_count[0], expected,
                         f"Expected {expected} memory posts, got {posted_count[0]}")

    def test_metadata_includes_book_info(self):
        """send_memory() must include book metadata."""
        posted = []

        def capture(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        meta = {"title": "Why We Sleep", "author": "Matthew Walker", "year": 2017}
        with patch("urllib.request.urlopen", side_effect=capture):
            _mod.send_memory("Sleep is important.", meta)

        self.assertEqual(posted[0]["metadata"]["title"], "Why We Sleep")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_slack_notified_on_completion(self):
        """main() must call post_status after completing ingest."""
        with patch("urllib.request.urlopen") as mock_url:
            mock_r = MagicMock()
            mock_r.__enter__ = lambda s: s
            mock_r.__exit__ = MagicMock(return_value=False)
            mock_url.return_value = mock_r
            with patch("time.sleep"):
                _mod.main()
        _nova_cfg.post_both.assert_called()

    def test_all_book_titles_non_empty(self):
        """All book titles must be non-empty strings."""
        for book in _mod.BOOKS:
            self.assertIsInstance(book["title"], str)
            self.assertGreater(len(book["title"]), 0)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"ingest_dream_books.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_main_function_exists(self):
        self.assertTrue(callable(_mod.main))

    def test_books_count_reasonable(self):
        self.assertGreaterEqual(len(_mod.BOOKS), 5)

    def test_constants_defined(self):
        for attr in ["MEMORY_SERVER", "SOURCE", "BATCH_DELAY", "BOOKS"]:
            self.assertTrue(hasattr(_mod, attr))


if __name__ == "__main__":
    unittest.main(verbosity=2)
