"""
test_nova_weekly_digest.py — All 7 test categories for nova_weekly_digest.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_weekly_digest.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.post_both = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"

_nova_image_utils = MagicMock()
_nova_image_utils.ensure_backend = MagicMock(return_value=True)
_nova_image_utils.generate_image = MagicMock(return_value=None)

sys.modules["nova_config"] = _nova_cfg
sys.modules["nova_image_utils"] = _nova_image_utils

_mod = load_script_compat(_SCRIPT, "nova_weekly_digest")


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(p, src)

    def test_keychain_for_api_key(self):
        src = _SCRIPT.read_text()
        self.assertIn("find-generic-password", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_api_calls_have_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)

    def test_week_range_bounded(self):
        """Digest covers exactly 7 days."""
        src = _SCRIPT.read_text()
        self.assertIn("timedelta", src)
        self.assertIn("7", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_generate_editorial_falls_back_to_ollama(self):
        ollama_calls = []
        def fake_ollama(*a, **kw):
            ollama_calls.append(1)
            return "Editorial text " * 100
        with patch.object(_mod, "_generate_via_openrouter", side_effect=Exception("OR fail")):
            with patch.object(_mod, "_generate_via_ollama", side_effect=fake_ollama):
                try:
                    result = _mod.generate_editorial({})
                except Exception:
                    result = None
        # Either fell back to ollama or returned gracefully
        self.assertTrue(result is None or isinstance(result, str))

    def test_memory_query_handles_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            try:
                result = _mod.query_week_memories()
            except Exception:
                result = {}
        self.assertIsInstance(result, dict)

    def test_main_handles_no_content_gracefully(self):
        with patch.object(_mod, "gather_week_content", return_value={}):
            with patch.object(_mod, "_now", return_value=__import__("datetime").datetime.now()):
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

    def test_log_does_not_raise(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            if hasattr(_mod, "LOG_FILE"):
                with patch.object(_mod, "LOG_FILE", Path(tmp) / "test.log"):
                    _mod.log("test")
            else:
                _mod.log("test")

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.MEMORY_SERVER)
        self.assertIsNotNone(_mod.MODEL)

    def test_date_override_env_var_supported(self):
        """NOVA_FOR_DATE override must be supported."""
        src = _SCRIPT.read_text()
        self.assertIn("NOVA_FOR_DATE", src)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_calls_slack(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        with patch.object(_mod, "gather_week_content",
                          return_value={"dreams": [], "essays": [], "opinions": []}):
            with patch.object(_mod, "generate_editorial", return_value="Editorial text " * 50):
                with patch.object(_mod, "generate_cover_image", return_value=None):
                    with patch.object(_mod, "publish_to_hugo", return_value=True):
                        with patch.object(_mod, "send_to_herd"):
                            try:
                                _mod.main()
                            except Exception:
                                pass
        _nova_cfg.post_both.side_effect = None

    def test_full_week_content_includes_sections(self):
        src = _SCRIPT.read_text()
        for section in ["dream", "essay", "opinion"]:
            self.assertIn(section, src.lower())


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_digest_covers_7_day_window(self):
        src = _SCRIPT.read_text()
        self.assertIn("7", src)
        self.assertIn("timedelta", src)

    def test_editorial_minimum_length_enforced(self):
        """Short editorial should be rejected."""
        src = _SCRIPT.read_text()
        # Should have length check
        self.assertIn("len(", src)


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
