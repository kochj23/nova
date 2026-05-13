"""
test_ingest_programming_classics.py — All 7 test categories for ingest_programming_classics.py
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
_SCRIPT = Path(__file__).parent.parent / "scripts" / "ingest_programming_classics.py"
_spec = importlib.util.spec_from_file_location("ingest_programming_classics", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_memory_server_localhost(self):
        self.assertIn("127.0.0.1", _mod.MEMORY_SERVER)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_batch_delay_bounded(self):
        self.assertLessEqual(_mod.BATCH_DELAY, 2.0)
        self.assertGreater(_mod.BATCH_DELAY, 0)

    def test_notify_every_defined(self):
        self.assertEqual(_mod.NOTIFY_EVERY, 300)

    def test_books_memories_count(self):
        total = sum(len(b["memories"]) for b in _mod.BOOKS)
        self.assertGreater(total, 50)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_silent_on_failure(self):
        """remember() must not raise on network failure."""
        def fail(*args, **kwargs):
            raise OSError("server down")

        with patch("urllib.request.urlopen", side_effect=fail):
            with patch("time.sleep"):
                _mod.remember("Test memory.", {})
        # No exception raised

    def test_error_logged_on_failure(self):
        src = _SCRIPT.read_text()
        self.assertIn("ERR:", src)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_books_structure(self):
        for book in _mod.BOOKS:
            self.assertIn("title", book)
            self.assertIn("author", book)
            self.assertIn("year", book)
            self.assertIn("memories", book)

    def test_source_constant(self):
        self.assertEqual(_mod.SOURCE, "programming_books")

    def test_ingest_book_function_exists(self):
        self.assertTrue(callable(_mod.ingest_book))

    def test_maybe_notify_function_exists(self):
        self.assertTrue(callable(_mod.maybe_notify))

    def test_remember_increments_total(self):
        initial = _mod._total_memories
        mock_r = MagicMock()
        mock_r.__enter__ = lambda s: s
        mock_r.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_r):
            with patch("time.sleep"):
                with patch.object(_mod, "maybe_notify"):
                    _mod.remember("Test memory about algorithms.", {})
        self.assertEqual(_mod._total_memories, initial + 1)

    def test_pragmatic_programmer_in_books(self):
        titles = [b["title"] for b in _mod.BOOKS]
        self.assertTrue(any("Pragmatic" in t for t in titles))

    def test_clean_code_in_books(self):
        titles = [b["title"] for b in _mod.BOOKS]
        self.assertTrue(any("Clean Code" in t for t in titles))


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_ingest_book_stores_all_memories(self):
        stored = [0]

        def mock_remember(text, meta):
            stored[0] += 1

        book = _mod.BOOKS[0]
        with patch.object(_mod, "remember", side_effect=mock_remember):
            _mod.ingest_book(book["title"], book["author"],
                             book["year"], book["memories"])

        self.assertEqual(stored[0], len(book["memories"]))

    def test_metadata_includes_book_title(self):
        """remember() must pass book metadata."""
        captured = []

        def mock_req(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=mock_req):
            with patch("time.sleep"):
                with patch.object(_mod, "maybe_notify"):
                    _mod.remember("Test memory.", {"book": "Clean Code"})

        self.assertEqual(captured[0]["metadata"]["book"], "Clean Code")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_slack_notified_at_start_and_end(self):
        src = _SCRIPT.read_text()
        self.assertIn("Ingest Starting", src)
        self.assertIn("Ingest Complete", src)

    def test_all_books_ingested_in_main(self):
        books_ingested = []

        def mock_ingest(title, author, year, memories):
            books_ingested.append(title)

        with patch.object(_mod, "ingest_book", side_effect=mock_ingest):
            with patch.object(_mod, "post_notify"):
                _mod.main()

        self.assertEqual(len(books_ingested), len(_mod.BOOKS))


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"ingest_programming_classics.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_main_function_exists(self):
        self.assertTrue(callable(_mod.main))

    def test_constants_present(self):
        for attr in ["MEMORY_SERVER", "SOURCE", "BATCH_DELAY",
                     "NOTIFY_EVERY", "BOOKS"]:
            self.assertTrue(hasattr(_mod, attr))


if __name__ == "__main__":
    unittest.main(verbosity=2)
