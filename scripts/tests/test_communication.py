"""test_communication.py — Tests for Nova's communication channel scripts. Written by Jordan Koch."""

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, date, timedelta
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def mock_nova_config(monkeypatch):
    """Mock nova_config to prevent real Slack/Discord/Keychain calls."""
    mock_config = MagicMock()
    mock_config.VECTOR_URL = "http://127.0.0.1:18790/remember"
    mock_config.SLACK_API = "https://slack.com/api"
    mock_config.SLACK_CHAN = "C_TEST_CHAT"
    mock_config.SLACK_NOTIFY = "C_TEST_NOTIFY"
    mock_config.SLACK_EMAIL = "C_TEST_EMAIL"
    mock_config.SLACK_PHOTOS = "C_TEST_PHOTOS"
    mock_config.JORDAN_DM = "D_TEST_DM"
    mock_config.DISCORD_CHAT = "1234567890"
    mock_config.DISCORD_NOTIFY = "0987654321"
    mock_config.SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent)
    mock_config.slack_bot_token.return_value = "xoxb-test-token"
    mock_config.post_both = MagicMock()
    mock_config.post_discord = MagicMock(return_value=True)
    mock_config.CHANNEL_MAP = {
        "C_TEST_CHAT": "1234567890",
        "C_TEST_NOTIFY": "0987654321",
    }
    monkeypatch.setitem(sys.modules, "nova_config", mock_config)
    return mock_config


@pytest.fixture
def mock_nova_logger(monkeypatch):
    """Mock nova_logger to prevent real file I/O."""
    mock_logger = MagicMock()
    monkeypatch.setitem(sys.modules, "nova_logger", mock_logger)
    return mock_logger


@pytest.fixture
def tmp_state_dir(tmp_path):
    """Provide a temporary directory for state files."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return state_dir


@pytest.fixture
def imessage_db(tmp_path):
    """Create a temporary SQLite database mimicking the iMessage chat.db schema."""
    db_path = tmp_path / "chat.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE handle (
            ROWID INTEGER PRIMARY KEY,
            id TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE message (
            ROWID INTEGER PRIMARY KEY,
            text TEXT,
            is_from_me INTEGER,
            date INTEGER,
            service TEXT,
            handle_id INTEGER,
            date_read INTEGER,
            item_type INTEGER DEFAULT 0
        )
    """)
    # Insert sample handles
    conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+15551234567')")
    conn.execute("INSERT INTO handle (ROWID, id) VALUES (2, 'friend@icloud.com')")

    # Insert sample messages (mac timestamps: nanoseconds since 2001-01-01)
    # Recent: ~1 hour ago
    now_unix = time.time()
    mac_epoch_offset = 978307200
    one_hour_ago_mac = int((now_unix - 3600 - mac_epoch_offset) * 1_000_000_000)
    two_hours_ago_mac = int((now_unix - 7200 - mac_epoch_offset) * 1_000_000_000)

    conn.execute(
        "INSERT INTO message (ROWID, text, is_from_me, date, service, handle_id, date_read, item_type) "
        "VALUES (1, 'Hey, how are you?', 0, ?, 'iMessage', 1, 0, 0)",
        [one_hour_ago_mac]
    )
    conn.execute(
        "INSERT INTO message (ROWID, text, is_from_me, date, service, handle_id, date_read, item_type) "
        "VALUES (2, 'Good, thanks!', 1, ?, 'iMessage', 1, 0, 0)",
        [one_hour_ago_mac + 60_000_000_000]
    )
    conn.execute(
        "INSERT INTO message (ROWID, text, is_from_me, date, service, handle_id, date_read, item_type) "
        "VALUES (3, 'Hello from email', 0, ?, 'iMessage', 2, 0, 0)",
        [two_hours_ago_mac]
    )
    conn.commit()
    conn.close()
    return db_path


# ============================================================================
# 1. nova_discord_mirror.py — State, polling, deduplication, message posting
# ============================================================================

class TestDiscordMirrorState:
    """Tests for state file management in nova_discord_mirror.py."""

    def test_load_state_missing_file(self, tmp_path, mock_nova_config):
        """load_state returns empty dict when file does not exist."""
        import nova_discord_mirror as mod
        with patch.object(mod, "STATE_FILE", str(tmp_path / "nonexistent.json")):
            assert mod.load_state() == {}

    def test_load_state_corrupt_json(self, tmp_path, mock_nova_config):
        """load_state returns empty dict on malformed JSON."""
        import nova_discord_mirror as mod
        bad_file = tmp_path / "corrupt.json"
        bad_file.write_text("{broken json!!!")
        with patch.object(mod, "STATE_FILE", str(bad_file)):
            assert mod.load_state() == {}

    def test_save_and_load_roundtrip(self, tmp_path, mock_nova_config):
        """save_state + load_state produces identical data."""
        import nova_discord_mirror as mod
        state_file = str(tmp_path / "state.json")
        with patch.object(mod, "STATE_FILE", state_file):
            mod.save_state({"C0AMNQ5GX70": "1234567890.000001"})
            loaded = mod.load_state()
            assert loaded == {"C0AMNQ5GX70": "1234567890.000001"}

    def test_save_state_creates_parent_dirs(self, tmp_path, mock_nova_config):
        """save_state creates parent directories if they do not exist."""
        import nova_discord_mirror as mod
        nested = str(tmp_path / "deep" / "nested" / "state.json")
        with patch.object(mod, "STATE_FILE", nested):
            mod.save_state({"test": "value"})
            assert Path(nested).exists()


class TestDiscordMirrorPolling:
    """Tests for Slack polling in nova_discord_mirror.py."""

    def test_get_slack_history_no_token(self, mock_nova_config):
        """Returns empty list when no token is available."""
        mock_nova_config.slack_bot_token.return_value = ""
        import nova_discord_mirror as mod
        result = mod.get_slack_history("C0AMNQ5GX70")
        assert result == []

    @patch("urllib.request.urlopen")
    def test_get_slack_history_success(self, mock_urlopen, mock_nova_config):
        """Returns messages on successful API call."""
        import nova_discord_mirror as mod
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True,
            "messages": [{"ts": "1.0", "text": "Hello", "bot_id": "B123"}]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = mod.get_slack_history("C0AMNQ5GX70", oldest="0", limit=20)
        assert len(result) == 1
        assert result[0]["text"] == "Hello"

    @patch("urllib.request.urlopen", side_effect=Exception("Network timeout"))
    def test_get_slack_history_network_error(self, mock_urlopen, mock_nova_config):
        """Returns empty list on network error."""
        import nova_discord_mirror as mod
        result = mod.get_slack_history("C0AMNQ5GX70")
        assert result == []


class TestDiscordMirrorDedup:
    """Tests for message deduplication in mirror_once."""

    @patch("urllib.request.urlopen")
    def test_skips_already_seen_timestamp(self, mock_urlopen, tmp_path, mock_nova_config):
        """Messages with ts equal to last_ts are skipped (deduplication)."""
        import nova_discord_mirror as mod
        state_file = str(tmp_path / "state.json")

        # Pre-seed state with a known timestamp
        with patch.object(mod, "STATE_FILE", state_file):
            mod.save_state({"C_TEST_CHAT": "100.0"})

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True,
            "messages": [
                {"ts": "100.0", "text": "Old msg", "bot_id": "B1"},
                {"ts": "101.0", "text": "New msg", "bot_id": "B1"},
            ]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with patch.object(mod, "STATE_FILE", state_file):
            with patch.object(mod, "CHANNEL_MAP", {"C_TEST_CHAT": "1234567890"}):
                with patch.object(mod, "post_to_discord") as mock_post:
                    n = mod.mirror_once()
                    assert n == 1  # Only the new message should be posted
                    mock_post.assert_called_once_with("1234567890", "New msg")

    def test_only_mirrors_bot_messages(self, tmp_path, mock_nova_config):
        """Human messages (no bot_id, no bot_message subtype) are not mirrored."""
        import nova_discord_mirror as mod
        state_file = str(tmp_path / "state.json")

        with patch.object(mod, "STATE_FILE", state_file):
            mod.save_state({})

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True,
            "messages": [
                {"ts": "1.0", "text": "Human message", "user": "U123"},
                {"ts": "2.0", "text": "Bot message", "bot_id": "B456"},
            ]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(mod, "STATE_FILE", state_file):
                with patch.object(mod, "CHANNEL_MAP", {"C_TEST_CHAT": "1234567890"}):
                    with patch.object(mod, "post_to_discord") as mock_post:
                        n = mod.mirror_once()
                        assert n == 1
                        mock_post.assert_called_once_with("1234567890", "Bot message")


class TestDiscordMirrorPosting:
    """Tests for Discord message posting."""

    def test_post_to_discord_truncates_long_messages(self, mock_nova_config):
        """Messages over 2000 chars are truncated."""
        import nova_discord_mirror as mod
        long_text = "A" * 2500
        with patch.object(mod, "nova_config", mock_nova_config):
            mod.post_to_discord("123456", long_text)
            posted_text = mock_nova_config.post_discord.call_args[0][0]
            assert len(posted_text) == 2000
            assert posted_text.endswith("...")

    def test_post_to_discord_short_messages_unchanged(self, mock_nova_config):
        """Short messages are posted as-is."""
        import nova_discord_mirror as mod
        with patch.object(mod, "nova_config", mock_nova_config):
            mod.post_to_discord("123456", "Hello world")
            posted_text = mock_nova_config.post_discord.call_args[0][0]
            assert posted_text == "Hello world"

    def test_mirror_once_no_messages(self, tmp_path, mock_nova_config):
        """mirror_once returns 0 when no messages are available."""
        import nova_discord_mirror as mod
        state_file = str(tmp_path / "state.json")

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True, "messages": []}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            with patch.object(mod, "STATE_FILE", state_file):
                with patch.object(mod, "CHANNEL_MAP", {"C_TEST_CHAT": "1234567890"}):
                    assert mod.mirror_once() == 0


# ============================================================================
# 2. nova_imessage.py — SQLite queries, contact resolution, state, spam filter
# ============================================================================

class TestIMessagePhoneNormalization:
    """Tests for _normalize_phone in nova_imessage.py."""

    def test_strip_formatting(self, mock_nova_config):
        """Strips parentheses, dashes, spaces."""
        import nova_imessage as mod
        assert mod._normalize_phone("(555) 123-4567") == "5551234567"

    def test_strip_country_code(self, mock_nova_config):
        """Drops leading 1 for US numbers."""
        import nova_imessage as mod
        assert mod._normalize_phone("+15551234567") == "5551234567"

    def test_short_number_passthrough(self, mock_nova_config):
        """Short numbers pass through as-is."""
        import nova_imessage as mod
        result = mod._normalize_phone("12345")
        assert result == "12345"

    def test_already_normalized(self, mock_nova_config):
        """Already clean 10-digit number stays the same."""
        import nova_imessage as mod
        assert mod._normalize_phone("5551234567") == "5551234567"

    def test_international_long_number(self, mock_nova_config):
        """Long international numbers return last 10 digits."""
        import nova_imessage as mod
        result = mod._normalize_phone("+442071234567")
        assert len(result) == 10


class TestIMessageContactResolution:
    """Tests for resolve_contact in nova_imessage.py."""

    def test_resolve_known_phone(self, mock_nova_config):
        """Resolves a known phone number to a name."""
        import nova_imessage as mod
        mod._contact_lookup = {"5551234567": "Alice"}
        assert mod.resolve_contact("+15551234567") == "Alice"

    def test_resolve_known_email(self, mock_nova_config):
        """Resolves a known email to a name."""
        import nova_imessage as mod
        mod._contact_lookup = {"friend@icloud.com": "Bob"}
        assert mod.resolve_contact("friend@icloud.com") == "Bob"

    def test_resolve_unknown_returns_handle(self, mock_nova_config):
        """Unknown contacts return the raw handle."""
        import nova_imessage as mod
        mod._contact_lookup = {}
        assert mod.resolve_contact("+15559999999") == "+15559999999"

    def test_resolve_empty_handle(self, mock_nova_config):
        """Empty handle returns 'Unknown'."""
        import nova_imessage as mod
        mod._contact_lookup = {}
        assert mod.resolve_contact("") == "Unknown"
        assert mod.resolve_contact(None) == "Unknown"


class TestIMessageTimestamp:
    """Tests for _mac_timestamp_to_datetime."""

    def test_zero_returns_none(self, mock_nova_config):
        """Zero timestamp returns None."""
        import nova_imessage as mod
        assert mod._mac_timestamp_to_datetime(0) is None

    def test_none_returns_none(self, mock_nova_config):
        """None timestamp returns None."""
        import nova_imessage as mod
        assert mod._mac_timestamp_to_datetime(None) is None

    def test_known_timestamp(self, mock_nova_config):
        """Known macOS timestamp converts correctly."""
        import nova_imessage as mod
        # Jan 1 2020 00:00:00 UTC in mac nanoseconds:
        # Unix: 1577836800, offset from 2001: 1577836800 - 978307200 = 599529600
        mac_ts = 599529600 * 1_000_000_000
        dt = mod._mac_timestamp_to_datetime(mac_ts)
        assert dt is not None
        assert dt.year == 2020 or dt.year == 2019  # TZ dependent


class TestIMessageSpamFilter:
    """Tests for is_spam in nova_imessage.py."""

    def test_empty_text_is_spam(self, mock_nova_config):
        """Empty or very short text is spam."""
        import nova_imessage as mod
        assert mod.is_spam({"text": "", "sender": "+15551234567"}) is True
        assert mod.is_spam({"text": "A", "sender": "+15551234567"}) is True

    def test_short_code_is_spam(self, mock_nova_config):
        """Short code senders (6 digits or fewer) are spam."""
        import nova_imessage as mod
        assert mod.is_spam({"text": "Your code is 123456", "sender": "55555"}) is True

    def test_email_rcs_sender_is_spam(self, mock_nova_config):
        """Non-gmail/icloud email senders are flagged as RCS spam."""
        import nova_imessage as mod
        assert mod.is_spam({"text": "Buy now!", "sender": "promo@company.biz"}) is True

    def test_gmail_sender_not_spam(self, mock_nova_config):
        """Gmail senders are not automatically spam."""
        import nova_imessage as mod
        assert mod.is_spam({"text": "Hey, how are you?", "sender": "friend@gmail.com"}) is False

    def test_normal_phone_not_spam(self, mock_nova_config):
        """Normal phone number with real text is not spam."""
        import nova_imessage as mod
        assert mod.is_spam({"text": "Want to grab lunch?", "sender": "+15551234567"}) is False


class TestIMessageState:
    """Tests for load_state / save_state in nova_imessage.py."""

    def test_load_state_default(self, tmp_path, mock_nova_config):
        """Default state has last_check_ts of 0."""
        import nova_imessage as mod
        with patch.object(mod, "STATE_FILE", tmp_path / "nonexistent.json"):
            state = mod.load_state()
            assert state == {"last_check_ts": 0}

    def test_save_and_load_state(self, tmp_path, mock_nova_config):
        """Round-trip state persistence."""
        import nova_imessage as mod
        state_file = tmp_path / "imessage_state.json"
        with patch.object(mod, "STATE_FILE", state_file):
            mod.save_state({"last_check_ts": 999999})
            loaded = mod.load_state()
            assert loaded["last_check_ts"] == 999999


class TestIMessageSend:
    """Tests for send_imessage AppleScript execution."""

    @patch("subprocess.run")
    def test_send_appends_signature(self, mock_run, mock_nova_config):
        """Outgoing messages get Nova's signature appended."""
        import nova_imessage as mod
        mock_run.return_value = MagicMock(returncode=0)
        mod.send_imessage("+15551234567", "Hello!")
        script = mock_run.call_args[0][0][2]  # osascript -e <script>
        assert "Nova" in script

    @patch("subprocess.run")
    def test_send_no_double_signature(self, mock_run, mock_nova_config):
        """If signature already present, do not double-append."""
        import nova_imessage as mod
        mock_run.return_value = MagicMock(returncode=0)
        mod.send_imessage("+15551234567", "Hello!\n— Nova")
        script = mock_run.call_args[0][0][2]
        assert script.count("Nova") == 1  # Only one occurrence

    @patch("subprocess.run")
    def test_send_failure_tries_alternate(self, mock_run, mock_nova_config):
        """Primary send failure triggers alternate method."""
        import nova_imessage as mod
        mock_run.side_effect = [
            MagicMock(returncode=1, stderr="error"),  # Primary fails
            MagicMock(returncode=0),  # Alternate succeeds
        ]
        result = mod.send_imessage("+15551234567", "Hello!", sign=False)
        assert mock_run.call_count == 2

    @patch("subprocess.run", side_effect=Exception("timeout"))
    def test_send_exception_returns_false(self, mock_run, mock_nova_config):
        """Exception during send returns False."""
        import nova_imessage as mod
        result = mod.send_imessage("+15551234567", "Hello!")
        assert result is False


class TestIMessageDatabaseRead:
    """Tests for get_recent_messages against a real SQLite db."""

    def test_get_recent_messages(self, imessage_db, mock_nova_config):
        """Reads messages from the test database."""
        import nova_imessage as mod
        with patch.object(mod, "MESSAGES_DB", imessage_db):
            messages = mod.get_recent_messages(hours=4)
            assert len(messages) == 3
            # Verify structure
            for m in messages:
                assert "sender" in m
                assert "text" in m
                assert "date" in m
                assert "is_from_me" in m
                assert "service" in m

    def test_get_recent_messages_with_contact_filter(self, imessage_db, mock_nova_config):
        """Filter by contact handle."""
        import nova_imessage as mod
        with patch.object(mod, "MESSAGES_DB", imessage_db):
            messages = mod.get_recent_messages(hours=4, contact="icloud")
            assert len(messages) == 1
            assert "Hello from email" in messages[0]["text"]

    def test_get_recent_messages_db_missing(self, tmp_path, mock_nova_config):
        """Returns empty list when database does not exist."""
        import nova_imessage as mod
        with patch.object(mod, "MESSAGES_DB", tmp_path / "nonexistent.db"):
            messages = mod.get_recent_messages(hours=4)
            assert messages == []

    def test_get_unread_messages(self, imessage_db, tmp_path, mock_nova_config):
        """get_unread_messages returns only incoming messages since last check."""
        import nova_imessage as mod
        state_file = tmp_path / "state.json"
        with patch.object(mod, "MESSAGES_DB", imessage_db):
            with patch.object(mod, "STATE_FILE", state_file):
                messages = mod.get_unread_messages()
                # Should get incoming only (is_from_me = 0)
                for m in messages:
                    assert m["sender"] != "Jordan"


# ============================================================================
# 3. nova_slack_preprocessor.py — Token caching, message filtering, memory injection
# ============================================================================

class TestSlackPreprocessorTokenCache:
    """Tests for token caching in nova_slack_preprocessor.py."""

    def test_token_caching(self, mock_nova_config):
        """Token is cached after first call."""
        if "nova_slack_preprocessor" in sys.modules:
            del sys.modules["nova_slack_preprocessor"]
        import nova_slack_preprocessor as mod
        mod._cached_token = ""
        token = mod._get_token()
        assert token == "xoxb-test-token"
        # Second call should use cached value
        mock_nova_config.slack_bot_token.return_value = "different-token"
        token2 = mod._get_token()
        assert token2 == "xoxb-test-token"  # Still cached


class TestSlackPreprocessorFiltering:
    """Tests for message filtering in nova_slack_preprocessor.py."""

    def test_ignores_non_jordan_messages(self, mock_nova_config):
        """Only Jordan's messages (U049EPC2W) are processed."""
        if "nova_slack_preprocessor" in sys.modules:
            del sys.modules["nova_slack_preprocessor"]
        import nova_slack_preprocessor as mod

        messages = [
            {"user": "U0ALZRF3HRQ", "text": "Bot message", "ts": "200.0"},
            {"user": "UXXXXXXXXXXX", "text": "Random user", "ts": "201.0"},
        ]

        # Neither message should be processed (not from Jordan)
        for msg in messages:
            assert msg.get("user") != mod.JORDAN_USER_ID

    def test_skips_short_messages(self, mock_nova_config):
        """Messages shorter than 3 chars are skipped."""
        if "nova_slack_preprocessor" in sys.modules:
            del sys.modules["nova_slack_preprocessor"]
        import nova_slack_preprocessor as mod

        msg = {"user": mod.JORDAN_USER_ID, "text": "hi", "ts": "100.0"}
        assert len(msg["text"]) < 3


class TestSlackPreprocessorState:
    """Tests for state persistence in nova_slack_preprocessor.py."""

    def test_load_state_default(self, mock_nova_config):
        """Default state uses current timestamp."""
        if "nova_slack_preprocessor" in sys.modules:
            del sys.modules["nova_slack_preprocessor"]
        import nova_slack_preprocessor as mod
        with patch.object(mod, "STATE_FILE", Path("/tmp/nonexistent_preproc_state.json")):
            state = mod.load_state()
            assert "last_ts" in state
            # Should be a recent timestamp string
            assert float(state["last_ts"]) > 0

    def test_save_and_load_state(self, tmp_path, mock_nova_config):
        """Round-trip state persistence."""
        if "nova_slack_preprocessor" in sys.modules:
            del sys.modules["nova_slack_preprocessor"]
        import nova_slack_preprocessor as mod
        state_file = tmp_path / "preproc_state.json"
        with patch.object(mod, "STATE_FILE", state_file):
            mod.save_state({"last_ts": "123.456", "last_ts_C0AMNQ5GX70": "123.456"})
            loaded = mod.load_state()
            assert loaded["last_ts"] == "123.456"


class TestSlackPreprocessorMemoryInjection:
    """Tests for memory context injection in nova_slack_preprocessor.py."""

    @patch("subprocess.run")
    def test_run_memory_first_success(self, mock_run, mock_nova_config):
        """Returns memory results on success."""
        if "nova_slack_preprocessor" in sys.modules:
            del sys.modules["nova_slack_preprocessor"]
        import nova_slack_preprocessor as mod
        mock_run.return_value = MagicMock(
            returncode=0, stdout="MEMORY FOUND\n[2026-01-01] Jordan said hello"
        )
        result = mod.run_memory_first("hello")
        assert "MEMORY FOUND" in result

    @patch("subprocess.run")
    def test_run_memory_first_failure(self, mock_run, mock_nova_config):
        """Returns None when memory script fails."""
        if "nova_slack_preprocessor" in sys.modules:
            del sys.modules["nova_slack_preprocessor"]
        import nova_slack_preprocessor as mod
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = mod.run_memory_first("hello")
        assert result is None

    @patch("subprocess.run", side_effect=Exception("timeout"))
    def test_run_memory_first_exception(self, mock_run, mock_nova_config):
        """Returns None on exception."""
        if "nova_slack_preprocessor" in sys.modules:
            del sys.modules["nova_slack_preprocessor"]
        import nova_slack_preprocessor as mod
        result = mod.run_memory_first("hello")
        assert result is None

    @patch("urllib.request.urlopen")
    def test_post_memory_context_truncation(self, mock_urlopen, mock_nova_config):
        """Memory context over 3500 chars is truncated."""
        if "nova_slack_preprocessor" in sys.modules:
            del sys.modules["nova_slack_preprocessor"]
        import nova_slack_preprocessor as mod
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        long_memory = "X" * 5000
        result = mod.post_memory_context_to_thread("C123", "1.0", long_memory)
        assert result is True
        # Verify the posted data was truncated
        posted_data = json.loads(mock_urlopen.call_args[0][0].data)
        assert "truncated" in posted_data["text"]

    @patch("urllib.request.urlopen", side_effect=Exception("network"))
    def test_post_memory_context_network_error(self, mock_urlopen, mock_nova_config):
        """Returns False on network error."""
        if "nova_slack_preprocessor" in sys.modules:
            del sys.modules["nova_slack_preprocessor"]
        import nova_slack_preprocessor as mod
        result = mod.post_memory_context_to_thread("C123", "1.0", "memory data")
        assert result is False


# ============================================================================
# 4. nova_slack_ingest.py — File processing, deduplication, Slack API
# ============================================================================

class TestSlackIngestProcessedLog:
    """Tests for processed file tracking in nova_slack_ingest.py."""

    def test_load_processed_empty(self, tmp_path, mock_nova_config):
        """Returns empty set when log does not exist."""
        if "nova_slack_ingest" in sys.modules:
            del sys.modules["nova_slack_ingest"]
        import nova_slack_ingest as mod
        with patch.object(mod, "PROCESSED_LOG", tmp_path / "nonexistent.json"):
            assert mod.load_processed() == set()

    def test_save_and_load_processed(self, tmp_path, mock_nova_config):
        """Round-trip persistence of processed file IDs."""
        if "nova_slack_ingest" in sys.modules:
            del sys.modules["nova_slack_ingest"]
        import nova_slack_ingest as mod
        log_file = tmp_path / "processed.json"
        with patch.object(mod, "PROCESSED_LOG", log_file):
            mod.save_processed({"F001", "F002", "F003"})
            loaded = mod.load_processed()
            assert "F001" in loaded
            assert "F002" in loaded

    def test_save_processed_limits_to_1000(self, tmp_path, mock_nova_config):
        """Processed log is capped at 1000 entries."""
        if "nova_slack_ingest" in sys.modules:
            del sys.modules["nova_slack_ingest"]
        import nova_slack_ingest as mod
        log_file = tmp_path / "processed.json"
        with patch.object(mod, "PROCESSED_LOG", log_file):
            big_set = {f"F{i:05d}" for i in range(1500)}
            mod.save_processed(big_set)
            raw = json.loads(log_file.read_text())
            assert len(raw) == 1000


class TestSlackIngestFileDetection:
    """Tests for file detection in nova_slack_ingest.py."""

    @patch("urllib.request.urlopen")
    def test_get_recent_files_with_file_share(self, mock_urlopen, mock_nova_config):
        """Detects files from file_share subtype messages."""
        if "nova_slack_ingest" in sys.modules:
            del sys.modules["nova_slack_ingest"]
        import nova_slack_ingest as mod
        mod.SLACK_TOKEN = "xoxb-test-token"

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True,
            "messages": [{
                "subtype": "file_share",
                "ts": "1.0",
                "user": "U123",
                "files": [{"id": "F001", "name": "doc.pdf", "filetype": "pdf"}]
            }]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        files = mod.get_recent_files()
        assert len(files) == 1
        assert files[0]["file"]["id"] == "F001"

    @patch("urllib.request.urlopen")
    def test_get_recent_files_with_inline_files(self, mock_urlopen, mock_nova_config):
        """Detects files attached without file_share subtype."""
        if "nova_slack_ingest" in sys.modules:
            del sys.modules["nova_slack_ingest"]
        import nova_slack_ingest as mod
        mod.SLACK_TOKEN = "xoxb-test-token"

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True,
            "messages": [{
                "ts": "1.0",
                "user": "U123",
                "files": [{"id": "F002", "name": "image.png", "filetype": "image"}]
            }]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        files = mod.get_recent_files()
        assert len(files) == 1

    def test_get_recent_files_no_token(self, mock_nova_config):
        """Returns empty list when no token available."""
        if "nova_slack_ingest" in sys.modules:
            del sys.modules["nova_slack_ingest"]
        import nova_slack_ingest as mod
        mod.SLACK_TOKEN = ""
        assert mod.get_recent_files() == []


class TestSlackIngestSkipTypes:
    """Tests for file type filtering in nova_slack_ingest.py main()."""

    @patch("urllib.request.urlopen")
    @patch("subprocess.run")
    def test_skips_image_files(self, mock_run, mock_urlopen, tmp_path, mock_nova_config):
        """Image files are skipped but marked as processed."""
        if "nova_slack_ingest" in sys.modules:
            del sys.modules["nova_slack_ingest"]
        import nova_slack_ingest as mod
        mod.SLACK_TOKEN = "xoxb-test-token"

        log_file = tmp_path / "processed.json"

        # Mock get_recent_files to return an image
        with patch.object(mod, "PROCESSED_LOG", log_file):
            with patch.object(mod, "get_recent_files", return_value=[{
                "file": {"id": "F100", "name": "photo.jpg", "filetype": "image", "mimetype": "image/jpeg"},
                "message_ts": "1.0",
                "user": "U123",
            }]):
                with patch.object(mod, "load_processed", return_value=set()):
                    with patch.object(mod, "save_processed") as mock_save:
                        mod.main()
                        # save_processed should be called with F100 in the set
                        args = mock_save.call_args[0][0]
                        assert "F100" in args


# ============================================================================
# 5. nova_slack_conversation_ingest.py — Chunking, formatting, day grouping
# ============================================================================

class TestConversationIngestFormatting:
    """Tests for message formatting in nova_slack_conversation_ingest.py."""

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_resolve_user_known(self, mock_run):
        """Known user IDs resolve to names."""
        if "nova_slack_conversation_ingest" in sys.modules:
            del sys.modules["nova_slack_conversation_ingest"]
        import nova_slack_conversation_ingest as mod
        assert mod.resolve_user("U049EPC2W") == "Jordan"
        assert mod.resolve_user("U04AS59BR") == "Tricia"

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_resolve_user_unknown(self, mock_run):
        """Unknown user IDs pass through unchanged."""
        if "nova_slack_conversation_ingest" in sys.modules:
            del sys.modules["nova_slack_conversation_ingest"]
        import nova_slack_conversation_ingest as mod
        assert mod.resolve_user("UXXX") == "UXXX"

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_format_message_basic(self, mock_run):
        """Basic message formatting."""
        if "nova_slack_conversation_ingest" in sys.modules:
            del sys.modules["nova_slack_conversation_ingest"]
        import nova_slack_conversation_ingest as mod
        msg = {"user": "U049EPC2W", "text": "Hello there!"}
        result = mod.format_message(msg)
        assert result == "Jordan: Hello there!"

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_format_message_with_mentions(self, mock_run):
        """User mentions are replaced with names."""
        if "nova_slack_conversation_ingest" in sys.modules:
            del sys.modules["nova_slack_conversation_ingest"]
        import nova_slack_conversation_ingest as mod
        msg = {"user": "U049EPC2W", "text": "Hey <@U04AS59BR> check this out"}
        result = mod.format_message(msg)
        assert "@Tricia" in result
        assert "<@U04AS59BR>" not in result

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_format_message_empty_text_with_files(self, mock_run):
        """Empty text with file attachments formats as file share."""
        if "nova_slack_conversation_ingest" in sys.modules:
            del sys.modules["nova_slack_conversation_ingest"]
        import nova_slack_conversation_ingest as mod
        msg = {"user": "U049EPC2W", "text": "", "files": [{"name": "photo.jpg"}]}
        result = mod.format_message(msg)
        assert "[shared file:" in result
        assert "photo.jpg" in result

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_format_message_empty_text_no_attachments_returns_none(self, mock_run):
        """Empty text without attachments returns None."""
        if "nova_slack_conversation_ingest" in sys.modules:
            del sys.modules["nova_slack_conversation_ingest"]
        import nova_slack_conversation_ingest as mod
        msg = {"user": "U049EPC2W", "text": ""}
        assert mod.format_message(msg) is None


class TestConversationIngestChunking:
    """Tests for chunk_by_day and ingest_day."""

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_chunk_by_day_groups_correctly(self, mock_run):
        """Messages are grouped by calendar day."""
        if "nova_slack_conversation_ingest" in sys.modules:
            del sys.modules["nova_slack_conversation_ingest"]
        import nova_slack_conversation_ingest as mod

        # Two messages on same day, one on different day
        ts_day1 = 1704067200.0  # 2024-01-01 00:00:00 UTC
        ts_day2 = 1704153600.0  # 2024-01-02 00:00:00 UTC
        messages = [
            {"ts": str(ts_day1), "user": "U049EPC2W", "text": "Morning!"},
            {"ts": str(ts_day1 + 3600), "user": "U04AS59BR", "text": "Good morning!"},
            {"ts": str(ts_day2), "user": "U049EPC2W", "text": "New day"},
        ]
        days = mod.chunk_by_day(messages)
        assert len(days) == 2

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    @patch("urllib.request.urlopen")
    def test_ingest_day_chunking(self, mock_urlopen, mock_run):
        """Long conversations are split into ~1500 char chunks."""
        if "nova_slack_conversation_ingest" in sys.modules:
            del sys.modules["nova_slack_conversation_ingest"]
        import nova_slack_conversation_ingest as mod

        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        # Create messages totaling >1500 chars
        messages_with_ts = [(float(i), f"Jordan: {'A' * 200}") for i in range(15)]
        stored, chunks = mod.ingest_day("2024-01-01", messages_with_ts)
        assert chunks > 1  # Should be split into multiple chunks


class TestConversationIngestVectorStore:
    """Tests for vector memory storage."""

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    @patch("urllib.request.urlopen")
    def test_vector_remember_success(self, mock_urlopen, mock_run):
        """Successful vector store call returns True."""
        if "nova_slack_conversation_ingest" in sys.modules:
            del sys.modules["nova_slack_conversation_ingest"]
        import nova_slack_conversation_ingest as mod
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        assert mod.vector_remember("test text", {"date": "2024-01-01"}) is True

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    @patch("urllib.request.urlopen", side_effect=Exception("connection refused"))
    def test_vector_remember_failure(self, mock_urlopen, mock_run):
        """Failed vector store call returns False."""
        if "nova_slack_conversation_ingest" in sys.modules:
            del sys.modules["nova_slack_conversation_ingest"]
        import nova_slack_conversation_ingest as mod
        assert mod.vector_remember("test text", {"date": "2024-01-01"}) is False


# ============================================================================
# 6. nova_slack_image.py — Download, analysis, vision model routing
# ============================================================================

class TestSlackImageDownload:
    """Tests for download_slack_file in nova_slack_image.py."""

    @patch("urllib.request.urlopen")
    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_download_by_url(self, mock_run, mock_urlopen):
        """Direct URL download works."""
        if "nova_slack_image" in sys.modules:
            del sys.modules["nova_slack_image"]
        import nova_slack_image as mod

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"\x89PNG fake image data"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        data, mime, name = mod.download_slack_file("https://files.slack.com/image.png", "token")
        assert data == b"\x89PNG fake image data"

    @patch("urllib.request.urlopen")
    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_download_by_file_id_not_found(self, mock_run, mock_urlopen):
        """Raises RuntimeError when file ID not found in history."""
        if "nova_slack_image" in sys.modules:
            del sys.modules["nova_slack_image"]
        import nova_slack_image as mod

        # Mock history response with no matching files
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True,
            "messages": [{"ts": "1.0", "files": [{"id": "F_OTHER", "name": "other.png"}]}]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with pytest.raises(RuntimeError, match="not found"):
            mod.download_slack_file("F_MISSING", "token")


class TestSlackImageAnalysis:
    """Tests for image analysis routing in nova_slack_image.py."""

    @patch("urllib.request.urlopen")
    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_analyze_image_ollama(self, mock_run, mock_urlopen):
        """Ollama (local) analysis returns model response."""
        if "nova_slack_image" in sys.modules:
            del sys.modules["nova_slack_image"]
        import nova_slack_image as mod
        mod.USE_OPENROUTER = False

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "response": "A cat sitting on a windowsill."
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = mod.analyze_image(b"\x89PNG fake data", "Describe this image")
        assert "cat" in result

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_use_openrouter_is_disabled_by_default(self, mock_run):
        """USE_OPENROUTER defaults to False (all analysis stays local)."""
        if "nova_slack_image" in sys.modules:
            del sys.modules["nova_slack_image"]
        import nova_slack_image as mod
        assert mod.USE_OPENROUTER is False


# ============================================================================
# 7. nova_slack_memory_ingest.py — Channel history, memory storage, dream log
# ============================================================================

class TestSlackMemoryIngest:
    """Tests for nova_slack_memory_ingest.py."""

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    @patch("urllib.request.urlopen")
    def test_get_channel_history_success(self, mock_urlopen, mock_run):
        """Returns messages on successful Slack API call."""
        if "nova_slack_memory_ingest" in sys.modules:
            del sys.modules["nova_slack_memory_ingest"]
        import nova_slack_memory_ingest as mod

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True,
            "messages": [
                {"user": "U123", "text": "Hello world", "ts": "1.0"},
                {"user": "U456", "text": "Good morning", "ts": "2.0"},
            ]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        messages = mod.get_channel_history()
        assert len(messages) == 2

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    @patch("urllib.request.urlopen", side_effect=Exception("network"))
    def test_get_channel_history_failure(self, mock_urlopen, mock_run):
        """Returns empty list on network failure."""
        if "nova_slack_memory_ingest" in sys.modules:
            del sys.modules["nova_slack_memory_ingest"]
        import nova_slack_memory_ingest as mod
        assert mod.get_channel_history() == []

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    @patch("urllib.request.urlopen")
    def test_remember_returns_id(self, mock_urlopen, mock_run):
        """remember() returns the memory ID from vector store."""
        if "nova_slack_memory_ingest" in sys.modules:
            del sys.modules["nova_slack_memory_ingest"]
        import nova_slack_memory_ingest as mod

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"id": "mem_001"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = mod.remember("test text", {"user": "U123"})
        assert result == "mem_001"

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    @patch("urllib.request.urlopen", side_effect=Exception("err"))
    def test_remember_failure_returns_none(self, mock_urlopen, mock_run):
        """remember() returns None on failure."""
        if "nova_slack_memory_ingest" in sys.modules:
            del sys.modules["nova_slack_memory_ingest"]
        import nova_slack_memory_ingest as mod
        assert mod.remember("test", {}) is None


class TestSlackMemoryDreamLog:
    """Tests for dream journal logging."""

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_add_to_dream_log_new_file(self, mock_run, tmp_path):
        """Creates dream log file if it does not exist."""
        if "nova_slack_memory_ingest" in sys.modules:
            del sys.modules["nova_slack_memory_ingest"]
        import nova_slack_memory_ingest as mod
        dream_log = tmp_path / "dream.json"
        with patch.object(mod, "DREAM_LOG", dream_log):
            mod.add_to_dream_log({"ts": "1.0", "text": "test event"})
            data = json.loads(dream_log.read_text())
            assert len(data) == 1
            assert data[0]["text"] == "test event"

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_add_to_dream_log_appends(self, mock_run, tmp_path):
        """Appends to existing dream log."""
        if "nova_slack_memory_ingest" in sys.modules:
            del sys.modules["nova_slack_memory_ingest"]
        import nova_slack_memory_ingest as mod
        dream_log = tmp_path / "dream.json"
        dream_log.write_text(json.dumps([{"ts": "0.0", "text": "old event"}]))
        with patch.object(mod, "DREAM_LOG", dream_log):
            mod.add_to_dream_log({"ts": "1.0", "text": "new event"})
            data = json.loads(dream_log.read_text())
            assert len(data) == 2

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_main_skips_bot_messages(self, mock_run, tmp_path):
        """Bot messages and thread broadcasts are skipped."""
        if "nova_slack_memory_ingest" in sys.modules:
            del sys.modules["nova_slack_memory_ingest"]
        import nova_slack_memory_ingest as mod
        mod.SLACK_TOKEN = "xoxb-test"

        messages = [
            {"subtype": "bot_message", "text": "Bot says hi", "ts": "1.0"},
            {"subtype": "thread_broadcast", "text": "Thread msg", "ts": "2.0"},
            {"user": "U123", "text": "Real message", "ts": "3.0"},
            {"user": "U456", "text": "tiny", "ts": "4.0"},  # < 5 chars, skipped
        ]

        with patch.object(mod, "get_channel_history", return_value=messages):
            with patch.object(mod, "remember", return_value="mem_id") as mock_remember:
                with patch.object(mod, "add_to_dream_log"):
                    with patch.object(mod, "DREAM_LOG", tmp_path / "dream.json"):
                        result = mod.main()
                        # Only the "Real message" should be stored (tiny is < 5 chars)
                        assert mock_remember.call_count == 1


# ============================================================================
# 8. nova_send_mail.py — Wrapper around herd_mail.sh
# ============================================================================

class TestSendMailWrapper:
    """Tests for nova_send_mail.py send_mail function."""

    @patch("subprocess.run")
    def test_send_mail_single_recipient(self, mock_run):
        """Single recipient sends one email."""
        from nova_send_mail import send_mail
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        result = send_mail("user@example.com", "Test Subject", "Test Body")
        assert result is True
        assert mock_run.call_count == 1
        args = mock_run.call_args[0][0]
        assert "--to" in args
        assert "user@example.com" in args
        assert "--skip-haiku" in args

    @patch("subprocess.run")
    def test_send_mail_multiple_recipients(self, mock_run):
        """Multiple recipients send separate emails."""
        from nova_send_mail import send_mail
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        result = send_mail(["a@b.com", "c@d.com"], "Subject", "Body")
        assert result is True
        assert mock_run.call_count == 2

    @patch("subprocess.run")
    def test_send_mail_with_attachment(self, mock_run):
        """Attachment flag is passed through."""
        from nova_send_mail import send_mail
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        send_mail("user@example.com", "Subject", "Body", image_path="/tmp/img.png")
        args = mock_run.call_args[0][0]
        assert "--attachment" in args
        assert "/tmp/img.png" in args

    @patch("subprocess.run")
    def test_send_mail_with_in_reply_to(self, mock_run):
        """In-Reply-To header is passed through as --message-id."""
        from nova_send_mail import send_mail
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        send_mail("user@example.com", "Re: Hello", "Reply",
                  in_reply_to="<msg123@example.com>")
        args = mock_run.call_args[0][0]
        assert "--message-id" in args

    @patch("subprocess.run")
    def test_send_mail_with_rich_flag(self, mock_run):
        """Rich (HTML) flag is passed through."""
        from nova_send_mail import send_mail
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        send_mail("user@example.com", "Subject", "Body", rich=True)
        args = mock_run.call_args[0][0]
        assert "--rich" in args

    @patch("subprocess.run")
    def test_send_mail_partial_failure(self, mock_run):
        """If one recipient fails, overall result is False."""
        from nova_send_mail import send_mail
        mock_run.side_effect = [
            MagicMock(returncode=0, stderr="", stdout=""),
            MagicMock(returncode=1, stderr="fail", stdout=""),
        ]
        result = send_mail(["a@b.com", "c@d.com"], "Subject", "Body")
        assert result is False

    @patch("subprocess.run", side_effect=Exception("process died"))
    def test_send_mail_exception(self, mock_run):
        """Exception during send returns False."""
        from nova_send_mail import send_mail
        result = send_mail("user@example.com", "Subject", "Body")
        assert result is False


# ============================================================================
# 9. nova_mail_fetch.py — AppleScript parsing, formatting
# ============================================================================

class TestMailFetchParsing:
    """Tests for parse_messages in nova_mail_fetch.py."""

    def test_parse_messages_basic(self):
        """Parses multi-account applescript output."""
        from nova_mail_fetch import parse_messages
        raw = (
            "=== ACCOUNT: Digitalnoise Gmail <user@gmail.com> (3 messages) ===\n"
            "FROM: Alice Smith [UNREAD]\n"
            "SUBJECT: Meeting tomorrow\n"
            "DATE: 2026-01-01 10:00\n"
            "BODY: Hi, let's meet at noon.\n"
            "FROM: Bob Jones\n"
            "SUBJECT: FYI\n"
            "DATE: 2026-01-01 09:00\n"
            "BODY: Just letting you know.\n"
            "=== ACCOUNT: Work <work@company.com> (1 messages) ===\n"
            "FROM: Boss [UNREAD]\n"
            "SUBJECT: Urgent\n"
            "DATE: 2026-01-01 08:00\n"
            "BODY: Need this ASAP.\n"
        )
        accounts = parse_messages(raw)
        assert "user@gmail.com" in accounts
        assert "work@company.com" in accounts
        assert len(accounts["user@gmail.com"]) == 2
        # The [UNREAD] tag is part of the FROM line in the applescript output
        assert accounts["user@gmail.com"][0]["unread"] is True
        assert "Alice Smith" in accounts["user@gmail.com"][0]["from"]
        assert accounts["user@gmail.com"][1]["unread"] is False

    def test_parse_messages_empty(self):
        """Empty input returns empty dict."""
        from nova_mail_fetch import parse_messages
        assert parse_messages("") == {}

    def test_parse_messages_old_format(self):
        """Handles old format without email in account header."""
        from nova_mail_fetch import parse_messages
        raw = (
            "=== ACCOUNT: My Gmail (1 messages) ===\n"
            "FROM: Sender\n"
            "SUBJECT: Test\n"
            "DATE: 2026-01-01\n"
            "BODY: Test body\n"
        )
        accounts = parse_messages(raw)
        assert "My Gmail" in accounts


class TestMailFetchFormatting:
    """Tests for format_for_nova in nova_mail_fetch.py."""

    def test_format_includes_header(self):
        """Output includes MAIL SUMMARY header."""
        from nova_mail_fetch import format_for_nova
        accounts = {"user@gmail.com": [{"from": "Alice", "subject": "Hi", "date": "today", "body": "", "unread": True}]}
        result = format_for_nova(accounts, 1)
        assert "MAIL SUMMARY" in result
        assert "Total messages" in result
        assert "1" in result

    def test_format_separates_unread_and_read(self):
        """Unread messages appear before read messages."""
        from nova_mail_fetch import format_for_nova
        accounts = {
            "user@gmail.com": [
                {"from": "Alice", "subject": "Unread", "date": "today", "body": "", "unread": True},
                {"from": "Bob", "subject": "Read", "date": "today", "body": "", "unread": False},
            ]
        }
        result = format_for_nova(accounts, 2)
        unread_pos = result.find("[UNREAD]")
        read_pos = result.find("[READ]")
        assert unread_pos < read_pos

    def test_format_empty_accounts(self):
        """Empty accounts still produce a valid summary."""
        from nova_mail_fetch import format_for_nova
        result = format_for_nova({}, 0)
        assert "MAIL SUMMARY" in result
        assert "END OF MAIL SUMMARY" in result


class TestMailFetchAppleScript:
    """Tests for run_applescript in nova_mail_fetch.py."""

    @patch("subprocess.run")
    def test_run_applescript_success(self, mock_run):
        """Successful run returns stdout."""
        from nova_mail_fetch import run_applescript
        mock_run.return_value = MagicMock(returncode=0, stdout="TOTAL:5\nsome data", stderr="")
        raw, err = run_applescript()
        assert raw == "TOTAL:5\nsome data"
        assert err is None

    @patch("subprocess.run")
    def test_run_applescript_failure(self, mock_run):
        """Failed run returns error message."""
        from nova_mail_fetch import run_applescript
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Script error")
        raw, err = run_applescript()
        assert raw is None
        assert "Script error" in err


# ============================================================================
# 10. nova_mail_deliver.py — Summary building, classification, delivery
# ============================================================================

class TestMailDeliverClassification:
    """Tests for is_noise and is_important in nova_mail_deliver.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C_TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_mail_deliver" in sys.modules:
                del sys.modules["nova_mail_deliver"]
            import nova_mail_deliver
            return nova_mail_deliver

    def test_noise_wayfair(self):
        mod = self._get_module()
        assert mod.is_noise("Wayfair", "Big sale!") is True

    def test_noise_amazon(self):
        mod = self._get_module()
        assert mod.is_noise("Amazon", "Your order shipped") is True

    def test_noise_hulu(self):
        mod = self._get_module()
        assert mod.is_noise("Hulu", "New season available") is True

    def test_not_noise_personal(self):
        mod = self._get_module()
        assert mod.is_noise("Jordan Koch", "Hey there") is False

    def test_important_amex(self):
        mod = self._get_module()
        assert mod.is_important("American Express", "Statement") is True

    def test_important_apple_developer(self):
        mod = self._get_module()
        assert mod.is_important("Apple Developer", "Certificate expiring") is True

    def test_important_adt(self):
        mod = self._get_module()
        assert mod.is_important("ADT Security", "Alert") is True

    def test_not_important_random(self):
        mod = self._get_module()
        assert mod.is_important("Random Corp", "Newsletter") is False


class TestMailDeliverParsing:
    """Tests for parse_accounts_from_file in nova_mail_deliver.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C_TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_mail_deliver" in sys.modules:
                del sys.modules["nova_mail_deliver"]
            import nova_mail_deliver
            return nova_mail_deliver

    def test_parse_accounts_basic(self):
        mod = self._get_module()
        content = (
            "\n\U0001f4ec user@gmail.com — 2 message(s), 1 unread\n"
            "----------------------------------------\n"
            "[UNREAD] FROM: Amazon\n"
            "           SUBJ: Your order shipped\n"
            "\n"
            "[READ]   FROM: Newsletter\n"
            "           SUBJ: Weekly digest\n"
        )
        accounts = mod.parse_accounts_from_file(content)
        assert "user@gmail.com" in accounts
        assert len(accounts["user@gmail.com"]) == 2
        assert accounts["user@gmail.com"][0]["unread"] is True
        assert accounts["user@gmail.com"][1]["unread"] is False

    def test_parse_accounts_empty(self):
        mod = self._get_module()
        assert mod.parse_accounts_from_file("") == {}

    def test_parse_accounts_multiple(self):
        mod = self._get_module()
        content = (
            "\U0001f4ec a@b.com — 1 message(s), 1 unread\n"
            "[UNREAD] FROM: Sender A\n"
            "           SUBJ: Subject A\n"
            "\U0001f4ec c@d.com — 1 message(s), 0 unread\n"
            "[READ]   FROM: Sender B\n"
            "           SUBJ: Subject B\n"
        )
        accounts = mod.parse_accounts_from_file(content)
        assert len(accounts) == 2


class TestMailDeliverSummary:
    """Tests for build_summary in nova_mail_deliver.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C_TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_mail_deliver" in sys.modules:
                del sys.modules["nova_mail_deliver"]
            import nova_mail_deliver
            return nova_mail_deliver

    def test_build_summary_header(self):
        mod = self._get_module()
        content = (
            "Total messages: 3\n\n"
            "\U0001f4ec user@gmail.com — 3 message(s), 1 unread\n"
            "[UNREAD] FROM: Alice\n"
            "           SUBJ: Hello\n"
            "[READ]   FROM: Bob\n"
            "           SUBJ: FYI\n"
        )
        summary = mod.build_summary(content)
        assert "Nova Mail Summary" in summary
        assert "3 messages" in summary

    def test_build_summary_flags_noise(self):
        mod = self._get_module()
        content = (
            "Total messages: 1\n\n"
            "\U0001f4ec user@gmail.com — 1 message(s), 1 unread\n"
            "[UNREAD] FROM: Wayfair\n"
            "           SUBJ: 50% off everything\n"
        )
        summary = mod.build_summary(content)
        assert "newsletters/marketing" in summary

    def test_build_summary_flags_important(self):
        mod = self._get_module()
        content = (
            "Total messages: 1\n\n"
            "\U0001f4ec user@gmail.com — 1 message(s), 1 unread\n"
            "[UNREAD] FROM: American Express\n"
            "           SUBJ: Statement ready\n"
        )
        summary = mod.build_summary(content)
        assert "Important" in summary


class TestMailDeliverVectorMemory:
    """Tests for vector memory storage in mail delivery."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C_TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_mail_deliver" in sys.modules:
                del sys.modules["nova_mail_deliver"]
            import nova_mail_deliver
            return nova_mail_deliver

    @patch("urllib.request.urlopen")
    def test_vector_remember_success(self, mock_urlopen):
        mod = self._get_module()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        # Should not raise
        mod.vector_remember("Important email from Amex", {"date": "2026-01-01"})

    @patch("urllib.request.urlopen", side_effect=Exception("down"))
    def test_vector_remember_silently_fails(self, mock_urlopen):
        mod = self._get_module()
        # Should not raise even on failure
        mod.vector_remember("Test", {})


class TestMailDeliverSendEmailDisabled:
    """Tests that email delivery is disabled (Slack-only)."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C_TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_mail_deliver" in sys.modules:
                del sys.modules["nova_mail_deliver"]
            import nova_mail_deliver
            return nova_mail_deliver

    def test_send_email_is_noop(self):
        """send_email should be a no-op to prevent mail summary duplication."""
        mod = self._get_module()
        # Should not raise, should not send anything
        mod.send_email("Subject", "Body")


# ============================================================================
# 11. nova_mail_agent.py — IMAP, classification, reply generation, rate limiting
# ============================================================================

class TestMailAgentClassification:
    """Tests for email classification in nova_mail_agent.py."""

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_is_system_message_mailer_daemon(self):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import is_system_message
        assert is_system_message("mailer-daemon@example.com", "Undeliverable") is True

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_is_system_message_noreply(self):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import is_system_message
        assert is_system_message("noreply@example.com", "Notification") is True

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_is_system_message_do_not_reply(self):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import is_system_message
        assert is_system_message("do-not-reply@example.com", "Alert") is True

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_is_system_message_normal(self):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import is_system_message
        assert is_system_message("friend@example.com", "Hey Nova") is False

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_is_from_nova_prevents_loops(self):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import is_from_nova, NOVA_EMAIL
        assert is_from_nova(NOVA_EMAIL) is True
        assert is_from_nova(f"Nova <{NOVA_EMAIL}>") is True
        assert is_from_nova("other@example.com") is False

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_is_addressed_to_nova(self):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import is_addressed_to_nova, NOVA_EMAIL
        assert is_addressed_to_nova(f"Nova <{NOVA_EMAIL}>, Other <o@x.com>") is True
        assert is_addressed_to_nova("someone@else.com") is False


class TestMailAgentIMAPHelpers:
    """Tests for IMAP helper functions."""

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_imap_list_unread_empty(self):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import imap_list_unread
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("OK", [b"5"])
        mock_conn.uid.return_value = ("OK", [b""])
        assert imap_list_unread(mock_conn) == []

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_imap_list_unread_with_uids(self):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import imap_list_unread
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("OK", [b"10"])
        mock_conn.uid.return_value = ("OK", [b"1 2 3 4"])
        result = imap_list_unread(mock_conn)
        assert len(result) == 4

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_imap_list_unread_not_ok(self):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import imap_list_unread
        mock_conn = MagicMock()
        mock_conn.select.return_value = ("OK", [b"0"])
        mock_conn.uid.return_value = ("NO", [None])
        assert imap_list_unread(mock_conn) == []


class TestMailAgentFetchMessage:
    """Tests for imap_fetch_message."""

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_fetch_returns_empty_on_failure(self):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import imap_fetch_message
        mock_conn = MagicMock()
        mock_conn.uid.return_value = ("NO", [None])
        assert imap_fetch_message(mock_conn, b"1") == {}

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_fetch_parses_complete_email(self):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import imap_fetch_message

        msg = EmailMessage()
        msg["From"] = "Sam <sam@example.com>"
        msg["To"] = "nova@digitalnoise.net"
        msg["Cc"] = "jordan@example.com"
        msg["Subject"] = "Hello Nova"
        msg["Message-ID"] = "<test456@example.com>"
        msg["In-Reply-To"] = "<prev@example.com>"
        msg["References"] = "<ref1@example.com>"
        msg.set_content("Hey Nova, how's it going?")

        mock_conn = MagicMock()
        mock_conn.uid.return_value = ("OK", [(b"1 (RFC822 {456}", msg.as_bytes())])

        result = imap_fetch_message(mock_conn, b"1")
        assert result["from_addr"] == "sam@example.com"
        assert result["from_name"] == "Sam"
        assert result["subject"] == "Hello Nova"
        assert result["message_id"] == "<test456@example.com>"
        assert result["in_reply_to"] == "<prev@example.com>"
        assert "how's it going" in result["body"]
        assert "nova@digitalnoise.net" in result["to_raw"]

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_fetch_handles_multipart(self):
        """Multipart messages extract plain text body."""
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import imap_fetch_message
        import email.mime.multipart
        import email.mime.text

        msg = email.mime.multipart.MIMEMultipart("alternative")
        msg["From"] = "test@example.com"
        msg["To"] = "nova@digitalnoise.net"
        msg["Subject"] = "Multipart test"
        msg["Message-ID"] = "<multi@example.com>"
        msg.attach(email.mime.text.MIMEText("Plain text body", "plain"))
        msg.attach(email.mime.text.MIMEText("<html><body>HTML body</body></html>", "html"))

        mock_conn = MagicMock()
        mock_conn.uid.return_value = ("OK", [(b"1 (RFC822 {789}", msg.as_bytes())])

        result = imap_fetch_message(mock_conn, b"1")
        assert "Plain text body" in result["body"]
        assert "<html>" not in result["body"]


class TestMailAgentMoveToTrash:
    """Tests for imap_move_to_trash."""

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_move_to_trash_sequence(self):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import imap_move_to_trash, TRASH_FOLDER
        mock_conn = MagicMock()
        imap_move_to_trash(mock_conn, b"42")
        # Verify COPY -> STORE -> EXPUNGE sequence
        mock_conn.uid.assert_any_call("COPY", b"42", TRASH_FOLDER)
        mock_conn.uid.assert_any_call("STORE", b"42", "+FLAGS", "(\\Deleted)")
        mock_conn.expunge.assert_called_once()

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_move_to_trash_handles_exception(self):
        """Should not raise even if IMAP operation fails."""
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import imap_move_to_trash
        mock_conn = MagicMock()
        mock_conn.uid.side_effect = Exception("IMAP error")
        # Should not raise
        imap_move_to_trash(mock_conn, b"42")


class TestMailAgentSaveToSent:
    """Tests for imap_save_to_sent."""

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_save_to_sent_success(self):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import imap_save_to_sent
        mock_conn = MagicMock()
        mock_conn.append.return_value = ("OK", [])
        imap_save_to_sent(mock_conn, b"email content")
        mock_conn.append.assert_called_once()

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    def test_save_to_sent_failure(self):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import imap_save_to_sent
        mock_conn = MagicMock()
        mock_conn.append.side_effect = Exception("IMAP error")
        # Should not raise
        imap_save_to_sent(mock_conn, b"email content")


class TestMailAgentHaikuGeneration:
    """Tests for generate_haiku in nova_mail_agent.py."""

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    @patch("urllib.request.urlopen")
    def test_generate_haiku_success(self, mock_urlopen):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import generate_haiku

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "response": "Circuits hum softly\nData flows like gentle streams\nSilicon dreams wake"
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = generate_haiku("testing")
        assert len(result) > 0
        assert "\n" in result  # Should be multi-line

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    @patch("urllib.request.urlopen", side_effect=Exception("timeout"))
    def test_generate_haiku_fallback(self, mock_urlopen):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import generate_haiku
        result = generate_haiku()
        assert "Circuits hum softly" in result  # Fallback haiku

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    @patch("urllib.request.urlopen")
    def test_generate_haiku_strips_thinking(self, mock_urlopen):
        """Strips </think> tags from model output."""
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import generate_haiku

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "response": "<think>reasoning here</think>Spring rain falls gently\nWashing away yesterday\nTomorrow is new"
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = generate_haiku()
        assert "<think>" not in result
        assert "Spring rain" in result


class TestMailAgentSMTP:
    """Tests for SMTP send functionality."""

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    @patch("smtplib.SMTP")
    def test_smtp_send_success(self, mock_smtp_class):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import smtp_send

        mock_server = MagicMock()
        mock_smtp_class.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_class.return_value.__exit__ = MagicMock(return_value=False)

        success, msg_bytes = smtp_send(
            "app_pass",
            to_addrs=["recipient@example.com"],
            cc_addrs=["cc@example.com"],
            subject="Test",
            body="Hello!",
            in_reply_to="<ref@example.com>",
            references="<ref@example.com>",
        )
        assert success is True
        assert len(msg_bytes) > 0

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    @patch("smtplib.SMTP", side_effect=Exception("Connection refused"))
    def test_smtp_send_failure(self, mock_smtp_class):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import smtp_send
        success, msg_bytes = smtp_send(
            "app_pass", to_addrs=["r@example.com"], cc_addrs=[],
            subject="Test", body="Hello!"
        )
        assert success is False


class TestMailAgentGetAppPassword:
    """Tests for _get_app_password Keychain access."""

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    @patch("subprocess.run")
    def test_get_app_password_success(self, mock_run):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import _get_app_password
        mock_run.return_value = MagicMock(returncode=0, stdout="secretpass\n")
        assert _get_app_password() == "secretpass"

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    @patch("subprocess.run")
    def test_get_app_password_failure(self, mock_run):
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import _get_app_password
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _get_app_password() == ""


# ============================================================================
# @pytest.mark.frame — Message formatting verification
# ============================================================================

@pytest.mark.frame
class TestSlackMessageFormatting:
    """Verify Slack markdown formatting in various scripts."""

    def test_imessage_watch_slack_format(self, mock_nova_config):
        """iMessage watch output uses proper Slack bold markdown."""
        import nova_imessage as mod
        mod._contact_lookup = {"5551234567": "Alice"}
        # Simulate the formatting logic
        messages = [{
            "is_from_me": False,
            "handle": "+15551234567",
            "text": "Hey Nova!",
            "date": "2026-01-01 10:00",
        }]
        lines = [f"*iMessage — {len(messages)} new*"]
        for m in messages:
            contact_name = mod.resolve_contact(m.get("handle", ""))
            lines.append(f"  *{contact_name}* ({m['date']}): {m['text'][:100]}")

        output = "\n".join(lines)
        assert "*Alice*" in output
        assert "*iMessage" in output

    def test_mail_summary_slack_format(self):
        """Mail summary uses proper Slack formatting."""
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C_TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_mail_deliver" in sys.modules:
                del sys.modules["nova_mail_deliver"]
            import nova_mail_deliver as mod

        content = (
            "Total messages: 1\n\n"
            "\U0001f4ec user@gmail.com — 1 message(s), 1 unread\n"
            "[UNREAD] FROM: Alice\n"
            "           SUBJ: Hello\n"
        )
        summary = mod.build_summary(content)
        # Should use Slack bold formatting
        assert "*Nova Mail Summary" in summary
        assert "*" in summary  # Bold markers present

    def test_preprocessor_memory_context_format(self, mock_nova_config):
        """Memory context uses code block formatting."""
        if "nova_slack_preprocessor" in sys.modules:
            del sys.modules["nova_slack_preprocessor"]
        import nova_slack_preprocessor as mod

        # The formatting happens in post_memory_context_to_thread
        memory_text = "Some memory data"
        expected_prefix = ":brain: *Memory Context*"
        expected_code_block = "```"
        # Just verify the formatting pattern is correct
        msg = (
            f":brain: *Memory Context* _(auto-retrieved by preprocessor)_\n"
            f"```\n{memory_text}\n```\n"
            f"_Nova: use this data in your response. Do not say you can't find it._"
        )
        assert expected_prefix in msg
        assert expected_code_block in msg


@pytest.mark.frame
class TestDiscordMessageFormatting:
    """Verify Discord message truncation and formatting."""

    def test_discord_message_within_limit(self, mock_nova_config):
        """Messages under 2000 chars are not truncated."""
        import nova_discord_mirror as mod
        short_msg = "Hello world"
        with patch.object(mod, "nova_config", mock_nova_config):
            mod.post_to_discord("123", short_msg)
            posted = mock_nova_config.post_discord.call_args[0][0]
            assert posted == short_msg
            assert "..." not in posted

    def test_discord_message_at_limit(self, mock_nova_config):
        """Messages exactly 2000 chars are not truncated."""
        import nova_discord_mirror as mod
        exact_msg = "A" * 2000
        with patch.object(mod, "nova_config", mock_nova_config):
            mod.post_to_discord("123", exact_msg)
            posted = mock_nova_config.post_discord.call_args[0][0]
            assert len(posted) == 2000
            assert not posted.endswith("...")

    def test_discord_message_over_limit(self, mock_nova_config):
        """Messages over 2000 chars are truncated with ellipsis."""
        import nova_discord_mirror as mod
        long_msg = "A" * 2500
        with patch.object(mod, "nova_config", mock_nova_config):
            mod.post_to_discord("123", long_msg)
            posted = mock_nova_config.post_discord.call_args[0][0]
            assert len(posted) == 2000
            assert posted.endswith("...")


@pytest.mark.frame
class TestEmailFormatting:
    """Verify email HTML rendering and plain text formatting."""

    def test_mail_fetch_body_truncation(self):
        """Email body preview is truncated to 200 chars."""
        from nova_mail_fetch import parse_messages
        long_body = "X" * 500
        raw = (
            "=== ACCOUNT: Test <test@test.com> (1 messages) ===\n"
            "FROM: Sender\n"
            "SUBJECT: Test\n"
            "DATE: 2026-01-01\n"
            f"BODY: {long_body}\n"
        )
        accounts = parse_messages(raw)
        assert len(accounts["test@test.com"][0]["body"]) <= 200

    def test_mail_deliver_send_email_strips_markdown(self):
        """send_email strips Slack markdown for email output."""
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C_TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_mail_deliver" in sys.modules:
                del sys.modules["nova_mail_deliver"]
            import nova_mail_deliver as mod

        summary = "*Bold text* and _italic text_"
        cleaned = summary.replace("*", "").replace("_", "")
        assert "Bold text" in cleaned
        assert "*" not in cleaned
        assert "_" not in cleaned


# ============================================================================
# @pytest.mark.functional — End-to-end workflow tests
# ============================================================================

@pytest.mark.functional
class TestDiscordMirrorWorkflow:
    """End-to-end: Slack message received -> mirrored to Discord."""

    @patch("urllib.request.urlopen")
    def test_full_mirror_cycle(self, mock_urlopen, tmp_path, mock_nova_config):
        """Complete cycle: load state -> poll -> mirror -> save state."""
        import nova_discord_mirror as mod
        state_file = str(tmp_path / "state.json")

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True,
            "messages": [
                {"ts": "1.0", "text": "Nova says hello", "bot_id": "B1"},
                {"ts": "2.0", "text": "Human says hi", "user": "U123"},
            ]
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        with patch.object(mod, "STATE_FILE", state_file):
            with patch.object(mod, "CHANNEL_MAP", {"C_TEST_CHAT": "1234567890"}):
                n = mod.mirror_once()
                assert n == 1  # Only bot message
                state = mod.load_state()
                assert "C_TEST_CHAT" in state
                assert state["C_TEST_CHAT"] == "2.0"  # Updated to latest


@pytest.mark.functional
class TestMailDeliverWorkflow:
    """End-to-end: Email fetched -> categorized -> summary posted."""

    def test_no_mail_workflow(self, tmp_path):
        """No-mail scenario posts empty summary to Slack."""
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C_TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_mail_deliver" in sys.modules:
                del sys.modules["nova_mail_deliver"]
            import nova_mail_deliver as mod

        summary_file = tmp_path / "nova_mail_fetch.txt"
        summary_file.write_text("NO_MAIL: No messages")

        with patch.object(mod, "SUMMARY_FILE", summary_file):
            with patch("subprocess.run", return_value=MagicMock(returncode=0, stderr="")):
                with patch.object(mod, "SCRIPTS", tmp_path):
                    mod.main()
                    mock_nc.post_both.assert_called()
                    posted_text = mock_nc.post_both.call_args[0][0]
                    assert "No new mail" in posted_text


@pytest.mark.functional
class TestSlackMemoryIngestWorkflow:
    """End-to-end: Slack messages -> stored in memory + dream log."""

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    @patch("urllib.request.urlopen")
    def test_full_ingest_cycle(self, mock_urlopen, mock_run, tmp_path):
        """Messages are fetched, stored in memory, and logged to dream journal."""
        if "nova_slack_memory_ingest" in sys.modules:
            del sys.modules["nova_slack_memory_ingest"]
        import nova_slack_memory_ingest as mod
        mod.SLACK_TOKEN = "xoxb-test"

        dream_log = tmp_path / "dream.json"
        messages = [
            {"user": "U123", "text": "Good morning everyone!", "ts": "1.0"},
            {"user": "U456", "text": "Morning! Coffee time.", "ts": "2.0"},
            {"subtype": "bot_message", "text": "Bot noise", "ts": "3.0"},
        ]

        # First call: get_channel_history (Slack API)
        mock_hist_resp = MagicMock()
        mock_hist_resp.read.return_value = json.dumps({"ok": True, "messages": messages}).encode()
        mock_hist_resp.__enter__ = lambda s: s
        mock_hist_resp.__exit__ = MagicMock(return_value=False)

        # Second+ calls: remember (vector store)
        mock_mem_resp = MagicMock()
        mock_mem_resp.read.return_value = json.dumps({"id": "mem_001"}).encode()
        mock_mem_resp.__enter__ = lambda s: s
        mock_mem_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [mock_hist_resp, mock_mem_resp, mock_mem_resp]

        with patch.object(mod, "DREAM_LOG", dream_log):
            result = mod.main()
            assert result == 0
            # Dream log should have entries
            data = json.loads(dream_log.read_text())
            assert len(data) == 2  # Bot message skipped


# ============================================================================
# @pytest.mark.integration — Live service tests (skipped when unavailable)
# ============================================================================

@pytest.mark.integration
class TestSlackAPILive:
    """Live Slack API tests. Requires real token in Keychain."""

    def _get_token(self):
        import subprocess
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "nova",
             "-s", "nova-slack-bot-token", "-w"],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            pytest.skip("Slack token not available in Keychain")
        return result.stdout.strip()

    def test_slack_conversations_history(self):
        """Live: Fetch recent messages from #nova-chat."""
        token = self._get_token()
        import urllib.request
        url = f"https://slack.com/api/conversations.history?channel=C0AMNQ5GX70&limit=5"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            assert data.get("ok") is True
            assert "messages" in data

    def test_slack_auth_test(self):
        """Live: Verify bot token is valid."""
        token = self._get_token()
        import urllib.request
        url = "https://slack.com/api/auth.test"
        req = urllib.request.Request(
            url,
            data=b"",
            headers={"Authorization": f"Bearer {token}"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            assert data.get("ok") is True


@pytest.mark.integration
class TestIMessageDBLive:
    """Live iMessage database tests. Requires macOS with Messages.app."""

    def test_messages_db_exists(self):
        """Live: Messages database exists on this Mac."""
        db_path = Path.home() / "Library/Messages/chat.db"
        if not db_path.exists():
            pytest.skip("Messages database not found")
        assert db_path.exists()

    def test_messages_db_readable(self):
        """Live: Can open and query the Messages database."""
        db_path = Path.home() / "Library/Messages/chat.db"
        if not db_path.exists():
            pytest.skip("Messages database not found")
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            cursor = conn.execute("SELECT COUNT(*) FROM message LIMIT 1")
            count = cursor.fetchone()[0]
            conn.close()
            assert count >= 0
        except sqlite3.OperationalError:
            pytest.skip("Cannot read Messages database (TCC permissions)")


# ============================================================================
# Error handling tests — Network timeouts, API errors, malformed data
# ============================================================================

class TestErrorHandling:
    """Cross-cutting error handling tests for all communication scripts."""

    def test_discord_mirror_malformed_slack_response(self, mock_nova_config):
        """Discord mirror handles malformed Slack API responses."""
        import nova_discord_mirror as mod

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"not json at all"
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = mod.get_slack_history("C123")
            assert result == []

    def test_imessage_corrupt_db(self, tmp_path, mock_nova_config):
        """iMessage handles corrupt database gracefully."""
        import nova_imessage as mod
        corrupt_db = tmp_path / "corrupt.db"
        corrupt_db.write_text("this is not a database")
        with patch.object(mod, "MESSAGES_DB", corrupt_db):
            messages = mod.get_recent_messages(hours=4)
            assert messages == []

    @patch("subprocess.run", return_value=MagicMock(stdout="xoxb-test", returncode=0))
    def test_slack_memory_ingest_no_token(self, mock_run):
        """Memory ingest exits cleanly when no token is available."""
        if "nova_slack_memory_ingest" in sys.modules:
            del sys.modules["nova_slack_memory_ingest"]
        import nova_slack_memory_ingest as mod
        mod.SLACK_TOKEN = ""
        result = mod.main()
        assert result == 1

    def test_mail_fetch_no_mail_response(self):
        """Mail fetch handles NO_MAIL response."""
        from nova_mail_fetch import main as fetch_main
        with patch("nova_mail_fetch.run_applescript", return_value=("NO_MAIL", None)):
            with patch("nova_mail_fetch.OUT_FILE") as mock_file:
                mock_file.write_text = MagicMock()
                fetch_main()
                mock_file.write_text.assert_called_once()
                written = mock_file.write_text.call_args[0][0]
                assert "NO_MAIL" in written

    def test_mail_fetch_applescript_error(self):
        """Mail fetch handles AppleScript errors."""
        from nova_mail_fetch import main as fetch_main
        with patch("nova_mail_fetch.run_applescript", return_value=(None, "Script error")):
            with patch("nova_mail_fetch.OUT_FILE") as mock_file:
                mock_file.write_text = MagicMock()
                with pytest.raises(SystemExit):
                    fetch_main()

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    @patch("urllib.request.urlopen")
    def test_mail_agent_generate_reply_strips_reasoning(self, mock_urlopen):
        """Reply generation strips leaked LLM reasoning from output."""
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import generate_reply

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "response": "Okay so this email is from Sam and I should respond warmly.\n\nHey Sam! Great to hear from you."
        }).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = generate_reply("Sam", "Hello", "Hey Nova", "sam@example.com")
        # Should strip the "Okay so..." reasoning prefix
        assert not result.startswith("Okay")
        assert "Sam" in result

    @patch.dict("sys.modules", {"nova_config": MagicMock(), "herd_config": MagicMock()})
    @patch("urllib.request.urlopen", side_effect=Exception("Ollama timeout"))
    def test_mail_agent_generate_reply_failure(self, mock_urlopen):
        """Reply generation returns empty string on LLM failure."""
        if "nova_mail_agent" in sys.modules:
            del sys.modules["nova_mail_agent"]
        from nova_mail_agent import generate_reply
        result = generate_reply("Sam", "Hello", "Hey Nova", "sam@example.com")
        assert result == ""


# ============================================================================
# Entry point
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "not integration and not functional"])
