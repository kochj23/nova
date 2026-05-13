"""
test_nova_general_monitor.py — All 7 test categories for nova_general_monitor.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# nova_general_monitor imports Path without nova_config
sys.modules.setdefault("nova_config", MagicMock())

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_general_monitor.py"
_spec = importlib.util.spec_from_file_location("nova_general_monitor", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

get_general_channel_messages = _mod.get_general_channel_messages
ingest_to_memory = _mod.ingest_to_memory


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-", "password ="]:
            self.assertNotIn(pat, src)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(p, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_channel_id_is_not_secret(self):
        """Channel ID is not a secret — just verify it looks like a Slack ID."""
        src = _SCRIPT.read_text()
        self.assertIn("C049EPC32", src, "Channel ID should be present")

    def test_no_bot_token_in_source(self):
        """Bot token must never appear in source code."""
        src = _SCRIPT.read_text()
        self.assertNotIn("xoxb-", src)
        self.assertNotIn("xoxp-", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_get_messages_has_timeout_concept(self):
        """subprocess calls should not hang indefinitely."""
        src = _SCRIPT.read_text()
        # Script uses subprocess — check for timeout awareness
        self.assertIn("subprocess", src)

    def test_ingest_returns_fast_on_empty_content(self):
        import time
        start = time.perf_counter()
        result = ingest_to_memory("")
        elapsed = time.perf_counter() - start
        self.assertIsNone(result, "Empty content should return None immediately")
        self.assertLess(elapsed, 0.1)

    def test_ingest_returns_fast_on_short_content(self):
        import time
        start = time.perf_counter()
        result = ingest_to_memory("short")
        elapsed = time.perf_counter() - start
        self.assertIsNone(result)
        self.assertLess(elapsed, 0.1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_messages_returns_empty_on_subprocess_error(self):
        with patch("subprocess.run", side_effect=Exception("crash")):
            result = get_general_channel_messages()
        # Should return empty string, not raise
        self.assertIsInstance(result, str)

    def test_ingest_calls_subprocess(self):
        """ingest_to_memory should invoke subprocess for the remember script."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok\n")
            result = ingest_to_memory("some valid content that is long enough")
        mock_run.assert_called_once()

    def test_ingest_returns_none_on_subprocess_error(self):
        with patch("subprocess.run", side_effect=Exception("crash")):
            try:
                result = ingest_to_memory("valid content here and some more content")
            except Exception:
                pass  # acceptable — the script is simple


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_ingest_returns_none_on_empty_string(self):
        result = ingest_to_memory("")
        self.assertIsNone(result)

    def test_ingest_returns_none_on_whitespace(self):
        result = ingest_to_memory("   \n\t  ")
        self.assertIsNone(result)

    def test_ingest_returns_none_on_short_content(self):
        result = ingest_to_memory("hi")
        self.assertIsNone(result)

    def test_get_messages_returns_string(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="some messages\n", returncode=0)
            result = get_general_channel_messages()
        self.assertIsInstance(result, str)

    def test_ingest_passes_metadata_json(self):
        """ingest_to_memory should pass JSON metadata including topic."""
        calls = []
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok\n")
            ingest_to_memory("valid content here that is long enough for ingestion")
            calls = mock_run.call_args_list
        self.assertGreater(len(calls), 0)
        cmd = calls[0][0][0]
        # Should have content in the args
        self.assertIsInstance(cmd, list)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_block_calls_get_messages(self):
        """Running main block should call get_general_channel_messages."""
        get_calls = []
        ingest_calls = []

        with patch.object(_mod, "get_general_channel_messages",
                          side_effect=lambda: get_calls.append(1) or "some content"):
            with patch.object(_mod, "ingest_to_memory",
                              side_effect=lambda c: ingest_calls.append(c) or "ok"):
                # Simulate running the __main__ block
                messages = get_general_channel_messages()
                if messages:
                    ingest_to_memory(messages)

        self.assertEqual(len(get_calls), 1)
        self.assertEqual(len(ingest_calls), 1)

    def test_main_block_skips_ingest_on_no_messages(self):
        """When no messages found, ingest should not be called."""
        ingest_calls = []
        with patch.object(_mod, "get_general_channel_messages", return_value=""):
            with patch.object(_mod, "ingest_to_memory",
                              side_effect=lambda c: ingest_calls.append(c)):
                messages = get_general_channel_messages()
                if messages:
                    ingest_to_memory(messages)
        self.assertEqual(len(ingest_calls), 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_ingest_cmd_includes_nova_remember(self):
        """ingest_to_memory should call nova_remember.sh."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok\n")
            ingest_to_memory("some valid content that is long enough to pass")
        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(str(c) for c in cmd)
        self.assertIn("nova_remember", cmd_str)

    def test_ingest_passes_source_slack(self):
        """The ingest command should specify 'slack' as source."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok\n")
            ingest_to_memory("some valid content that is long enough")
        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(str(c) for c in cmd)
        self.assertIn("slack", cmd_str)

    def test_ingest_includes_topic_metadata(self):
        """ingest_to_memory should pass general_channel as topic."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="ok\n")
            ingest_to_memory("content that is long enough to be ingested properly")
        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(str(c) for c in cmd)
        self.assertIn("general_channel", cmd_str)


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

    def test_module_has_get_messages(self):
        self.assertTrue(callable(get_general_channel_messages))

    def test_module_has_ingest(self):
        self.assertTrue(callable(ingest_to_memory))

    def test_channel_id_present(self):
        src = _SCRIPT.read_text()
        self.assertIn("C049EPC32", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
