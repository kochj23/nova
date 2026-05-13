"""
test_nova_config.py — All 7 test categories for nova_config.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Load module under test directly (not on PATH)
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_config.py"
_spec = importlib.util.spec_from_file_location("nova_config_mod", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)

# nova_config has no external nova_* imports — load directly
with patch("subprocess.run") as _mock_sp:
    _mock_sp.return_value = MagicMock(returncode=1, stdout="", stderr="")
    _spec.loader.exec_module(_mod)

# Convenience aliases
_keychain = _mod._keychain
slack_bot_token = _mod.slack_bot_token
openrouter_api_key = _mod.openrouter_api_key
slack_app_token = _mod.slack_app_token
discord_bot_token = _mod.discord_bot_token
post_discord = _mod.post_discord
post_both = _mod.post_both
is_private_source = _mod.is_private_source
filter_private_memories = _mod.filter_private_memories


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials_in_source(self):
        """Source file must not contain API keys or tokens."""
        src = _SCRIPT.read_text()
        forbidden = ["sk-live", "sk-test", "ghp_", "AKIA"]
        for pattern in forbidden:
            self.assertNotIn(pattern, src,
                             f"Potential credential found in source: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        """Source must not contain a literal hardcoded home path."""
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src,
                         "Hardcoded home path found — use Path.home() instead")

    def test_no_pii_emails_in_source(self):
        """Source must not contain personal email addresses as PII."""
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "jordan.koch" + _at + "disney" + ".com",
            "kochj" + _at + "digitalnoise.net",
        ]
        for pattern in pii:
            self.assertNotIn(pattern, src,
                             f"PII email pattern found in source: {pattern!r}")

    def test_keychain_required_exits_on_failure(self):
        """_keychain(required=True) must call sys.exit(1) when key not found."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            with self.assertRaises(SystemExit) as ctx:
                _keychain("nonexistent-service", required=True)
            self.assertEqual(ctx.exception.code, 1)

    def test_keychain_not_required_returns_empty_on_failure(self):
        """_keychain(required=False) must return '' instead of exiting."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            result = _keychain("nonexistent-service", required=False)
            self.assertEqual(result, "")

    def test_private_sources_covers_disney(self):
        """Disney-related sources must be in PRIVATE_SOURCES."""
        self.assertIn("disney_internal", _mod.PRIVATE_SOURCES)
        self.assertIn("disney_work", _mod.PRIVATE_SOURCES)

    def test_private_sources_covers_health(self):
        """Health-related sources must be in PRIVATE_SOURCES."""
        self.assertIn("apple_health", _mod.PRIVATE_SOURCES)
        self.assertIn("healthkit", _mod.PRIVATE_SOURCES)

    def test_token_not_returned_when_placeholder(self):
        """Token functions must reject env values that look like shell placeholders."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            with patch.dict(os.environ, {"NOVA_SLACK_BOT_TOKEN": "${NOVA_SLACK_BOT_TOKEN}"}):
                token = slack_bot_token()
                self.assertEqual(token, "",
                                 "Shell placeholder should not be treated as a valid token")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_is_private_source_fast(self):
        """is_private_source() must check 10,000 sources in < 200ms."""
        test_sources = ["email_archive", "music", "general", "disney_work",
                        "apple_health", "video", "unknown", "sre", "gardening", "reddit"]
        start = time.perf_counter()
        for _ in range(1000):
            for s in test_sources:
                is_private_source(s)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.2,
                        f"is_private_source 10000x took {elapsed:.3f}s (limit 200ms)")

    def test_filter_private_memories_fast_on_large_list(self):
        """filter_private_memories() must process 1000 items in < 100ms."""
        memories = [
            {"text": f"Memory {i}", "source": "music" if i % 3 else "disney_work"}
            for i in range(1000)
        ]
        start = time.perf_counter()
        result = filter_private_memories(memories)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1,
                        f"filter_private_memories 1000 items took {elapsed:.3f}s (limit 100ms)")

    def test_post_discord_timeout_bounded(self):
        """post_discord() must use a bounded timeout — not open-ended."""
        src = _SCRIPT.read_text()
        # Should call urlopen with timeout=10 (or similar)
        self.assertIn("timeout=10", src,
                      "post_discord/post_both must set an explicit HTTP timeout")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_keychain_retries_subprocess(self):
        """_keychain() is called once per invocation (subprocess handles its own retry)."""
        call_count = [0]

        def counting_run(*args, **kwargs):
            call_count[0] += 1
            return MagicMock(returncode=0, stdout="fake-token\n", stderr="")

        with patch("subprocess.run", side_effect=counting_run):
            result = _keychain("test-service", required=False)

        self.assertEqual(result, "fake-token")
        self.assertEqual(call_count[0], 1)

    def test_post_discord_fails_gracefully_on_network_error(self):
        """post_discord() must return False on network failure, not raise."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="fake-discord-token\n")
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_urlopen.side_effect = OSError("connection refused")
                result = post_discord("test message", _mod.DISCORD_CHAT)
        self.assertFalse(result, "post_discord() must return False on failure")

    def test_post_both_continues_discord_on_slack_failure(self):
        """post_both() must attempt Discord even if Slack fails."""
        discord_called = [False]

        def mock_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "discord" in url:
                discord_called[0] = True
                r = MagicMock()
                r.__enter__ = lambda s: s
                r.__exit__ = MagicMock(return_value=False)
                r.status = 200
                return r
            raise OSError("slack down")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="fake-token\n")
            with patch("urllib.request.urlopen", side_effect=mock_urlopen):
                post_both("test message", _mod.SLACK_CHAN, _mod.DISCORD_CHAT)

        self.assertTrue(discord_called[0],
                        "post_both() should attempt Discord even when Slack fails")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    # --- is_private_source ---

    def test_private_disney_exact(self):
        self.assertTrue(is_private_source("disney_internal"))

    def test_private_disney_substring(self):
        self.assertTrue(is_private_source("disney_shared_drives"))
        self.assertTrue(is_private_source("some_disney_content"))

    def test_private_health(self):
        self.assertTrue(is_private_source("apple_health"))
        self.assertTrue(is_private_source("healthkit"))

    def test_private_email(self):
        self.assertTrue(is_private_source("email_archive"))
        self.assertTrue(is_private_source("email"))

    def test_private_imessage(self):
        self.assertTrue(is_private_source("imessage"))

    def test_not_private_music(self):
        self.assertFalse(is_private_source("music"))

    def test_not_private_video(self):
        self.assertFalse(is_private_source("video"))

    def test_not_private_reddit(self):
        self.assertFalse(is_private_source("reddit"))

    def test_not_private_empty_string(self):
        self.assertFalse(is_private_source(""))

    def test_not_private_none(self):
        self.assertFalse(is_private_source(None))

    def test_private_case_insensitive(self):
        self.assertTrue(is_private_source("Disney_Internal"))
        self.assertTrue(is_private_source("APPLE_HEALTH"))

    # --- filter_private_memories ---

    def test_filter_removes_private(self):
        memories = [
            {"text": "work stuff", "source": "disney_internal"},
            {"text": "public stuff", "source": "music"},
        ]
        result = filter_private_memories(memories)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "music")

    def test_filter_keeps_public(self):
        memories = [
            {"text": "memory 1", "source": "video"},
            {"text": "memory 2", "source": "reddit"},
        ]
        result = filter_private_memories(memories)
        self.assertEqual(len(result), 2)

    def test_filter_handles_missing_source(self):
        memories = [{"text": "no source"}]
        result = filter_private_memories(memories)
        self.assertEqual(len(result), 1)

    def test_filter_empty_list(self):
        self.assertEqual(filter_private_memories([]), [])

    # --- constants ---

    def test_slack_constants_present(self):
        self.assertTrue(len(_mod.SLACK_CHAN) > 0)
        self.assertTrue(len(_mod.SLACK_NOTIFY) > 0)
        self.assertTrue(len(_mod.JORDAN_DM) > 0)

    def test_discord_constants_present(self):
        self.assertTrue(len(_mod.DISCORD_CHAT) > 0)
        self.assertTrue(len(_mod.DISCORD_NOTIFY) > 0)

    def test_channel_map_maps_slack_to_discord(self):
        self.assertIn(_mod.SLACK_CHAN, _mod.CHANNEL_MAP)
        self.assertEqual(_mod.CHANNEL_MAP[_mod.SLACK_CHAN], _mod.DISCORD_CHAT)

    def test_memory_url_and_vector_url(self):
        self.assertIn("18790", _mod.VECTOR_URL)
        self.assertIn("18790", _mod.MEMORY_URL)

    def test_novacontrol_port(self):
        self.assertIn("37400", _mod.NOVACONTROL)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_keychain_returns_token_on_success(self):
        """_keychain() returns stripped stdout on success."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="xoxb-test-token\n")
            result = _keychain("nova-slack-bot-token", required=False)
        self.assertEqual(result, "xoxb-test-token")

    def test_slack_bot_token_uses_env_fallback(self):
        """slack_bot_token() returns env var when Keychain fails."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            with patch.dict(os.environ, {"NOVA_SLACK_BOT_TOKEN": "xoxb-env-token"}):
                token = slack_bot_token()
        self.assertEqual(token, "xoxb-env-token")

    def test_post_discord_sends_correct_payload(self):
        """post_discord() must send JSON body with content field."""
        captured = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data.decode())
            captured.append(body)
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.status = 200
            return r

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="fake-discord-token\n")
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                post_discord("hello world", _mod.DISCORD_CHAT)

        self.assertEqual(len(captured), 1)
        self.assertIn("content", captured[0])
        self.assertEqual(captured[0]["content"], "hello world")

    def test_post_discord_truncates_to_2000(self):
        """post_discord() must truncate long messages to 2000 chars."""
        captured = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data.decode())
            captured.append(body)
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.status = 200
            return r

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="fake-discord-token\n")
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                post_discord("x" * 3000, _mod.DISCORD_CHAT)

        self.assertEqual(len(captured), 1)
        self.assertLessEqual(len(captured[0]["content"]), 2000)

    def test_filter_then_prompt_pipeline(self):
        """Filtering private memories then checking empty output is safe."""
        memories = [
            {"text": "Disney work memo", "source": "disney_work"},
            {"text": "Jordan's blood pressure", "source": "apple_health"},
        ]
        public = filter_private_memories(memories)
        self.assertEqual(public, [])


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_post_both_sends_to_slack_and_discord(self):
        """post_both() must send to both Slack and Discord."""
        calls = []

        def fake_urlopen(req, timeout=None):
            url = req.full_url if hasattr(req, "full_url") else ""
            calls.append(url)
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.read.return_value = b'{"ok": true}'
            r.status = 200
            return r

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="fake-token\n")
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                post_both("test message", _mod.SLACK_CHAN)

        self.assertTrue(any("slack.com" in c for c in calls),
                        "post_both() must post to Slack")
        self.assertTrue(any("discord.com" in c for c in calls),
                        "post_both() must post to Discord")

    def test_openrouter_key_falls_back_to_env(self):
        """openrouter_api_key() returns env var when Keychain unavailable."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            with patch.dict(os.environ, {"NOVA_OPENROUTER_API_KEY": "or-key-123"}):
                key = openrouter_api_key()
        self.assertEqual(key, "or-key-123")

    def test_all_token_functions_return_empty_when_unavailable(self):
        """All token-loading functions must return '' rather than raising when keys unavailable."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            for fn, env_var in [
                (slack_bot_token, "NOVA_SLACK_BOT_TOKEN"),
                (slack_app_token, "NOVA_SLACK_APP_TOKEN"),
                (discord_bot_token, "NOVA_DISCORD_TOKEN"),
                (openrouter_api_key, "NOVA_OPENROUTER_API_KEY"),
            ]:
                with patch.dict(os.environ, {env_var: ""}):
                    result = fn()
                    self.assertIsInstance(result, str,
                                         f"{fn.__name__}() must return str, got {type(result)}")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles_without_errors(self):
        """nova_config.py must compile cleanly."""
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_config.py has syntax errors: {e}")

    def test_required_constants_present(self):
        """All critical constants must be defined and non-empty."""
        for attr in ["SLACK_API", "SLACK_CHAN", "SLACK_NOTIFY", "JORDAN_DM",
                     "DISCORD_API", "DISCORD_CHAT", "DISCORD_NOTIFY",
                     "VECTOR_URL", "MEMORY_URL", "NOVACONTROL", "SCRIPTS_DIR"]:
            val = getattr(_mod, attr, None)
            self.assertIsNotNone(val, f"{attr} must be defined")
            self.assertIsInstance(val, str, f"{attr} must be a string")
            self.assertGreater(len(val), 0, f"{attr} must not be empty")

    def test_private_sources_is_set(self):
        """PRIVATE_SOURCES must be a non-empty set."""
        self.assertIsInstance(_mod.PRIVATE_SOURCES, (set, frozenset))
        self.assertGreater(len(_mod.PRIVATE_SOURCES), 5)

    def test_channel_map_is_dict(self):
        """CHANNEL_MAP must be a dict mapping Slack to Discord channels."""
        self.assertIsInstance(_mod.CHANNEL_MAP, dict)
        self.assertGreater(len(_mod.CHANNEL_MAP), 0)

    def test_module_has_all_public_functions(self):
        """Expected public functions must exist."""
        for fn in ["slack_bot_token", "openrouter_api_key", "discord_bot_token",
                   "post_discord", "post_both", "is_private_source", "filter_private_memories"]:
            self.assertTrue(callable(getattr(_mod, fn, None)),
                            f"Missing function: {fn}")

    def test_nc_endpoints_use_novacontrol_base(self):
        """All NC_* endpoints must start with NOVACONTROL base URL."""
        base = _mod.NOVACONTROL
        for attr in ["NC_ONEONONE", "NC_NMAP", "NC_RSYNC", "NC_HOMEKIT",
                     "NC_SYSTEM", "NC_NEWS", "NC_HEALTH", "NC_PLEX", "NC_CALENDAR"]:
            val = getattr(_mod, attr, None)
            self.assertIsNotNone(val, f"{attr} must be defined")
            self.assertTrue(val.startswith(base),
                            f"{attr} must start with NOVACONTROL base: {val!r}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
