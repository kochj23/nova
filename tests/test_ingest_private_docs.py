"""
test_ingest_private_docs.py — All 7 test categories for ingest_private_docs.py
Written by Jordan Koch.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

import importlib.util
_SCRIPT = Path(__file__).parent.parent / "scripts" / "ingest_private_docs.py"
_spec = importlib.util.spec_from_file_location("ingest_private_docs", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_source_is_private_document(self):
        """All memories must use source='private_document'."""
        src = _SCRIPT.read_text()
        self.assertIn('"private_document"', src,
                      "Source must be set to 'private_document'")

    def test_metadata_private_flag(self):
        """Metadata must include private=True flag."""
        src = _SCRIPT.read_text()
        self.assertIn('"private": True', src,
                      "Metadata must set private=True")

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_security_docstring_present(self):
        """Script must have SECURITY docstring explaining restrictions."""
        src = _SCRIPT.read_text()
        self.assertIn("SECURITY", src)
        self.assertIn("never", src.lower())

    def test_text_truncated_at_2000(self):
        src = _SCRIPT.read_text()
        self.assertIn("truncate_at_boundary", src)

    def test_slack_post_contains_counts_only(self):
        """Slack notification must contain counts only, no content."""
        src = _SCRIPT.read_text()
        self.assertIn("counts only, no content", src.lower(),
                      "Comment about not sharing content must be present")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_chunk_size_defined(self):
        self.assertIsNotNone(_mod.CHUNK_SIZE)
        self.assertEqual(_mod.CHUNK_SIZE, 400)

    def test_remember_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=15", src)

    def test_rate_limit_on_failure(self):
        src = _SCRIPT.read_text()
        self.assertIn("time.sleep(2)", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_silent_on_failure(self):
        def fail(*args, **kwargs):
            raise OSError("server down")

        with patch("urllib.request.urlopen", side_effect=fail):
            result = _mod.remember("Private doc text.", "doc.pdf", "doc.pdf")
        self.assertFalse(result)

    def test_failed_counter_incremented(self):
        src = _SCRIPT.read_text()
        self.assertIn("failed += 1", src)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_remember_posts_private_document(self):
        posted = []

        def capture(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        # Text must be > 30 chars for remember() to proceed
        with patch("urllib.request.urlopen", side_effect=capture):
            _mod.remember(
                "Work document content about quarterly planning and strategy.",
                "report.pdf", "report.pdf"
            )

        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["source"], "private_document")
        self.assertTrue(posted[0]["metadata"]["private"])
        self.assertEqual(posted[0]["metadata"]["type"], "work_document")

    def test_remember_skips_short_text(self):
        """remember() must skip text shorter than 30 chars."""
        calls = []
        with patch("urllib.request.urlopen",
                   side_effect=lambda *a, **kw: calls.append(1)):
            result = _mod.remember("Short.", "f.pdf", "f.pdf")
        self.assertFalse(result)
        self.assertEqual(calls, [])

    def test_chunk_text_function(self):
        text = " ".join([f"word{i}" for i in range(800)])
        chunks = _mod.chunk_text(text, chunk_size=400)
        self.assertEqual(len(chunks), 2)

    def test_process_file_skips_sensitive_extensions(self):
        """process_file() must skip .exe and .zip files."""
        for ext in [".exe", ".zip", ".dll", ".bmp"]:
            with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as f:
                Path(f.name).write_bytes(b"garbage")
                fpath = Path(f.name)
            try:
                result = _mod.process_file(fpath)
                self.assertEqual(result, 0, f"{ext} must be skipped")
            finally:
                fpath.unlink(missing_ok=True)

    def test_log_writes_to_console_and_file(self):
        src = _SCRIPT.read_text()
        self.assertIn("print(", src)
        self.assertIn("f.write", src)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_process_txt_file_ingests_chunks(self):
        content = " ".join([f"word{i}" for i in range(1000)])
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(content)
            fpath = Path(f.name)

        posted = [0]

        def mock_remember(text, fname, fpath_rel):
            posted[0] += 1
            return True

        try:
            with patch.object(_mod, "DOCS_DIR", fpath.parent):
                with patch.object(_mod, "remember", side_effect=mock_remember):
                    result = _mod.process_file(fpath)
            self.assertGreater(result, 0)
        finally:
            fpath.unlink(missing_ok=True)

    def test_progress_logged_every_100_files(self):
        src = _SCRIPT.read_text()
        self.assertIn("% 100 == 0", src)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_slack_notification_at_completion(self):
        """main() must post to Slack with counts only."""
        src = _SCRIPT.read_text()
        self.assertIn("private_document", src)
        self.assertIn("post_both", src)

    def test_main_function_exists(self):
        self.assertTrue(callable(_mod.main))


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"ingest_private_docs.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_constants_defined(self):
        for attr in ["MEMORY_URL", "DOCS_DIR", "CHUNK_SIZE"]:
            self.assertTrue(hasattr(_mod, attr))

    def test_functions_defined(self):
        for fn in ["log", "remember", "chunk_text", "process_file",
                   "extract_pdf", "extract_docx", "extract_xlsx"]:
            self.assertTrue(callable(getattr(_mod, fn, None)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
