"""
test_github_monitor.py — All 7 test categories for github_monitor.py
Written by Jordan Koch.
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "github_monitor.py"
_spec = importlib.util.spec_from_file_location("github_monitor", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_tokens(self):
        """Source must not contain hardcoded GitHub tokens."""
        src = _SCRIPT.read_text()
        self.assertNotIn("ghp_", src)
        self.assertNotIn("github_pat_", src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_gh_cli_used_not_api_direct(self):
        """Script must use gh CLI, not raw API with token in URL."""
        src = _SCRIPT.read_text()
        self.assertIn("gh", src)
        self.assertNotIn("https://api.github.com", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_days_parameter_bounded(self):
        """days parameter in get_commits should have a reasonable default."""
        src = _SCRIPT.read_text()
        self.assertIn("days=1", src,
                      "Default days should be bounded (1)")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_subprocess_exception_caught(self):
        """get_commits() must catch subprocess exceptions."""
        src = _SCRIPT.read_text()
        self.assertIn("except:", src,
                      "subprocess calls must have exception handling")

    def test_returns_none_on_failure(self):
        """get_commits() must return None on failure."""
        result = _mod.get_commits("nonexistent/repo", days=1)
        self.assertIsNone(result,
                          "Should return None when gh CLI fails")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_get_commits_function_exists(self):
        """get_commits() must be defined."""
        self.assertTrue(callable(_mod.get_commits))

    def test_get_commits_takes_repo_and_days(self):
        """get_commits() must accept repo and days parameters."""
        import inspect
        sig = inspect.signature(_mod.get_commits)
        params = list(sig.parameters.keys())
        self.assertIn("repo", params)
        self.assertIn("days", params)

    def test_get_commits_uses_json_flag(self):
        """get_commits() must request JSON output from gh."""
        src = _SCRIPT.read_text()
        self.assertIn("--json", src,
                      "gh CLI call must request JSON output")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_get_commits_with_mock_gh(self):
        """get_commits() must handle gh CLI output."""
        import json

        def fake_run(*args, **kwargs):
            r = MagicMock()
            r.returncode = 0
            r.stdout = json.dumps({"nameWithOwner": "kochj23/test-repo"})
            return r

        with patch("subprocess.run", side_effect=fake_run):
            result = _mod.get_commits("kochj23/test-repo", days=1)
        self.assertIsNotNone(result)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_script_prints_usage(self):
        """Script should print usage instructions."""
        src = _SCRIPT.read_text()
        self.assertIn("Usage", src)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"github_monitor.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_main_guard_present(self):
        """Script must have if __name__ == '__main__' guard."""
        src = _SCRIPT.read_text()
        self.assertIn('if __name__ == "__main__"', src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
