#!/usr/bin/env python3
"""
test_daily_content.py — Combined tests for nova_daily_essay.py, nova_daily_opinion.py,
and nova_weekly_digest.py.

Covers: source selection, memory fetching, LLM generation with fallbacks,
email delivery, Slack posting, Hugo publishing, digest compilation,
state management, security (no PII leakage, email scrubbing).

Run: python3 -m pytest tests/test_daily_content.py -v
Written by Jordan Koch.
"""

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def essay_module(mock_nova_config):
    """Import nova_daily_essay fresh."""
    for mod in list(sys.modules.keys()):
        if "nova_daily_essay" in mod:
            del sys.modules[mod]
    # Mock herd_config and subprocess calls for Keychain
    sys.modules["herd_config"] = MagicMock(HERD=[{"name": "Test", "email": "test@example.com"}])
    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=1, stdout="", stderr="")
        import nova_daily_essay
    return nova_daily_essay


@pytest.fixture
def opinion_module(mock_nova_config):
    """Import nova_daily_opinion fresh."""
    for mod in list(sys.modules.keys()):
        if "nova_daily_opinion" in mod:
            del sys.modules[mod]
    sys.modules["herd_config"] = MagicMock(HERD=[{"name": "Test", "email": "test@example.com"}])
    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=1, stdout="", stderr="")
        import nova_daily_opinion
    return nova_daily_opinion


@pytest.fixture
def digest_module(mock_nova_config):
    """Import nova_weekly_digest fresh."""
    for mod in list(sys.modules.keys()):
        if "nova_weekly_digest" in mod:
            del sys.modules[mod]
    sys.modules["herd_config"] = MagicMock(HERD=[{"name": "Test", "email": "test@example.com"}])
    with patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=1, stdout="", stderr="")
        import nova_weekly_digest
    return nova_weekly_digest


# ═══════════════════════════════════════════════════════════════════════════════
# DAILY ESSAY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestEssayStateManagement:
    """Tests for essay state load/save."""

    def test_load_default_state(self, essay_module, tmp_path):
        with patch.object(essay_module, "STATE_FILE", tmp_path / "nope.json"):
            state = essay_module.load_state()
        assert state == {"recent_sources": [], "essay_count": 0}

    def test_save_state(self, essay_module, tmp_path):
        sf = tmp_path / "essay_state.json"
        with patch.object(essay_module, "STATE_FILE", sf):
            essay_module.save_state({"recent_sources": ["security"], "essay_count": 5})
        data = json.loads(sf.read_text())
        assert data["essay_count"] == 5


class TestEssaySubjectSelection:
    """Tests for essay source/subject picking."""

    @patch("nova_daily_essay.get_source_counts_from_db")
    @patch("nova_daily_essay.get_sources_with_counts")
    def test_avoids_recent_sources(self, mock_api, mock_db, essay_module):
        mock_api.return_value = [
            {"source": "security", "count": 100},
            {"source": "philosophy", "count": 80},
        ]
        state = {"recent_sources": ["security"], "essay_count": 0}
        result = essay_module.pick_subject(state)
        assert result == "philosophy"

    @patch("nova_daily_essay.get_source_counts_from_db")
    @patch("nova_daily_essay.get_sources_with_counts")
    def test_resets_recent_when_all_used(self, mock_api, mock_db, essay_module):
        mock_api.return_value = [{"source": "security", "count": 100}]
        state = {"recent_sources": ["security"], "essay_count": 0}
        result = essay_module.pick_subject(state)
        assert result == "security"


class TestEssayExtractTitle:
    """Tests for title extraction."""

    def test_extracts_first_line(self, essay_module):
        essay = "# The Nature of Security\n\nBody text here."
        assert essay_module.extract_title(essay) == "The Nature of Security"

    def test_strips_markdown_headers(self, essay_module):
        essay = "## My Title Here\n\nContent."
        assert essay_module.extract_title(essay) == "My Title Here"

    def test_fallback_on_empty(self, essay_module):
        essay = "\n\n\n"
        assert "Essay" in essay_module.extract_title(essay)


class TestEssayPromptBuilding:
    """Tests for essay prompt construction."""

    @patch("nova_daily_essay._load_writing_lessons")
    def test_builds_system_and_user_prompts(self, mock_lessons, essay_module):
        mock_lessons.return_value = ""
        memories = [{"text": "Memory text here about security.", "metadata": "{}", "created_at": "2026-01-01"}]
        sys_prompt, user_prompt = essay_module._build_essay_prompt("security", memories)
        assert "PEEL" in sys_prompt
        assert "Third person" in sys_prompt
        assert "security" in user_prompt.lower() or "Security" in user_prompt

    @patch("nova_daily_essay._load_writing_lessons")
    def test_injects_writing_lessons(self, mock_lessons, essay_module):
        mock_lessons.return_value = "Stop using passive voice."
        sys_prompt, _ = essay_module._build_essay_prompt("security", [{"text": "test", "metadata": "{}", "created_at": ""}])
        assert "Stop using passive voice" in sys_prompt


class TestEssayGeneration:
    """Tests for essay generation with fallbacks."""

    @patch("nova_daily_essay._generate_via_ollama")
    @patch("nova_daily_essay._generate_via_openrouter")
    @patch("nova_daily_essay.get_openrouter_key")
    def test_primary_openrouter(self, mock_key, mock_openrouter, mock_ollama, essay_module):
        mock_key.return_value = "test-key"
        mock_openrouter.return_value = "A" * 600
        memories = [{"text": "memory", "metadata": "{}", "created_at": ""}]
        result = essay_module.generate_essay("security", memories)
        assert result is not None
        assert len(result) >= 500

    @patch("nova_daily_essay._generate_via_ollama")
    @patch("nova_daily_essay._generate_via_openrouter")
    @patch("nova_daily_essay.get_openrouter_key")
    def test_fallback_to_ollama(self, mock_key, mock_openrouter, mock_ollama, essay_module):
        mock_key.return_value = "test-key"
        mock_openrouter.side_effect = Exception("API down")
        mock_ollama.return_value = "B" * 600
        memories = [{"text": "memory", "metadata": "{}", "created_at": ""}]
        result = essay_module.generate_essay("security", memories)
        assert result is not None

    @patch("nova_daily_essay._generate_via_ollama")
    @patch("nova_daily_essay._generate_via_openrouter")
    @patch("nova_daily_essay.get_openrouter_key")
    def test_returns_none_on_short_output(self, mock_key, mock_openrouter, mock_ollama, essay_module):
        mock_key.return_value = "test-key"
        mock_openrouter.return_value = "Short"
        mock_ollama.return_value = ""
        memories = [{"text": "memory", "metadata": "{}", "created_at": ""}]
        result = essay_module.generate_essay("security", memories)
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# DAILY OPINION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestOpinionNewsFetch:
    """Tests for Google News RSS fetching."""

    @patch("urllib.request.urlopen")
    def test_parses_rss_feed(self, mock_urlopen, opinion_module):
        import io
        rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><item><title>Big News Story</title><link>https://news.google.com/1</link><pubDate>Mon, 01 Jan 2026</pubDate></item><item><title>Another Story</title><link>https://news.google.com/2</link></item></channel></rss>"""
        mock_resp = io.BytesIO(rss_xml.encode())
        mock_urlopen.return_value = mock_resp
        stories = opinion_module.fetch_news()
        assert len(stories) == 2
        assert stories[0]["title"] == "Big News Story"

    @patch("urllib.request.urlopen")
    def test_handles_rss_failure(self, mock_urlopen, opinion_module):
        mock_urlopen.side_effect = Exception("DNS failure")
        stories = opinion_module.fetch_news()
        assert stories == []


class TestOpinionStorySelection:
    """Tests for story selection."""

    def test_avoids_recent_stories(self, opinion_module):
        stories = [
            {"title": "Story A", "link": "", "published": ""},
            {"title": "Story B", "link": "", "published": ""},
        ]
        state = {"recent_stories": ["Story A"], "opinion_count": 0}
        result = opinion_module.pick_story(stories, state)
        assert result["title"] == "Story B"

    def test_returns_none_for_empty_list(self, opinion_module):
        assert opinion_module.pick_story([], {"recent_stories": []}) is None


class TestOpinionExtractTitle:
    """Tests for opinion title extraction."""

    def test_extracts_first_line(self, opinion_module):
        text = "The Absurdity of Modern Tech Bros\n\nBody paragraph..."
        assert opinion_module.extract_title(text) == "The Absurdity of Modern Tech Bros"


# ═══════════════════════════════════════════════════════════════════════════════
# WEEKLY DIGEST TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestDigestDataGathering:
    """Tests for digest section compilation."""

    def test_get_week_range(self, digest_module):
        start, end = digest_module.get_week_range()
        assert len(start) == 10  # YYYY-MM-DD
        assert len(end) == 10

    def test_format_plex_summary_no_items(self, digest_module):
        result = digest_module.format_plex_summary([])
        assert "No viewing" in result

    def test_format_plex_summary_with_movies(self, digest_module):
        items = [
            {"title": "Aliens", "type": "movie", "grandparentTitle": "", "year": 1986},
            {"title": "Blade Runner", "type": "movie", "grandparentTitle": "", "year": 1982},
        ]
        result = digest_module.format_plex_summary(items)
        assert "Movies watched" in result
        assert "2" in result

    def test_format_plex_summary_with_episodes(self, digest_module):
        items = [
            {"title": "Ep 1", "type": "episode", "grandparentTitle": "Breaking Bad", "year": 2013},
            {"title": "Ep 2", "type": "episode", "grandparentTitle": "Breaking Bad", "year": 2013},
            {"title": "Ep 1", "type": "episode", "grandparentTitle": "Better Call Saul", "year": 2022},
        ]
        result = digest_module.format_plex_summary(items)
        assert "Breaking Bad" in result
        assert "Better Call Saul" in result


class TestDigestBodyFormatting:
    """Tests for digest body assembly."""

    def test_format_digest_body(self, digest_module):
        data = {
            "dreams": [{"date": "2026-05-01", "theme": "flying", "mood": "wonder"}],
            "essays": [{"date": "2026-05-02", "title": "On Security", "subject": "security"}],
            "opinions": [],
            "plex_items": [],
            "plex_summary": "No viewing activity recorded this week.",
            "health": {"failures": [], "total_memories": 100000, "memory_growth": 500},
            "herd_activity": "3 incoming herd messages this week",
            "memory_sources": [{"source": "security", "count": 200}],
        }
        result = digest_module.format_digest_body(data)
        assert "flying" in result
        assert "On Security" in result
        assert "100,000" in result


class TestDigestEmailScrubbing:
    """Tests for email address removal from public content."""

    def test_scrub_emails_removes_personal(self, digest_module):
        text = "Contact someone@corp.example.com or user@example.com for info"
        result = digest_module.scrub_emails(text)
        assert "someone@corp.example.com" not in result
        assert "user@example.com" not in result
        assert "[email redacted]" in result

    def test_scrub_emails_preserves_nova(self, digest_module):
        text = "From: nova@digitalnoise.net"
        result = digest_module.scrub_emails(text)
        assert "nova@digitalnoise.net" in result


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestContentSecurity:
    """Security tests across all daily content scripts."""

    def test_essay_no_hardcoded_keys(self, essay_module):
        import inspect
        source = inspect.getsource(essay_module)
        assert "sk-" not in source
        assert "AKIA" not in source

    def test_opinion_no_hardcoded_keys(self, opinion_module):
        import inspect
        source = inspect.getsource(opinion_module)
        assert "sk-" not in source

    def test_digest_no_hardcoded_keys(self, digest_module):
        import inspect
        source = inspect.getsource(digest_module)
        assert "sk-" not in source

    def test_opinion_uses_email_scrubbing(self, opinion_module):
        """Verify opinion publishes with email redaction."""
        import inspect
        source = inspect.getsource(opinion_module.publish_to_site)
        assert "email redacted" in source or "EMAIL_PATTERN" in source


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.functional
class TestEssayWorkflow:
    """Functional test for full essay pipeline."""

    @patch("nova_daily_essay.publish_to_journal")
    @patch("nova_daily_essay.post_to_slack")
    @patch("nova_daily_essay.send_to_herd")
    @patch("nova_daily_essay.generate_essay_image")
    @patch("nova_daily_essay.generate_essay")
    @patch("nova_daily_essay.fetch_memories")
    @patch("nova_daily_essay.pick_subject")
    @patch("nova_daily_essay.save_state")
    @patch("nova_daily_essay.load_state")
    def test_full_pipeline(self, mock_load, mock_save, mock_pick, mock_fetch,
                           mock_gen, mock_image, mock_herd, mock_slack, mock_journal, essay_module):
        mock_load.return_value = {"recent_sources": [], "essay_count": 0}
        mock_pick.return_value = "security"
        mock_fetch.return_value = [{"text": f"Memory {i}", "metadata": "{}", "created_at": ""} for i in range(25)]
        mock_gen.return_value = "# Great Title\n\nEssay body content " * 50
        mock_image.return_value = None

        essay_module.main()

        mock_gen.assert_called_once()
        mock_herd.assert_called_once()
        mock_slack.assert_called_once()
        mock_save.assert_called_once()


@pytest.mark.functional
class TestOpinionWorkflow:
    """Functional test for full opinion pipeline."""

    @patch("nova_daily_opinion.publish_to_site")
    @patch("nova_daily_opinion.post_to_slack")
    @patch("nova_daily_opinion.send_to_herd")
    @patch("nova_daily_opinion.generate_image")
    @patch("nova_daily_opinion.generate_opinion")
    @patch("nova_daily_opinion.fetch_related_memories")
    @patch("nova_daily_opinion.pick_story")
    @patch("nova_daily_opinion.fetch_news")
    @patch("nova_daily_opinion.save_state")
    @patch("nova_daily_opinion.load_state")
    def test_full_pipeline(self, mock_load, mock_save, mock_fetch_news, mock_pick,
                           mock_memories, mock_gen, mock_image, mock_herd,
                           mock_slack, mock_publish, opinion_module):
        mock_load.return_value = {"recent_stories": [], "opinion_count": 0}
        mock_fetch_news.return_value = [{"title": "Big Story", "link": "https://example.com", "published": ""}]
        mock_pick.return_value = {"title": "Big Story", "link": "https://example.com", "published": ""}
        mock_memories.return_value = []
        mock_gen.return_value = "Bold Opinion Title\n\n" + "Opinion body " * 100
        mock_image.return_value = None

        opinion_module.main()

        mock_gen.assert_called_once()
        mock_herd.assert_called_once()
        mock_slack.assert_called_once()
        mock_save.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestIntegration:
    """Integration tests requiring live services."""

    def test_memory_server_stats(self, essay_module):
        """Verify memory server stats endpoint works."""
        import urllib.request
        try:
            resp = urllib.request.urlopen(f"{essay_module.MEMORY_SERVER}/stats", timeout=5)
            data = json.loads(resp.read())
            assert "sources" in data or "by_source" in data
        except Exception:
            pytest.skip("Memory server not running")
