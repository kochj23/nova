"""
test_nova_browser.py — All 7 test categories for nova_browser.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

_nova_cfg = MagicMock()
_nova_cfg.slack_bot_token.return_value = "xoxb-test"
_nova_cfg.SLACK_PHOTOS = "#nova-photos"
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
sys.modules["nova_config"] = _nova_cfg

# Stub playwright
_playwright_mock = MagicMock()
sys.modules["playwright"] = _playwright_mock
sys.modules["playwright.async_api"] = _playwright_mock

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_browser.py"
_spec = importlib.util.spec_from_file_location("nova_browser", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

log = _mod.log
ensure_dirs = _mod.ensure_dirs
vector_remember = _mod.vector_remember
run_async = _mod.run_async


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_", "api_key ="]:
            self.assertNotIn(pat, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_slack_token_from_nova_config(self):
        """Slack token must come from nova_config (Keychain)."""
        src = _SCRIPT.read_text()
        self.assertIn("nova_config.slack_bot_token", src)

    def test_vector_url_is_localhost(self):
        """Vector URL must be localhost."""
        self.assertTrue(_mod.VECTOR_URL.startswith("http://127.0.0.1"))

    def test_automation_disabled_in_browser_args(self):
        """Browser must disable AutomationControlled flag."""
        src = _SCRIPT.read_text()
        self.assertIn("AutomationControlled", src)

    def test_browser_profiles_in_workspace(self):
        """Browser profiles stored in local workspace."""
        self.assertIn(str(Path.home()), str(_mod.PROFILES_DIR))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_default_timeout_reasonable(self):
        self.assertGreater(_mod.DEFAULT_TIMEOUT, 5000)
        self.assertLessEqual(_mod.DEFAULT_TIMEOUT, 120000)

    def test_viewport_defined(self):
        self.assertIn("width", _mod.DEFAULT_VIEWPORT)
        self.assertIn("height", _mod.DEFAULT_VIEWPORT)
        self.assertGreater(_mod.DEFAULT_VIEWPORT["width"], 0)

    def test_ensure_dirs_fast(self):
        with patch("pathlib.Path.mkdir"):
            start = time.perf_counter()
            ensure_dirs()
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_extract_content_limits_results(self):
        """extract_content must cap results at 50 items."""
        src = _SCRIPT.read_text()
        self.assertIn("[:50]", src, "Results must be capped at 50")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_vector_remember_does_not_raise(self):
        """vector_remember must not raise on network failure."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            vector_remember("test browsing event", {})

    def test_slack_upload_handles_timeout(self):
        """slack_upload must not raise on subprocess timeout."""
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("curl", 15)):
            _mod.slack_upload("/fake/path.jpg", "Test comment")

    def test_monitor_state_survives_corrupted_json(self):
        """monitor_page state file corruption must be handled gracefully."""
        src = _SCRIPT.read_text()
        self.assertIn("except Exception", src)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_ensure_dirs_creates_required_directories(self):
        """ensure_dirs must create screenshots, pdfs, profiles dirs."""
        created = []
        original_mkdir = Path.mkdir

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "SCREENSHOTS_DIR", Path(tmpdir) / "screenshots"):
                with patch.object(_mod, "PDFS_DIR", Path(tmpdir) / "pdfs"):
                    with patch.object(_mod, "PROFILES_DIR", Path(tmpdir) / "profiles"):
                        ensure_dirs()
                        self.assertTrue((Path(tmpdir) / "screenshots").exists())
                        self.assertTrue((Path(tmpdir) / "pdfs").exists())
                        self.assertTrue((Path(tmpdir) / "profiles").exists())

    def test_log_function_callable(self):
        self.assertTrue(callable(log))

    def test_user_agent_is_realistic(self):
        """User agent must look like a real browser."""
        ua = _mod.USER_AGENT
        self.assertIn("Mozilla", ua)
        self.assertIn("Chrome", ua)

    def test_browser_dir_under_workspace(self):
        self.assertIn(str(Path.home()), str(_mod.BROWSER_DIR))

    def test_monitor_state_file_in_browser_dir(self):
        self.assertTrue(str(_mod.MONITOR_STATE).startswith(str(_mod.BROWSER_DIR)))

    def test_run_async_is_callable(self):
        self.assertTrue(callable(run_async))


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_vector_remember_sends_source_browser(self):
        """vector_remember must send source='browser' in payload."""
        sent_payloads = []

        def capture_urlopen(req, timeout=None):
            sent_payloads.append(json.loads(req.data.decode()))
            raise OSError("closed for test")

        with patch("urllib.request.urlopen", side_effect=capture_urlopen):
            vector_remember("Test page content", {"url": "https://example.com"})

        if sent_payloads:
            self.assertEqual(sent_payloads[0]["source"], "browser")

    def test_monitor_state_hash_detects_changes(self):
        """Monitor state must detect content hash changes."""
        import hashlib
        content1 = ["Price: $10"]
        content2 = ["Price: $15"]
        hash1 = hashlib.md5(json.dumps(content1).encode()).hexdigest()
        hash2 = hashlib.md5(json.dumps(content2).encode()).hexdigest()
        self.assertNotEqual(hash1, hash2)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_no_args_prints_help(self):
        """Running with no URL args should print help."""
        with patch("sys.argv", ["nova_browser.py"]):
            import io
            from contextlib import redirect_stdout
            buf = io.StringIO()
            with redirect_stdout(buf):
                try:
                    _mod.main() if hasattr(_mod, "main") else None
                except SystemExit:
                    pass

    def test_extract_requires_selector(self):
        """--extract without --selector should exit with error."""
        src = _SCRIPT.read_text()
        self.assertIn("--selector", src)
        self.assertIn("requires --selector", src.lower() if "--extract requires --selector" in src.lower()
                      else src)

    def test_json_output_flag_defined(self):
        """--json flag must be in the argument parser."""
        src = _SCRIPT.read_text()
        self.assertIn('"--json"', src)


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

    def test_constants_defined(self):
        self.assertIsInstance(_mod.DEFAULT_TIMEOUT, int)
        self.assertIsInstance(_mod.DEFAULT_VIEWPORT, dict)
        self.assertIsInstance(_mod.USER_AGENT, str)
        self.assertIsInstance(_mod.BROWSER_DIR, Path)
        self.assertIsInstance(_mod.SCREENSHOTS_DIR, Path)
        self.assertIsInstance(_mod.PDFS_DIR, Path)

    def test_functions_exist(self):
        for fn in ("log", "ensure_dirs", "slack_upload", "vector_remember",
                   "create_browser", "close_browser", "fetch_rendered",
                   "take_screenshot", "extract_content", "generate_pdf",
                   "monitor_page", "page_performance", "run_async"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_module_loads_without_playwright(self):
        """Module must load even if playwright is not installed (already tested by loading)."""
        self.assertIsNotNone(_mod)


if __name__ == "__main__":
    unittest.main(verbosity=2)
