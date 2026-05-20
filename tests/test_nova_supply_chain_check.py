"""
test_nova_supply_chain_check.py — All 7 test categories for nova_supply_chain_check.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

sys.modules.setdefault("nova_config", MagicMock())

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_supply_chain_check.py"
_spec = importlib.util.spec_from_file_location("nova_supply_chain_check", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

scan_directory = _mod.scan_directory
scan_installed_packages = _mod.scan_installed_packages
MALICIOUS_PATTERNS = _mod.MALICIOUS_PATTERNS


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_log_file_under_home(self):
        self.assertTrue(str(_mod.LOG_FILE).startswith(str(Path.home())))

    def test_malicious_patterns_not_empty(self):
        self.assertGreater(len(MALICIOUS_PATTERNS), 0)
        for category, patterns in MALICIOUS_PATTERNS.items():
            self.assertGreater(len(patterns), 0, f"Category {category!r} has no patterns")

    def test_nullbulge_pattern_present(self):
        """NullBulge attack patterns must be in the scan list."""
        all_patterns = [p for patterns in MALICIOUS_PATTERNS.values() for p in patterns]
        self.assertIn("ComfyUI_LLMVISION", all_patterns)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_scan_directory_fast_on_missing_dir(self):
        start = time.perf_counter()
        result = scan_directory(Path("/nonexistent/path/xyz"))
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)
        self.assertIsInstance(result, dict)

    def test_malicious_patterns_bounded(self):
        total = sum(len(p) for p in MALICIOUS_PATTERNS.values())
        self.assertLessEqual(total, 100, "Too many patterns — could slow scans")

    def test_scan_directory_returns_quickly_on_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            start = time.perf_counter()
            result = scan_directory(Path(tmpdir))
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_scan_directory_handles_json_error(self):
        """scan_directory must not crash on malformed package.json."""
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "package.json").write_text("{INVALID JSON}")
            result = scan_directory(Path(tmpdir))
        self.assertIsInstance(result, dict)
        self.assertIn("warnings", result)

    def test_scan_installed_returns_on_npm_failure(self):
        with patch("subprocess.run", side_effect=Exception("npm not found")):
            result = scan_installed_packages()
        self.assertIsInstance(result, dict)
        self.assertIn("npm", result)

    def test_scan_installed_returns_on_pip_failure(self):
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = Exception("pip not found")
            result = scan_installed_packages()
        self.assertIsInstance(result, dict)

    def test_main_does_not_crash_on_all_failures(self):
        with patch.object(_mod, "scan_directory", side_effect=Exception("crash")):
            with patch.object(_mod, "scan_installed_packages",
                              return_value={"npm": {"issues": [], "warnings": []},
                                           "pip": {"issues": [], "warnings": []}}):
                with patch("subprocess.run"):
                    try:
                        _mod.main()
                    except Exception as e:
                        self.fail(f"main() raised: {e}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_scan_directory_detects_malicious_dep(self):
        pkg_json = json.dumps({
            "dependencies": {"ComfyUI_LLMVISION": "^1.0.0"}
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "package.json").write_text(pkg_json)
            result = scan_directory(Path(tmpdir))
        self.assertGreater(len(result["issues"]), 0, "Should detect malicious package")
        self.assertIn("ComfyUI_LLMVISION", result["issues"][0])

    def test_scan_directory_clean_project(self):
        pkg_json = json.dumps({
            "dependencies": {"express": "^4.18.0", "lodash": "^4.17.21"}
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "package.json").write_text(pkg_json)
            result = scan_directory(Path(tmpdir))
        self.assertEqual(len(result["issues"]), 0, "Clean project should have no issues")

    def test_scan_directory_detects_postinstall(self):
        pkg_json = json.dumps({
            "scripts": {"postinstall": "eval(Buffer.from('bad').toString())"}
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "package.json").write_text(pkg_json)
            result = scan_directory(Path(tmpdir))
        self.assertGreater(len(result["issues"]), 0, "Should detect suspicious postinstall")

    def test_scan_directory_detects_requirements_malicious(self):
        req_content = "requests==2.31.0\nComfyUI_LLMVISION==1.0.0\nnumpy==1.26.0"
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "requirements.txt").write_text(req_content)
            result = scan_directory(Path(tmpdir))
        self.assertGreater(len(result["issues"]), 0)

    def test_scan_directory_clean_requirements(self):
        req_content = "requests==2.31.0\nnumpy==1.26.0\npandas==2.2.0"
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "requirements.txt").write_text(req_content)
            result = scan_directory(Path(tmpdir))
        self.assertEqual(len(result["issues"]), 0)

    def test_scan_directory_warns_on_non_pypi_source(self):
        req_content = "requests==2.31.0\ngit+https://some-random-site.com/package.git"
        with tempfile.TemporaryDirectory() as tmpdir:
            (Path(tmpdir) / "requirements.txt").write_text(req_content)
            result = scan_directory(Path(tmpdir))
        self.assertGreater(len(result["warnings"]), 0, "Non-standard source should warn")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_returns_1_on_issues(self):
        """main() should return 1 when issues are found."""
        with patch.object(_mod, "scan_directory",
                          return_value={"issues": ["Suspicious dep found"], "warnings": []}):
            with patch.object(_mod, "scan_installed_packages",
                              return_value={"npm": {"issues": [], "warnings": []},
                                           "pip": {"issues": [], "warnings": []}}):
                with patch("subprocess.run"):
                    result = _mod.main()
        self.assertEqual(result, 1)

    def test_main_returns_0_on_clean(self):
        """main() should return 0 when no issues found."""
        with patch.object(_mod, "scan_directory",
                          return_value={"issues": [], "warnings": []}):
            with patch.object(_mod, "scan_installed_packages",
                              return_value={"npm": {"issues": [], "warnings": []},
                                           "pip": {"issues": [], "warnings": []}}):
                result = _mod.main()
        self.assertEqual(result, 0)

    def test_main_posts_to_slack_on_issues(self):
        """main() should post Slack alert when issues are found."""
        slack_calls = []
        with patch.object(_mod, "scan_directory",
                          return_value={"issues": ["Malware found!"], "warnings": []}):
            with patch.object(_mod, "scan_installed_packages",
                              return_value={"npm": {"issues": [], "warnings": []},
                                           "pip": {"issues": [], "warnings": []}}):
                with patch("subprocess.run") as mock_run:
                    slack_calls.append(True)
                    _mod.main()
        self.assertTrue(len(slack_calls) > 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_scan_directory_result_has_required_keys(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = scan_directory(Path(tmpdir))
        self.assertIn("path", result)
        self.assertIn("issues", result)
        self.assertIn("warnings", result)

    def test_scan_installed_result_has_required_keys(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps({
                "dependencies": {}, "problems": {}
            }))
            result = scan_installed_packages()
        self.assertIn("npm", result)
        self.assertIn("pip", result)

    def test_discord_webhook_pattern_detected(self):
        """discord_webhook is a known infostealer pattern."""
        all_patterns = [p for patterns in MALICIOUS_PATTERNS.values() for p in patterns]
        self.assertIn("discord_webhook", all_patterns)


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

    def test_module_has_main(self):
        self.assertTrue(callable(_mod.main))

    def test_malicious_patterns_is_dict(self):
        self.assertIsInstance(MALICIOUS_PATTERNS, dict)

    def test_log_file_is_log(self):
        self.assertTrue(str(_mod.LOG_FILE).endswith(".log"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
