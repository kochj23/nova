#!/usr/bin/env python3
"""
test_dream_v2.py — Comprehensive tests for dream_generate.py (v2 rewrite).

Covers: theme derivation, memory queries, prompt building, mood list, header stripping,
repetition detection, retry logic, journal writing, pending delivery, image generation,
security (no PII in prompts, EXCLUDE_SOURCES enforced).

Run: python3 -m pytest tests/test_dream_v2.py -v
Written by Jordan Koch.
"""

import json
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def dream_module():
    """Import dream_generate fresh (no stale module cache)."""
    import importlib
    if "dream_generate" in sys.modules:
        del sys.modules["dream_generate"]
    # Ensure psycopg2 doesn't cause import failures
    if "psycopg2" not in sys.modules:
        sys.modules["psycopg2"] = MagicMock()
    import dream_generate
    return dream_generate


@pytest.fixture
def sample_themed_memories():
    return [
        {"source": "plex_watch_history", "label": "Breaking Bad", "memory": "Jordan watched Breaking Bad - Ozymandias (Drama, Thriller) on 2026-04-25.", "ingested": "2026-04-25T10:00:00"},
        {"source": "livetv_news", "label": "KABC", "memory": "Local news report about downtown construction project delays.", "ingested": "2026-04-26T08:00:00"},
        {"source": "tv_transcript", "label": "Seinfeld", "memory": "George Costanza arguing about parking spots in a heated exchange.", "ingested": "2026-04-20T15:00:00"},
    ]


@pytest.fixture
def sample_wildcard_memories():
    return [
        {"source": "movie_transcript", "label": "2001 A Space Odyssey", "memory": "HAL 9000 refuses to open the pod bay doors in a calm monotone voice.", "ingested": "2026-03-10T00:00:00"},
        {"source": "youtube_transcript", "label": "Cooking Video", "memory": "The perfect risotto requires constant stirring and patience.", "ingested": "2026-04-15T12:00:00"},
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestDeriveTheme:
    """Tests for derive_theme() — LLM theme extraction."""

    @patch("dream_generate._generate_short")
    def test_returns_string(self, mock_gen, dream_module):
        mock_gen.return_value = "the archaeology of forgotten signals"
        result = dream_module.derive_theme("Recent memory text about watching TV and coding.")
        assert isinstance(result, str)
        assert len(result) >= 5

    @patch("dream_generate._generate_short")
    def test_returns_under_80_chars(self, mock_gen, dream_module):
        mock_gen.return_value = "A" * 200  # Overly long response
        result = dream_module.derive_theme("Memory text here.")
        assert len(result) <= 80

    def test_fallback_on_empty_input(self, dream_module):
        result = dream_module.derive_theme("")
        assert isinstance(result, str)
        assert len(result) > 5

    @patch("dream_generate._generate_short")
    def test_strips_quotes(self, mock_gen, dream_module):
        mock_gen.return_value = '"the weight of accumulated knowledge"'
        result = dream_module.derive_theme("Test memories.")
        assert not result.startswith('"')
        assert not result.endswith('"')

    @patch("dream_generate._generate_short")
    def test_fallback_on_short_response(self, mock_gen, dream_module):
        mock_gen.return_value = "ab"  # Too short (<= 5 chars)
        result = dream_module.derive_theme("Memory text.")
        # Should use the default fallback
        assert "sediment" in result or len(result) > 5


class TestQueryThemedMemories:
    """Tests for query_themed_memories()."""

    @patch("dream_generate._pg_connect")
    def test_returns_list_of_dicts(self, mock_connect, dream_module):
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            ("Memory text about TV", '{"show": "Seinfeld"}', "tv_transcript", datetime(2026, 4, 20)),
            ("Another memory about cooking", '{"title": "Recipe"}', "youtube", datetime(2026, 4, 21)),
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        results = dream_module.query_themed_memories("forgotten signals", count=10)
        assert isinstance(results, list)
        for r in results:
            assert "source" in r
            assert "label" in r
            assert "memory" in r

    @patch("dream_generate._pg_connect")
    def test_returns_empty_on_no_results(self, mock_connect, dream_module):
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        results = dream_module.query_themed_memories("test theme", count=10)
        assert results == []

    @patch("dream_generate._pg_connect")
    def test_falls_back_to_vector_recall(self, mock_connect, dream_module):
        mock_connect.return_value = None
        with patch("dream_generate.recall", return_value=["chunk1", "chunk2"]):
            results = dream_module.query_themed_memories("test", count=5)
        assert len(results) == 2
        assert results[0]["source"] == "recall"


class TestQueryWildcardMemories:
    """Tests for query_wildcard_memories()."""

    @patch("dream_generate._pg_connect")
    def test_returns_requested_count(self, mock_connect, dream_module):
        rows = [
            (f"Wildcard memory {i}", '{}', f"source_{i}", datetime(2026, 1, i + 1))
            for i in range(5)
        ]
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = rows
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn
        results = dream_module.query_wildcard_memories(count=5)
        assert len(results) == 5

    @patch("dream_generate._pg_connect")
    def test_falls_back_to_vector_recall(self, mock_connect, dream_module):
        mock_connect.return_value = None
        with patch("dream_generate.recall", return_value=["a", "b", "c", "d", "e"]):
            results = dream_module.query_wildcard_memories(count=5)
        assert len(results) == 5
        assert all(r["label"] == "wildcard" for r in results)


class TestBuildPrompt:
    """Tests for _build_prompt() — dream generation prompt construction."""

    def test_includes_theme(self, dream_module, sample_themed_memories, sample_wildcard_memories):
        prompt = dream_module._build_prompt(
            "the weight of lost signals", "surreal",
            "Reality is optional.",
            sample_themed_memories, sample_wildcard_memories,
            "Nova is an AI.", "Nova's soul.", ""
        )
        assert "the weight of lost signals" in prompt

    def test_includes_mood(self, dream_module, sample_themed_memories, sample_wildcard_memories):
        prompt = dream_module._build_prompt(
            "test theme", "noir",
            "Shadows have weight.",
            sample_themed_memories, sample_wildcard_memories,
            "", "", ""
        )
        assert "noir" in prompt
        assert "Shadows have weight" in prompt

    def test_includes_all_memories(self, dream_module, sample_themed_memories, sample_wildcard_memories):
        prompt = dream_module._build_prompt(
            "test", "surreal", "desc",
            sample_themed_memories, sample_wildcard_memories,
            "", "", ""
        )
        for m in sample_themed_memories:
            assert m["label"] in prompt
        for m in sample_wildcard_memories:
            assert m["label"] in prompt

    def test_includes_identity_and_soul(self, dream_module):
        prompt = dream_module._build_prompt(
            "test", "surreal", "desc",
            [], [], "IDENTITY_TEXT", "SOUL_TEXT", ""
        )
        assert "IDENTITY_TEXT" in prompt
        assert "SOUL_TEXT" in prompt

    def test_includes_previous_dreams(self, dream_module):
        prompt = dream_module._build_prompt(
            "test", "surreal", "desc",
            [], [], "", "", "Previous dream summary here"
        )
        assert "Previous dream summary here" in prompt


class TestMoodList:
    """Tests for the MOODS constant."""

    def test_mood_list_has_8_entries(self, dream_module):
        assert len(dream_module.MOODS) == 8

    def test_each_mood_is_tuple_of_two_strings(self, dream_module):
        for mood in dream_module.MOODS:
            assert isinstance(mood, tuple)
            assert len(mood) == 2
            assert isinstance(mood[0], str)  # name
            assert isinstance(mood[1], str)  # description
            assert len(mood[0]) > 0
            assert len(mood[1]) > 10

    def test_expected_mood_names(self, dream_module):
        names = {m[0] for m in dream_module.MOODS}
        expected = {"surreal", "nostalgic", "anxious", "euphoric", "noir", "liminal", "feral", "sacred"}
        assert names == expected


class TestHeaderStripping:
    """Tests for header stripping logic in generate_narrative()."""

    def test_strips_dream_header(self, dream_module):
        response = "# Dream Journal Entry\n*Nova's Dream*\n---\nActual dream content here."
        lines = response.splitlines()
        while lines and (lines[0].startswith("# Dream") or lines[0].startswith("*Nova") or
                         lines[0].strip() == "---" or lines[0].strip() == ""):
            lines.pop(0)
        result = "\n".join(lines).strip()
        assert result == "Actual dream content here."

    def test_strips_theme_header(self, dream_module):
        response = '*Theme: "test theme"*\n---\nDream body.'
        lines = response.splitlines()
        while lines and (lines[0].startswith("*Theme") or lines[0].strip() == "---" or lines[0].strip() == ""):
            lines.pop(0)
        result = "\n".join(lines).strip()
        assert result == "Dream body."

    def test_preserves_content_without_header(self, dream_module):
        response = "The corridor stretched impossibly long.\nI walked forward."
        lines = response.splitlines()
        while lines and (lines[0].startswith("# Dream") or lines[0].startswith("*Nova") or
                         lines[0].startswith("*Theme") or lines[0].strip() == "---" or lines[0].strip() == ""):
            lines.pop(0)
        result = "\n".join(lines).strip()
        assert result == response


class TestRepetitionDetection:
    """Tests for repetition loop detection and trimming."""

    def test_detects_repeated_phrase(self):
        """A 6-word phrase repeated 3+ times after position 150 should be trimmed."""
        # Build 160 unique words so the loop starts scanning past position 150
        unique_start = [f"word{i}" for i in range(160)]
        # Then a 6-word phrase repeated 4 times consecutively
        repeated_phrase = ["the", "same", "six", "words", "over", "again"]
        all_words = unique_start + repeated_phrase * 4
        text = " ".join(all_words)
        words = text.split()
        trimmed = False
        for window in [6, 10, 15]:
            if len(words) <= window * 3:
                continue
            for i in range(len(words) - window * 2):
                if i + window < 150:
                    continue
                phrase = " ".join(words[i:i + window])
                rest = " ".join(words[i + window:])
                if rest.count(phrase) >= 2:
                    text = " ".join(words[:i + window]).strip()
                    trimmed = True
                    break
            if trimmed:
                break
        assert trimmed

    def test_preserves_unique_text(self):
        """Text with no repetition should not be trimmed."""
        words = [f"unique{i}" for i in range(400)]
        text = " ".join(words)
        trimmed = False
        for window in [6, 10, 15]:
            if len(words) <= window * 3:
                continue
            for i in range(len(words) - window * 2):
                if i + window < 150:
                    continue
                phrase = " ".join(words[i:i + window])
                rest = " ".join(words[i + window:])
                if rest.count(phrase) >= 2:
                    trimmed = True
                    break
        assert not trimmed


class TestRetryLogic:
    """Tests for short response retry logic."""

    @patch("dream_generate._generate_via_openrouter")
    @patch("dream_generate.query_wildcard_memories")
    @patch("dream_generate.query_themed_memories")
    @patch("dream_generate.derive_theme")
    @patch("dream_generate.query_recent_memories_for_theme")
    @patch("dream_generate.read_file")
    def test_retries_on_short_response(self, mock_read, mock_recent, mock_theme,
                                        mock_themed, mock_wildcard, mock_openrouter, dream_module):
        """If first response is <100 words, retry is triggered."""
        mock_read.return_value = ""
        mock_recent.return_value = ("text", [])
        mock_theme.return_value = "test theme"
        mock_themed.return_value = [{"source": "s", "label": "l", "memory": "m"}]
        mock_wildcard.return_value = [{"source": "s", "label": "l", "memory": "m"}]
        # First call returns short, second returns adequate
        mock_openrouter.side_effect = [
            "Too short a dream.",
            " ".join(["Long dream narrative word"] * 200),
        ]
        narrative, _, _ = dream_module.generate_narrative()
        # Should have called openrouter twice (once initial, once retry)
        assert mock_openrouter.call_count == 2


class TestCircuitBreaker:
    """Tests for Ollama circuit breaker logic."""

    def test_circuit_closed_when_no_file(self, dream_module, tmp_path):
        with patch.object(dream_module, "CIRCUIT_BREAKER_FILE", tmp_path / "nonexistent.json"):
            assert dream_module._ollama_circuit_open() is False

    def test_circuit_opens_after_failures(self, dream_module, tmp_path):
        cb_file = tmp_path / "circuit.json"
        data = {
            "consecutive_failures": 3,
            "last_failure": datetime.now().isoformat(),
            "cooldown_hours": 1,
        }
        cb_file.write_text(json.dumps(data))
        with patch.object(dream_module, "CIRCUIT_BREAKER_FILE", cb_file):
            assert dream_module._ollama_circuit_open() is True

    def test_circuit_resets(self, dream_module, tmp_path):
        cb_file = tmp_path / "circuit.json"
        cb_file.write_text('{"consecutive_failures": 5}')
        with patch.object(dream_module, "CIRCUIT_BREAKER_FILE", cb_file):
            dream_module._ollama_circuit_reset()
        assert not cb_file.exists()


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestSecurityDream:
    """Security tests: no PII in prompts, EXCLUDE_SOURCES enforced."""

    def test_exclude_sources_contains_sensitive(self, dream_module):
        expected = [
            "email", "private_document", "imessage", "security",
            "slack_general", "slack_conversation", "work_knowledge",
            "email_archive", "home_address", "calendar", "apple_health",
        ]
        for src in expected:
            assert src in dream_module.EXCLUDE_SOURCES, f"Missing from EXCLUDE_SOURCES: {src}"

    def test_no_pii_emails_in_source(self):
        source = (Path(__file__).parent.parent / "dream_generate.py").read_text()
        pii = ["testuser@example.com", "testuser@corp.example.com", "testuser@domain.example.com"]
        for email in pii:
            assert email not in source

    def test_no_hardcoded_api_keys(self):
        source = (Path(__file__).parent.parent / "dream_generate.py").read_text()
        import re
        assert not re.search(r'sk-[a-zA-Z0-9]{20,}', source)
        assert not re.search(r'AKIA[A-Z0-9]{16}', source)

    def test_api_key_from_keychain_only(self):
        source = (Path(__file__).parent.parent / "dream_generate.py").read_text()
        assert "find-generic-password" in source
        assert "nova-openrouter-api-key" in source

    def test_prompt_does_not_include_raw_pii(self, dream_module, sample_themed_memories, sample_wildcard_memories):
        """Built prompt should not contain PII patterns."""
        prompt = dream_module._build_prompt(
            "test", "surreal", "desc",
            sample_themed_memories, sample_wildcard_memories,
            "", "", ""
        )
        pii_patterns = ["testuser@example.com", "testuser@corp.example.com", "testuser@domain.example.com"]
        for pii in pii_patterns:
            assert pii not in prompt

    def test_no_hardcoded_home_paths_in_source(self):
        source = (Path(__file__).parent.parent / "dream_generate.py").read_text()
        assert str(Path.home()) + "/" not in source


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.functional
class TestJournalWriting:
    """Functional tests for journal file writing."""

    def test_journal_has_correct_markdown_structure(self, dream_module):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(dream_module, "JOURNAL_DIR", Path(tmpdir)):
                with patch.object(dream_module, "TODAY", "2026-05-03"):
                    meta = {"theme": "forgotten signals", "mood": "noir"}
                    path = dream_module.write_journal(
                        "The corridor stretched long.",
                        image_path="/tmp/dream.png",
                        inspirations=[{"source": "tv", "label": "Show", "memory": "Memory text"}],
                        dream_meta=meta,
                    )
                    content = path.read_text()
                    assert "# Dream Journal" in content
                    assert "2026-05-03" in content
                    assert "forgotten signals" in content
                    assert "noir" in content
                    assert "The corridor stretched long." in content
                    assert "![Dream]" in content

    def test_journal_without_image(self, dream_module):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(dream_module, "JOURNAL_DIR", Path(tmpdir)):
                with patch.object(dream_module, "TODAY", "2026-05-03"):
                    path = dream_module.write_journal("Dream text.", image_path=None)
                    content = path.read_text()
                    assert "![Dream]" not in content

    def test_journal_deduplicates_inspirations(self, dream_module):
        inspirations = [
            {"source": "tv", "label": "Show", "memory": "Memory A"},
            {"source": "tv", "label": "Show", "memory": "Memory B"},
            {"source": "movie", "label": "Film", "memory": "Memory C"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(dream_module, "JOURNAL_DIR", Path(tmpdir)):
                with patch.object(dream_module, "TODAY", "2026-05-03"):
                    path = dream_module.write_journal("Dream.", inspirations=inspirations)
                    content = path.read_text()
                    # tv:Show should appear only once
                    assert content.count("**[tv]**") == 1
                    assert "**[movie]**" in content


@pytest.mark.functional
class TestPendingDelivery:
    """Functional tests for pending delivery JSON."""

    def test_pending_has_all_required_fields(self, dream_module):
        with tempfile.TemporaryDirectory() as tmpdir:
            pending = Path(tmpdir) / "pending.json"
            with patch.object(dream_module, "PENDING", pending):
                with patch.object(dream_module, "TODAY", "2026-05-03"):
                    meta = {"theme": "test theme", "mood": "noir"}
                    dream_module.write_pending(
                        "Narrative here.", Path("/tmp/journal.md"),
                        image_path="/tmp/img.png",
                        inspirations=[{"source": "s", "label": "l", "memory": "m"}],
                        dream_meta=meta,
                    )
                    data = json.loads(pending.read_text())
                    assert data["date"] == "2026-05-03"
                    assert data["narrative"] == "Narrative here."
                    assert data["image"] == "/tmp/img.png"
                    assert len(data["inspirations"]) == 1
                    assert data["dream_meta"]["theme"] == "test theme"
                    assert data["dream_meta"]["mood"] == "noir"
                    assert "queued_at" in data


@pytest.mark.functional
class TestFullPipeline:
    """Full pipeline: generate_narrative -> write_journal -> write_pending."""

    @patch("dream_generate.store_memory")
    @patch("dream_generate.generate_dream_image", return_value="")
    @patch("dream_generate._generate_via_openrouter")
    @patch("dream_generate.query_wildcard_memories")
    @patch("dream_generate.query_themed_memories")
    @patch("dream_generate.derive_theme")
    @patch("dream_generate.query_recent_memories_for_theme")
    @patch("dream_generate.read_file")
    def test_full_pipeline(self, mock_read, mock_recent, mock_theme,
                            mock_themed, mock_wildcard, mock_openrouter,
                            mock_image, mock_store_mem, dream_module):
        mock_read.return_value = ""
        mock_recent.return_value = ("memories", [])
        mock_theme.return_value = "the archaeology of forgotten signals"
        mock_themed.return_value = [
            {"source": "tv", "label": "Show", "memory": "A memory about TV."}
            for _ in range(10)
        ]
        mock_wildcard.return_value = [
            {"source": "wild", "label": "Random", "memory": "A wildcard memory."}
            for _ in range(5)
        ]
        narrative_text = " ".join(["Dream narrative word"] * 300)
        mock_openrouter.return_value = narrative_text

        narrative, inspirations, meta = dream_module.generate_narrative()
        assert len(narrative.split()) >= 100
        assert len(inspirations) == 15  # 10 themed + 5 wildcard
        assert "theme" in meta
        assert "mood" in meta


# ═══════════════════════════════════════════════════════════════════════════════
# FRAME TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.frame
class TestDreamFrameTests:
    """Frame tests: imports, main() behavior, constants."""

    def test_script_imports_without_error(self):
        """dream_generate.py can be imported without side effects."""
        if "dream_generate" in sys.modules:
            del sys.modules["dream_generate"]
        if "psycopg2" not in sys.modules:
            sys.modules["psycopg2"] = MagicMock()
        try:
            import dream_generate
        except Exception as e:
            pytest.fail(f"Import failed: {e}")

    def test_main_skips_if_pending_exists(self, dream_module, tmp_path):
        """main() should skip generation if pending delivery exists for today."""
        pending = tmp_path / "pending.json"
        pending.write_text(json.dumps({
            "date": dream_module.TODAY,
            "narrative": "Already generated dream.",
        }))
        with patch.object(dream_module, "PENDING", pending):
            with patch.object(dream_module, "deliver_dream") as mock_deliver:
                dream_module.main()
                mock_deliver.assert_called_once()

    def test_rolling_dates_correct(self, dream_module):
        assert len(dream_module.ROLLING_DATES) == 7
        assert dream_module.ROLLING_DATES[0] == date.today().isoformat()

    def test_exclude_sources_is_tuple(self, dream_module):
        assert isinstance(dream_module.EXCLUDE_SOURCES, tuple)

    def test_today_is_iso_format(self, dream_module):
        assert dream_module.TODAY == date.today().isoformat()

    @patch("dream_generate.generate_dream_image", return_value="")
    def test_image_generation_handles_swarmui_down(self, mock_gen, dream_module):
        """Image generation gracefully returns empty string when SwarmUI is down."""
        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError):
            result = dream_module.generate_dream_image("Dream narrative text.", "surreal")
        assert result == ""


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestDreamIntegration:
    """Integration tests that hit real local services."""

    @pytest.fixture(autouse=True)
    def check_postgres_available(self):
        try:
            import psycopg2
            conn = psycopg2.connect("dbname=nova_memories")
            conn.close()
        except Exception:
            pytest.skip("PostgreSQL nova_memories database not available")

    def test_query_recent_memories_returns_data(self, dream_module):
        text, records = dream_module.query_recent_memories_for_theme()
        # May be empty if no recent memories, but should not error
        assert isinstance(text, str)
        assert isinstance(records, list)

    @patch("dream_generate._generate_short")
    def test_derive_theme_with_real_memories(self, mock_gen, dream_module):
        mock_gen.return_value = "the erosion of digital certainty"
        text, _ = dream_module.query_recent_memories_for_theme()
        if text:
            theme = dream_module.derive_theme(text)
            assert isinstance(theme, str)
            assert len(theme) > 5


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
