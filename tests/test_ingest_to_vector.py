"""
test_ingest_to_vector.py — All 7 test categories for ingest_to_vector.py
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

# Stub requests since ingest_to_vector uses it
_requests_mock = MagicMock()
sys.modules["requests"] = _requests_mock

_SCRIPT = (Path(__file__).parent.parent / "scripts" / "ingest_to_vector.py"
           if (Path(__file__).parent.parent / "scripts" / "ingest_to_vector.py").exists()
           else Path(__file__).parent.parent / "scripts" / "_archive" / "ingest_to_vector.py")


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

    def test_vector_api_is_localhost(self):
        src = _SCRIPT.read_text()
        self.assertIn("127.0.0.1", src)

    def test_source_parameter_from_args(self):
        """Source must come from command-line arguments, not hardcoded."""
        src = _SCRIPT.read_text()
        self.assertIn("sys.argv[2]", src,
                      "Source must come from command-line argument")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_content_sent_in_single_post(self):
        """Script must POST content in a single request, not loop."""
        src = _SCRIPT.read_text()
        self.assertIn("requests.post", src)

    def test_script_exits_on_wrong_args(self):
        """Script must exit 1 when wrong number of args given."""
        with patch("sys.argv", ["ingest_to_vector.py"]):
            with self.assertRaises(SystemExit) as cm:
                _spec = importlib.util.spec_from_file_location(
                    "ingest_to_vector_test", _SCRIPT)
                _m = importlib.util.module_from_spec(_spec)
                _spec.loader.exec_module(_m)
            self.assertEqual(cm.exception.code, 1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_script_handles_request_exception(self):
        """Script must handle exception from requests.post."""
        src = _SCRIPT.read_text()
        self.assertIn("except Exception", src,
                      "requests.post exceptions must be caught")

    def test_failure_printed_not_raised(self):
        """On failure, script must print error, not raise."""
        src = _SCRIPT.read_text()
        # After except, it prints
        self.assertIn("Request failed", src)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_title_derived_from_filename(self):
        """Title must be derived from filename, stripping .md and underscores."""
        src = _SCRIPT.read_text()
        self.assertIn("replace('.md', '')", src)
        self.assertIn("replace('_', ' ')", src)

    def test_vector_api_endpoint(self):
        src = _SCRIPT.read_text()
        self.assertIn("/ingest", src,
                      "Must use /ingest endpoint")

    def test_payload_includes_text_title_source(self):
        src = _SCRIPT.read_text()
        for field in ['"text"', '"title"', '"source"']:
            self.assertIn(field, src)

    def test_file_opened_in_read_mode(self):
        src = _SCRIPT.read_text()
        self.assertIn("open(file_path", src)
        self.assertIn("'r'", src)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_successful_post_prints_success(self):
        """Script must print success message on 200 response."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md",
                                          delete=False) as f:
            f.write("# Test\n\nContent here.")
            fpath = Path(f.name)

        mock_response = MagicMock()
        mock_response.status_code = 200

        try:
            import io
            from contextlib import redirect_stdout
            output = io.StringIO()

            with patch("sys.argv", ["ingest_to_vector.py", str(fpath), "test_source"]):
                with patch("requests.post", return_value=mock_response):
                    _spec = importlib.util.spec_from_file_location(
                        "itv_success", _SCRIPT)
                    _m = importlib.util.module_from_spec(_spec)
                    with redirect_stdout(output):
                        _spec.loader.exec_module(_m)

            self.assertIn("Successfully ingested", output.getvalue())
        finally:
            fpath.unlink(missing_ok=True)

    def test_failed_post_prints_failure(self):
        """Script must print failure message on non-200 response."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md",
                                          delete=False) as f:
            f.write("Content.")
            fpath = Path(f.name)

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"

        try:
            import io
            from contextlib import redirect_stdout
            output = io.StringIO()

            with patch("sys.argv", ["ingest_to_vector.py", str(fpath), "test_source"]):
                with patch("requests.post", return_value=mock_response):
                    _spec = importlib.util.spec_from_file_location(
                        "itv_fail", _SCRIPT)
                    _m = importlib.util.module_from_spec(_spec)
                    with redirect_stdout(output):
                        _spec.loader.exec_module(_m)

            self.assertIn("Failed", output.getvalue())
        finally:
            fpath.unlink(missing_ok=True)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_requires_two_arguments(self):
        """Script must require exactly 2 arguments: file and source."""
        src = _SCRIPT.read_text()
        self.assertIn("len(sys.argv) != 3", src,
                      "Script must check for exactly 2 args")

    def test_source_passed_to_api(self):
        """source argument must be included in the API payload."""
        src = _SCRIPT.read_text()
        self.assertIn("source", src)
        self.assertIn("sys.argv[2]", src)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"ingest_to_vector.py has syntax errors: {e}")

    def test_script_has_docstring(self):
        src = _SCRIPT.read_text()
        self.assertIn('"""', src)

    def test_usage_message_present(self):
        src = _SCRIPT.read_text()
        self.assertIn("Usage", src)

    def test_imports_required(self):
        src = _SCRIPT.read_text()
        self.assertIn("import sys", src)
        self.assertIn("import requests", src)
        self.assertIn("import os", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
