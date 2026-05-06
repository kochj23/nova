#!/usr/bin/env python3
"""
test_research_paper.py — Comprehensive tests for nova_research_paper.py.

Covers: topic selection, memory gathering, search angle generation, web research,
thesis generation, chapter writing, abstract/conclusion, reference formatting,
Hugo publishing, Slack notifications, state management, security (no PII, excluded sources).

Run: python3 -m pytest tests/test_research_paper.py -v
Written by Jordan Koch.
"""

import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def research_module(mock_nova_config):
    """Import nova_research_paper fresh with mocked nova_config."""
    import importlib
    for mod in list(sys.modules.keys()):
        if "nova_research_paper" in mod:
            del sys.modules[mod]
    import nova_research_paper
    return nova_research_paper


@pytest.fixture
def sample_state():
    return {"recent_topics": ["security", "philosophy"], "paper_count": 5}


@pytest.fixture
def sample_memories():
    return [
        {"text": f"Memory {i}: detailed content about the topic area with enough text.", "metadata": {"title": f"Source {i}", "type": "article"}, "score": 0.9 - i * 0.01}
        for i in range(120)
    ]


@pytest.fixture
def sample_web_sources():
    return [
        {"title": f"Web Source {i}", "content": f"Content about topic from web source {i}.", "url": f"https://example.com/source-{i}", "engine": "searxng"}
        for i in range(30)
    ]


@pytest.fixture
def sample_outline():
    return {
        "paper_type": "argumentative",
        "title": "The Intersection of Gnostic Thought and Modern Cybersecurity",
        "thesis": "Ancient gnostic frameworks for hidden knowledge offer a lens to analyze modern information security.",
        "research_question": "How do gnostic epistemologies parallel threat modeling approaches?",
        "chapters": [
            {"title": "Hidden Knowledge in Antiquity", "description": "Gnostic traditions of esoteric knowledge.", "needs_diagram": False, "diagram_type": ""},
            {"title": "Modern Threat Landscapes", "description": "Contemporary cybersecurity frameworks.", "needs_diagram": True, "diagram_type": "flowchart"},
            {"title": "Parallels and Divergences", "description": "Where ancient and modern overlap.", "needs_diagram": False, "diagram_type": ""},
            {"title": "Applied Framework", "description": "Practical synthesis of both traditions.", "needs_diagram": True, "diagram_type": "sequence"},
        ],
        "key_arguments": ["argument 1", "argument 2"],
        "methodology_note": "Comparative analysis approach.",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoadSaveState:
    """Tests for state persistence."""

    def test_load_state_creates_default(self, research_module, tmp_path):
        with patch.object(research_module, "STATE_FILE", tmp_path / "nonexistent.json"):
            state = research_module.load_state()
        assert state == {"recent_topics": [], "paper_count": 0}

    def test_load_state_reads_existing(self, research_module, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"recent_topics": ["a"], "paper_count": 3}))
        with patch.object(research_module, "STATE_FILE", state_file):
            state = research_module.load_state()
        assert state["paper_count"] == 3
        assert state["recent_topics"] == ["a"]

    def test_save_state_writes_json(self, research_module, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        state_file = state_dir / "test.json"
        with patch.object(research_module, "STATE_FILE", state_file):
            research_module.save_state({"paper_count": 7, "recent_topics": ["x"]})
        data = json.loads(state_file.read_text())
        assert data["paper_count"] == 7


class TestPickTopic:
    """Tests for topic selection logic."""

    @patch("nova_research_paper.get_source_counts")
    def test_excludes_recent_topics(self, mock_counts, research_module):
        mock_counts.return_value = {"security": 200, "philosophy": 150, "psychology": 180}
        state = {"recent_topics": ["security", "philosophy"], "paper_count": 5}
        source, desc = research_module.pick_topic(state)
        assert source == "psychology"

    @patch("nova_research_paper.get_source_counts")
    def test_excludes_private_sources(self, mock_counts, research_module):
        mock_counts.return_value = {"disney": 500, "cloud_governance": 300, "mycology": 150}
        state = {"recent_topics": [], "paper_count": 0}
        source, desc = research_module.pick_topic(state)
        assert source not in research_module.EXCLUDED_SOURCES
        assert source == "mycology"

    @patch("nova_research_paper.get_source_counts")
    def test_returns_none_when_no_candidates(self, mock_counts, research_module):
        mock_counts.return_value = {"security": 10}  # Below MIN_MEMORIES
        state = {"recent_topics": [], "paper_count": 0}
        source, desc = research_module.pick_topic(state)
        assert source is None
        assert desc is None

    @patch("nova_research_paper.get_source_counts")
    def test_selects_from_top_pool(self, mock_counts, research_module):
        counts = {s: 200 for s in research_module.AMBITIOUS_SOURCES}
        mock_counts.return_value = counts
        state = {"recent_topics": [], "paper_count": 0}
        source, desc = research_module.pick_topic(state)
        assert source in research_module.AMBITIOUS_SOURCES
        assert desc is not None


class TestGatherMemories:
    """Tests for memory gathering logic."""

    @patch("nova_research_paper.recall_memories")
    @patch("nova_research_paper.generate_search_angles")
    def test_deduplicates_memories(self, mock_angles, mock_recall, research_module):
        mock_angles.return_value = ["angle 1", "angle 2"]
        # Return same memory from both angles
        same_memory = {"text": "A" * 200 + " unique content", "metadata": {}, "score": 0.9}
        mock_recall.return_value = [same_memory] * 5
        result = research_module.gather_memories("security", "cybersecurity topics")
        # Should deduplicate based on first 200 chars
        assert len(result) <= 5

    @patch("nova_research_paper.recall_memories")
    @patch("nova_research_paper.generate_search_angles")
    def test_tries_without_source_filter_on_low_count(self, mock_angles, mock_recall, research_module):
        mock_angles.return_value = ["angle 1"]
        # First calls with source return few, later without source returns more
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if kwargs.get("source"):
                return [{"text": f"M{i} " * 50, "metadata": {}, "score": 0.8} for i in range(call_count[0] * 5)]
            return [{"text": f"NoSource{i} " * 50, "metadata": {}, "score": 0.7} for i in range(50)]

        mock_recall.side_effect = side_effect
        result = research_module.gather_memories("security", "cybersecurity topics")
        assert len(result) > 0


class TestSearchAngles:
    """Tests for LLM-powered search angle generation."""

    @patch("nova_research_paper.nova_config")
    def test_fallback_when_no_api_key(self, mock_config, research_module):
        mock_config.openrouter_api_key.return_value = ""
        result = research_module.generate_search_angles("security", "cybersecurity topics")
        assert len(result) == 5
        assert all(r == "cybersecurity topics" for r in result)

    @patch("urllib.request.urlopen")
    @patch("nova_research_paper.nova_config")
    def test_parses_llm_response(self, mock_config, mock_urlopen, research_module):
        mock_config.openrouter_api_key.return_value = "test-key"
        response_data = {
            "choices": [{"message": {"content": "1. Query one\n2. Query two\n3. Query three"}}]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp
        result = research_module.generate_search_angles("security", "cybersecurity topics")
        assert len(result) == 3
        assert "Query one" in result[0]


class TestSearxngSearch:
    """Tests for SearXNG web search."""

    @patch("urllib.request.urlopen")
    def test_returns_structured_results(self, mock_urlopen, research_module):
        response_data = {
            "results": [
                {"title": "Result 1", "content": "Content 1", "url": "https://example.com/1", "engine": "google"},
                {"title": "Result 2", "content": "Content 2", "url": "https://example.com/2", "engine": "ddg"},
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        result = research_module.searxng_search("test query", max_results=5)
        assert len(result) == 2
        assert result[0]["title"] == "Result 1"
        assert result[0]["url"] == "https://example.com/1"

    @patch("urllib.request.urlopen")
    def test_handles_search_failure(self, mock_urlopen, research_module):
        mock_urlopen.side_effect = Exception("connection refused")
        result = research_module.searxng_search("test query")
        assert result == []


class TestThesisGeneration:
    """Tests for thesis and outline generation."""

    @patch("urllib.request.urlopen")
    @patch("nova_research_paper.nova_config")
    def test_parses_json_response(self, mock_config, mock_urlopen, research_module, sample_memories, sample_web_sources):
        mock_config.openrouter_api_key.return_value = "test-key"
        outline = {
            "paper_type": "argumentative",
            "title": "Test Paper Title",
            "thesis": "Test thesis statement.",
            "chapters": [{"title": "Ch1", "description": "Desc", "needs_diagram": False, "diagram_type": ""}],
            "key_arguments": ["arg1"],
            "methodology_note": "test",
        }
        response_data = {"choices": [{"message": {"content": json.dumps(outline)}}]}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_urlopen.return_value = mock_resp
        result = research_module.generate_thesis_and_outline("security", "cybersecurity", sample_memories[:30], sample_web_sources[:15])
        assert result["title"] == "Test Paper Title"
        assert result["paper_type"] == "argumentative"

    @patch("nova_research_paper.nova_config")
    def test_returns_empty_when_no_key(self, mock_config, research_module, sample_memories, sample_web_sources):
        mock_config.openrouter_api_key.return_value = ""
        result = research_module.generate_thesis_and_outline("security", "cybersecurity", sample_memories[:30], sample_web_sources[:15])
        assert result == {}


class TestFormatReferences:
    """Tests for APA reference formatting."""

    def test_includes_web_sources(self, research_module, sample_web_sources):
        memories = [{"text": "test memory text", "metadata": {"title": "Source A", "type": "article"}}]
        result = research_module.format_references(memories, sample_web_sources[:5], "security")
        assert "Web Source 0" in result
        assert "https://example.com/source-0" in result

    def test_includes_memory_source_info(self, research_module, sample_web_sources):
        memories = [
            {"text": "Memory about security topic with enough text to show.", "metadata": json.dumps({"title": "Security Guide", "type": "book"})}
        ]
        result = research_module.format_references(memories, sample_web_sources[:3], "security")
        assert "Memory Database" in result
        assert "security" in result

    def test_handles_string_metadata(self, research_module, sample_web_sources):
        memories = [{"text": "test memory", "metadata": '{"title": "Test Title"}'}]
        result = research_module.format_references(memories, sample_web_sources[:2], "security")
        assert "Test Title" in result


class TestImageGeneration:
    """Tests for cover and chapter image generation."""

    @patch("urllib.request.urlopen")
    def test_skips_when_swarmui_unavailable(self, mock_urlopen, research_module):
        mock_urlopen.side_effect = Exception("Connection refused")
        result = research_module._generate_image_direct("test prompt")
        assert result is None

    @patch("subprocess.run")
    @patch("urllib.request.urlopen")
    def test_calls_generate_image_script(self, mock_urlopen, mock_run, research_module, tmp_path):
        # SwarmUI check passes
        mock_resp = MagicMock()
        mock_urlopen.return_value = mock_resp

        # Image generation succeeds
        img_path = tmp_path / "output.png"
        img_path.write_bytes(b"fake png")
        mock_run.return_value = MagicMock(returncode=0, stdout=str(img_path), stderr="")

        result = research_module._generate_image_direct("test prompt")
        assert result == str(img_path)


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestSecurity:
    """Security tests for nova_research_paper.py."""

    def test_excluded_sources_are_private(self, research_module):
        """Verify that all private/work sources are excluded."""
        for source in ["disney", "cloud_governance", "infrastructure", "gdrive", "work", "google_drive"]:
            assert source in research_module.EXCLUDED_SOURCES

    def test_no_hardcoded_api_keys(self, research_module):
        """Verify no API keys are hardcoded in the module."""
        import inspect
        source_code = inspect.getsource(research_module)
        assert "sk-" not in source_code
        assert "AKIA" not in source_code
        assert "ghp_" not in source_code

    def test_openrouter_url_is_correct(self, research_module):
        assert research_module.OPENROUTER_URL == "https://openrouter.ai/api/v1/chat/completions"

    def test_memory_server_is_localhost(self, research_module):
        assert "127.0.0.1" in research_module.MEMORY_SERVER

    @patch("nova_research_paper.get_source_counts")
    def test_excluded_sources_never_selected(self, mock_counts, research_module):
        """Even if excluded sources have high counts, they should never be selected."""
        counts = {s: 9999 for s in research_module.EXCLUDED_SOURCES}
        counts["mycology"] = 200
        mock_counts.return_value = counts
        state = {"recent_topics": [], "paper_count": 0}
        source, _ = research_module.pick_topic(state)
        assert source not in research_module.EXCLUDED_SOURCES


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.functional
class TestMainWorkflow:
    """Functional tests for the full research paper workflow."""

    @patch("nova_research_paper.post_to_slack")
    @patch("nova_research_paper.publish_to_hugo")
    @patch("nova_research_paper.generate_conclusion")
    @patch("nova_research_paper.generate_abstract")
    @patch("nova_research_paper.generate_chapter_image")
    @patch("nova_research_paper.generate_chapter")
    @patch("nova_research_paper.generate_cover_image")
    @patch("nova_research_paper.generate_thesis_and_outline")
    @patch("nova_research_paper.gather_web_sources")
    @patch("nova_research_paper.gather_memories")
    @patch("nova_research_paper.generate_search_angles")
    @patch("nova_research_paper.pick_topic")
    @patch("nova_research_paper.save_state")
    @patch("nova_research_paper.load_state")
    def test_full_pipeline_success(
        self, mock_load, mock_save, mock_pick, mock_angles,
        mock_memories, mock_web, mock_outline, mock_cover,
        mock_chapter, mock_ch_image, mock_abstract, mock_conclusion,
        mock_publish, mock_slack, research_module, sample_memories,
        sample_web_sources, sample_outline
    ):
        mock_load.return_value = {"recent_topics": [], "paper_count": 0}
        mock_pick.return_value = ("psychology", "cognitive psychology")
        mock_angles.return_value = ["angle 1", "angle 2"]
        mock_memories.return_value = sample_memories
        mock_web.return_value = sample_web_sources
        mock_outline.return_value = sample_outline
        mock_cover.return_value = None
        mock_chapter.return_value = "Chapter content " * 100
        mock_ch_image.return_value = None
        mock_abstract.return_value = "Abstract text here."
        mock_conclusion.return_value = "Conclusion text here."
        mock_publish.return_value = True

        research_module.main()

        mock_pick.assert_called_once()
        mock_memories.assert_called_once()
        mock_web.assert_called_once()
        mock_outline.assert_called_once()
        assert mock_chapter.call_count == len(sample_outline["chapters"])
        mock_publish.assert_called_once()
        mock_slack.assert_called_once()
        mock_save.assert_called_once()

    @patch("nova_research_paper.pick_topic")
    @patch("nova_research_paper.load_state")
    def test_aborts_on_no_topic(self, mock_load, mock_pick, research_module):
        mock_load.return_value = {"recent_topics": [], "paper_count": 0}
        mock_pick.return_value = (None, None)
        # Should not raise
        research_module.main()


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestIntegration:
    """Integration tests that require live services."""

    def test_memory_server_health(self, research_module):
        """Verify memory server is reachable."""
        import urllib.request
        try:
            resp = urllib.request.urlopen(f"{research_module.MEMORY_SERVER}/health", timeout=5)
            data = json.loads(resp.read())
            assert "status" in data or resp.status == 200
        except Exception:
            pytest.skip("Memory server not running")

    def test_searxng_reachable(self, research_module):
        """Verify SearXNG is reachable."""
        import urllib.request
        try:
            resp = urllib.request.urlopen(f"{research_module.SEARXNG_URL}?q=test&format=json", timeout=5)
            assert resp.status == 200
        except Exception:
            pytest.skip("SearXNG not running")
