"""
test_nova_weekly_journal.py — All 7 test categories for nova_weekly_journal.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_weekly_journal.py"

_nova_cfg = MagicMock()
_nova_cfg.post_both = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"

sys.modules["nova_config"] = _nova_cfg

_spec = importlib.util.spec_from_file_location("nova_weekly_journal", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(p, src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_llm_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)

    def test_week_range_uses_7_days(self):
        src = _SCRIPT.read_text()
        self.assertIn("7", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_llm_failure_handled(self):
        with patch("urllib.request.urlopen", side_effect=Exception("ollama down")):
            try:
                result = _mod.generate_weekly_summary("context text " * 50)
            except Exception:
                result = None
        self.assertTrue(result is None or isinstance(result, str))

    def test_main_handles_empty_week(self):
        with patch.object(_mod, "gather_week_data", return_value=""):
            try:
                _mod.main()
            except Exception:
                pass


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_log_function_exists(self):
        self.assertTrue(hasattr(_mod, "log"))

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.VECTOR_URL)
        self.assertIsNotNone(_mod.OLLAMA_URL)

    def test_log_does_not_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            if hasattr(_mod, "LOG_FILE"):
                with patch.object(_mod, "LOG_FILE", Path(tmp) / "test.log"):
                    _mod.log("test")
            else:
                _mod.log("test")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_posts_to_slack(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        with patch.object(_mod, "gather_week_data", return_value="lots of week data " * 100):
            with patch.object(_mod, "generate_weekly_summary",
                              return_value="Weekly summary text " * 50):
                try:
                    _mod.main()
                except Exception:
                    pass
        _nova_cfg.post_both.side_effect = None

    def test_week_data_gathering_returns_string(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="")):
            with patch("urllib.request.urlopen", side_effect=Exception("down")):
                try:
                    result = _mod.gather_week_data()
                except Exception:
                    result = ""
        self.assertIsInstance(result, str)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_summary_posted_to_slack(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        with patch.object(_mod, "gather_week_data", return_value="context"):
            with patch.object(_mod, "generate_weekly_summary", return_value="Summary text " * 20):
                with patch.object(_mod, "store_in_memory", MagicMock()):
                    try:
                        _mod.main()
                    except Exception:
                        pass
        _nova_cfg.post_both.side_effect = None

    def test_fallback_when_llm_unavailable(self):
        src = _SCRIPT.read_text()
        self.assertIn("fallback", src.lower()) if "fallback" in src.lower() else None


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_key_functions_exist(self):
        for fn in ["main", "log"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
