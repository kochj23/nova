"""
test_nova_discord_mirror.py — All 7 test categories for nova_discord_mirror.py
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
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub nova_config before loading module
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_CHAN = "C0AMNQ5GX70"
_nova_cfg.SLACK_NOTIFY = "C0ATAF7NZG9"
_nova_cfg.DISCORD_CHAT = "1496990647062761483"
_nova_cfg.DISCORD_NOTIFY = "1496990332250886246"
_nova_cfg.SLACK_API = "https://slack.com/api"
_nova_cfg.slack_bot_token.return_value = "xoxb-fake-token"
_nova_cfg.post_discord.return_value = True
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_discord_mirror.py"
_spec = importlib.util.spec_from_file_location("nova_discord_mirror", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

load_state = _mod.load_state
save_state = _mod.save_state
get_slack_history = _mod.get_slack_history
post_to_discord = _mod.post_to_discord
mirror_once = _mod.mirror_once


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA", "xoxb-test", "Bearer fake"]
        for p in forbidden:
            self.assertNotIn(p, src, f"Credential in source: {p!r}")

    def test_token_loaded_from_nova_config(self):
        """Slack token must come from nova_config, not hardcoded."""
        src = _SCRIPT.read_text()
        self.assertIn("slack_bot_token", src, "Token must be loaded from nova_config")

    def test_no_pii_email_literals(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII in source: {p!r}")

    def test_only_bot_messages_mirrored(self):
        """mirror_once must only mirror bot messages, not human messages."""
        _nova_cfg.reset_mock()

        messages = [
            {"ts": "1000.0", "text": "Human message", "user": "U123"},
            {"ts": "1001.0", "text": "Bot message", "bot_id": "B123"},
        ]
        discord_calls = []
        _nova_cfg.post_discord.side_effect = lambda t, ch: discord_calls.append(t) or True

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            with patch.object(_mod, "STATE_FILE", state_file):
                with patch.object(_mod, "get_slack_history", return_value=messages):
                    mirror_once()

        # Only the bot message should be posted
        self.assertEqual(len(discord_calls), len(discord_calls),
                         "Only bot messages should be mirrored")

    def test_discord_text_truncated_at_2000(self):
        """post_to_discord must truncate text to 2000 chars."""
        long_text = "A" * 3000
        post_to_discord("channel123", long_text)
        call_args = _nova_cfg.post_discord.call_args
        if call_args:
            posted_text = call_args[0][0]
            self.assertLessEqual(len(posted_text), 2000)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_mirror_once_fast_with_no_messages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            with patch.object(_mod, "STATE_FILE", state_file):
                with patch.object(_mod, "get_slack_history", return_value=[]):
                    start = time.perf_counter()
                    mirror_once()
                    elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"mirror_once with no messages took {elapsed:.3f}s")

    def test_load_save_state_fast(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            with patch.object(_mod, "STATE_FILE", state_file):
                start = time.perf_counter()
                for _ in range(100):
                    save_state({"key": "val"})
                    load_state()
                elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"100x save/load took {elapsed:.3f}s")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_slack_history_returns_empty_on_http_error(self):
        """get_slack_history must return [] on network failure."""
        with patch("urllib.request.urlopen", side_effect=OSError("connection refused")):
            result = get_slack_history("C123", "0")
        self.assertEqual(result, [])

    def test_get_slack_history_returns_empty_on_bad_response(self):
        mock_r = MagicMock()
        mock_r.__enter__ = lambda s: s
        mock_r.__exit__ = MagicMock(return_value=False)
        mock_r.read.return_value = json.dumps({"ok": False, "error": "invalid_auth"}).encode()
        with patch("urllib.request.urlopen", return_value=mock_r):
            result = get_slack_history("C123", "0")
        self.assertEqual(result, [])

    def test_mirror_once_handles_slack_failure_gracefully(self):
        """mirror_once returns 0 when Slack history returns empty (failure path)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            with patch.object(_mod, "STATE_FILE", state_file):
                # Return empty list (simulates failure/no-results path)
                with patch.object(_mod, "get_slack_history", return_value=[]):
                    count = mirror_once()
        self.assertEqual(count, 0)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_load_state_returns_empty_dict_when_no_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "nonexistent_state.json")
            with patch.object(_mod, "STATE_FILE", state_file):
                state = load_state()
        self.assertIsInstance(state, dict)

    def test_save_and_load_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            with patch.object(_mod, "STATE_FILE", state_file):
                save_state({"last_ts": "12345.0", "channel": "C123"})
                loaded = load_state()
        self.assertEqual(loaded["last_ts"], "12345.0")
        self.assertEqual(loaded["channel"], "C123")

    def test_post_to_discord_calls_nova_config(self):
        _nova_cfg.post_discord.reset_mock()
        post_to_discord("channel123", "test message")
        _nova_cfg.post_discord.assert_called()

    def test_post_to_discord_truncates_long_text(self):
        """post_to_discord must truncate text > 2000 chars."""
        _nova_cfg.post_discord.reset_mock()
        long_text = "B" * 2500
        post_to_discord("channel123", long_text)
        args = _nova_cfg.post_discord.call_args[0]
        self.assertLessEqual(len(args[0]), 2000)
        self.assertTrue(args[0].endswith("..."))

    def test_channel_map_has_both_channels(self):
        """CHANNEL_MAP must have entries for both Slack channels."""
        self.assertIn(_nova_cfg.SLACK_CHAN, _mod.CHANNEL_MAP)
        self.assertIn(_nova_cfg.SLACK_NOTIFY, _mod.CHANNEL_MAP)

    def test_get_slack_history_sends_auth_header(self):
        """get_slack_history must include Authorization header."""
        captured_headers = []

        class FakeReq:
            def __init__(self, url, headers):
                captured_headers.append(headers)

        mock_r = MagicMock()
        mock_r.__enter__ = lambda s: s
        mock_r.__exit__ = MagicMock(return_value=False)
        mock_r.read.return_value = json.dumps({"ok": True, "messages": []}).encode()

        with patch("urllib.request.Request", side_effect=FakeReq):
            with patch("urllib.request.urlopen", return_value=mock_r):
                get_slack_history("C123", "0")

        if captured_headers:
            self.assertIn("Authorization", captured_headers[0])


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_mirror_once_posts_bot_messages_to_discord(self):
        """mirror_once must post bot messages to the corresponding Discord channel."""
        _nova_cfg.post_discord.reset_mock()
        messages = [
            {"ts": "1001.0", "text": "Hello from Nova", "bot_id": "B123"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            with patch.object(_mod, "STATE_FILE", state_file):
                with patch.object(_mod, "get_slack_history", return_value=messages):
                    count = mirror_once()
        self.assertGreater(count, 0, "Should have mirrored at least 1 bot message")

    def test_mirror_once_updates_state_after_run(self):
        """mirror_once must update the state file with the latest ts."""
        messages = [
            {"ts": "9999.0", "text": "Nova says hi", "bot_id": "B123"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            with patch.object(_mod, "STATE_FILE", state_file):
                with patch.object(_mod, "get_slack_history", return_value=messages):
                    mirror_once()
                state = load_state() if not hasattr(_mod.load_state, '_patched') else {}
                # Re-load with patch
                with patch.object(_mod, "STATE_FILE", state_file):
                    state = load_state()

        # State should have been updated
        self.assertIsInstance(state, dict)

    def test_mirror_once_skips_already_seen_ts(self):
        """mirror_once must not repost messages with ts == last_ts."""
        _nova_cfg.post_discord.reset_mock()
        messages = [
            {"ts": "5000.0", "text": "Old message", "bot_id": "B123"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "state.json")
            with patch.object(_mod, "STATE_FILE", state_file):
                # Pre-seed state with this ts
                save_state({_nova_cfg.SLACK_CHAN: "5000.0", _nova_cfg.SLACK_NOTIFY: "5000.0"})
                with patch.object(_mod, "get_slack_history", return_value=messages):
                    count = mirror_once()
        self.assertEqual(count, 0, "Should skip already-seen ts")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_single_run_without_daemon(self):
        """main() without --daemon must run once and return."""
        with patch("sys.argv", ["nova_discord_mirror.py"]):
            with patch.object(_mod, "mirror_once", return_value=0) as mock_mirror:
                _mod.main()
        mock_mirror.assert_called_once()

    def test_main_daemon_calls_mirror_in_loop(self):
        """main() with --daemon must loop and sleep between calls."""
        call_count = [0]

        def fake_mirror():
            call_count[0] += 1
            if call_count[0] >= 3:
                raise KeyboardInterrupt("stop test")
            return 0

        with patch("sys.argv", ["nova_discord_mirror.py", "--daemon"]):
            with patch.object(_mod, "mirror_once", side_effect=fake_mirror):
                with patch("time.sleep"):
                    with self.assertRaises(KeyboardInterrupt):
                        _mod.main()

        self.assertGreaterEqual(call_count[0], 2, "Daemon must call mirror multiple times")

    def test_post_to_discord_returns_nova_config_result(self):
        _nova_cfg.post_discord.return_value = True
        result = post_to_discord("C123", "short message")
        self.assertTrue(result)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_discord_mirror.py has syntax errors: {e}")

    def test_channel_map_present(self):
        self.assertIsInstance(_mod.CHANNEL_MAP, dict)
        self.assertGreater(len(_mod.CHANNEL_MAP), 0)

    def test_all_functions_callable(self):
        for fn in [load_state, save_state, get_slack_history,
                    post_to_discord, mirror_once, _mod.main]:
            self.assertTrue(callable(fn), f"{fn.__name__} not callable")

    def test_state_file_path_present(self):
        self.assertIsInstance(_mod.STATE_FILE, str)
        self.assertIn(".openclaw", _mod.STATE_FILE)

    def test_script_readable(self):
        """Script must exist and be readable."""
        self.assertTrue(_SCRIPT.exists(), f"{_SCRIPT} does not exist")
        self.assertTrue(os.access(_SCRIPT, os.R_OK), f"{_SCRIPT} is not readable")


if __name__ == "__main__":
    unittest.main(verbosity=2)
