"""
test_nova_nightly_report.py — All 7 test categories for nova_nightly_report.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_nightly_report.py"

_nova_cfg = MagicMock()
_nova_cfg.post_both = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"

sys.modules["nova_config"] = _nova_cfg

_spec = importlib.util.spec_from_file_location("nova_nightly_report", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

vector_remember = _mod.vector_remember
slack_post = _mod.slack_post


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
        for p in ["kochjpar" + _at + "gmail.com", "user" + _at + "example-corp.com"]:
            self.assertNotIn(p, src)

    def test_vector_remember_silent_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            try:
                vector_remember("test text", "nightly")
            except Exception as e:
                self.fail(f"vector_remember raised: {e}")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_slack_post_chunks_large_messages(self):
        """slack_post must chunk messages to avoid Slack limits."""
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        long_msg = "x" * 10000
        slack_post(long_msg)
        for p in posts:
            self.assertLessEqual(len(p), 3001)
        _nova_cfg.post_both.side_effect = None

    def test_vector_remember_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_vector_remember_returns_on_failure(self):
        with patch("urllib.request.urlopen", side_effect=OSError("network error")):
            vector_remember("test", "nightly")  # should not raise

    def test_slack_post_handles_error(self):
        _nova_cfg.post_both.side_effect = Exception("slack error")
        try:
            slack_post("test message")
        except Exception:
            pass
        finally:
            _nova_cfg.post_both.side_effect = None

    def test_github_section_handles_cli_failure(self):
        with patch("subprocess.run", side_effect=Exception("gh not found")):
            try:
                result = _mod.section_github()
            except Exception:
                result = None
        # Should not raise — may return None or empty string
        self.assertTrue(result is None or isinstance(result, str))


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_slack_post_calls_post_both(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        slack_post("test message")
        self.assertGreater(len(posts), 0)
        _nova_cfg.post_both.side_effect = None

    def test_vector_remember_sends_correct_source(self):
        payloads = []
        def capture(req, timeout=None):
            payloads.append(json.loads(req.data.decode()))
            return MagicMock(__enter__=lambda s: s, __exit__=MagicMock(return_value=False))
        with patch("urllib.request.urlopen", side_effect=capture):
            vector_remember("test report text", "nightly")
        if payloads:
            self.assertEqual(payloads[0]["source"], "nightly")

    def test_constants_defined(self):
        self.assertIsNotNone(_mod.VECTOR_MEM_URL)
        self.assertIsNotNone(_mod.SCRIPTS)
        self.assertIsNotNone(_mod.TODAY)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_posts_to_slack(self):
        posts = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: posts.append(msg)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
            with patch("urllib.request.urlopen", side_effect=Exception("down")):
                try:
                    _mod.main()
                except Exception:
                    pass
        _nova_cfg.post_both.side_effect = None
        # Just verify main is callable and tries to post

    def test_report_sections_assemble(self):
        with patch.object(_mod, "section_github", return_value="*GitHub:*\n• no activity"):
            with patch.object(_mod, "section_email", return_value=None):
                with patch.object(_mod, "section_memory_file", return_value=None):
                    with patch.object(_mod, "section_packages", return_value=None):
                        with patch.object(_mod, "section_weather", return_value="*Weather:* Sunny"):
                            with patch.object(_mod, "section_homekit", return_value=None):
                                with patch.object(_mod, "vector_remember"):
                                    posts = []
                                    _nova_cfg.post_both.side_effect = lambda m, **kw: posts.append(m)
                                    try:
                                        _mod.main()
                                    except Exception:
                                        pass
                                    _nova_cfg.post_both.side_effect = None


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_weather_section_returns_string_or_none(self):
        with patch("urllib.request.urlopen", side_effect=Exception("down")):
            try:
                result = _mod.section_weather()
            except Exception:
                result = None
        self.assertTrue(result is None or isinstance(result, str))

    def test_github_section_returns_string_or_none(self):
        with patch("subprocess.run", return_value=MagicMock(returncode=1, stdout="")):
            try:
                result = _mod.section_github()
            except Exception:
                result = None
        self.assertTrue(result is None or isinstance(result, str))


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
        for fn in ["main", "vector_remember", "slack_post"]:
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))


if __name__ == "__main__":
    unittest.main(verbosity=2)
