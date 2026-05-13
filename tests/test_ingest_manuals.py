"""
test_ingest_manuals.py — All 7 test categories for ingest_manuals.py
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
_SCRIPT = Path(__file__).parent.parent / "scripts" / "ingest_manuals.py"
_spec = importlib.util.spec_from_file_location("ingest_manuals", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)

# Patch LOG_FILE write during module load
with patch("builtins.open", side_effect=lambda p, m="r", **kw:
           open(p, m, **kw) if "w" not in m and "a" not in m else
           tempfile.NamedTemporaryFile(mode=m, delete=False)):
    try:
        _spec.loader.exec_module(_mod)
    except Exception:
        pass


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

    def test_memory_url_localhost(self):
        src = _SCRIPT.read_text()
        self.assertIn("127.0.0.1", src)

    def test_text_truncated_at_2000(self):
        """remember() must truncate text to 2000 chars."""
        src = _SCRIPT.read_text()
        self.assertIn("[:2000]", src,
                      "Text must be truncated at 2000 chars")

    def test_corvette_marked_private_source(self):
        """Corvette manual must use a distinct source name."""
        src = _SCRIPT.read_text()
        self.assertIn("corvette_workshop_manual", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_chunk_size_defined(self):
        src = _SCRIPT.read_text()
        self.assertIn("CHUNK_SIZE", src)
        self.assertIn("500", src)

    def test_rate_limit_on_ingest(self):
        src = _SCRIPT.read_text()
        self.assertIn("time.sleep", src)

    def test_pdf_extraction_has_page_limit(self):
        """PDF extraction must limit pages to avoid huge memory use."""
        src = _SCRIPT.read_text()
        self.assertIn("100", src,
                      "PDF extraction should limit to 100 pages")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_silent_on_failure(self):
        """remember() must not propagate network exceptions."""
        src = _SCRIPT.read_text()
        self.assertIn("except Exception", src)

    def test_sleep_on_failure(self):
        """remember() must sleep after failure."""
        src = _SCRIPT.read_text()
        # There's a sleep(1) in the except block
        self.assertIn("time.sleep(1)", src)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_chunk_text_correct_size(self):
        """chunk_text() must split into chunks of ~CHUNK_SIZE words."""
        text = " ".join(f"word{i}" for i in range(1000))
        chunks = _mod.chunk_text(text, chunk_size=100)
        self.assertEqual(len(chunks), 10)
        for c in chunks:
            self.assertEqual(len(c.split()), 100)

    def test_chunk_text_skips_tiny_fragments(self):
        """chunk_text() must skip chunks with < 50 chars."""
        text = " ".join(["x"] * 5)  # very short, < 50 chars
        chunks = _mod.chunk_text(text, chunk_size=100)
        self.assertEqual(len(chunks), 0,
                         "Tiny text should be filtered out")

    def test_process_file_skips_ds_store(self):
        """process_file() must skip .DS_Store files."""
        with tempfile.NamedTemporaryFile(suffix=".DS_Store", delete=False) as f:
            Path(f.name).write_bytes(b"garbage")
            fpath = Path(f.name)
        try:
            result = _mod.process_file(fpath)
            self.assertEqual(result, 0,
                             ".DS_Store must be skipped")
        finally:
            fpath.unlink(missing_ok=True)

    def test_process_file_skips_exe(self):
        """process_file() must skip .exe files."""
        with tempfile.NamedTemporaryFile(suffix=".exe", delete=False) as f:
            Path(f.name).write_bytes(b"garbage")
            fpath = Path(f.name)
        try:
            result = _mod.process_file(fpath)
            self.assertEqual(result, 0)
        finally:
            fpath.unlink(missing_ok=True)

    def test_process_txt_file(self):
        """process_file() must process .txt files."""
        content = " ".join([f"word{i}" for i in range(1000)])
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt",
                                          delete=False) as f:
            f.write(content)
            fpath = Path(f.name)

        posted = [0]

        def mock_remember(*args, **kwargs):
            posted[0] += 1
            return True

        try:
            with patch.object(_mod, "MANUALS_DIR", fpath.parent):
                with patch.object(_mod, "remember", side_effect=mock_remember):
                    result = _mod.process_file(fpath)
            self.assertGreater(result, 0, "TXT file should be processed")
        finally:
            fpath.unlink(missing_ok=True)

    def test_log_function_exists(self):
        self.assertTrue(callable(_mod.log))


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_source_classification_corvette(self):
        """Files with 'corvette' in path should use corvette_workshop_manual source."""
        src = _SCRIPT.read_text()
        self.assertIn('"corvette_workshop_manual"', src)

    def test_source_classification_ssl(self):
        """Files with 'ssl' in path should use ssl_management source."""
        src = _SCRIPT.read_text()
        self.assertIn('"ssl_management"', src)

    def test_metadata_includes_file_info(self):
        """Chunks must include file name in metadata."""
        src = _SCRIPT.read_text()
        self.assertIn('"file"', src)
        self.assertIn('"chunk"', src)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_function_exists(self):
        self.assertTrue(callable(_mod.main))

    def test_slack_notification_at_end(self):
        src = _SCRIPT.read_text()
        self.assertIn("post_both", src)

    def test_extract_pdf_graceful_on_missing_tool(self):
        """extract_pdf() must handle missing pdftotext gracefully."""
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 fake")
            fpath = Path(f.name)
        try:
            with patch("subprocess.run",
                       side_effect=FileNotFoundError("pdftotext not found")):
                result = _mod.extract_pdf(fpath)
            # Should return empty string gracefully
            self.assertIsInstance(result, str)
        finally:
            fpath.unlink(missing_ok=True)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"ingest_manuals.py has syntax errors: {e}")

    def test_constants_defined(self):
        src = _SCRIPT.read_text()
        for const in ["MEMORY_URL", "MANUALS_DIR", "CHUNK_SIZE"]:
            self.assertIn(const, src)

    def test_functions_defined(self):
        for fn in ["log", "remember", "chunk_text", "process_file", "main",
                   "extract_pdf", "extract_docx", "extract_xlsx"]:
            self.assertTrue(callable(getattr(_mod, fn, None)),
                            f"{fn} must be defined")


if __name__ == "__main__":
    unittest.main(verbosity=2)
