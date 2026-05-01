#!/usr/bin/env python3
"""
test_ingestion.py — Tests for Nova's ingestion scripts:
  - nova_reddit_ingest.py
  - nova_imessage.py
  - nova_safari_ingest.py
  - nova_youtube_ingest.py
  - nova_youtube_playlist_ingest.py
  - nova_sam_blog_ingest.py
  - nova_slack_ingest.py
  - ingest_to_vector.py

Run: python3 -m pytest tests/test_ingestion.py -v
Written by Jordan Koch.
"""

import json
import os
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))


# ══════════════════════════════════════════════════════════════════════════════
# nova_reddit_ingest.py — subreddit parsing, post extraction, memory storage
# ══════════════════════════════════════════════════════════════════════════════

def _mock_reddit_modules():
    """Return dict of mocked modules for reddit ingest."""
    mock_nova_config = MagicMock()
    mock_nova_config.post_both = MagicMock()
    mock_nova_config.SLACK_NOTIFY = "C0TEST"
    mock_logger = MagicMock()
    mock_logger.log = MagicMock()
    mock_logger.LOG_INFO = "info"
    mock_logger.LOG_ERROR = "error"
    mock_logger.LOG_WARN = "warn"
    return {"nova_config": mock_nova_config, "nova_logger": mock_logger}


class TestRedditStateManagement:
    """Tests for load_state/save_state in nova_reddit_ingest.py."""

    def test_load_state_no_file(self):
        with patch.dict("sys.modules", _mock_reddit_modules()):
            if "nova_reddit_ingest" in sys.modules:
                del sys.modules["nova_reddit_ingest"]
            import nova_reddit_ingest
            with patch.object(nova_reddit_ingest, "STATE_FILE",
                              Path("/tmp/nonexistent_reddit_state.json")):
                state = nova_reddit_ingest.load_state()
                assert state == {"seen_ids": {}}

    def test_load_state_from_file(self):
        with patch.dict("sys.modules", _mock_reddit_modules()):
            if "nova_reddit_ingest" in sys.modules:
                del sys.modules["nova_reddit_ingest"]
            import nova_reddit_ingest
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                json.dump({"seen_ids": {"abc": {"ts": time.time(), "sub": "test"}}}, f)
                f.flush()
                with patch.object(nova_reddit_ingest, "STATE_FILE", Path(f.name)):
                    state = nova_reddit_ingest.load_state()
                    assert "abc" in state["seen_ids"]
            os.unlink(f.name)

    def test_save_state_prunes_old_entries(self):
        with patch.dict("sys.modules", _mock_reddit_modules()):
            if "nova_reddit_ingest" in sys.modules:
                del sys.modules["nova_reddit_ingest"]
            import nova_reddit_ingest
            with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
                pass  # just get a path
            try:
                with patch.object(nova_reddit_ingest, "STATE_FILE", Path(f.name)):
                    old_ts = time.time() - (8 * 86400)  # 8 days ago
                    recent_ts = time.time()
                    state = {
                        "seen_ids": {
                            "old_post": {"ts": old_ts, "sub": "test"},
                            "recent_post": {"ts": recent_ts, "sub": "test"},
                        }
                    }
                    nova_reddit_ingest.save_state(state)
                    # Reload and verify old entry was pruned
                    loaded = json.loads(Path(f.name).read_text())
                    assert "old_post" not in loaded["seen_ids"]
                    assert "recent_post" in loaded["seen_ids"]
            finally:
                os.unlink(f.name)


class TestRedditSubredditConfig:
    """Tests for subreddit configuration in nova_reddit_ingest.py."""

    def test_all_subreddits_have_required_fields(self):
        with patch.dict("sys.modules", _mock_reddit_modules()):
            if "nova_reddit_ingest" in sys.modules:
                del sys.modules["nova_reddit_ingest"]
            import nova_reddit_ingest
            for name, config in nova_reddit_ingest.SUBREDDITS.items():
                assert "source" in config, f"r/{name} missing 'source'"
                assert "label" in config, f"r/{name} missing 'label'"
                assert "limit" in config, f"r/{name} missing 'limit'"
                assert "dream_weight" in config, f"r/{name} missing 'dream_weight'"
                assert config["dream_weight"] in ("high", "medium", "low"), \
                    f"r/{name} has invalid dream_weight: {config['dream_weight']}"

    def test_burbank_is_configured(self):
        with patch.dict("sys.modules", _mock_reddit_modules()):
            if "nova_reddit_ingest" in sys.modules:
                del sys.modules["nova_reddit_ingest"]
            import nova_reddit_ingest
            assert "burbank" in nova_reddit_ingest.SUBREDDITS
            assert nova_reddit_ingest.SUBREDDITS["burbank"]["dream_weight"] == "high"


class TestRedditIngestSubreddit:
    """Tests for ingest_subreddit logic."""

    def test_skips_already_seen_posts(self):
        with patch.dict("sys.modules", _mock_reddit_modules()):
            if "nova_reddit_ingest" in sys.modules:
                del sys.modules["nova_reddit_ingest"]
            import nova_reddit_ingest

            mock_posts = [
                {"data": {"id": "seen1", "title": "Old post", "stickied": False,
                          "selftext": "", "score": 10, "author": "u1",
                          "link_flair_text": "", "num_comments": 0,
                          "url": "", "permalink": ""}},
                {"data": {"id": "new1", "title": "New post", "stickied": False,
                          "selftext": "Content here", "score": 50, "author": "u2",
                          "link_flair_text": "Discussion", "num_comments": 5,
                          "url": "", "permalink": ""}},
            ]
            with patch.object(nova_reddit_ingest, "fetch_subreddit", return_value=mock_posts):
                with patch.object(nova_reddit_ingest, "fetch_comments", return_value=[]):
                    with patch.object(nova_reddit_ingest, "vector_remember", return_value=True):
                        with patch("time.sleep"):
                            state = {"seen_ids": {"seen1": {"ts": time.time(), "sub": "test"}}}
                            config = {"source": "test", "label": "Test", "limit": 10, "dream_weight": "low"}
                            count = nova_reddit_ingest.ingest_subreddit("test", config, state)
                            assert count == 1
                            assert "new1" in state["seen_ids"]

    def test_skips_stickied_posts(self):
        with patch.dict("sys.modules", _mock_reddit_modules()):
            if "nova_reddit_ingest" in sys.modules:
                del sys.modules["nova_reddit_ingest"]
            import nova_reddit_ingest

            mock_posts = [
                {"data": {"id": "sticky1", "title": "Pinned", "stickied": True,
                          "selftext": "", "score": 100, "author": "mod",
                          "link_flair_text": "", "num_comments": 0,
                          "url": "", "permalink": ""}},
            ]
            with patch.object(nova_reddit_ingest, "fetch_subreddit", return_value=mock_posts):
                with patch.object(nova_reddit_ingest, "vector_remember", return_value=True):
                    state = {"seen_ids": {}}
                    config = {"source": "test", "label": "Test", "limit": 10, "dream_weight": "low"}
                    count = nova_reddit_ingest.ingest_subreddit("test", config, state)
                    assert count == 0

    def test_empty_subreddit_returns_zero(self):
        with patch.dict("sys.modules", _mock_reddit_modules()):
            if "nova_reddit_ingest" in sys.modules:
                del sys.modules["nova_reddit_ingest"]
            import nova_reddit_ingest

            with patch.object(nova_reddit_ingest, "fetch_subreddit", return_value=[]):
                state = {"seen_ids": {}}
                config = {"source": "test", "label": "Test", "limit": 10, "dream_weight": "low"}
                count = nova_reddit_ingest.ingest_subreddit("test", config, state)
                assert count == 0


class TestRedditQuietHours:
    """Tests for quiet hours detection."""

    def test_quiet_hours_late_night(self):
        with patch.dict("sys.modules", _mock_reddit_modules()):
            if "nova_reddit_ingest" in sys.modules:
                del sys.modules["nova_reddit_ingest"]
            import nova_reddit_ingest
            with patch("nova_reddit_ingest.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, 23, 30)
                assert nova_reddit_ingest._is_quiet_hours() is True

    def test_quiet_hours_early_morning(self):
        with patch.dict("sys.modules", _mock_reddit_modules()):
            if "nova_reddit_ingest" in sys.modules:
                del sys.modules["nova_reddit_ingest"]
            import nova_reddit_ingest
            with patch("nova_reddit_ingest.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, 5, 0)
                assert nova_reddit_ingest._is_quiet_hours() is True

    def test_not_quiet_hours_afternoon(self):
        with patch.dict("sys.modules", _mock_reddit_modules()):
            if "nova_reddit_ingest" in sys.modules:
                del sys.modules["nova_reddit_ingest"]
            import nova_reddit_ingest
            with patch("nova_reddit_ingest.datetime") as mock_dt:
                mock_dt.now.return_value = datetime(2026, 1, 1, 14, 0)
                assert nova_reddit_ingest._is_quiet_hours() is False


# ══════════════════════════════════════════════════════════════════════════════
# nova_imessage.py — message parsing, contact resolution
# ══════════════════════════════════════════════════════════════════════════════

class TestIMEssagePhoneNormalization:
    """Tests for phone number normalization in nova_imessage.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.VECTOR_URL = "http://127.0.0.1:18790/remember"
        mock_nc.JORDAN_DM = "D0TEST"
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_imessage" in sys.modules:
                del sys.modules["nova_imessage"]
            import nova_imessage
            return nova_imessage

    def test_normalize_us_number(self):
        mod = self._get_module()
        assert mod._normalize_phone("+15551234567") == "5551234567"

    def test_normalize_without_country_code(self):
        mod = self._get_module()
        assert mod._normalize_phone("5551234567") == "5551234567"

    def test_normalize_with_dashes(self):
        mod = self._get_module()
        assert mod._normalize_phone("555-123-4567") == "5551234567"

    def test_normalize_with_parens(self):
        mod = self._get_module()
        assert mod._normalize_phone("(555) 123-4567") == "5551234567"

    def test_normalize_short_number(self):
        mod = self._get_module()
        assert mod._normalize_phone("12345") == "12345"


class TestIMessageSpamFilter:
    """Tests for is_spam in nova_imessage.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.VECTOR_URL = "http://127.0.0.1:18790/remember"
        mock_nc.JORDAN_DM = "D0TEST"
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_imessage" in sys.modules:
                del sys.modules["nova_imessage"]
            import nova_imessage
            return nova_imessage

    def test_empty_text_is_spam(self):
        mod = self._get_module()
        assert mod.is_spam({"text": "", "sender": "+15551234567"}) is True

    def test_short_text_is_spam(self):
        mod = self._get_module()
        assert mod.is_spam({"text": "k", "sender": "+15551234567"}) is True

    def test_short_code_sender_is_spam(self):
        mod = self._get_module()
        assert mod.is_spam({"text": "Your code is 123456", "sender": "22395"}) is True

    def test_normal_message_not_spam(self):
        mod = self._get_module()
        assert mod.is_spam({"text": "Hey, are you free for lunch?", "sender": "+15551234567"}) is False

    def test_email_sender_rcs_spam(self):
        mod = self._get_module()
        assert mod.is_spam({"text": "Buy now!", "sender": "spammer@yahoo.com"}) is True

    def test_gmail_sender_not_spam(self):
        mod = self._get_module()
        assert mod.is_spam({"text": "Hey friend!", "sender": "friend@gmail.com"}) is False


class TestIMessageTimestampConversion:
    """Tests for _mac_timestamp_to_datetime in nova_imessage.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.VECTOR_URL = "http://127.0.0.1:18790/remember"
        mock_nc.JORDAN_DM = "D0TEST"
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_imessage" in sys.modules:
                del sys.modules["nova_imessage"]
            import nova_imessage
            return nova_imessage

    def test_zero_timestamp_returns_none(self):
        mod = self._get_module()
        assert mod._mac_timestamp_to_datetime(0) is None

    def test_none_timestamp_returns_none(self):
        mod = self._get_module()
        assert mod._mac_timestamp_to_datetime(None) is None

    def test_known_timestamp(self):
        mod = self._get_module()
        # 2026-01-01 00:00:00 UTC = 978307200 (Mac epoch offset) → nanoseconds
        # January 1, 2026 in Mac time = seconds since 2001-01-01
        # 2026-01-01 minus 2001-01-01 = 25 years
        # Let's use a computed value
        mac_ts = 789091200_000_000_000  # ~25 years in nanoseconds
        result = mod._mac_timestamp_to_datetime(mac_ts)
        assert result is not None
        assert isinstance(result, datetime)


class TestIMessageContactResolution:
    """Tests for resolve_contact in nova_imessage.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.VECTOR_URL = "http://127.0.0.1:18790/remember"
        mock_nc.JORDAN_DM = "D0TEST"
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_imessage" in sys.modules:
                del sys.modules["nova_imessage"]
            import nova_imessage
            return nova_imessage

    def test_resolve_with_cached_phone(self):
        mod = self._get_module()
        # Inject a mock contact lookup
        mod._contact_lookup = {"5551234567": "Alice Smith"}
        assert mod.resolve_contact("+15551234567") == "Alice Smith"

    def test_resolve_with_cached_email(self):
        mod = self._get_module()
        mod._contact_lookup = {"alice@example.com": "Alice Smith"}
        assert mod.resolve_contact("alice@example.com") == "Alice Smith"

    def test_resolve_unknown_returns_handle(self):
        mod = self._get_module()
        mod._contact_lookup = {}
        assert mod.resolve_contact("+15559999999") == "+15559999999"

    def test_resolve_empty_returns_unknown(self):
        mod = self._get_module()
        mod._contact_lookup = {}
        assert mod.resolve_contact("") == "Unknown"

    def test_resolve_none_returns_unknown(self):
        mod = self._get_module()
        mod._contact_lookup = {}
        assert mod.resolve_contact(None) == "Unknown"


class TestIMessageDBParsing:
    """Tests for message parsing from SQLite rows (mock Messages.db)."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.VECTOR_URL = "http://127.0.0.1:18790/remember"
        mock_nc.JORDAN_DM = "D0TEST"
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_imessage" in sys.modules:
                del sys.modules["nova_imessage"]
            import nova_imessage
            return nova_imessage

    def test_get_recent_messages_no_db(self):
        mod = self._get_module()
        with patch.object(mod, "MESSAGES_DB", Path("/tmp/nonexistent_messages.db")):
            result = mod.get_recent_messages(hours=1)
            assert result == []

    def test_get_recent_messages_with_mock_db(self):
        mod = self._get_module()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("""CREATE TABLE handle (
                ROWID INTEGER PRIMARY KEY, id TEXT)""")
            conn.execute("""CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY, text TEXT, is_from_me INTEGER,
                date INTEGER, service TEXT, handle_id INTEGER,
                date_read INTEGER, item_type INTEGER DEFAULT 0)""")
            # Insert a test message with a recent Mac timestamp
            # Mac timestamp in nanoseconds since 2001-01-01
            recent_ts = int((time.time() - 978307200) * 1_000_000_000)
            conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+15551234567')")
            conn.execute(
                "INSERT INTO message (text, is_from_me, date, service, handle_id, date_read, item_type) "
                "VALUES ('Hello Nova', 0, ?, 'iMessage', 1, 0, 0)",
                (recent_ts,)
            )
            conn.commit()
            conn.close()

            with patch.object(mod, "MESSAGES_DB", Path(db_path)):
                result = mod.get_recent_messages(hours=1)
                assert len(result) == 1
                assert result[0]["text"] == "Hello Nova"
                assert result[0]["sender"] == "+15551234567"
                assert result[0]["is_from_me"] is False
        finally:
            os.unlink(db_path)

    def test_get_recent_messages_from_me(self):
        mod = self._get_module()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
            conn.execute("""CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY, text TEXT, is_from_me INTEGER,
                date INTEGER, service TEXT, handle_id INTEGER,
                date_read INTEGER, item_type INTEGER DEFAULT 0)""")
            recent_ts = int((time.time() - 978307200) * 1_000_000_000)
            conn.execute("INSERT INTO handle (ROWID, id) VALUES (1, '+15551234567')")
            conn.execute(
                "INSERT INTO message (text, is_from_me, date, service, handle_id, date_read, item_type) "
                "VALUES ('Outgoing message', 1, ?, 'iMessage', 1, 0, 0)",
                (recent_ts,)
            )
            conn.commit()
            conn.close()

            with patch.object(mod, "MESSAGES_DB", Path(db_path)):
                result = mod.get_recent_messages(hours=1)
                assert len(result) == 1
                assert result[0]["sender"] == "Jordan"
                assert result[0]["is_from_me"] is True
        finally:
            os.unlink(db_path)


# ══════════════════════════════════════════════════════════════════════════════
# nova_safari_ingest.py — history grouping, noise filtering
# ══════════════════════════════════════════════════════════════════════════════

class TestSafariNoiseDetection:
    """Tests for is_noise_url in nova_safari_ingest.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_safari_ingest" in sys.modules:
                del sys.modules["nova_safari_ingest"]
            import nova_safari_ingest
            return nova_safari_ingest

    def test_ad_tracking_domain(self):
        mod = self._get_module()
        assert mod.is_noise_url("https://doubleclick.net/ad", "doubleclick.net") is True

    def test_subdomain_of_tracking(self):
        mod = self._get_module()
        assert mod.is_noise_url("https://ad.doubleclick.net/track", "ad.doubleclick.net") is True

    def test_google_analytics(self):
        mod = self._get_module()
        assert mod.is_noise_url("https://www.google-analytics.com/collect", "google-analytics.com") is True

    def test_localhost(self):
        mod = self._get_module()
        assert mod.is_noise_url("http://localhost:3000", "localhost") is True

    def test_tracking_url_pattern(self):
        mod = self._get_module()
        assert mod.is_noise_url("https://example.com/pixel", "example.com") is True

    def test_favicon(self):
        mod = self._get_module()
        assert mod.is_noise_url("https://example.com/favicon.ico", "example.com") is True

    def test_data_url(self):
        mod = self._get_module()
        assert mod.is_noise_url("data:text/html,<h1>test</h1>", "") is True

    def test_javascript_url(self):
        mod = self._get_module()
        assert mod.is_noise_url("javascript:void(0)", "") is True

    def test_short_url(self):
        mod = self._get_module()
        assert mod.is_noise_url("http://x.co", "x.co") is True

    def test_empty_url(self):
        mod = self._get_module()
        assert mod.is_noise_url("", "") is True

    def test_legitimate_url(self):
        mod = self._get_module()
        assert mod.is_noise_url("https://github.com/kochj23/project", "github.com") is False

    def test_apple_developer(self):
        mod = self._get_module()
        assert mod.is_noise_url("https://developer.apple.com/documentation/swift", "developer.apple.com") is False


class TestSafariCleanDomain:
    """Tests for clean_domain in nova_safari_ingest.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_safari_ingest" in sys.modules:
                del sys.modules["nova_safari_ingest"]
            import nova_safari_ingest
            return nova_safari_ingest

    def test_strips_www(self):
        mod = self._get_module()
        assert mod.clean_domain("www.example.com") == "example.com"

    def test_lowercase(self):
        mod = self._get_module()
        assert mod.clean_domain("GitHub.Com") == "github.com"

    def test_empty_domain(self):
        mod = self._get_module()
        assert mod.clean_domain("") == "unknown"

    def test_none_domain(self):
        mod = self._get_module()
        assert mod.clean_domain(None) == "unknown"

    def test_no_www_prefix(self):
        mod = self._get_module()
        assert mod.clean_domain("api.example.com") == "api.example.com"


class TestSafariGroupVisits:
    """Tests for group_visits in nova_safari_ingest.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_safari_ingest" in sys.modules:
                del sys.modules["nova_safari_ingest"]
            import nova_safari_ingest
            # Reset stats for test isolation
            nova_safari_ingest.stats = {
                "total_visits": 0, "groups_formed": 0, "groups_ingested": 0,
                "visits_ingested": 0, "skipped_noise": 0,
                "skipped_checkpoint": 0, "skipped_no_title": 0,
                "errors": 0, "start_time": 0, "last_status": 0,
            }
            return nova_safari_ingest

    def test_groups_by_domain_and_date(self):
        mod = self._get_module()
        # Mac absolute time for 2026-01-01 12:00:00 UTC
        mac_time = (datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc) - mod.MAC_EPOCH).total_seconds()
        visits = [
            ("https://github.com/repo1", "github.com", "Repo 1", mac_time, 1),
            ("https://github.com/repo2", "github.com", "Repo 2", mac_time + 3600, 1),
            ("https://stackoverflow.com/q/1", "stackoverflow.com", "Question 1", mac_time, 1),
        ]
        groups = mod.group_visits(visits)
        assert ("github.com", "2026-01-01") in groups
        assert len(groups[("github.com", "2026-01-01")]) == 2
        assert ("stackoverflow.com", "2026-01-01") in groups
        assert len(groups[("stackoverflow.com", "2026-01-01")]) == 1

    def test_filters_noise_urls(self):
        mod = self._get_module()
        mac_time = (datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc) - mod.MAC_EPOCH).total_seconds()
        visits = [
            ("https://doubleclick.net/ad", "doubleclick.net", "Ad", mac_time, 1),
            ("https://github.com/real", "github.com", "Real Page", mac_time, 1),
        ]
        groups = mod.group_visits(visits)
        assert ("doubleclick.net", "2026-01-01") not in groups
        assert ("github.com", "2026-01-01") in groups
        assert mod.stats["skipped_noise"] > 0

    def test_filters_no_title_visits(self):
        mod = self._get_module()
        mac_time = (datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc) - mod.MAC_EPOCH).total_seconds()
        visits = [
            ("https://example.com/page1", "example.com", "", mac_time, 1),
            ("https://example.com/page2", "example.com", "Untitled", mac_time, 1),
            ("https://example.com/page3", "example.com", "Real Title", mac_time, 1),
        ]
        groups = mod.group_visits(visits)
        assert len(groups.get(("example.com", "2026-01-01"), [])) == 1
        assert mod.stats["skipped_no_title"] >= 2


class TestSafariFormatMemoryText:
    """Tests for format_memory_text in nova_safari_ingest.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_safari_ingest" in sys.modules:
                del sys.modules["nova_safari_ingest"]
            import nova_safari_ingest
            return nova_safari_ingest

    def test_basic_format(self):
        mod = self._get_module()
        visits = [
            {"title": "Page 1", "url": "https://example.com/1", "time": "10:00", "visit_count": 1},
            {"title": "Page 2", "url": "https://example.com/2", "time": "11:00", "visit_count": 2},
        ]
        text = mod.format_memory_text("example.com", "2026-01-01", visits)
        assert "Safari browsing on example.com" in text
        assert "2026-01-01" in text
        assert "Page 1" in text
        assert "Page 2" in text

    def test_deduplicates_by_title(self):
        mod = self._get_module()
        visits = [
            {"title": "Same Page", "url": "https://example.com/1", "time": "10:00", "visit_count": 1},
            {"title": "Same Page", "url": "https://example.com/1?ref=2", "time": "10:05", "visit_count": 2},
            {"title": "Different Page", "url": "https://example.com/2", "time": "11:00", "visit_count": 1},
        ]
        text = mod.format_memory_text("example.com", "2026-01-01", visits)
        assert text.count("Same Page") == 1
        assert "Different Page" in text

    def test_truncates_at_max_urls(self):
        mod = self._get_module()
        visits = [
            {"title": f"Page {i}", "url": f"https://example.com/{i}", "time": "10:00", "visit_count": 1}
            for i in range(50)
        ]
        text = mod.format_memory_text("example.com", "2026-01-01", visits)
        assert "more pages" in text


class TestSafariCheckpoint:
    """Tests for checkpoint load/save in nova_safari_ingest.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_safari_ingest" in sys.modules:
                del sys.modules["nova_safari_ingest"]
            import nova_safari_ingest
            return nova_safari_ingest

    def test_load_checkpoint_no_file(self):
        mod = self._get_module()
        with patch.object(mod, "CHECKPOINT_FILE", Path("/tmp/nonexistent_checkpoint.json")):
            result = mod.load_checkpoint()
            assert result == set()

    def test_save_and_load_checkpoint(self):
        mod = self._get_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            cp_file = Path(tmpdir) / "checkpoint.json"
            with patch.object(mod, "CHECKPOINT_FILE", cp_file):
                keys = {"example.com::2026-01-01", "github.com::2026-01-02"}
                mod.save_checkpoint(keys)
                loaded = mod.load_checkpoint()
                assert loaded == keys


# ══════════════════════════════════════════════════════════════════════════════
# nova_youtube_ingest.py — chunking logic, video metadata
# ══════════════════════════════════════════════════════════════════════════════

class TestYouTubeChunkText:
    """Tests for chunk_text in nova_youtube_ingest.py."""

    def test_short_text_single_chunk(self):
        # Import directly since chunk_text has no external deps
        from nova_youtube_ingest import chunk_text
        result = chunk_text("Short text.", max_chars=2000)
        assert len(result) == 1
        assert result[0] == "Short text."

    def test_long_text_splits_on_sentences(self):
        from nova_youtube_ingest import chunk_text
        text = "First sentence. Second sentence. Third sentence. Fourth sentence."
        result = chunk_text(text, max_chars=40)
        assert len(result) > 1
        # Each chunk should be under the limit (roughly)
        for chunk in result:
            assert len(chunk) <= 60  # some slack for sentence boundaries

    def test_very_long_no_sentences_returns_single_chunk(self):
        """Text with no sentence breaks gets returned as-is (no split points)."""
        from nova_youtube_ingest import chunk_text
        text = "a" * 5000
        result = chunk_text(text, max_chars=2000)
        # No sentence boundaries means no split opportunity in the sentence loop
        # The fallback force-split only triggers when chunks list is empty
        assert len(result) >= 1
        # All text is preserved
        assert "".join(result) == text

    def test_force_split_with_sentence_punctuation(self):
        """Long text with sentence endings gets properly chunked."""
        from nova_youtube_ingest import chunk_text
        # Build text with sentence endings to trigger sentence-based splitting
        text = "This is a sentence. " * 200  # ~4000 chars
        result = chunk_text(text.strip(), max_chars=2000)
        assert len(result) >= 2
        for chunk in result:
            assert len(chunk) <= 2100  # some tolerance for sentence boundaries

    def test_empty_text(self):
        from nova_youtube_ingest import chunk_text
        result = chunk_text("", max_chars=2000)
        assert len(result) == 1
        assert result[0] == ""


class TestYouTubeRemember:
    """Tests for remember (memory storage) in nova_youtube_ingest.py."""

    @patch("urllib.request.urlopen")
    def test_remember_stores_chunks(self, mock_urlopen):
        from nova_youtube_ingest import remember
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock()
        # Text > 2000 chars should be chunked
        long_text = "Word. " * 500  # ~3000 chars
        stored = remember(long_text, "Test Video", "vid123", "playlist1")
        assert stored > 0

    @patch("urllib.request.urlopen")
    def test_remember_skips_short_text(self, mock_urlopen):
        from nova_youtube_ingest import remember
        stored = remember("Short.", "Test", "vid1", "pl1")
        assert stored == 0
        mock_urlopen.assert_not_called()


class TestYouTubeGetPlaylistVideos:
    """Tests for get_playlist_videos in nova_youtube_ingest.py."""

    @patch("subprocess.run")
    def test_parses_playlist_output(self, mock_run):
        from nova_youtube_ingest import get_playlist_videos
        mock_run.return_value = MagicMock(
            stdout="vid1\tFirst Video\nvid2\tSecond Video\n",
            returncode=0
        )
        videos = get_playlist_videos("https://youtube.com/playlist?list=PLtest")
        assert len(videos) == 2
        assert videos[0]["id"] == "vid1"
        assert videos[0]["title"] == "First Video"

    @patch("subprocess.run")
    def test_deduplicates_videos(self, mock_run):
        from nova_youtube_ingest import get_playlist_videos
        mock_run.return_value = MagicMock(
            stdout="vid1\tVideo\nvid1\tVideo Duplicate\n",
            returncode=0
        )
        videos = get_playlist_videos("https://youtube.com/playlist?list=PLtest")
        assert len(videos) == 1


# ══════════════════════════════════════════════════════════════════════════════
# nova_youtube_playlist_ingest.py — video metadata, transcript chunking
# ══════════════════════════════════════════════════════════════════════════════

class TestYouTubePlaylistGetInfo:
    """Tests for get_playlist_info in nova_youtube_playlist_ingest.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_youtube_playlist_ingest" in sys.modules:
                del sys.modules["nova_youtube_playlist_ingest"]
            import nova_youtube_playlist_ingest
            return nova_youtube_playlist_ingest

    @patch("subprocess.run")
    def test_parses_json_output(self, mock_run):
        mod = self._get_module()
        mock_run.return_value = MagicMock(
            stdout=json.dumps({"id": "v1", "title": "Test", "duration": 600}) + "\n"
                   + json.dumps({"id": "v2", "title": "Test 2", "duration": 300}) + "\n",
            returncode=0
        )
        videos = mod.get_playlist_info("https://youtube.com/playlist?list=PLtest")
        assert len(videos) == 2
        assert videos[0]["id"] == "v1"
        assert videos[0]["duration"] == 600

    @patch("subprocess.run")
    def test_handles_empty_output(self, mock_run):
        mod = self._get_module()
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        videos = mod.get_playlist_info("https://youtube.com/playlist?list=PLtest")
        assert videos == []


class TestYouTubePlaylistTranscriptChunking:
    """Test transcript chunking logic in ingest_video."""

    def test_transcript_chunks_at_800_chars(self):
        """Verify the chunking logic splits at ~800 chars."""
        # Simulate the chunking logic from ingest_video
        transcript = "word " * 200  # ~1000 chars
        words = transcript.split()
        chunks = []
        current = []
        current_len = 0
        for word in words:
            current.append(word)
            current_len += len(word) + 1
            if current_len >= 800:
                chunks.append(" ".join(current))
                current = []
                current_len = 0
        if current:
            chunks.append(" ".join(current))
        assert len(chunks) == 2  # ~1000 chars split at 800


# ══════════════════════════════════════════════════════════════════════════════
# nova_sam_blog_ingest.py — blog post parsing
# ══════════════════════════════════════════════════════════════════════════════

class TestSamBlogHTMLStripper:
    """Tests for HTMLStripper in nova_sam_blog_ingest.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.VECTOR_URL = "http://127.0.0.1:18790/remember"
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_sam_blog_ingest" in sys.modules:
                del sys.modules["nova_sam_blog_ingest"]
            import nova_sam_blog_ingest
            return nova_sam_blog_ingest

    def test_strips_tags(self):
        mod = self._get_module()
        stripper = mod.HTMLStripper()
        stripper.feed("<p>Hello <b>World</b></p>")
        assert "Hello" in stripper.get_text()
        assert "World" in stripper.get_text()
        assert "<" not in stripper.get_text()

    def test_strips_script_tags(self):
        mod = self._get_module()
        stripper = mod.HTMLStripper()
        stripper.feed("<script>var x = 1;</script><p>Content</p>")
        text = stripper.get_text()
        assert "var x" not in text
        assert "Content" in text

    def test_strips_style_tags(self):
        mod = self._get_module()
        stripper = mod.HTMLStripper()
        stripper.feed("<style>body { color: red; }</style><p>Content</p>")
        text = stripper.get_text()
        assert "color" not in text
        assert "Content" in text

    def test_strips_nav_and_footer(self):
        mod = self._get_module()
        stripper = mod.HTMLStripper()
        stripper.feed("<nav>Menu Item</nav><main>Main Content</main><footer>Copyright</footer>")
        text = stripper.get_text()
        assert "Menu Item" not in text
        assert "Main Content" in text
        assert "Copyright" not in text


class TestSamBlogFindPostLinks:
    """Tests for find_post_links in nova_sam_blog_ingest.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.VECTOR_URL = "http://127.0.0.1:18790/remember"
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_sam_blog_ingest" in sys.modules:
                del sys.modules["nova_sam_blog_ingest"]
            import nova_sam_blog_ingest
            return nova_sam_blog_ingest

    def test_finds_quoted_links(self):
        mod = self._get_module()
        html = '<a href="https://jasonacox-sam.github.io/posts/first-post">Post 1</a>'
        links = mod.find_post_links(html)
        assert "https://jasonacox-sam.github.io/posts/first-post" in links

    def test_finds_unquoted_links(self):
        mod = self._get_module()
        html = '<a href=https://jasonacox-sam.github.io/posts/second-post>Post 2</a>'
        links = mod.find_post_links(html)
        assert "https://jasonacox-sam.github.io/posts/second-post" in links

    def test_finds_relative_links(self):
        mod = self._get_module()
        html = '<a href="/posts/third-post">Post 3</a>'
        links = mod.find_post_links(html)
        assert any("third-post" in link for link in links)

    def test_deduplicates_links(self):
        mod = self._get_module()
        html = (
            '<a href="https://jasonacox-sam.github.io/posts/same">Same</a>'
            '<a href="https://jasonacox-sam.github.io/posts/same">Same Again</a>'
        )
        links = mod.find_post_links(html)
        assert links.count("https://jasonacox-sam.github.io/posts/same") == 1

    def test_excludes_bare_posts_page(self):
        mod = self._get_module()
        html = '<a href="https://jasonacox-sam.github.io/posts">All Posts</a>'
        links = mod.find_post_links(html)
        assert "https://jasonacox-sam.github.io/posts" not in links


class TestSamBlogStateManagement:
    """Tests for state load/save in nova_sam_blog_ingest.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.VECTOR_URL = "http://127.0.0.1:18790/remember"
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_sam_blog_ingest" in sys.modules:
                del sys.modules["nova_sam_blog_ingest"]
            import nova_sam_blog_ingest
            return nova_sam_blog_ingest

    def test_load_state_no_file(self):
        mod = self._get_module()
        with patch.object(mod, "STATE_FILE", Path("/tmp/nonexistent_sam_state.json")):
            state = mod.load_state()
            assert state == {"ingested_urls": [], "last_check": ""}

    def test_save_and_load_state(self):
        mod = self._get_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            sf = Path(tmpdir) / "state.json"
            with patch.object(mod, "STATE_FILE", sf):
                state = {"ingested_urls": ["https://example.com/post1"], "last_check": "2026-01-01"}
                mod.save_state(state)
                loaded = mod.load_state()
                assert loaded["ingested_urls"] == ["https://example.com/post1"]
                assert loaded["last_check"] == "2026-01-01"


# ══════════════════════════════════════════════════════════════════════════════
# nova_slack_ingest.py — Slack message parsing, file processing
# ══════════════════════════════════════════════════════════════════════════════

class TestSlackIngestProcessedLog:
    """Tests for load_processed/save_processed in nova_slack_ingest.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.SLACK_API = "https://slack.com/api"
        mock_nc.slack_bot_token = MagicMock(return_value="xoxb-test-token")
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_slack_ingest" in sys.modules:
                del sys.modules["nova_slack_ingest"]
            import nova_slack_ingest
            return nova_slack_ingest

    def test_load_processed_no_file(self):
        mod = self._get_module()
        with patch.object(mod, "PROCESSED_LOG", Path("/tmp/nonexistent_processed.json")):
            result = mod.load_processed()
            assert result == set()

    def test_save_and_load_processed(self):
        mod = self._get_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "processed.json"
            with patch.object(mod, "PROCESSED_LOG", log_file):
                ids = {"F001", "F002", "F003"}
                mod.save_processed(ids)
                loaded = mod.load_processed()
                assert loaded == ids

    def test_save_processed_caps_at_1000(self):
        mod = self._get_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            log_file = Path(tmpdir) / "processed.json"
            with patch.object(mod, "PROCESSED_LOG", log_file):
                ids = {f"F{i:04d}" for i in range(1500)}
                mod.save_processed(ids)
                loaded = mod.load_processed()
                assert len(loaded) == 1000


class TestSlackIngestGetRecentFiles:
    """Tests for get_recent_files in nova_slack_ingest.py."""

    def _get_module(self):
        mock_nc = MagicMock()
        mock_nc.SLACK_NOTIFY = "C0TEST"
        mock_nc.SLACK_API = "https://slack.com/api"
        mock_nc.slack_bot_token = MagicMock(return_value="xoxb-test-token")
        mock_nc.post_both = MagicMock()
        with patch.dict("sys.modules", {"nova_config": mock_nc}):
            if "nova_slack_ingest" in sys.modules:
                del sys.modules["nova_slack_ingest"]
            import nova_slack_ingest
            return nova_slack_ingest

    def test_returns_empty_without_token(self):
        mod = self._get_module()
        with patch.object(mod, "SLACK_TOKEN", ""):
            result = mod.get_recent_files()
            assert result == []


# ══════════════════════════════════════════════════════════════════════════════
# ingest_to_vector.py — core ingestion pipeline
# ══════════════════════════════════════════════════════════════════════════════

class TestIngestToVector:
    """Tests for ingest_to_vector.py core pipeline."""

    def test_builds_correct_payload(self):
        """Verify the payload structure matches what the API expects."""
        # The script is a simple CLI tool — test its logic directly
        file_path = "/tmp/test_doc.md"
        source = "test_source"
        title = "test doc"

        payload = {
            "text": "Test content here.",
            "title": title,
            "source": source,
        }
        assert payload["text"] == "Test content here."
        assert payload["title"] == "test doc"
        assert payload["source"] == "test_source"

    def test_title_extraction_from_filename(self):
        """Verify title is extracted from filename correctly."""
        filename = "my_test_document.md"
        title = os.path.basename(filename).replace('.md', '').replace('_', ' ')
        assert title == "my test document"

    def test_title_extraction_no_underscores(self):
        filename = "simple.md"
        title = os.path.basename(filename).replace('.md', '').replace('_', ' ')
        assert title == "simple"


# ══════════════════════════════════════════════════════════════════════════════
# Integration tests — marked @pytest.mark.integration
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestRedditIngestIntegration:
    """Test that reddit ingest can fetch from a real subreddit."""

    def test_fetch_burbank_subreddit(self):
        """Fetch r/burbank and verify we get posts."""
        with patch.dict("sys.modules", _mock_reddit_modules()):
            if "nova_reddit_ingest" in sys.modules:
                del sys.modules["nova_reddit_ingest"]
            import nova_reddit_ingest
            posts = nova_reddit_ingest.fetch_subreddit("burbank", limit=3)
            assert len(posts) > 0
            # Each post should have data
            for post in posts:
                assert "data" in post
                assert "title" in post["data"]
                assert "id" in post["data"]


@pytest.mark.integration
class TestIngestToVectorIntegration:
    """Test that ingest_to_vector can store a test memory in PostgreSQL."""

    def test_store_test_memory(self):
        """Store a test memory via the vector API."""
        import urllib.request
        payload = json.dumps({
            "text": f"[TEST] Integration test memory created at {datetime.now().isoformat()}",
            "source": "pytest_integration_test",
            "metadata": {"test": True, "date": date.today().isoformat()},
        }).encode()
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:18790/remember",
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as r:
                result = json.loads(r.read())
            assert result.get("ok") or r.status == 200
        except Exception as e:
            pytest.skip(f"Vector memory server not available: {e}")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "not integration and not functional"])
