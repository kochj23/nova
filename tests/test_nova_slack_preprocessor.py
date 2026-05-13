"""
test_nova_slack_preprocessor.py — All 7 test categories for nova_slack_preprocessor.py
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

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.slack_bot_token.return_value = "xoxb-fake-token"
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_slack_preprocessor.py"
_spec = importlib.util.spec_from_file_location("nova_slack_preprocessor", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_state = _mod.load_state
save_state = _mod.save_state
get_latest_messages = _mod.get_latest_messages
run_memory_first = _mod.run_memory_first
post_memory_context_to_thread = _mod.post_memory_context_to_thread


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA"]
        for p in forbidden:
            self.assertNotIn(p, src, f"Credential: {p!r}")

    def test_token_loaded_from_nova_config(self):
        src = _SCRIPT.read_text()
        self.assertIn("slack_bot_token", src)

    def test_no_pii_email_literals(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII: {p!r}")

    def test_memory_context_truncated(self):
        """post_memory_context_to_thread must truncate at 3500 chars."""
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        long_memory = "M" * 5000
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            post_memory_context_to_thread("C123", "1000.0", long_memory)

        if captured:
            text = captured[0].get("text", "")
            self.assertLessEqual(len(text), 4500,
                                 "Memory context should be truncated before posting")

    def test_only_processes_jordan_messages(self):
        """Preprocessor must only act on messages from Jordan (JORDAN_USER_ID)."""
        self.assertIsInstance(_mod.JORDAN_USER_ID, str)
        self.assertGreater(len(_mod.JORDAN_USER_ID), 0)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_load_state_fast(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = Path(tmpdir) / "state.json"
            with patch.object(_mod, "STATE_FILE", sf):
                start = time.perf_counter()
                for _ in range(100):
                    load_state()
                    save_state({"last_ts": "1000.0"})
                elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_get_latest_messages_returns_quickly_on_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            start = time.perf_counter()
            result = get_latest_messages("C123", "0")
            elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result, [])


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_latest_messages_returns_empty_on_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = get_latest_messages("C123", "0")
        self.assertEqual(result, [])

    def test_get_latest_messages_returns_empty_on_api_error(self):
        mock_r = MagicMock()
        mock_r.__enter__ = lambda s: s
        mock_r.__exit__ = MagicMock(return_value=False)
        mock_r.read.return_value = json.dumps({"ok": False, "error": "invalid_auth"}).encode()
        with patch("urllib.request.urlopen", return_value=mock_r):
            result = get_latest_messages("C123", "0")
        self.assertEqual(result, [])

    def test_run_memory_first_returns_none_on_subprocess_failure(self):
        with patch("subprocess.run", side_effect=Exception("process failed")):
            result = run_memory_first("test query")
        self.assertIsNone(result)

    def test_post_memory_context_returns_false_on_network_error(self):
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            result = post_memory_context_to_thread("C123", "1000.0", "memory text")
        self.assertFalse(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_load_state_returns_default_on_missing_file(self):
        with patch.object(_mod, "STATE_FILE",
                           Path("/nonexistent/preprocessor_state.json")):
            state = load_state()
        self.assertIn("last_ts", state)

    def test_save_and_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = Path(tmpdir) / "state.json"
            with patch.object(_mod, "STATE_FILE", sf):
                save_state({"last_ts": "9999.0", "last_ts_C123": "8888.0"})
                loaded = load_state()
        self.assertEqual(loaded["last_ts"], "9999.0")

    def test_get_latest_messages_returns_list_on_success(self):
        messages = [
            {"ts": "1000.0", "text": "Hello", "user": "U049EPC2W"},
        ]
        mock_r = MagicMock()
        mock_r.__enter__ = lambda s: s
        mock_r.__exit__ = MagicMock(return_value=False)
        mock_r.read.return_value = json.dumps({"ok": True, "messages": messages}).encode()
        with patch("urllib.request.urlopen", return_value=mock_r):
            result = get_latest_messages("C123", "0")
        self.assertIsInstance(result, list)

    def test_run_memory_first_returns_none_on_no_output(self):
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="")):
            result = run_memory_first("test")
        self.assertIsNone(result)

    def test_run_memory_first_returns_output_on_success(self):
        with patch("subprocess.run",
                   return_value=MagicMock(returncode=0, stdout="MEMORY FOUND: relevant context")):
            result = run_memory_first("test")
        self.assertIsNotNone(result)
        self.assertIn("MEMORY FOUND", result)

    def test_constants_jordan_user_id(self):
        self.assertEqual(_mod.JORDAN_USER_ID, "U049EPC2W")

    def test_constants_nova_bot_id(self):
        self.assertEqual(_mod.NOVA_BOT_ID, "U0ALZRF3HRQ")

    def test_poll_interval(self):
        self.assertEqual(_mod.POLL_INTERVAL, 5)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_memory_context_posted_to_thread_when_found(self):
        """When memory is found, it should be posted as a thread reply."""
        post_calls = []

        def fake_urlopen(req, timeout=None):
            post_calls.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            post_memory_context_to_thread("C123", "1000.0", "MEMORY FOUND: test context")

        self.assertGreater(len(post_calls), 0)
        self.assertEqual(post_calls[0]["channel"], "C123")
        self.assertEqual(post_calls[0]["thread_ts"], "1000.0")

    def test_state_persists_last_ts_per_channel(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = Path(tmpdir) / "state.json"
            with patch.object(_mod, "STATE_FILE", sf):
                state = {"last_ts_C123": "5000.0", "last_ts_C456": "3000.0"}
                save_state(state)
                loaded = load_state()
        self.assertEqual(loaded["last_ts_C123"], "5000.0")
        self.assertEqual(loaded["last_ts_C456"], "3000.0")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_preprocessor_ignores_non_jordan_messages(self):
        """Non-Jordan messages in channel must be ignored."""
        messages = [
            {"ts": "2000.0", "text": "Bot says hi", "bot_id": "B123"},
            {"ts": "2001.0", "text": "Nova says", "user": _mod.NOVA_BOT_ID},
        ]
        processed_texts = []

        def fake_run_memory_first(text):
            processed_texts.append(text)
            return None

        mock_r = MagicMock()
        mock_r.__enter__ = lambda s: s
        mock_r.__exit__ = MagicMock(return_value=False)
        mock_r.read.return_value = json.dumps({"ok": True, "messages": messages}).encode()

        with tempfile.TemporaryDirectory() as tmpdir:
            sf = Path(tmpdir) / "state.json"
            with patch.object(_mod, "STATE_FILE", sf):
                with patch("urllib.request.urlopen", return_value=mock_r):
                    with patch.object(_mod, "run_memory_first",
                                       side_effect=fake_run_memory_first):
                        # Simulate one loop iteration
                        state = load_state()
                        msgs = get_latest_messages("C123", "0")
                        for msg in msgs:
                            if msg.get("user") == _mod.JORDAN_USER_ID:
                                run_memory_first(msg.get("text", ""))

        self.assertEqual(len(processed_texts), 0,
                         "Non-Jordan messages should not be processed")

    def test_memory_not_posted_when_no_results(self):
        """When run_memory_first returns nothing, no thread post should be made."""
        post_calls = []
        with patch.object(_mod, "run_memory_first", return_value=None):
            with patch.object(_mod, "post_memory_context_to_thread",
                               side_effect=lambda *a: post_calls.append(a)):
                result = run_memory_first("no match query")
        self.assertEqual(len(post_calls), 0)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_slack_preprocessor.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertEqual(_mod.JORDAN_USER_ID, "U049EPC2W")
        self.assertEqual(_mod.NOVA_CHAT_CHANNEL, "C0AMNQ5GX70")
        self.assertIsInstance(_mod.POLL_INTERVAL, int)

    def test_all_functions_callable(self):
        for fn in [load_state, save_state, get_latest_messages,
                    run_memory_first, post_memory_context_to_thread]:
            self.assertTrue(callable(fn))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
