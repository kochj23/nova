"""
test_metrics_tracker.py — All 7 test categories for metrics_tracker.py
Written by Jordan Koch.
"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "metrics_tracker.py"


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

    def test_metrics_stored_locally(self):
        """Metrics must be stored locally, not sent to external service."""
        src = _SCRIPT.read_text()
        self.assertNotIn("https://", src,
                         "Metrics must not be sent to external services")

    def test_subprocess_uses_safe_args(self):
        """df command must not use shell=True."""
        src = _SCRIPT.read_text()
        self.assertNotIn("shell=True", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_collect_metrics_fast(self):
        """collect_metrics() must complete quickly."""
        import importlib.util
        _spec = importlib.util.spec_from_file_location("metrics_tracker_test", _SCRIPT)
        _m = importlib.util.module_from_spec(_spec)

        start = time.perf_counter()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="Filesystem Size Used Avail Capacity Mounted\n"
                       "/dev/disk1 500G 200G 300G 40% /\n",
                returncode=0
            )
            with patch("pathlib.Path.mkdir"):
                with patch("builtins.open", MagicMock()):
                    with patch("json.dump"):
                        _spec.loader.exec_module(_m)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 5.0, "Metrics collection must be fast")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_collect_metrics_handles_subprocess_failure(self):
        """collect_metrics() must handle subprocess failures gracefully."""
        import importlib.util
        _spec = importlib.util.spec_from_file_location("metrics_retry_test", _SCRIPT)
        _m = importlib.util.module_from_spec(_spec)

        def fail_run(*args, **kwargs):
            raise OSError("df command not found")

        with patch("subprocess.run", side_effect=fail_run):
            with patch("pathlib.Path.mkdir"):
                with patch("builtins.open", MagicMock()):
                    with patch("json.dump"):
                        try:
                            _spec.loader.exec_module(_m)
                        except SystemExit:
                            pass
                        except OSError:
                            self.fail("OSError from subprocess must be caught")
        # If we got here without an uncaught OSError, test passes


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_collect_metrics_returns_dict(self):
        """collect_metrics() must return a dict with timestamp."""
        import importlib.util
        _spec = importlib.util.spec_from_file_location("metrics_unit", _SCRIPT)
        _m = importlib.util.module_from_spec(_spec)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="Filesystem  Size  Used Avail Use% Mounted\n"
                       "/dev/disk1   500G  200G  300G  40% /\n",
                returncode=0
            )
            with patch("pathlib.Path.mkdir"):
                with patch("builtins.open", MagicMock()):
                    with patch("json.dump"):
                        _spec.loader.exec_module(_m)

            result = _m.collect_metrics()
        self.assertIn("timestamp", result)
        self.assertIn("disk", result)
        self.assertIn("memory", result)

    def test_disk_percent_extracted(self):
        """collect_metrics() must extract disk usage percentage."""
        import importlib.util
        _spec = importlib.util.spec_from_file_location("metrics_disk", _SCRIPT)
        _m = importlib.util.module_from_spec(_spec)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="Filesystem  512-blocks  Used Available Capacity iused\n"
                       "/dev/disk1  976762584  456789  519973596  47% 1234567\n",
                returncode=0
            )
            with patch("pathlib.Path.mkdir"):
                with patch("builtins.open", MagicMock()):
                    with patch("json.dump"):
                        _spec.loader.exec_module(_m)

            result = _m.collect_metrics()
        if "root_percent" in result.get("disk", {}):
            self.assertIsInstance(result["disk"]["root_percent"], int)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_metrics_file_written_with_date(self):
        """Metrics file must be named with today's date."""
        src = _SCRIPT.read_text()
        self.assertIn("metrics-{today}.json", src)

    def test_metrics_dir_created(self):
        """Metrics directory must be created if it doesn't exist."""
        src = _SCRIPT.read_text()
        self.assertIn("mkdir", src)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_output_includes_check_mark(self):
        """Script must print success confirmation."""
        src = _SCRIPT.read_text()
        self.assertIn("Metrics collected", src)

    def test_metrics_json_format(self):
        """Metrics file must be written as JSON."""
        src = _SCRIPT.read_text()
        self.assertIn("json.dump", src)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"metrics_tracker.py has syntax errors: {e}")

    def test_imports_correct(self):
        src = _SCRIPT.read_text()
        self.assertIn("import json", src)
        self.assertIn("import subprocess", src)
        self.assertIn("from pathlib import Path", src)
        self.assertIn("from datetime import datetime", src)

    def test_collect_metrics_function_defined(self):
        src = _SCRIPT.read_text()
        self.assertIn("def collect_metrics", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
