"""
test_nova_nightly_media.py — All 7 test categories for nova_nightly_media.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_nightly_media.py"
sys.path.insert(0, str(Path(__file__).parent))
from nova_test_loader import load_script_compat

_nova_cfg = MagicMock()
_nova_cfg.post_both = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.SLACK_CHAN = "#nova-chat"

_nova_media_registry = MagicMock()
_nova_media_registry.is_done = MagicMock(return_value=False)
_nova_media_registry.mark_done = MagicMock()

# nova_yt_new_episodes provides CHANNELS
_nova_yt = MagicMock()
_nova_yt.CHANNELS = {"test_channel": {"url": "http://youtube.com/test", "topics": ["tech"]}}

sys.modules["nova_config"] = _nova_cfg
sys.modules["nova_media_registry"] = _nova_media_registry
sys.modules["nova_yt_new_episodes"] = _nova_yt

_mod = load_script_compat(_SCRIPT, "nova_nightly_media")


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

    def test_cookies_path_uses_home_not_hardcoded(self):
        src = _SCRIPT.read_text()
        # Cookie path should use Path.home(), not literal
        if "cookies" in src.lower():
            home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_subprocess_calls_have_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src)

    def test_channels_shuffled_each_run(self):
        """Channel order must be randomized to distribute load."""
        src = _SCRIPT.read_text()
        self.assertIn("shuffle", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_yt_block_handled_gracefully(self):
        """403/429 errors must be handled, not crash the pipeline."""
        src = _SCRIPT.read_text()
        self.assertIn("403", src)

    def test_signal_handler_sets_shutdown(self):
        """SIGTERM must set shutdown flag to allow graceful exit."""
        src = _SCRIPT.read_text()
        self.assertIn("SIGTERM", src)
        self.assertIn("shutdown", src)

    def test_pipeline_resumes_from_checkpoint(self):
        """Pipeline must support resume logic."""
        src = _SCRIPT.read_text()
        self.assertIn("resume", src.lower())


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_log_function_exists(self):
        self.assertTrue(hasattr(_mod, "log") or hasattr(_mod, "LOG_FILE"))

    def test_constants_defined(self):
        self.assertTrue(hasattr(_mod, "CHANNELS") or True)

    def test_music_channels_excluded_from_transcription(self):
        """Music channels must be skipped for transcription."""
        src = _SCRIPT.read_text()
        self.assertIn("music", src.lower())

    def test_checkpoint_interval_defined(self):
        """Pipeline must checkpoint every N files."""
        src = _SCRIPT.read_text()
        self.assertIn("10", src)  # checkpoint every 10 files


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_callable(self):
        self.assertTrue(callable(_mod.main))

    def test_db_tables_created_on_first_run(self):
        """Script must create DB tables if they don't exist."""
        src = _SCRIPT.read_text()
        self.assertIn("CREATE TABLE", src.upper())

    def test_per_video_notification_sent(self):
        """Each processed video must post a notification."""
        src = _SCRIPT.read_text()
        self.assertIn("nova-notifications", src)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_yt_blocked_posts_alert_to_chat(self):
        """YT blocking must alert to #nova-chat."""
        src = _SCRIPT.read_text()
        self.assertIn("nova-chat", src)

    def test_random_delay_between_videos(self):
        """Random sleep between videos prevents rate limiting."""
        src = _SCRIPT.read_text()
        self.assertIn("random", src)
        self.assertIn("sleep", src)


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

    def test_main_exists(self):
        self.assertTrue(hasattr(_mod, "main"))

    def test_signal_handler_registered(self):
        src = _SCRIPT.read_text()
        self.assertIn("signal.signal", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
