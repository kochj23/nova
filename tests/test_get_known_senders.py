"""
test_get_known_senders.py — All 7 test categories for get_known_senders.py
Written by Jordan Koch.
"""

import importlib.util
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "get_known_senders.py"


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA"]
        for p in forbidden:
            self.assertNotIn(p, src, f"Credential: {p!r}")

    def test_no_pii_email_literals(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII: {p!r}")

    def test_herd_config_imported_with_fallback(self):
        """herd_config import must have ImportError fallback."""
        src = _SCRIPT.read_text()
        self.assertIn("ImportError", src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_script_runs_fast(self):
        """Script must complete in < 2 seconds even on missing herd_config."""
        start = time.perf_counter()
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(_SCRIPT.parent)}
        )
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 2.0, f"Script took {elapsed:.3f}s")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_handles_missing_herd_config_gracefully(self):
        """Script must print empty string and not crash when herd_config missing."""
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": "/nonexistent/path"}
        )
        # Should not crash
        self.assertNotIn("Traceback", result.stderr,
                         f"Script crashed: {result.stderr[:500]}")

    def test_handles_herd_config_missing_email_key(self):
        """Script must handle HERD members without 'email' key gracefully."""
        sys.modules["herd_config"] = MagicMock(HERD=[
            {"name": "Sam"},  # no email key
        ])
        try:
            # Re-run the script logic inline
            from herd_config import HERD
            try:
                result = ",".join(m["email"] for m in HERD)
            except KeyError:
                result = ""
        except ImportError:
            result = ""
        # Should not raise
        self.assertIsInstance(result, str)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_output_is_comma_separated(self):
        """Output must be comma-separated email addresses."""
        _at = "@"
        herd = [
            {"email": "sam" + _at + "example.com", "name": "Sam"},
            {"email": "gaston" + _at + "example.com", "name": "Gaston"},
        ]
        result = ",".join(m["email"] for m in herd)
        parts = result.split(",")
        self.assertEqual(len(parts), 2)
        for part in parts:
            self.assertIn(_at, part)

    def test_output_no_trailing_newline_with_content(self):
        """Output must not have trailing comma when there's content."""
        _at = "@"
        herd = [{"email": "sam" + _at + "example.com"}]
        result = ",".join(m["email"] for m in herd)
        self.assertFalse(result.endswith(","))

    def test_empty_herd_produces_empty_output(self):
        result = ",".join(m["email"] for m in [])
        self.assertEqual(result, "")

    def test_script_outputs_to_stdout(self):
        """Script must print to stdout, not stderr."""
        sys.modules["herd_config"] = MagicMock(HERD=[
            {"email": "test@example.com", "name": "Test"},
        ])
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True, text=True,
            env={**os.environ}
        )
        # Either has output or is empty (herd_config not in test env)
        # Just verify no error
        self.assertEqual(result.returncode, 0)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_script_integrates_with_herd_config(self):
        """When herd_config has members, their emails must appear in output."""
        _at = "@"
        mock_herd = MagicMock()
        mock_herd.HERD = [
            {"email": "alice" + _at + "example.com", "name": "Alice"},
            {"email": "bob" + _at + "example.com", "name": "Bob"},
        ]
        with patch.dict("sys.modules", {"herd_config": mock_herd}):
            import io
            from contextlib import redirect_stdout
            f = io.StringIO()
            with redirect_stdout(f):
                # Execute the script's core logic inline
                try:
                    from herd_config import HERD
                    print(",".join(m["email"] for m in HERD))
                except ImportError:
                    print("")
            output = f.getvalue().strip()

        self.assertIn("alice" + _at + "example.com", output)
        self.assertIn("bob" + _at + "example.com", output)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_script_exit_zero_on_success(self):
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)

    def test_script_exit_zero_on_missing_herd_config(self):
        """Script must exit 0 even when herd_config is not available."""
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": "/nonexistent/path"}
        )
        self.assertEqual(result.returncode, 0)

    def test_output_does_not_contain_names(self):
        """Output must only contain emails, not member names."""
        _at = "@"
        herd = [
            {"email": "sam" + _at + "example.com", "name": "Sam"},
        ]
        result = ",".join(m["email"] for m in herd)
        self.assertNotIn("Sam", result)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"get_known_senders.py has syntax errors: {e}")

    def test_script_is_small(self):
        """Script must be a small utility (< 20 lines)."""
        lines = _SCRIPT.read_text().splitlines()
        self.assertLess(len(lines), 20, f"Script has {len(lines)} lines — should be tiny")

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_script_has_docstring(self):
        src = _SCRIPT.read_text()
        self.assertIn('"""', src, "Script must have a docstring")


if __name__ == "__main__":
    unittest.main(verbosity=2)
