#!/usr/bin/env python3
"""
test_after_dark.py — Comprehensive tests for nova_after_dark.py.

Covers: Wikipedia event fetching, event selection, SearXNG search, memory recall,
Ollama/OpenRouter generation, image generation, Hugo publishing, Slack posting,
state management, comedy rules enforcement, security checks.

Run: python3 -m pytest tests/test_after_dark.py -v
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
def after_dark_module(mock_nova_config):
    """Import nova_after_dark fresh with mocked nova_config."""
    import importlib
    for mod in list(sys.modules.keys()):
        if "nova_after_dark" in mod:
            del sys.modules[mod]
    import nova_after_dark
    return nova_after_dark


@pytest.fixture
def sample_events():
    return [
        {"year": 1969, "text": "The Apollo 11 mission lands on the Moon"},
        {"year": 1776, "text": "The United States Declaration of Independence is adopted"},
        {"year": 2004, "text": "Facebook launches from a Harvard dorm room"},
        {"year": 1912, "text": "The Titanic sinks on its maiden voyage"},
        {"year": 1989, "text": "The Berlin Wall falls"},
    ]


@pytest.fixture
def sample_state():
    return {"recent_topics": ["The Apollo 11 mission lands on the Moon"[:50]], "episode_count": 42}


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoadSaveState:
    """Tests for state management."""

    def test_load_default_state(self, after_dark_module, tmp_path):
        with patch.object(after_dark_module, "STATE_FILE", tmp_path / "nonexistent.json"):
            state = after_dark_module.load_state()
        assert state == {"recent_topics": [], "episode_count": 0}

    def test_save_and_reload_state(self, after_dark_module, tmp_path):
        state_file = tmp_path / "state.json"
        with patch.object(after_dark_module, "STATE_FILE", state_file):
            after_dark_module.save_state({"recent_topics": ["test"], "episode_count": 10})
            loaded = after_dark_module.load_state()
        assert loaded["episode_count"] == 10


class TestFetchTodayInHistory:
    """Tests for Wikipedia event fetching."""

    @patch("urllib.request.urlopen")
    def test_parses_events_response(self, mock_urlopen, after_dark_module):
        response_data = {
            "events": [
                {"year": 1969, "text": "Moon landing"},
                {"year": 1776, "text": "Independence"},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        events = after_dark_module.fetch_today_in_history()
        assert len(events) == 2
        assert events[0]["year"] == 1969

    @patch("urllib.request.urlopen")
    def test_handles_api_failure(self, mock_urlopen, after_dark_module):
        mock_urlopen.side_effect = Exception("timeout")
        events = after_dark_module.fetch_today_in_history()
        assert events == []


class TestPickEvent:
    """Tests for event selection logic."""

    def test_avoids_recent_topics(self, after_dark_module, sample_events):
        state = {"recent_topics": [sample_events[0]["text"][:50]], "episode_count": 5}
        event = after_dark_module.pick_event(sample_events, state)
        assert event is not None
        assert event["text"][:50] != sample_events[0]["text"][:50]

    def test_returns_none_for_empty_events(self, after_dark_module):
        event = after_dark_module.pick_event([], {"recent_topics": []})
        assert event is None

    def test_prefers_longer_events(self, after_dark_module):
        events = [
            {"year": 2000, "text": "Short"},
            {"year": 2001, "text": "A much longer event description with many more details and interesting facts"},
        ]
        # Over many picks, longer events should be preferred (weighted by length)
        picks = set()
        for _ in range(50):
            event = after_dark_module.pick_event(events, {"recent_topics": []})
            picks.add(event["text"])
        # Should at least pick the longer one sometimes (it's in top pool)
        assert "A much longer event description with many more details and interesting facts" in picks


class TestSearxngSearch:
    """Tests for SearXNG integration."""

    @patch("urllib.request.urlopen")
    def test_returns_structured_results(self, mock_urlopen, after_dark_module):
        response_data = {
            "results": [
                {"title": "Apollo 11", "content": "Moon landing details", "url": "https://nasa.gov/apollo11"},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        results = after_dark_module.searxng_search("apollo 11")
        assert len(results) == 1
        assert results[0]["title"] == "Apollo 11"

    @patch("urllib.request.urlopen")
    def test_handles_failure_gracefully(self, mock_urlopen, after_dark_module):
        mock_urlopen.side_effect = Exception("connection refused")
        results = after_dark_module.searxng_search("test")
        assert results == []


class TestRecallMemories:
    """Tests for vector memory recall."""

    @patch("urllib.request.urlopen")
    def test_returns_text_list(self, mock_urlopen, after_dark_module):
        response_data = {
            "memories": [
                {"text": "Memory about space exploration and the moon."},
                {"text": "Another memory about astronomy."},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp
        memories = after_dark_module.recall_memories("moon landing")
        assert len(memories) == 2
        assert "space exploration" in memories[0]

    @patch("urllib.request.urlopen")
    def test_handles_failure(self, mock_urlopen, after_dark_module):
        mock_urlopen.side_effect = Exception("timeout")
        memories = after_dark_module.recall_memories("test")
        assert memories == []


class TestGenerateMonologue:
    """Tests for LLM monologue generation."""

    @patch("nova_after_dark._generate_openrouter")
    @patch("nova_after_dark._generate_ollama")
    def test_tries_ollama_first(self, mock_ollama, mock_openrouter, after_dark_module):
        mock_ollama.return_value = "A" * 500  # Long enough result
        event = {"year": 1969, "text": "Moon landing"}
        result = after_dark_module.generate_monologue(event, "context", "memories")
        assert len(result) >= 300
        mock_ollama.assert_called_once()
        mock_openrouter.assert_not_called()

    @patch("nova_after_dark._generate_openrouter")
    @patch("nova_after_dark._generate_ollama")
    def test_falls_back_to_openrouter(self, mock_ollama, mock_openrouter, after_dark_module):
        mock_ollama.side_effect = Exception("Ollama down")
        mock_openrouter.return_value = "B" * 500
        event = {"year": 1969, "text": "Moon landing"}
        result = after_dark_module.generate_monologue(event, "context", "memories")
        assert len(result) >= 300
        mock_openrouter.assert_called_once()

    @patch("nova_after_dark._generate_openrouter")
    @patch("nova_after_dark._generate_ollama")
    def test_returns_empty_on_all_failures(self, mock_ollama, mock_openrouter, after_dark_module):
        mock_ollama.side_effect = Exception("down")
        mock_openrouter.side_effect = Exception("also down")
        event = {"year": 1969, "text": "Moon landing"}
        result = after_dark_module.generate_monologue(event, "context", "memories")
        assert result == ""


class TestImageGeneration:
    """Tests for image generation."""

    @patch("subprocess.run")
    def test_retries_on_failure(self, mock_run, after_dark_module):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        event = {"year": 1969, "text": "Moon landing"}
        result = after_dark_module.generate_image(event)
        assert result is None
        assert mock_run.call_count == 3  # 3 retries

    @patch("subprocess.run")
    def test_returns_path_on_success(self, mock_run, after_dark_module, tmp_path):
        img_path = tmp_path / "image.png"
        img_path.write_bytes(b"fake png")
        mock_run.return_value = MagicMock(returncode=0, stdout=str(img_path), stderr="")
        event = {"year": 1969, "text": "Moon landing"}
        result = after_dark_module.generate_image(event)
        assert result == str(img_path)


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestSecurity:
    """Security tests for nova_after_dark.py."""

    def test_no_hardcoded_credentials(self, after_dark_module):
        import inspect
        source = inspect.getsource(after_dark_module)
        assert "sk-" not in source
        assert "xoxb-" not in source
        assert "Bearer " not in source or "Bearer {" in source or 'f"Bearer ' in source

    def test_services_are_localhost(self, after_dark_module):
        assert "127.0.0.1" in after_dark_module.OLLAMA_URL
        assert "127.0.0.1" in after_dark_module.SEARXNG_URL
        assert "127.0.0.1" in after_dark_module.MEMORY_SERVER

    def test_no_pii_in_prompts(self, after_dark_module):
        """The comedy system prompt should not contain personal info."""
        import inspect
        source = inspect.getsource(after_dark_module.generate_monologue)
        assert "kochj" not in source.lower()
        # Verify no personal work email leaked into prompts
        assert "jordan.koch@" not in source


# ═══════════════════════════════════════════════════════════════════════════════
# FRAMEWORK TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestErrorHandling:
    """Tests for graceful error handling."""

    @patch("nova_after_dark.post_to_slack")
    @patch("nova_after_dark.publish_to_hugo")
    @patch("nova_after_dark.generate_image")
    @patch("nova_after_dark.generate_monologue")
    @patch("nova_after_dark.recall_memories")
    @patch("nova_after_dark.searxng_search")
    @patch("nova_after_dark.pick_event")
    @patch("nova_after_dark.fetch_today_in_history")
    @patch("nova_after_dark.save_state")
    @patch("nova_after_dark.load_state")
    def test_aborts_on_short_monologue(
        self, mock_load, mock_save, mock_fetch, mock_pick,
        mock_search, mock_recall, mock_gen, mock_image,
        mock_publish, mock_slack, after_dark_module
    ):
        mock_load.return_value = {"recent_topics": [], "episode_count": 0}
        mock_fetch.return_value = [{"year": 1969, "text": "Moon landing"}]
        mock_pick.return_value = {"year": 1969, "text": "Moon landing"}
        mock_search.return_value = []
        mock_recall.return_value = []
        mock_gen.return_value = "Too short"  # Under 200 chars

        after_dark_module.main()

        mock_publish.assert_not_called()
        mock_slack.assert_not_called()
        mock_save.assert_not_called()

    @patch("nova_after_dark.fetch_today_in_history")
    @patch("nova_after_dark.load_state")
    def test_aborts_on_no_events(self, mock_load, mock_fetch, after_dark_module):
        mock_load.return_value = {"recent_topics": [], "episode_count": 0}
        mock_fetch.return_value = []
        # Should not raise
        after_dark_module.main()


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestIntegration:
    """Integration tests requiring live services."""

    def test_wikipedia_api_reachable(self, after_dark_module):
        """Verify Wikipedia On This Day API is reachable."""
        import urllib.request
        try:
            url = f"{after_dark_module.WIKI_API}/05/06"
            req = urllib.request.Request(url, headers={"User-Agent": "Nova/1.0 test", "Accept": "application/json"})
            resp = urllib.request.urlopen(req, timeout=10)
            assert resp.status == 200
        except Exception:
            pytest.skip("Wikipedia API not reachable")
