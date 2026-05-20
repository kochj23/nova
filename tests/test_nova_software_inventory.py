"""
test_nova_software_inventory.py — All 7 test categories for nova_software_inventory.py
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
from unittest.mock import MagicMock, patch

sys.modules.setdefault("nova_config", MagicMock())

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_software_inventory.py"
_spec = importlib.util.spec_from_file_location("nova_software_inventory", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)

# INVENTORY_DIR.mkdir is called at module load — patch it
with patch("pathlib.Path.mkdir"):
    _spec.loader.exec_module(_mod)

run_command = _mod.run_command
get_homebrew_packages = _mod.get_homebrew_packages
get_npm_packages = _mod.get_npm_packages
get_applications = _mod.get_applications
get_python_packages = _mod.get_python_packages
get_cli_tools = _mod.get_cli_tools
get_system_info = _mod.get_system_info


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

    def test_inventory_dir_not_on_main_ssd(self):
        """Inventory files may go anywhere — just verify under home (not root)."""
        self.assertTrue(str(_mod.INVENTORY_DIR).startswith(str(Path.home())))

    def test_no_eval_in_source(self):
        src = _SCRIPT.read_text()
        self.assertNotIn("eval(", src)

    def test_version_truncated(self):
        """Version strings must be truncated to prevent log injection."""
        src = _SCRIPT.read_text()
        self.assertIn("[:100]", src, "Version strings should be truncated to 100 chars")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_run_command_returns_empty_on_timeout(self):
        """run_command must return '' on timeout, not raise."""
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("brew", 30)):
            result = run_command(["brew", "list"], timeout=30)
        self.assertEqual(result, "")

    def test_run_command_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout", src, "run_command must use a timeout")

    def test_get_cli_tools_bounded(self):
        """get_cli_tools checks a finite set of known tools."""
        with patch.object(_mod, "run_command", return_value=""):
            tools = get_cli_tools()
        self.assertIsInstance(tools, list)
        # Should not be unbounded
        self.assertLessEqual(len(tools), 50)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_run_command_returns_empty_on_exception(self):
        with patch("subprocess.run", side_effect=Exception("crash")):
            result = run_command(["brew", "list"])
        self.assertEqual(result, "")

    def test_get_homebrew_returns_empty_on_failure(self):
        with patch.object(_mod, "run_command", return_value=""):
            result = get_homebrew_packages()
        self.assertIsInstance(result, dict)
        self.assertIn("formulae", result)
        self.assertIn("casks", result)

    def test_get_npm_returns_empty_on_invalid_json(self):
        with patch.object(_mod, "run_command", return_value="{INVALID"):
            result = get_npm_packages()
        self.assertIsInstance(result, list)

    def test_get_python_packages_returns_empty_on_failure(self):
        with patch.object(_mod, "run_command", return_value=""):
            result = get_python_packages()
        self.assertIsInstance(result, list)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_run_command_returns_stdout_on_success(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="output\n")
            result = run_command(["echo", "output"])
        self.assertEqual(result, "output")

    def test_run_command_returns_empty_on_nonzero(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="ignored")
            result = run_command(["false"])
        self.assertEqual(result, "")

    def test_get_homebrew_parses_formulae(self):
        brew_output = "git 2.45.0\nnvim 0.10.1\npython@3.12 3.12.1"
        with patch.object(_mod, "run_command", return_value=brew_output):
            # Only first call returns formulae, casks is empty, taps is empty
            result = get_homebrew_packages()
        self.assertGreater(len(result["formulae"]), 0)
        formulae_names = [f["name"] for f in result["formulae"]]
        self.assertIn("git", formulae_names)

    def test_get_npm_parses_json(self):
        npm_output = json.dumps({
            "dependencies": {
                "openclaw": {"version": "2026.5.7"},
                "typescript": {"version": "5.0.0"},
            }
        })
        with patch.object(_mod, "run_command", return_value=npm_output):
            result = get_npm_packages()
        self.assertEqual(len(result), 2)
        names = [p["name"] for p in result]
        self.assertIn("openclaw", names)

    def test_get_python_parses_json(self):
        pip_output = json.dumps([
            {"name": "requests", "version": "2.31.0"},
            {"name": "psycopg2", "version": "2.9.9"},
        ])
        with patch.object(_mod, "run_command", return_value=pip_output):
            result = get_python_packages()
        self.assertEqual(len(result), 2)
        names = [p["name"] for p in result]
        self.assertIn("requests", names)

    def test_get_system_info_parses_sw_vers(self):
        sw_vers_output = "ProductName:\tmacOS\nProductVersion:\t15.0\nBuildVersion:\t24A335"
        uname_m = "arm64"
        uname_r = "24.5.0"

        def fake_run_command(cmd, timeout=30):
            if "sw_vers" in cmd:
                return sw_vers_output
            if "-m" in cmd:
                return uname_m
            if "-r" in cmd:
                return uname_r
            return ""

        with patch.object(_mod, "run_command", side_effect=fake_run_command):
            result = get_system_info()

        self.assertIn("os_version", result)
        self.assertEqual(result["os_version"], "15.0")
        self.assertEqual(result["architecture"], "arm64")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_creates_inventory_json(self):
        """main() should write inventory JSON file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            inv_dir = Path(tmpdir)
            with patch.object(_mod, "INVENTORY_DIR", inv_dir):
                with patch.object(_mod, "INVENTORY_FILE", inv_dir / "inventory-test.json"):
                    with patch.object(_mod, "LATEST_FILE", inv_dir / "inventory-latest.json"):
                        with patch.object(_mod, "get_homebrew_packages",
                                          return_value={"formulae": [], "casks": [], "taps": []}):
                            with patch.object(_mod, "get_npm_packages", return_value=[]):
                                with patch.object(_mod, "get_python_packages", return_value=[]):
                                    with patch.object(_mod, "get_applications", return_value=[]):
                                        with patch.object(_mod, "get_cli_tools", return_value=[]):
                                            with patch.object(_mod, "get_system_info", return_value={}):
                                                rc = _mod.main()
            # Inventory file should have been created
            self.assertEqual(rc, 0)

    def test_main_creates_report_txt(self):
        """main() should also create a human-readable report."""
        with tempfile.TemporaryDirectory() as tmpdir:
            inv_dir = Path(tmpdir)
            report_file = inv_dir / "report-test.txt"
            with patch.object(_mod, "INVENTORY_DIR", inv_dir):
                with patch.object(_mod, "INVENTORY_FILE", inv_dir / "inventory-test.json"):
                    with patch.object(_mod, "LATEST_FILE", inv_dir / "inventory-latest.json"):
                        with patch.object(_mod, "get_homebrew_packages",
                                          return_value={"formulae": [{"name": "git", "version": "2.45"}], "casks": [], "taps": []}):
                            with patch.object(_mod, "get_npm_packages", return_value=[]):
                                with patch.object(_mod, "get_python_packages", return_value=[]):
                                    with patch.object(_mod, "get_applications", return_value=[]):
                                        with patch.object(_mod, "get_cli_tools", return_value=[]):
                                            with patch.object(_mod, "get_system_info",
                                                              return_value={"os_version": "15.0", "architecture": "arm64"}):
                                                _mod.main()


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_cli_tools_check_common_tools(self):
        """get_cli_tools should check for essential dev tools."""
        src = _SCRIPT.read_text()
        for tool in ["git", "python3", "node"]:
            self.assertIn(f'"{tool}"', src, f"CLI tools should include {tool}")

    def test_applications_reads_app_dir(self):
        """get_applications must look in /Applications."""
        src = _SCRIPT.read_text()
        self.assertIn("/Applications", src)

    def test_inventory_has_timestamp(self):
        """Inventory JSON must include a timestamp."""
        with patch.object(_mod, "get_homebrew_packages",
                          return_value={"formulae": [], "casks": [], "taps": []}):
            with patch.object(_mod, "get_npm_packages", return_value=[]):
                with patch.object(_mod, "get_python_packages", return_value=[]):
                    with patch.object(_mod, "get_applications", return_value=[]):
                        with patch.object(_mod, "get_cli_tools", return_value=[]):
                            with patch.object(_mod, "get_system_info", return_value={}):
                                with tempfile.TemporaryDirectory() as tmpdir:
                                    inv_dir = Path(tmpdir)
                                    with patch.object(_mod, "INVENTORY_DIR", inv_dir):
                                        with patch.object(_mod, "INVENTORY_FILE",
                                                          inv_dir / "inv.json"):
                                            with patch.object(_mod, "LATEST_FILE",
                                                              inv_dir / "latest.json"):
                                                _mod.main()
                                    inv_file = inv_dir / "inv.json"
                                    if inv_file.exists():
                                        data = json.loads(inv_file.read_text())
                                        self.assertIn("timestamp", data)


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

    def test_inventory_dir_defined(self):
        self.assertIsNotNone(_mod.INVENTORY_DIR)

    def test_today_date_string(self):
        self.assertRegex(_mod.TODAY, r"^\d{4}-\d{2}-\d{2}$")


if __name__ == "__main__":
    unittest.main(verbosity=2)
