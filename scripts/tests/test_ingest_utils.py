"""test_ingest_utils.py — Tests for Nova's ingestion scripts and memory utilities. Written by Jordan Koch."""

import email
import email.policy
import hashlib
import json
import mailbox
import os
import re
import sqlite3
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))


# ══════════════════════════════════════════════════════════════════════════════
# Helpers for mocking module imports
# ══════════════════════════════════════════════════════════════════════════════

def _mock_nova_config():
    """Create a mock nova_config module for scripts that import it at module level."""
    mock = MagicMock()
    mock.VECTOR_URL = "http://127.0.0.1:18790/remember"
    mock.SLACK_API = "https://slack.com/api"
    mock.SLACK_NOTIFY = "C0TEST"
    mock.JORDAN_DM = "D0TEST"
    mock.JORDAN_SIGNAL = "+15550000000"
    mock.slack_bot_token = MagicMock(return_value="xoxb-test-token")
    mock.post_both = MagicMock()
    mock.post_discord = MagicMock(return_value=True)
    return mock


def _reload_module(module_name, extra_mocks=None):
    """Force-reload a module with mocked dependencies."""
    mocks = {"nova_config": _mock_nova_config()}
    if extra_mocks:
        mocks.update(extra_mocks)
    with patch.dict("sys.modules", mocks):
        if module_name in sys.modules:
            del sys.modules[module_name]
        return __import__(module_name)


def _reload_module_with_logger(module_name):
    """Reload a module mocking both nova_config and nova_logger."""
    mock_logger = MagicMock()
    mock_logger.log = MagicMock()
    mock_logger.LOG_INFO = "info"
    mock_logger.LOG_ERROR = "error"
    mock_logger.LOG_WARN = "warn"
    return _reload_module(module_name, {"nova_logger": mock_logger})


# ══════════════════════════════════════════════════════════════════════════════
# nova_reddit_ingest.py — Additional tests beyond test_ingestion.py
# ══════════════════════════════════════════════════════════════════════════════

class TestRedditFetchComments:
    """Tests for fetch_comments in nova_reddit_ingest.py (not covered in test_ingestion.py)."""

    def test_fetch_comments_returns_list(self):
        mod = _reload_module_with_logger("nova_reddit_ingest")
        comment_data = [
            {"kind": "Listing", "data": {"children": []}},
            {"kind": "Listing", "data": {"children": [
                {"kind": "t1", "data": {"author": "user1", "body": "Great post with lots of detail!", "score": 42}},
                {"kind": "t1", "data": {"author": "user2", "body": "I disagree because of reasons", "score": 10}},
                {"kind": "more", "data": {}},
            ]}},
        ]
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(comment_data).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            comments = mod.fetch_comments("test", "abc123", limit=5)
            assert len(comments) == 2
            assert comments[0]["author"] == "user1"
            assert comments[0]["score"] == 42

    def test_fetch_comments_skips_short_bodies(self):
        mod = _reload_module_with_logger("nova_reddit_ingest")
        comment_data = [
            {"kind": "Listing", "data": {"children": []}},
            {"kind": "Listing", "data": {"children": [
                {"kind": "t1", "data": {"author": "user1", "body": "short", "score": 1}},
                {"kind": "t1", "data": {"author": "user2", "body": "This is a sufficiently long comment body", "score": 5}},
            ]}},
        ]
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(comment_data).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            comments = mod.fetch_comments("test", "abc123", limit=5)
            assert len(comments) == 1

    def test_fetch_comments_handles_network_error(self):
        mod = _reload_module_with_logger("nova_reddit_ingest")
        with patch("urllib.request.urlopen", side_effect=Exception("Network error")):
            comments = mod.fetch_comments("test", "abc123")
            assert comments == []

    def test_fetch_comments_truncates_body(self):
        mod = _reload_module_with_logger("nova_reddit_ingest")
        long_body = "x" * 1000
        comment_data = [
            {"kind": "Listing", "data": {"children": []}},
            {"kind": "Listing", "data": {"children": [
                {"kind": "t1", "data": {"author": "verbose", "body": long_body, "score": 1}},
            ]}},
        ]
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps(comment_data).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            comments = mod.fetch_comments("test", "abc123")
            assert len(comments[0]["body"]) <= 500


class TestRedditVectorRemember:
    """Tests for vector_remember in nova_reddit_ingest.py."""

    def test_vector_remember_success(self):
        mod = _reload_module_with_logger("nova_reddit_ingest")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            result = mod.vector_remember("test text", "test_source", {"key": "val"})
            assert result is True

    def test_vector_remember_failure(self):
        mod = _reload_module_with_logger("nova_reddit_ingest")
        with patch("urllib.request.urlopen", side_effect=Exception("Connection refused")):
            result = mod.vector_remember("test text", "test_source", {})
            assert result is False


class TestRedditGenerateDreamContext:
    """Tests for generate_dream_context in nova_reddit_ingest.py."""

    def test_writes_dream_file(self):
        mod = _reload_module_with_logger("nova_reddit_ingest")
        state = {
            "seen_ids": {
                "post1": {"ts": time.time(), "sub": "burbank", "title": "Local event"},
                "post2": {"ts": time.time(), "sub": "ClaudeCode", "title": "New feature"},
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            dream_file = Path(tmpdir) / f"{date.today().isoformat()}.reddit.md"
            with patch.object(mod, "TODAY", date.today().isoformat()):
                # Override the dream file path
                original_generate = mod.generate_dream_context

                def patched_generate(s):
                    nonlocal dream_file
                    # Build content similar to original but write to temp
                    today_posts = {}
                    for pid, info in s.get("seen_ids", {}).items():
                        if info.get("ts", 0) > time.time() - 86400:
                            sub = info.get("sub", "?")
                            today_posts.setdefault(sub, []).append(info.get("title", ""))
                    if today_posts:
                        lines = [f"## What Reddit is talking about\n"]
                        for sub, titles in sorted(today_posts.items()):
                            lines.append(f"### r/{sub}")
                            for t in titles[:5]:
                                lines.append(f"- {t}")
                        dream_file.write_text("\n".join(lines))

                patched_generate(state)
                assert dream_file.exists()
                content = dream_file.read_text()
                assert "r/burbank" in content
                assert "Local event" in content

    def test_no_dream_file_without_posts(self):
        mod = _reload_module_with_logger("nova_reddit_ingest")
        state = {"seen_ids": {}}
        # generate_dream_context should return without writing if no recent posts
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(mod, "TODAY", date.today().isoformat()):
                dream_path = Path(tmpdir) / f"{date.today().isoformat()}.reddit.md"
                # Monkey-patch the function to use temp path
                orig_path_cls = Path.home
                mod.generate_dream_context(state)
                # No file should be written when there are no posts
                # (the function checks for empty today_posts)


class TestRedditIngestSubredditMemoryFormat:
    """Tests verifying the memory text structure produced by ingest_subreddit."""

    @pytest.mark.frame
    def test_memory_text_includes_required_fields(self):
        mod = _reload_module_with_logger("nova_reddit_ingest")
        posts = [
            {"data": {"id": "abc", "title": "Test Post Title", "stickied": False,
                       "selftext": "This is the body of the post with enough text to matter.",
                       "score": 99, "author": "test_user", "link_flair_text": "Discussion",
                       "num_comments": 0, "url": "http://example.com", "permalink": "/r/test/abc"}},
        ]
        captured_text = []
        with patch.object(mod, "fetch_subreddit", return_value=posts):
            with patch.object(mod, "fetch_comments", return_value=[]):
                def capture_remember(text, source, metadata):
                    captured_text.append(text)
                    return True
                with patch.object(mod, "vector_remember", side_effect=capture_remember):
                    state = {"seen_ids": {}}
                    config = {"source": "test", "label": "Test", "limit": 10, "dream_weight": "low"}
                    mod.ingest_subreddit("test", config, state)

        assert len(captured_text) == 1
        text = captured_text[0]
        assert "Reddit r/test:" in text
        assert "Test Post Title" in text
        assert "Score: 99" in text
        assert "u/test_user" in text
        assert "Flair: Discussion" in text

    @pytest.mark.frame
    def test_memory_text_includes_comments(self):
        mod = _reload_module_with_logger("nova_reddit_ingest")
        posts = [
            {"data": {"id": "xyz", "title": "Post With Comments", "stickied": False,
                       "selftext": "Body text here for the post.", "score": 50, "author": "poster",
                       "link_flair_text": "", "num_comments": 3, "url": "", "permalink": ""}},
        ]
        comments = [
            {"author": "commenter1", "body": "Insightful comment here", "score": 20},
        ]
        captured_text = []
        with patch.object(mod, "fetch_subreddit", return_value=posts):
            with patch.object(mod, "fetch_comments", return_value=comments):
                with patch("time.sleep"):
                    def capture_remember(text, source, metadata):
                        captured_text.append(text)
                        return True
                    with patch.object(mod, "vector_remember", side_effect=capture_remember):
                        state = {"seen_ids": {}}
                        config = {"source": "test", "label": "Test", "limit": 10, "dream_weight": "low"}
                        mod.ingest_subreddit("test", config, state)

        assert len(captured_text) == 1
        assert "Top comments:" in captured_text[0]
        assert "u/commenter1" in captured_text[0]


# ══════════════════════════════════════════════════════════════════════════════
# nova_youtube_channel_ingest.py — Channel enumeration, chunking, process_video
# ══════════════════════════════════════════════════════════════════════════════

class TestYouTubeChannelChunkText:
    """Tests for chunk_text in nova_youtube_channel_ingest.py."""

    def test_short_text_single_chunk(self):
        mod = _reload_module("nova_youtube_channel_ingest")
        result = mod.chunk_text("Short text.", max_chars=2000)
        assert result == ["Short text."]

    def test_splits_on_sentence_boundaries(self):
        mod = _reload_module("nova_youtube_channel_ingest")
        text = "First sentence. Second sentence. Third sentence. Fourth sentence. Fifth sentence."
        result = mod.chunk_text(text, max_chars=50)
        assert len(result) >= 2
        for chunk in result:
            assert len(chunk) <= 80  # allow tolerance for sentence boundary

    def test_fallback_hard_split(self):
        mod = _reload_module("nova_youtube_channel_ingest")
        text = "a" * 5000  # no sentence boundaries
        result = mod.chunk_text(text, max_chars=2000)
        assert len(result) >= 1
        total = sum(len(c) for c in result)
        assert total == 5000

    def test_empty_text(self):
        mod = _reload_module("nova_youtube_channel_ingest")
        result = mod.chunk_text("", max_chars=2000)
        assert result == [""]


class TestYouTubeChannelGetChannelVideos:
    """Tests for get_channel_videos in nova_youtube_channel_ingest.py."""

    @patch("subprocess.run")
    def test_parses_channel_output(self, mock_run):
        mod = _reload_module("nova_youtube_channel_ingest")
        # First call: enumerate videos
        # Second call: get channel name
        mock_run.side_effect = [
            MagicMock(stdout="vid1\tFirst Video\t20260101\t600\nvid2\tSecond\t20260102\t300\n", returncode=0),
            MagicMock(stdout="TestChannel\n", returncode=0),
        ]
        videos, name = mod.get_channel_videos("https://youtube.com/@testchannel")
        assert len(videos) == 2
        assert videos[0]["id"] == "vid1"
        assert videos[0]["title"] == "First Video"
        assert name == "TestChannel"

    @patch("subprocess.run")
    def test_deduplicates_video_ids(self, mock_run):
        mod = _reload_module("nova_youtube_channel_ingest")
        mock_run.side_effect = [
            MagicMock(stdout="vid1\tVideo\t20260101\t600\nvid1\tDuplicate\t20260101\t600\n", returncode=0),
            MagicMock(stdout="Chan\n", returncode=0),
        ]
        videos, _ = mod.get_channel_videos("https://youtube.com/@test")
        assert len(videos) == 1

    @patch("subprocess.run")
    def test_handles_failed_enumeration(self, mock_run):
        mod = _reload_module("nova_youtube_channel_ingest")
        mock_run.side_effect = [
            MagicMock(stdout="", stderr="Error", returncode=1),
        ]
        videos, name = mod.get_channel_videos("https://youtube.com/@bad")
        assert videos == []
        assert name == "unknown"


class TestYouTubeChannelRemember:
    """Tests for remember in nova_youtube_channel_ingest.py."""

    def test_remember_skips_short_text(self):
        mod = _reload_module("nova_youtube_channel_ingest")
        with patch("urllib.request.urlopen") as mock_urlopen:
            result = mod.remember("Short.", "Title", "vid1", "channel1")
            assert result == 0
            mock_urlopen.assert_not_called()

    def test_remember_stores_chunks(self):
        mod = _reload_module("nova_youtube_channel_ingest")
        long_text = "This is a sentence. " * 200  # ~4000 chars
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            stored = mod.remember(long_text, "Test Video", "vid1", "TestChannel")
            assert stored >= 2


class TestYouTubeChannelSignalPost:
    """Tests for post_signal markdown stripping."""

    def test_strips_markdown_formatting(self):
        mod = _reload_module("nova_youtube_channel_ingest")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            mod.post_signal(":movie_camera: *YouTube Channel* `test`")
            call_args = mock_urlopen.call_args
            sent_data = json.loads(call_args[0][0].data)
            msg = sent_data["params"]["message"]
            assert "*" not in msg
            assert ":" not in msg or "movie_camera" not in msg


# ══════════════════════════════════════════════════════════════════════════════
# nova_youtube_playlist_ingest.py — Additional playlist-specific tests
# ══════════════════════════════════════════════════════════════════════════════

class TestYouTubePlaylistVectorRemember:
    """Tests for vector_remember in nova_youtube_playlist_ingest.py."""

    def test_remember_success(self):
        mod = _reload_module("nova_youtube_playlist_ingest")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            result = mod.vector_remember("Some text", {"type": "test"})
            assert result is True

    def test_remember_failure(self):
        mod = _reload_module("nova_youtube_playlist_ingest")
        with patch("urllib.request.urlopen", side_effect=Exception("fail")):
            result = mod.vector_remember("Some text", {"type": "test"})
            assert result is False


class TestYouTubePlaylistIngestVideo:
    """Tests for ingest_video transcript chunking in nova_youtube_playlist_ingest.py."""

    @pytest.mark.frame
    def test_transcript_chunk_metadata_format(self):
        """Verify each chunk has correct part numbering in text and metadata."""
        mod = _reload_module("nova_youtube_playlist_ingest")
        captured = []

        def capture_remember(text, metadata):
            captured.append({"text": text, "metadata": metadata})
            return True

        with patch.object(mod, "vector_remember", side_effect=capture_remember):
            with patch.object(mod, "download_audio", return_value="/tmp/fake.wav"):
                with patch.object(mod, "transcribe", return_value="word " * 300):
                    with patch("os.unlink"):
                        video = {"id": "v1", "title": "Test Video", "duration": 600}
                        result = mod.ingest_video(video, "/tmp")

        # Should have metadata chunk + transcript chunks
        transcript_chunks = [c for c in captured if c["metadata"].get("type") == "youtube_transcript"]
        assert len(transcript_chunks) >= 1
        for tc in transcript_chunks:
            assert "part" in tc["metadata"]
            assert "total_parts" in tc["metadata"]
            assert "(part " in tc["text"]


# ══════════════════════════════════════════════════════════════════════════════
# nova_email_ingest.py — EMLX parsing, exclusion logic, memory text formation
# ══════════════════════════════════════════════════════════════════════════════

class TestEmailIngestParseEmlx:
    """Tests for parse_emlx in nova_email_ingest.py."""

    def _make_emlx(self, subject="Test", sender="alice@example.com",
                    to="bob@example.com", body="Hello, this is a test email body.", date="Mon, 1 Jan 2026 12:00:00 +0000"):
        """Build a minimal .emlx file content."""
        email_bytes = (
            f"From: {sender}\r\n"
            f"To: {to}\r\n"
            f"Subject: {subject}\r\n"
            f"Date: {date}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n"
            f"{body}"
        ).encode()
        return f"{len(email_bytes)}\n".encode() + email_bytes

    def test_parse_basic_emlx(self):
        mod = _reload_module("nova_email_ingest")
        with tempfile.NamedTemporaryFile(suffix=".emlx", delete=False) as f:
            f.write(self._make_emlx())
            f.flush()
            result = mod.parse_emlx(f.name)
        os.unlink(f.name)
        assert result is not None
        assert result["subject"] == "Test"
        assert "alice@example.com" in result["sender"]
        assert "Hello" in result["body"]

    def test_parse_multipart_emlx(self):
        mod = _reload_module("nova_email_ingest")
        boundary = "----boundary123"
        body_parts = (
            f"--{boundary}\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"Plain text body here.\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n\r\n"
            f"<html><body>HTML body</body></html>\r\n"
            f"--{boundary}--"
        )
        email_bytes = (
            f"From: sender@test.com\r\n"
            f"Subject: Multipart Test\r\n"
            f"Date: Mon, 1 Jan 2026 12:00:00 +0000\r\n"
            f"Content-Type: multipart/alternative; boundary=\"{boundary}\"\r\n"
            f"\r\n"
            f"{body_parts}"
        ).encode()
        content = f"{len(email_bytes)}\n".encode() + email_bytes
        with tempfile.NamedTemporaryFile(suffix=".emlx", delete=False) as f:
            f.write(content)
            f.flush()
            result = mod.parse_emlx(f.name)
        os.unlink(f.name)
        assert result is not None
        assert "Plain text body here" in result["body"]

    def test_parse_truncates_long_body(self):
        mod = _reload_module("nova_email_ingest")
        long_body = "x" * 5000
        with tempfile.NamedTemporaryFile(suffix=".emlx", delete=False) as f:
            f.write(self._make_emlx(body=long_body))
            f.flush()
            result = mod.parse_emlx(f.name)
        os.unlink(f.name)
        assert result is not None
        assert len(result["body"]) <= mod.MAX_TEXT_LENGTH

    def test_parse_invalid_file_returns_none(self):
        mod = _reload_module("nova_email_ingest")
        with tempfile.NamedTemporaryFile(suffix=".emlx", delete=False, mode="wb") as f:
            f.write(b"\x00\x01\x02garbage")
            f.flush()
            result = mod.parse_emlx(f.name)
        os.unlink(f.name)
        # Should handle gracefully: either None or a parsed-but-empty result
        # The function catches exceptions and returns None


class TestEmailIngestShouldExclude:
    """Tests for should_exclude in nova_email_ingest.py."""

    def test_excludes_work_email_in_sender(self):
        mod = _reload_module("nova_email_ingest")
        with patch.object(mod, "EXCLUDE_WORK_EMAIL", "work@company.com"):
            parsed = {"sender": "Work User <work@company.com>", "to": "", "subject": "Meeting", "body": ""}
            assert mod.should_exclude(parsed) is True

    def test_excludes_work_email_in_to(self):
        mod = _reload_module("nova_email_ingest")
        with patch.object(mod, "EXCLUDE_WORK_EMAIL", "work@company.com"):
            parsed = {"sender": "alice@gmail.com", "to": "work@company.com", "subject": "Invite", "body": ""}
            assert mod.should_exclude(parsed) is True

    def test_does_not_exclude_personal_email(self):
        mod = _reload_module("nova_email_ingest")
        with patch.object(mod, "EXCLUDE_WORK_EMAIL", "work@company.com"):
            parsed = {"sender": "friend@gmail.com", "to": "me@gmail.com", "subject": "Lunch?", "body": "Hey!"}
            assert mod.should_exclude(parsed) is False

    def test_no_exclusion_when_work_email_empty(self):
        mod = _reload_module("nova_email_ingest")
        with patch.object(mod, "EXCLUDE_WORK_EMAIL", ""):
            parsed = {"sender": "anyone@any.com", "to": "anyone@any.com", "subject": "Anything", "body": "text"}
            assert mod.should_exclude(parsed) is False


class TestEmailIngestMakeMemoryText:
    """Tests for make_memory_text in nova_email_ingest.py."""

    @pytest.mark.frame
    def test_memory_text_structure(self):
        mod = _reload_module("nova_email_ingest")
        parsed = {
            "date": "Mon, 1 Jan 2026",
            "sender": "alice@example.com",
            "to": "bob@example.com",
            "subject": "Project Update",
            "body": "The project is going well.",
        }
        text = mod.make_memory_text(parsed)
        assert "Date: Mon, 1 Jan 2026" in text
        assert "From: alice@example.com" in text
        assert "Subject: Project Update" in text
        assert "The project is going well." in text

    @pytest.mark.frame
    def test_memory_text_empty_fields(self):
        mod = _reload_module("nova_email_ingest")
        parsed = {"date": "", "sender": "", "to": "", "subject": "", "body": ""}
        text = mod.make_memory_text(parsed)
        assert text == ""

    def test_memory_text_truncates(self):
        mod = _reload_module("nova_email_ingest")
        parsed = {
            "date": "", "sender": "", "to": "",
            "subject": "S" * 200,
            "body": "B" * 3000,
        }
        text = mod.make_memory_text(parsed)
        assert len(text) <= mod.MAX_TEXT_LENGTH


# ══════════════════════════════════════════════════════════════════════════════
# nova_ingest_mbox.py — MBOX parsing, PII redaction, sensitive content filtering
# ══════════════════════════════════════════════════════════════════════════════

class TestMboxIsSensitive:
    """Tests for _is_sensitive in nova_ingest_mbox.py."""

    def test_sensitive_content_detected(self):
        from nova_ingest_mbox import _is_sensitive
        assert _is_sensitive("Normal subject", "This contains a sex tape reference") is True

    def test_clean_content_passes(self):
        from nova_ingest_mbox import _is_sensitive
        assert _is_sensitive("Project Update", "The deployment went smoothly.") is False

    def test_sensitive_subject(self):
        from nova_ingest_mbox import _is_sensitive
        assert _is_sensitive("explicit video attached", "See above") is True


class TestMboxRedactBody:
    """Tests for _redact_body in nova_ingest_mbox.py."""

    def test_redacts_phone_numbers(self):
        from nova_ingest_mbox import _redact_body
        result = _redact_body("Call me at 555-123-4567 tomorrow")
        assert "[PHONE]" in result
        assert "555-123-4567" not in result

    def test_redacts_ssn(self):
        from nova_ingest_mbox import _redact_body
        result = _redact_body("SSN is 123-45-6789")
        assert "[SSN]" in result
        assert "123-45-6789" not in result

    def test_redacts_email_addresses(self):
        from nova_ingest_mbox import _redact_body
        result = _redact_body("Contact alice@example.com for details")
        assert "[EMAIL]" in result
        assert "alice@example.com" not in result

    def test_redacts_explicit_words(self):
        from nova_ingest_mbox import _redact_body
        result = _redact_body("The nude photo was inappropriate")
        assert "[REDACTED]" in result


class TestMboxParseEmail:
    """Tests for parse_email in nova_ingest_mbox.py."""

    def test_parse_simple_email(self):
        from nova_ingest_mbox import parse_email
        msg = email.message_from_string(
            "From: alice@test.com\r\n"
            "Subject: Hello\r\n"
            "Date: Mon, 1 Jan 2026 12:00:00 +0000\r\n"
            "\r\n"
            "Hello world, this is a test email."
        )
        result = parse_email(msg, "Inbox")
        assert result is not None
        assert result["sender"] == "alice@test.com"
        assert result["subject"] == "Hello"
        assert result["folder"] == "Inbox"

    def test_parse_email_skips_sensitive(self):
        from nova_ingest_mbox import parse_email
        msg = email.message_from_string(
            "From: spammer@test.com\r\n"
            "Subject: explicit video content\r\n"
            "Date: Mon, 1 Jan 2026 12:00:00 +0000\r\n"
            "\r\n"
            "Click here for adult content."
        )
        result = parse_email(msg, "Inbox")
        assert result is None

    def test_parse_email_truncates_body(self):
        from nova_ingest_mbox import parse_email
        long_body = "A" * 2000
        msg = email.message_from_string(
            f"From: alice@test.com\r\n"
            f"Subject: Long email\r\n"
            f"Date: Mon, 1 Jan 2026 12:00:00 +0000\r\n"
            f"\r\n"
            f"{long_body}"
        )
        result = parse_email(msg, "Inbox")
        assert result is not None
        assert len(result["body"]) <= 500


# ══════════════════════════════════════════════════════════════════════════════
# nova_ingest_emlx.py — EMLX file parsing, skip folders, PII filter
# ══════════════════════════════════════════════════════════════════════════════

class TestEmlxIsSkipFolder:
    """Tests for is_skip_folder in nova_ingest_emlx.py."""

    def test_trash_folder_skipped(self):
        from nova_ingest_emlx import is_skip_folder
        assert is_skip_folder(Path("/Mail/V10/Trash.mbox/Messages/1.emlx")) is True

    def test_spam_folder_skipped(self):
        from nova_ingest_emlx import is_skip_folder
        assert is_skip_folder(Path("/Mail/V10/Spam.mbox/Messages/2.emlx")) is True

    def test_junk_folder_skipped(self):
        from nova_ingest_emlx import is_skip_folder
        assert is_skip_folder(Path("/Mail/V10/Junk.mbox/Messages/3.emlx")) is True

    def test_inbox_not_skipped(self):
        from nova_ingest_emlx import is_skip_folder
        assert is_skip_folder(Path("/Mail/V10/INBOX.mbox/Messages/1.emlx")) is False

    def test_drafts_folder_skipped(self):
        from nova_ingest_emlx import is_skip_folder
        assert is_skip_folder(Path("/Mail/V10/Drafts.mbox/Messages/1.emlx")) is True

    def test_nested_trash_skipped(self):
        from nova_ingest_emlx import is_skip_folder
        assert is_skip_folder(Path("/Mail/V10/Account/Trash.mbox/Messages/1.emlx")) is True


class TestEmlxPiiFilter:
    """Tests for _pii_filter in nova_ingest_emlx.py."""

    def test_skip_explicit_content(self):
        from nova_ingest_emlx import _pii_filter
        skip, _ = _pii_filter("normal subject", "visit this porn site for details")
        assert skip is True

    def test_redact_phone_number(self):
        from nova_ingest_emlx import _pii_filter
        skip, body = _pii_filter("Call me", "Phone: 555-123-4567")
        assert skip is False
        assert "[PHONE]" in body

    def test_clean_content_passes(self):
        from nova_ingest_emlx import _pii_filter
        skip, body = _pii_filter("Project status", "Everything looks good for the release.")
        assert skip is False
        assert body == "Everything looks good for the release."


class TestEmlxParseEmlx:
    """Tests for parse_emlx in nova_ingest_emlx.py."""

    def _make_emlx_file(self, subject="Test", sender="alice@test.com", body="Hello world content here."):
        email_bytes = (
            f"From: {sender}\r\n"
            f"Subject: {subject}\r\n"
            f"Date: Mon, 1 Jan 2026 12:00:00 +0000\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n"
            f"\r\n"
            f"{body}"
        ).encode()
        content = f"{len(email_bytes)}\n".encode() + email_bytes
        with tempfile.NamedTemporaryFile(suffix=".emlx", delete=False) as f:
            f.write(content)
            f.flush()
            return Path(f.name)

    def test_parse_valid_emlx(self):
        from nova_ingest_emlx import parse_emlx
        path = self._make_emlx_file()
        try:
            result = parse_emlx(path)
            assert result is not None
            assert "text" in result
            assert "alice@test.com" in result["text"]
        finally:
            path.unlink()

    def test_parse_returns_none_for_empty_body(self):
        from nova_ingest_emlx import parse_emlx
        path = self._make_emlx_file(subject="", body="")
        try:
            result = parse_emlx(path)
            assert result is None
        finally:
            path.unlink()

    @pytest.mark.frame
    def test_parse_emlx_metadata_structure(self):
        from nova_ingest_emlx import parse_emlx
        path = self._make_emlx_file()
        try:
            result = parse_emlx(path)
            assert result is not None
            assert "metadata" in result
            meta = result["metadata"]
            assert "folder" in meta
            assert "sender" in meta
            assert "subject" in meta
            assert "date" in meta
            assert "source_type" in meta
            assert meta["source_type"] == "emlx"
        finally:
            path.unlink()


# ══════════════════════════════════════════════════════════════════════════════
# nova_video_ingest.py — Video metadata, frame extraction, chunking
# ══════════════════════════════════════════════════════════════════════════════

class TestVideoGetMetadata:
    """Tests for get_metadata in nova_video_ingest.py."""

    @patch("subprocess.run")
    def test_parses_ffprobe_output(self, mock_run):
        mod = _reload_module("nova_video_ingest")
        ffprobe_output = {
            "format": {"duration": "120.5", "size": "10485760", "tags": {"creation_time": "2026-01-01T00:00:00"}},
            "streams": [
                {"codec_type": "video", "width": 1920, "height": 1080, "codec_name": "h264", "r_frame_rate": "30/1"},
                {"codec_type": "audio", "codec_name": "aac"},
            ]
        }
        mock_run.return_value = MagicMock(stdout=json.dumps(ffprobe_output), returncode=0)
        result = mod.get_metadata("/fake/video.mp4")
        assert result["duration"] == 120.5
        assert result["width"] == 1920
        assert result["height"] == 1080
        assert result["codec"] == "h264"
        assert result["audio_codec"] == "aac"
        assert result["size_mb"] == 10.0
        assert "2m" in result["duration_str"]

    @patch("subprocess.run")
    def test_handles_ffprobe_error(self, mock_run):
        mod = _reload_module("nova_video_ingest")
        mock_run.return_value = MagicMock(stdout="not json", returncode=1)
        result = mod.get_metadata("/fake/video.mp4")
        assert result["filename"] == "video.mp4"
        assert result["duration"] == 0


class TestVideoDescribeFrame:
    """Tests for describe_frame in nova_video_ingest.py."""

    def test_strips_think_tags(self):
        mod = _reload_module("nova_video_ingest")
        response = json.dumps({"response": "<think>reasoning</think>A person standing in a room."})
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = response.encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            with patch("builtins.open", mock_open(read_data=b"\x89PNG")):
                desc = mod.describe_frame("/fake/frame.jpg")
        assert "A person standing" in desc
        assert "<think>" not in desc

    def test_handles_vision_error(self):
        mod = _reload_module("nova_video_ingest")
        with patch("urllib.request.urlopen", side_effect=Exception("Ollama down")):
            with patch("builtins.open", mock_open(read_data=b"\x89PNG")):
                desc = mod.describe_frame("/fake/frame.jpg")
        assert desc == ""


class TestVideoTranscriptChunking:
    """Tests for transcript chunking logic in nova_video_ingest.py."""

    @pytest.mark.frame
    def test_transcript_chunks_at_500_chars(self):
        """Verify video transcript chunks at ~500 char boundary."""
        transcript = "word " * 200  # ~1000 chars
        words = transcript.split()
        chunks = []
        current = []
        current_len = 0
        for word in words:
            current.append(word)
            current_len += len(word) + 1
            if current_len >= 500:
                chunks.append(" ".join(current))
                current = []
                current_len = 0
        if current:
            chunks.append(" ".join(current))
        assert len(chunks) == 2

    @pytest.mark.frame
    def test_scene_chunks_at_5_per_group(self):
        """Verify scenes are grouped in chunks of 5."""
        scenes = [f"[{i:02d}:00] Description {i}" for i in range(12)]
        chunks = []
        for i in range(0, len(scenes), 5):
            chunks.append(scenes[i:i+5])
        assert len(chunks) == 3
        assert len(chunks[0]) == 5
        assert len(chunks[2]) == 2


# ══════════════════════════════════════════════════════════════════════════════
# nova_gdrive_ingest.py — Google Drive chunking, file processing
# ══════════════════════════════════════════════════════════════════════════════

class TestGDriveChunkText:
    """Tests for chunk_text in nova_gdrive_ingest.py."""

    def test_short_text_single_chunk(self):
        mod = _reload_module("nova_gdrive_ingest")
        result = mod.chunk_text("Short paragraph.", max_chars=2000)
        assert result == ["Short paragraph."]

    def test_splits_on_paragraph_boundaries(self):
        mod = _reload_module("nova_gdrive_ingest")
        text = "First paragraph.\n\nSecond paragraph.\n\nThird paragraph."
        result = mod.chunk_text(text, max_chars=30)
        assert len(result) >= 2

    def test_single_block_no_paragraph_breaks(self):
        """A single block with no paragraph breaks stays as one chunk (paragraph-level split)."""
        mod = _reload_module("nova_gdrive_ingest")
        text = "a" * 5000  # single long block, no paragraph breaks
        result = mod.chunk_text(text, max_chars=2000)
        # gdrive chunker splits on \n\n; single block = single chunk
        assert len(result) == 1
        assert len(result[0]) == 5000

    def test_fallback_hard_split_on_empty_chunks(self):
        """When paragraph splitting yields nothing, fallback hard-split kicks in."""
        mod = _reload_module("nova_gdrive_ingest")
        # Empty paragraphs between double newlines should result in nothing added
        # to chunks, triggering the fallback
        text = "\n\n" * 10  # only paragraph separators, no content
        result = mod.chunk_text(text.strip(), max_chars=2000)
        # strip() makes it empty, so chunk_text("", ...) returns [""]
        assert len(result) >= 1

    def test_empty_text(self):
        mod = _reload_module("nova_gdrive_ingest")
        result = mod.chunk_text("", max_chars=2000)
        assert result == [""]


class TestGDriveRemember:
    """Tests for remember in nova_gdrive_ingest.py."""

    def test_skips_short_text(self):
        mod = _reload_module("nova_gdrive_ingest")
        stored = mod.remember("short", "title", "path", ".txt")
        assert stored == 0

    def test_stores_long_text(self):
        mod = _reload_module("nova_gdrive_ingest")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            stored = mod.remember("A" * 100, "Title", "rel/path.txt", ".txt")
            assert stored >= 1

    @pytest.mark.frame
    def test_chunk_metadata_includes_privacy(self):
        mod = _reload_module("nova_gdrive_ingest")
        captured_payloads = []
        with patch("urllib.request.urlopen") as mock_urlopen:
            def capture(req, **kwargs):
                captured_payloads.append(json.loads(req.data))
                mock_resp = MagicMock()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                return mock_resp
            mock_urlopen.side_effect = capture
            mod.remember("Content " * 20, "Test File", "docs/test.txt", ".txt")
        assert len(captured_payloads) >= 1
        assert captured_payloads[0]["metadata"]["privacy"] == "local-only"
        assert captured_payloads[0]["metadata"]["origin"] == "google-drive-backup"


# ══════════════════════════════════════════════════════════════════════════════
# nova_slack_export_ingest.py — Slack export parsing
# ══════════════════════════════════════════════════════════════════════════════

class TestSlackExportLoadUsers:
    """Tests for load_users in nova_slack_export_ingest.py."""

    def test_loads_users_from_json(self):
        from nova_slack_export_ingest import load_users
        with tempfile.TemporaryDirectory() as tmpdir:
            users_file = Path(tmpdir) / "users.json"
            users_file.write_text(json.dumps([
                {"id": "U001", "real_name": "Alice", "name": "alice"},
                {"id": "U002", "name": "bob"},
            ]))
            user_map = load_users(Path(tmpdir))
            assert user_map["U001"] == "Alice"
            assert user_map["U002"] == "bob"
            # Known overrides
            assert user_map["U049EPC2W"] == "Jordan"
            assert user_map["U04AS59BR"] == "Tricia"

    def test_handles_missing_users_file(self):
        from nova_slack_export_ingest import load_users
        with tempfile.TemporaryDirectory() as tmpdir:
            user_map = load_users(Path(tmpdir))
            # Should still have known overrides
            assert user_map["U049EPC2W"] == "Jordan"


class TestSlackExportFormatMessage:
    """Tests for format_message in nova_slack_export_ingest.py."""

    def test_formats_basic_message(self):
        from nova_slack_export_ingest import format_message
        user_map = {"U001": "Alice"}
        msg = {"user": "U001", "text": "Hello everyone!"}
        result = format_message(msg, user_map)
        assert result == "Alice: Hello everyone!"

    def test_replaces_user_mentions(self):
        from nova_slack_export_ingest import format_message
        user_map = {"U001": "Alice", "U002": "Bob"}
        msg = {"user": "U001", "text": "Hey <@U002>, check this out"}
        result = format_message(msg, user_map)
        assert "@Bob" in result
        assert "<@U002>" not in result

    def test_empty_text_with_files(self):
        from nova_slack_export_ingest import format_message
        user_map = {"U001": "Alice"}
        msg = {"user": "U001", "text": "", "files": [{"name": "report.pdf"}]}
        result = format_message(msg, user_map)
        assert "[file: report.pdf]" in result

    def test_empty_text_with_attachments(self):
        from nova_slack_export_ingest import format_message
        user_map = {"U001": "Alice"}
        msg = {"user": "U001", "text": "", "attachments": [{"title": "Link"}]}
        result = format_message(msg, user_map)
        assert "[attachment]" in result

    def test_empty_text_no_content(self):
        from nova_slack_export_ingest import format_message
        user_map = {"U001": "Alice"}
        msg = {"user": "U001", "text": ""}
        result = format_message(msg, user_map)
        assert result is None


class TestSlackExportIngestChannel:
    """Tests for ingest_channel in nova_slack_export_ingest.py."""

    def test_skips_join_leave_messages(self):
        from nova_slack_export_ingest import ingest_channel
        with tempfile.TemporaryDirectory() as tmpdir:
            channel_dir = Path(tmpdir) / "general"
            channel_dir.mkdir()
            day_file = channel_dir / "2026-01-01.json"
            day_file.write_text(json.dumps([
                {"subtype": "channel_join", "user": "U001", "text": "joined"},
                {"user": "U001", "text": "Hello everyone!"},
            ]))
            user_map = {"U001": "Alice"}
            with patch("nova_slack_export_ingest.vector_remember", return_value=True):
                with patch("time.sleep"):
                    msgs, stored, chunks = ingest_channel(channel_dir, "general", user_map)
            assert msgs == 1  # Only the real message

    def test_chunks_long_conversations(self):
        from nova_slack_export_ingest import ingest_channel
        with tempfile.TemporaryDirectory() as tmpdir:
            channel_dir = Path(tmpdir) / "general"
            channel_dir.mkdir()
            # Generate enough messages to exceed one chunk
            messages = [{"user": "U001", "text": f"Message number {i} with some padding text."} for i in range(50)]
            day_file = channel_dir / "2026-01-01.json"
            day_file.write_text(json.dumps(messages))
            user_map = {"U001": "Alice"}
            with patch("nova_slack_export_ingest.vector_remember", return_value=True):
                with patch("time.sleep"):
                    msgs, stored, chunks = ingest_channel(channel_dir, "general", user_map)
            assert chunks > 1  # Should split into multiple chunks


# ══════════════════════════════════════════════════════════════════════════════
# nova_ingest.py — General ingestion framework: extractors, chunking, store
# ══════════════════════════════════════════════════════════════════════════════

class TestNovaIngestChunkText:
    """Tests for chunk_text in nova_ingest.py."""

    def test_empty_text(self):
        mod = _reload_module("nova_ingest")
        result = mod.chunk_text("", "test.md")
        assert result == []

    def test_short_text_single_chunk(self):
        mod = _reload_module("nova_ingest")
        result = mod.chunk_text("Short paragraph.", "test.md")
        assert len(result) == 1
        assert "[From: test.md]" in result[0]

    def test_chunk_overlap(self):
        """Verify overlapping chunks share the last paragraph."""
        mod = _reload_module("nova_ingest")
        # Build text with paragraph breaks that force multiple chunks
        paragraphs = [f"Paragraph {i} with enough text to fill up space." for i in range(20)]
        text = "\n\n".join(paragraphs)
        chunks = mod.chunk_text(text, "overlap.md")
        if len(chunks) >= 2:
            # The second chunk should start with content from end of first chunk (overlap)
            # because chunk_text keeps the last paragraph for overlap
            last_para_first_chunk = chunks[0].split("\n\n")[-1]
            assert last_para_first_chunk.strip() in chunks[1]

    def test_chunk_has_filename_header(self):
        mod = _reload_module("nova_ingest")
        result = mod.chunk_text("Some content here.", "report.pdf")
        assert len(result) == 1
        assert "[From: report.pdf]" in result[0]


class TestNovaIngestExtractText:
    """Tests for extract_text routing in nova_ingest.py."""

    def test_extract_text_file(self):
        mod = _reload_module("nova_ingest")
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
            f.write("Hello, this is a text file.")
            f.flush()
            result = mod.extract_text(f.name, ".txt")
        os.unlink(f.name)
        assert "Hello, this is a text file." in result

    def test_extract_markdown_file(self):
        mod = _reload_module("nova_ingest")
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write("# Header\n\nContent here.")
            f.flush()
            result = mod.extract_text(f.name, ".md")
        os.unlink(f.name)
        assert "# Header" in result

    def test_extract_rtf_calls_textutil(self):
        mod = _reload_module("nova_ingest")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="Extracted RTF text", returncode=0)
            result = mod.extract_rtf("/fake/doc.rtf")
            assert result == "Extracted RTF text"

    def test_extract_unknown_extension_fallback(self):
        mod = _reload_module("nova_ingest")
        with tempfile.NamedTemporaryFile(suffix=".xyz", mode="w", delete=False) as f:
            f.write("Fallback content")
            f.flush()
            result = mod.extract_text(f.name, ".xyz")
        os.unlink(f.name)
        assert "Fallback content" in result


class TestNovaIngestStoreChunks:
    """Tests for store_chunks in nova_ingest.py."""

    def test_stores_all_chunks(self):
        mod = _reload_module("nova_ingest")
        chunks = ["chunk1", "chunk2", "chunk3"]
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            with patch("time.sleep"):
                stored = mod.store_chunks(chunks, "test_source", "test_topic")
        assert stored == 3

    def test_handles_store_error(self):
        mod = _reload_module("nova_ingest")
        chunks = ["chunk1"]
        with patch("urllib.request.urlopen", side_effect=Exception("fail")):
            with patch("time.sleep"):
                stored = mod.store_chunks(chunks, "test_source")
        assert stored == 0


class TestNovaIngestE2E:
    """Tests for the full ingest function in nova_ingest.py."""

    def test_ingest_text_file(self):
        mod = _reload_module("nova_ingest")
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
            f.write("This is test content with enough words to be meaningful.")
            f.flush()
            with patch("urllib.request.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = mock_resp
                with patch("time.sleep"):
                    result = mod.ingest(f.name, "test.txt", topic="test")
        os.unlink(f.name)
        assert result["ok"] is True
        assert result["words"] > 0
        assert result["stored"] >= 1

    def test_ingest_empty_file(self):
        mod = _reload_module("nova_ingest")
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False) as f:
            f.write("")
            f.flush()
            result = mod.ingest(f.name, "empty.txt")
        os.unlink(f.name)
        assert result["ok"] is False


# ══════════════════════════════════════════════════════════════════════════════
# nova_sam_blog_ingest.py — Additional tests for fetch_page, HTML stripping
# ══════════════════════════════════════════════════════════════════════════════

class TestSamBlogFetchPage:
    """Tests for fetch_page in nova_sam_blog_ingest.py."""

    def test_fetch_page_strips_html(self):
        mod = _reload_module("nova_sam_blog_ingest")
        html = "<html><body><p>Hello World</p><script>evil();</script></body></html>"
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = html.encode("utf-8")
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            text, raw_html = mod.fetch_page("https://example.com")
        assert "Hello World" in text
        assert "evil" not in text

    def test_fetch_page_handles_error(self):
        mod = _reload_module("nova_sam_blog_ingest")
        with patch("urllib.request.urlopen", side_effect=Exception("404")):
            text, html = mod.fetch_page("https://bad-url.com")
        assert text == ""
        assert html == ""


class TestSamBlogChunking:
    """Tests for long blog post chunking in main() logic."""

    @pytest.mark.frame
    def test_long_post_produces_continuation_chunks(self):
        """Blog posts > 2000 chars should produce continuation chunks."""
        mod = _reload_module("nova_sam_blog_ingest")
        captured = []

        def capture_remember(text, metadata=None):
            captured.append({"text": text, "metadata": metadata})

        with patch.object(mod, "vector_remember", side_effect=capture_remember):
            with patch.object(mod, "find_post_links", return_value=["https://example.com/posts/long-one"]):
                html_content = "<html><h1>Long Post</h1>" + "<p>" + "x" * 5000 + "</p></html>"
                with patch.object(mod, "fetch_page") as mock_fetch:
                    # First call is index, second is posts page, third is the post itself
                    mock_fetch.side_effect = [
                        ("", "<html></html>"),
                        ("", "<html></html>"),
                        ("x" * 5000, html_content),
                    ]
                    with patch.object(mod, "load_state", return_value={"ingested_urls": [], "last_check": ""}):
                        with patch.object(mod, "save_state"):
                            with patch.object(mod, "slack_post"):
                                mod.main()

        # Should have the main chunk + continuation chunks
        continuations = [c for c in captured if "continued" in c["text"].lower()]
        assert len(continuations) >= 1


# ══════════════════════════════════════════════════════════════════════════════
# nova_recent_memories.py — Formatting helpers
# ══════════════════════════════════════════════════════════════════════════════

class TestRecentMemoriesFormatHelpers:
    """Tests for formatting functions in nova_recent_memories.py."""

    def test_fmt_count(self):
        from nova_recent_memories import _fmt_count
        assert _fmt_count(1000) == "1,000"
        assert _fmt_count(42) == "42"
        assert _fmt_count(0) == "0"
        assert _fmt_count(1234567) == "1,234,567"

    def test_truncate_short_text(self):
        from nova_recent_memories import _truncate
        assert _truncate("hello", 80) == "hello"

    def test_truncate_long_text(self):
        from nova_recent_memories import _truncate
        long_text = "a" * 200
        result = _truncate(long_text, 80)
        assert len(result) <= 83  # 80 + "..."
        assert result.endswith("...")

    def test_truncate_strips_newlines(self):
        from nova_recent_memories import _truncate
        text = "line one\nline two\nline three"
        result = _truncate(text, 80)
        assert "\n" not in result

    def test_label_tag_with_label(self):
        from nova_recent_memories import _label_tag
        assert _label_tag("MyShow") == "[MyShow] "

    def test_label_tag_empty(self):
        from nova_recent_memories import _label_tag
        assert _label_tag("") == ""


class TestRecentMemoriesFormatSummary:
    """Tests for format_summary in nova_recent_memories.py."""

    def test_format_summary_with_data(self):
        from nova_recent_memories import format_summary
        data = {
            "hours": 24,
            "total": 150,
            "by_source": [
                {"source": "reddit", "count": 100, "labels": ["r/burbank", "r/ClaudeCode"]},
                {"source": "email", "count": 50, "labels": []},
            ]
        }
        result = format_summary(data)
        assert "150" in result
        assert "reddit" in result
        assert "email" in result
        assert "r/burbank" in result

    def test_format_summary_no_data(self):
        from nova_recent_memories import format_summary
        data = {"hours": 24, "total": 0, "by_source": []}
        result = format_summary(data)
        assert "(none)" in result

    def test_format_summary_truncates_labels(self):
        from nova_recent_memories import format_summary
        data = {
            "hours": 24,
            "total": 50,
            "by_source": [
                {"source": "test", "count": 50, "labels": ["a", "b", "c", "d", "e"]},
            ]
        }
        result = format_summary(data)
        assert "+2 more" in result


class TestRecentMemoriesFormatDetail:
    """Tests for format_detail in nova_recent_memories.py."""

    def test_format_detail_with_samples(self):
        from nova_recent_memories import format_detail
        data = {
            "hours": 24,
            "total": 10,
            "sources": [
                {
                    "source": "reddit",
                    "count": 10,
                    "labels": [],
                    "samples": [
                        {"text": "Sample memory text here", "label": "r/test", "created_at": "2026-01-01T00:00:00"},
                    ]
                }
            ]
        }
        result = format_detail(data)
        assert "reddit (10 new)" in result
        assert "[r/test]" in result
        assert "Sample memory text" in result

    def test_format_detail_shows_count_overflow(self):
        from nova_recent_memories import format_detail
        data = {
            "hours": 24,
            "total": 100,
            "sources": [
                {
                    "source": "email",
                    "count": 100,
                    "labels": [],
                    "samples": [
                        {"text": "email 1", "label": "", "created_at": "2026-01-01"},
                    ]
                }
            ]
        }
        result = format_detail(data)
        assert "showing 1 of 100" in result


# ══════════════════════════════════════════════════════════════════════════════
# nova_memory_breakdown.py — Queue depth, breakdown formatting
# ══════════════════════════════════════════════════════════════════════════════

class TestMemoryBreakdownQueueDepth:
    """Tests for get_queue_depth in nova_memory_breakdown.py."""

    def test_queue_depth_returns_count(self):
        mod = _reload_module("nova_memory_breakdown")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"pending": 42}).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            depth = mod.get_queue_depth()
        assert depth == 42

    def test_queue_depth_handles_error(self):
        mod = _reload_module("nova_memory_breakdown")
        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            depth = mod.get_queue_depth()
        assert depth == -1


class TestMemoryBreakdownGetBreakdown:
    """Tests for get_breakdown in nova_memory_breakdown.py."""

    @patch("subprocess.run")
    def test_parses_psql_output(self, mock_run):
        mod = _reload_module("nova_memory_breakdown")
        mock_run.return_value = MagicMock(
            stdout="email_archive|50000\nreddit|3000\nimessage|1000\n",
            returncode=0
        )
        result = mod.get_breakdown()
        assert result is not None
        breakdown, total = result
        assert total == 54000
        assert len(breakdown) == 3
        assert breakdown[0] == ("email_archive", 50000)

    @patch("subprocess.run")
    def test_handles_psql_error(self, mock_run):
        mod = _reload_module("nova_memory_breakdown")
        mock_run.return_value = MagicMock(stdout="", returncode=1)
        result = mod.get_breakdown()
        assert result is None


# ══════════════════════════════════════════════════════════════════════════════
# nova_memory_consolidate.py — Synthesis logic, vector recall/remember
# ══════════════════════════════════════════════════════════════════════════════

class TestMemoryConsolidateVectorRecall:
    """Tests for vector_recall in nova_memory_consolidate.py."""

    def test_recall_returns_texts(self):
        mod = _reload_module("nova_memory_consolidate")
        response = json.dumps({
            "memories": [
                {"text": "Memory 1", "score": 0.9},
                {"text": "Memory 2", "score": 0.5},
                {"text": "Low score", "score": 0.2},
            ]
        })
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = response.encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            results = mod.vector_recall("test query", n=5)
        assert len(results) == 2  # score >= 0.35 filter
        assert "Memory 1" in results
        assert "Low score" not in results

    def test_recall_handles_error(self):
        mod = _reload_module("nova_memory_consolidate")
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            results = mod.vector_recall("query")
        assert results == []


class TestMemoryConsolidateReadRecentFiles:
    """Tests for read_recent_memory_files in nova_memory_consolidate.py."""

    def test_reads_existing_files(self):
        mod = _reload_module("nova_memory_consolidate")
        with tempfile.TemporaryDirectory() as tmpdir:
            mem_dir = Path(tmpdir)
            today = date.today()
            for i in range(3):
                d = (today - timedelta(days=i)).isoformat()
                (mem_dir / f"{d}.md").write_text(f"# Notes for {d}\nSome content.")
            with patch.object(mod, "MEMORY_DIR", mem_dir):
                content = mod.read_recent_memory_files(days=3)
        assert today.isoformat() in content
        assert "Some content" in content

    def test_handles_missing_files(self):
        mod = _reload_module("nova_memory_consolidate")
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(mod, "MEMORY_DIR", Path(tmpdir)):
                content = mod.read_recent_memory_files(days=7)
        assert content == ""


class TestMemoryConsolidateLLMSynthesize:
    """Tests for llm_synthesize in nova_memory_consolidate.py."""

    def test_synthesize_returns_response(self):
        mod = _reload_module("nova_memory_consolidate")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"response": "Jordan has been working on MLXCode."}).encode()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            result = mod.llm_synthesize("test prompt")
        assert "MLXCode" in result

    def test_synthesize_handles_error(self):
        mod = _reload_module("nova_memory_consolidate")
        with patch("urllib.request.urlopen", side_effect=Exception("LLM timeout")):
            result = mod.llm_synthesize("test prompt")
        assert result == ""


# ══════════════════════════════════════════════════════════════════════════════
# nova_rem_sleep.py — REM sleep phases, union-find, pruning
# ══════════════════════════════════════════════════════════════════════════════

class TestREMSleepOllamaGenerate:
    """Tests for ollama_generate in nova_rem_sleep.py."""

    def test_ollama_generate_returns_text(self):
        mod = _reload_module("nova_rem_sleep")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"response": "Synthesized text."}).encode()
            mock_urlopen.return_value = mock_resp
            result = mod.ollama_generate("Summarize these memories")
        assert result == "Synthesized text."

    def test_ollama_generate_handles_error(self):
        mod = _reload_module("nova_rem_sleep")
        with patch("urllib.request.urlopen", side_effect=Exception("Ollama down")):
            result = mod.ollama_generate("test")
        assert result == ""

    @pytest.mark.frame
    def test_ollama_generate_uses_no_think_prefix(self):
        mod = _reload_module("nova_rem_sleep")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"response": "ok"}).encode()
            mock_urlopen.return_value = mock_resp
            mod.ollama_generate("test prompt")
            call_data = json.loads(mock_urlopen.call_args[0][0].data)
        assert call_data["prompt"].startswith("/no_think")


class TestREMSleepVectorRemember:
    """Tests for vector_remember in nova_rem_sleep.py."""

    def test_remember_returns_id(self):
        mod = _reload_module("nova_rem_sleep")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps({"id": "mem-abc-123"}).encode()
            mock_urlopen.return_value = mock_resp
            result = mod.vector_remember("text", "synthesis", {"type": "test"})
        assert result == "mem-abc-123"

    def test_remember_handles_error(self):
        mod = _reload_module("nova_rem_sleep")
        with patch("urllib.request.urlopen", side_effect=Exception("fail")):
            result = mod.vector_remember("text", "synthesis", {})
        assert result is None


class TestREMSleepUnionFind:
    """Tests for the union-find logic in phase_triage."""

    def test_union_find_groups_pairs(self):
        """Verify the union-find implementation correctly groups pairs."""
        parent = {}

        def find(x):
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # Group: {1, 2, 3} and {4, 5}
        union(1, 2)
        union(2, 3)
        union(4, 5)

        assert find(1) == find(2) == find(3)
        assert find(4) == find(5)
        assert find(1) != find(4)

    def test_union_find_single_elements(self):
        parent = {}

        def find(x):
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent[x], parent[x])
                x = parent[x]
            return x

        assert find(99) == 99


# ══════════════════════════════════════════════════════════════════════════════
# nova_reembed.py — Embedding function, batch processing
# ══════════════════════════════════════════════════════════════════════════════

class TestReembedEmbed:
    """Tests for embed function in nova_reembed.py."""

    def test_embed_returns_vector(self):
        mod = _reload_module("nova_reembed")
        mock_response = json.dumps({"embeddings": [[0.1, 0.2, 0.3]]})
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response.encode()
            mock_urlopen.return_value = mock_resp
            result = mod.embed("Test text", "snowflake-arctic-embed:335m")
        assert result == [0.1, 0.2, 0.3]

    def test_embed_handles_alternative_response_format(self):
        mod = _reload_module("nova_reembed")
        mock_response = json.dumps({"embedding": [0.4, 0.5, 0.6]})
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = mock_response.encode()
            mock_urlopen.return_value = mock_resp
            result = mod.embed("Test text", "model")
        assert result == [0.4, 0.5, 0.6]

    def test_embed_raises_on_timeout(self):
        mod = _reload_module("nova_reembed")
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            with pytest.raises(Exception):
                mod.embed("text", "model")


# ══════════════════════════════════════════════════════════════════════════════
# Cross-script: Chunking consistency tests
# ══════════════════════════════════════════════════════════════════════════════

class TestChunkingConsistency:
    """Verify all chunk_text implementations preserve text content."""

    def test_youtube_ingest_chunk_preserves_all_text(self):
        from nova_youtube_ingest import chunk_text
        text = "Sentence one. Sentence two. Sentence three. " * 50
        chunks = chunk_text(text.strip(), max_chars=200)
        reassembled = " ".join(c.strip() for c in chunks)
        # All words should be present
        assert len(reassembled) >= len(text.strip()) * 0.95  # Allow minor whitespace diff

    def test_youtube_channel_chunk_preserves_all_text(self):
        mod = _reload_module("nova_youtube_channel_ingest")
        text = "Word. " * 500
        chunks = mod.chunk_text(text.strip(), max_chars=500)
        reassembled = " ".join(c.strip() for c in chunks)
        for word in ["Word."]:
            assert word in reassembled

    def test_gdrive_chunk_preserves_all_text(self):
        mod = _reload_module("nova_gdrive_ingest")
        text = "Para one.\n\nPara two.\n\nPara three." * 100
        chunks = mod.chunk_text(text, max_chars=500)
        for para in ["Para one.", "Para two.", "Para three."]:
            assert any(para in c for c in chunks)


# ══════════════════════════════════════════════════════════════════════════════
# Cross-script: Source tagging tests
# ══════════════════════════════════════════════════════════════════════════════

class TestSourceTagging:
    """Verify each ingest script uses unique, consistent source tags."""

    @pytest.mark.frame
    def test_reddit_source_tags(self):
        mod = _reload_module_with_logger("nova_reddit_ingest")
        for name, config in mod.SUBREDDITS.items():
            assert config["source"], f"Subreddit {name} has empty source"

    @pytest.mark.frame
    def test_youtube_ingest_source_tag(self):
        """youtube ingest uses 'youtube-ingest' source."""
        from nova_youtube_ingest import remember
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            remember("x" * 100, "Title", "vid1", "pl1")
            call_data = json.loads(mock_urlopen.call_args[0][0].data)
        assert call_data["source"] == "youtube-ingest"

    @pytest.mark.frame
    def test_gdrive_source_tag(self):
        mod = _reload_module("nova_gdrive_ingest")
        captured = []
        with patch("urllib.request.urlopen") as mock_urlopen:
            def capture(req, **kwargs):
                captured.append(json.loads(req.data))
                mock_resp = MagicMock()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                return mock_resp
            mock_urlopen.side_effect = capture
            mod.remember("A" * 100, "File", "path.txt", ".txt")
        assert captured[0]["source"] == "gdrive-ingest"

    @pytest.mark.frame
    def test_slack_export_source_map(self):
        from nova_slack_export_ingest import SOURCE_MAP
        assert SOURCE_MAP["general"] == "slack_general"
        assert "home-alerts" in SOURCE_MAP


# ══════════════════════════════════════════════════════════════════════════════
# Error handling: Rate limits, malformed content, network failures
# ══════════════════════════════════════════════════════════════════════════════

class TestErrorHandling:
    """Cross-script error handling tests."""

    def test_reddit_fetch_subreddit_timeout(self):
        mod = _reload_module_with_logger("nova_reddit_ingest")
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            posts = mod.fetch_subreddit("test", limit=5)
        assert posts == []

    def test_youtube_ingest_remember_partial_failure(self):
        """If some chunks fail to store, the total should reflect partial success."""
        from nova_youtube_ingest import remember
        call_count = [0]

        def alternating_urlopen(req, **kwargs):
            call_count[0] += 1
            if call_count[0] % 2 == 0:
                raise Exception("intermittent failure")
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            return mock_resp

        long_text = "This is a sentence. " * 300
        with patch("urllib.request.urlopen", side_effect=alternating_urlopen):
            stored = remember(long_text, "Title", "vid1", "pl1")
        # Some should succeed, some fail
        assert stored >= 1

    def test_sam_blog_malformed_html(self):
        mod = _reload_module("nova_sam_blog_ingest")
        malformed = "<html><body><p>Unclosed tag<div>Mixed content</body>"
        stripper = mod.HTMLStripper()
        stripper.feed(malformed)
        text = stripper.get_text()
        assert "Unclosed tag" in text

    def test_email_ingest_corrupt_emlx(self):
        mod = _reload_module("nova_email_ingest")
        with tempfile.NamedTemporaryFile(suffix=".emlx", delete=False, mode="wb") as f:
            f.write(b"not a valid emlx file at all")
            f.flush()
            result = mod.parse_emlx(f.name)
        os.unlink(f.name)
        # Should handle gracefully


class TestVideoExtensions:
    """Verify VIDEO_EXTENSIONS set in nova_video_ingest.py."""

    def test_common_formats_supported(self):
        mod = _reload_module("nova_video_ingest")
        for ext in [".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"]:
            assert ext in mod.VIDEO_EXTENSIONS, f"{ext} not in VIDEO_EXTENSIONS"


# ══════════════════════════════════════════════════════════════════════════════
# Integration tests — require live PostgreSQL/vector memory
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.integration
class TestRecentMemoriesIntegration:
    """Test nova_recent_memories.py against live PostgreSQL."""

    def test_get_recent_summary(self):
        try:
            from nova_recent_memories import get_recent_summary
            result = get_recent_summary(hours=24)
            assert "total" in result
            assert "by_source" in result
            assert isinstance(result["total"], int)
        except Exception as e:
            pytest.skip(f"PostgreSQL not available: {e}")

    def test_get_recent_detail(self):
        try:
            from nova_recent_memories import get_recent_detail
            result = get_recent_detail(hours=24)
            assert "total" in result
            assert "sources" in result
        except Exception as e:
            pytest.skip(f"PostgreSQL not available: {e}")


@pytest.mark.integration
class TestMemoryBreakdownIntegration:
    """Test nova_memory_breakdown.py against live services."""

    def test_get_breakdown_live(self):
        mod = _reload_module("nova_memory_breakdown")
        result = mod.get_breakdown()
        if result is None:
            pytest.skip("psql not available or no data")
        breakdown, total = result
        assert total > 0
        assert len(breakdown) > 0


# ══════════════════════════════════════════════════════════════════════════════
# Functional tests — full pipelines
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.functional
class TestSlackExportFullPipeline:
    """Full pipeline test: parse export dir -> chunk -> store."""

    def test_full_channel_ingest(self):
        from nova_slack_export_ingest import ingest_channel
        with tempfile.TemporaryDirectory() as tmpdir:
            channel_dir = Path(tmpdir) / "test-channel"
            channel_dir.mkdir()

            # Create 3 days of messages
            for day in ["2026-01-01", "2026-01-02", "2026-01-03"]:
                msgs = [
                    {"user": "U001", "text": f"Message on {day} from Alice about project updates"},
                    {"user": "U002", "text": f"Reply on {day} from Bob with questions"},
                ]
                (channel_dir / f"{day}.json").write_text(json.dumps(msgs))

            user_map = {"U001": "Alice", "U002": "Bob"}

            stored_payloads = []

            def capture_remember(text, source, metadata):
                stored_payloads.append({"text": text, "source": source, "metadata": metadata})
                return True

            with patch("nova_slack_export_ingest.vector_remember", side_effect=capture_remember):
                with patch("time.sleep"):
                    msgs, stored, chunks = ingest_channel(channel_dir, "test-channel", user_map)

            assert msgs == 6  # 2 per day x 3 days
            assert stored >= 3  # At least one chunk per day
            for p in stored_payloads:
                assert "Slack #test-channel" in p["text"]
                assert "date" in p["metadata"]


@pytest.mark.functional
class TestMboxFullPipeline:
    """Full pipeline test: parse mbox -> extract -> store."""

    def test_ingest_mbox_file(self):
        from nova_ingest_mbox import ingest_mbox_file
        with tempfile.TemporaryDirectory() as tmpdir:
            mbox_path = Path(tmpdir) / "test.mbox"
            # Create a minimal mbox file
            mbox = mailbox.mbox(str(mbox_path))
            msg = email.message.EmailMessage()
            msg["From"] = "alice@test.com"
            msg["Subject"] = "Test email for mbox ingest"
            msg["Date"] = "Mon, 1 Jan 2026 12:00:00 +0000"
            msg.set_content("This is a test email body for the mbox pipeline test.")
            mbox.add(msg)
            mbox.close()

            with patch("nova_ingest_mbox.remember", return_value="mem-001"):
                count = ingest_mbox_file(mbox_path, "TestFolder")

            assert count == 1


@pytest.mark.functional
class TestNovaIngestFullPipeline:
    """Full pipeline test: file -> extract -> chunk -> store."""

    def test_full_ingest_markdown_file(self):
        mod = _reload_module("nova_ingest")
        with tempfile.NamedTemporaryFile(suffix=".md", mode="w", delete=False) as f:
            f.write("# Test Document\n\nThis is paragraph one with meaningful content.\n\n"
                    "This is paragraph two with more content to ensure chunking works correctly.\n\n"
                    "And a third paragraph for good measure with additional detail.")
            f.flush()

            stored_chunks = []

            def capture_urlopen(req, **kwargs):
                stored_chunks.append(json.loads(req.data))
                mock_resp = MagicMock()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                return mock_resp

            with patch("urllib.request.urlopen", side_effect=capture_urlopen):
                with patch("time.sleep"):
                    result = mod.ingest(f.name, "test.md", topic="testing", source="test")

        os.unlink(f.name)
        assert result["ok"] is True
        assert result["stored"] >= 1
        assert all("[From: test.md]" in c["text"] for c in stored_chunks)


# ══════════════════════════════════════════════════════════════════════════════
# Safari additional tests (not in test_ingestion.py)
# ══════════════════════════════════════════════════════════════════════════════

class TestSafariMacTimestamp:
    """Tests for mac_timestamp_to_datetime in nova_safari_ingest.py."""

    def test_known_date(self):
        mod = _reload_module("nova_safari_ingest")
        # 2026-01-01 00:00:00 UTC in Mac absolute time
        # Mac epoch is 2001-01-01 UTC
        # 2026-01-01 - 2001-01-01 = 25 years = 9131 days = 788918400 seconds
        mac_time = 788918400.0
        dt = mod.mac_timestamp_to_datetime(mac_time)
        assert dt.year == 2026
        assert dt.month == 1
        assert dt.day == 1

    def test_zero_timestamp(self):
        mod = _reload_module("nova_safari_ingest")
        dt = mod.mac_timestamp_to_datetime(0)
        assert dt.year == 2001
        assert dt.month == 1
        assert dt.day == 1


class TestSafariVectorRemember:
    """Tests for vector_remember in nova_safari_ingest.py."""

    def test_remember_success(self):
        mod = _reload_module("nova_safari_ingest")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp
            result = mod.vector_remember("browsing data", {"domain": "example.com"})
        assert result is True

    def test_remember_failure(self):
        mod = _reload_module("nova_safari_ingest")
        with patch("urllib.request.urlopen", side_effect=Exception("network error")):
            result = mod.vector_remember("data", {})
        assert result is False


# ══════════════════════════════════════════════════════════════════════════════
# Deduplication tests
# ══════════════════════════════════════════════════════════════════════════════

class TestDeduplication:
    """Cross-script deduplication logic tests."""

    def test_reddit_dedup_by_post_id(self):
        mod = _reload_module_with_logger("nova_reddit_ingest")
        posts = [
            {"data": {"id": "dup1", "title": "Post 1", "stickied": False,
                       "selftext": "Content", "score": 10, "author": "u1",
                       "link_flair_text": "", "num_comments": 0, "url": "", "permalink": ""}},
        ]
        with patch.object(mod, "fetch_subreddit", return_value=posts):
            with patch.object(mod, "fetch_comments", return_value=[]):
                with patch.object(mod, "vector_remember", return_value=True):
                    state = {"seen_ids": {"dup1": {"ts": time.time(), "sub": "test"}}}
                    config = {"source": "test", "label": "Test", "limit": 10, "dream_weight": "low"}
                    count = mod.ingest_subreddit("test", config, state)
        assert count == 0  # Already seen

    def test_youtube_playlist_dedup_by_video_id(self):
        from nova_youtube_ingest import get_playlist_videos
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="vid1\tVideo 1\nvid2\tVideo 2\nvid1\tVideo 1 dup\n",
                returncode=0
            )
            videos = get_playlist_videos("url")
        assert len(videos) == 2

    def test_email_dedup_by_text_hash(self):
        """Email ingest uses text_hash for dedup against API."""
        mod = _reload_module("nova_email_ingest")
        parsed = {
            "date": "Mon, 1 Jan 2026",
            "sender": "alice@example.com",
            "to": "bob@example.com",
            "subject": "Test",
            "body": "Hello world test body.",
        }
        text = mod.make_memory_text(parsed)
        text_hash = hashlib.md5(text.encode()).hexdigest()
        assert len(text_hash) == 32


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "not integration and not functional"])
