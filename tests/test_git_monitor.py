"""
test_git_monitor.py — All 7 test categories for git_monitor.py
Written by Jordan Koch.
"""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Stub nova_config
_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "git_monitor.py"


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        """Source must not contain API keys or passwords."""
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA", "password =", "token ="]
        for pattern in forbidden:
            self.assertNotIn(pattern, src,
                             f"Potential credential found: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        """Source must not hardcode the user's home path."""
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_repos_use_volumes_path(self):
        """Repo paths should reference /Volumes, not home directory."""
        src = _SCRIPT.read_text()
        self.assertIn("/Volumes/Data", src,
                      "Repos should be under /Volumes/Data")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_repo_list_bounded(self):
        """Repo list must have reasonable number of entries."""
        src = _SCRIPT.read_text()
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "repos":
                        if isinstance(node.value, ast.List):
                            self.assertLessEqual(
                                len(node.value.elts), 100,
                                "repos list should not be unbounded")

    def test_no_network_call_at_module_level(self):
        """Module should not make network calls at import time."""
        src = _SCRIPT.read_text()
        # Should not have subprocess.run at module top level for git fetch
        lines = src.splitlines()
        # Simple check: no git fetch in first 20 lines
        early = "\n".join(lines[:20])
        self.assertNotIn("git fetch", early,
                         "git fetch should not happen at module import time")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_path_check_before_subprocess(self):
        """Script must check if repo path exists before running git commands."""
        src = _SCRIPT.read_text()
        self.assertIn(".exists()", src,
                      "Script must check path existence before git commands")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_repos_list_defined(self):
        """repos list must be defined."""
        src = _SCRIPT.read_text()
        self.assertIn("repos", src)

    def test_repos_contains_focus_projects(self):
        """repos list must reference focus projects."""
        src = _SCRIPT.read_text()
        self.assertIn("MLXCode", src)
        self.assertIn("NMAPScanner", src)
        self.assertIn("RsyncGUI", src)

    def test_script_prints_initialization(self):
        """Script must print initialization confirmation."""
        src = _SCRIPT.read_text()
        self.assertIn("print", src)
        self.assertIn("Git monitor initialized", src)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_existing_repos_checked(self):
        """Script must verify repo paths before processing."""
        src = _SCRIPT.read_text()
        self.assertIn("Path(repo).exists()", src,
                      "Each repo path must be verified with .exists()")

    def test_repo_name_extracted(self):
        """Script must extract basename of repo for display."""
        src = _SCRIPT.read_text()
        self.assertIn(".name", src,
                      "Repo name should be extracted with .name")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_script_runs_without_error_when_repos_missing(self):
        """Script must not crash when repo directories don't exist."""
        import subprocess
        result = subprocess.run(
            [sys.executable, str(_SCRIPT)],
            capture_output=True, text=True, timeout=10
        )
        self.assertEqual(result.returncode, 0,
                         f"Script should exit 0: {result.stderr}")

    def test_output_includes_check_marks(self):
        """Output should include check marks for found repos."""
        src = _SCRIPT.read_text()
        self.assertIn("✓", src,
                      "Script should print check marks for accessible repos")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        """git_monitor.py must compile without errors."""
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"git_monitor.py has syntax errors: {e}")

    def test_script_is_python(self):
        """Script must have Python shebang."""
        src = _SCRIPT.read_text()
        self.assertTrue(src.startswith("#!/usr/bin/env python3"))

    def test_imports_present(self):
        """Required imports must be present."""
        src = _SCRIPT.read_text()
        self.assertIn("import subprocess", src)
        self.assertIn("from pathlib import Path", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
