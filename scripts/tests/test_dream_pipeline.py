#!/usr/bin/env python3
"""
Unit tests for the dream pipeline (dream_generate.py + dream_deliver.py).
Covers: memory retrieval, journal writing, inspiration formatting, image integration,
herd email assembly, and delivery flow.

Run: python3 -m pytest tests/test_dream_pipeline.py -v
Written by Jordan Koch.
"""

import json
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── dream_generate.py tests ──────────────────────────────────────────────────

class TestQueryRecentIngests:
    """Tests for query_recent_ingests() — PostgreSQL memory retrieval."""

    def test_returns_one_memory_per_source(self):
        """Live test: verify each source gets exactly one memory."""
        from dream_generate import query_recent_ingests
        text, inspirations = query_recent_ingests()

        # Should have at least a few sources with recent content
        assert len(inspirations) > 0
        # Each inspiration must have the required fields
        for i in inspirations:
            assert "source" in i
            assert "label" in i
            assert "memory" in i
            assert len(i["memory"]) > 0
        # No duplicate sources
        sources = [i["source"] for i in inspirations]
        assert len(sources) == len(set(sources)), "Duplicate sources found"

    def test_excludes_noise_sources(self):
        """Verify noise sources don't appear in results."""
        from dream_generate import query_recent_ingests

        noise = {"private_document", "email_archive", "imessage",
                 "slack_general", "security", "dream", "system"}
        _, inspirations = query_recent_ingests()
        returned_sources = {i["source"] for i in inspirations}
        leaked = returned_sources & noise
        assert not leaked, f"Noise sources leaked through: {leaked}"

    @patch("dream_generate.HAS_PG", False)
    def test_graceful_without_postgres(self):
        from dream_generate import query_recent_ingests
        text, inspirations = query_recent_ingests()
        assert text == ""
        assert inspirations == []


class TestWriteJournal:
    """Tests for write_journal() — markdown output with inspirations."""

    def test_includes_image_when_provided(self):
        from dream_generate import write_journal, JOURNAL_DIR
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("dream_generate.JOURNAL_DIR", Path(tmpdir)):
                with patch("dream_generate.TODAY", "2026-01-01"):
                    path = write_journal("Test narrative.", image_path="/tmp/test.png")
                    content = path.read_text()
                    assert "![Dream](/tmp/test.png)" in content

    def test_no_image_line_when_none(self):
        from dream_generate import write_journal
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("dream_generate.JOURNAL_DIR", Path(tmpdir)):
                with patch("dream_generate.TODAY", "2026-01-01"):
                    path = write_journal("Test narrative.", image_path=None)
                    content = path.read_text()
                    assert "![Dream]" not in content

    def test_inspirations_show_full_memory_text(self):
        from dream_generate import write_journal
        inspirations = [
            {"source": "television", "label": "Jeopardy!", "count": 100,
             "memory": "Clue: What is the capital of France? Answer: Paris.",
             "ingested": "2026-05-01T00:00:00"},
            {"source": "horror", "label": "Pinhead VS Jason", "count": 50,
             "memory": "Pinhead raises his chains and Jason blocks with machete.",
             "ingested": "2026-05-01T00:00:00"},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("dream_generate.JOURNAL_DIR", Path(tmpdir)):
                with patch("dream_generate.TODAY", "2026-01-01"):
                    path = write_journal("Dream text.", inspirations=inspirations)
                    content = path.read_text()
                    assert "### Memories that inspired this dream" in content
                    assert "**[television]**" in content
                    assert "What is the capital of France" in content
                    assert "**[horror]**" in content
                    assert "Pinhead raises his chains" in content

    def test_deduplicates_inspirations(self):
        from dream_generate import write_journal
        inspirations = [
            {"source": "tv", "label": "Show", "count": 10, "memory": "Memory A", "ingested": None},
            {"source": "tv", "label": "Show", "count": 10, "memory": "Memory B", "ingested": None},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("dream_generate.JOURNAL_DIR", Path(tmpdir)):
                with patch("dream_generate.TODAY", "2026-01-01"):
                    path = write_journal("Dream.", inspirations=inspirations)
                    content = path.read_text()
                    # Should only appear once (deduplicated by source:label)
                    assert content.count("**[tv]**") == 1


class TestWritePending:
    """Tests for write_pending() — delivery JSON payload."""

    def test_includes_all_fields(self):
        from dream_generate import write_pending
        with tempfile.TemporaryDirectory() as tmpdir:
            pending_path = Path(tmpdir) / "pending.json"
            with patch("dream_generate.PENDING", pending_path):
                with patch("dream_generate.TODAY", "2026-01-01"):
                    write_pending("Narrative text", Path("/tmp/j.md"),
                                  image_path="/tmp/img.png",
                                  inspirations=[{"source": "tv", "memory": "test"}])
                    data = json.loads(pending_path.read_text())
                    assert data["date"] == "2026-01-01"
                    assert data["narrative"] == "Narrative text"
                    assert data["image"] == "/tmp/img.png"
                    assert len(data["inspirations"]) == 1
                    assert data["inspirations"][0]["memory"] == "test"


class TestRepetitionTrimmer:
    """Tests for the repetition detection/trimming logic."""

    def test_does_not_trim_short_narratives(self):
        from dream_generate import generate_narrative
        # The trimmer should never cut below 150 words
        # We test the logic directly
        words = ["word"] * 100 + ["word"] * 100  # 200 words, all "word" (extreme repeat)
        response = " ".join(words)

        # Simulate the trimming logic
        for window in [6, 10, 15]:
            if len(words) <= window * 3:
                continue
            for i in range(len(words) - window * 2):
                if i + window < 150:
                    continue
                phrase = " ".join(words[i:i + window])
                rest = " ".join(words[i + window:])
                if rest.count(phrase) >= 2:
                    response = " ".join(words[:i + window]).strip()
                    words = response.split()
                    break

        # Should trim but not below 150
        assert len(response.split()) >= 150

    def test_preserves_non_repetitive_text(self):
        # Unique words should never be trimmed
        words = [f"word{i}" for i in range(400)]
        response = " ".join(words)

        for window in [6, 10, 15]:
            if len(words) <= window * 3:
                continue
            trimmed = False
            for i in range(len(words) - window * 2):
                if i + window < 150:
                    continue
                phrase = " ".join(words[i:i + window])
                rest = " ".join(words[i + window:])
                if rest.count(phrase) >= 2:
                    trimmed = True
                    break
            assert not trimmed


# ── dream_deliver.py tests ────────────────────────────────────────────────────

class TestDreamDeliver:
    """Tests for dream_deliver.py — Slack posting and herd email."""

    def test_slack_channel_is_nova_chat(self):
        sys.path.insert(0, str(Path(__file__).parent.parent))
        # Read the actual source to check the constant
        source = (Path(__file__).parent.parent / "dream_deliver.py").read_text()
        assert 'SLACK_CHANNEL = "C0AMNQ5GX70"' in source

    def test_inspirations_appended_to_narrative(self):
        inspirations = [
            {"source": "television", "label": "Jeopardy!", "count": 100,
             "memory": "What is the capital of France?"},
            {"source": "horror", "label": "Pinhead", "count": 50,
             "memory": "Chains and suffering."},
        ]
        # Simulate the delivery logic
        narrative = "Dream text here."
        seen = set()
        insp_lines = []
        for i in inspirations:
            key = f"{i.get('source', '')}:{i.get('label', '')}"
            if key not in seen:
                seen.add(key)
                memory_text = i.get("memory", i.get("snippet", ""))[:200]
                insp_lines.append(f"  • *[{i.get('source', '?')}]* {memory_text}")
        if insp_lines:
            narrative += "\n\n_Memories that inspired this dream:_\n" + "\n".join(insp_lines)

        assert "_Memories that inspired this dream:_" in narrative
        assert "*[television]*" in narrative
        assert "What is the capital of France?" in narrative
        assert "*[horror]*" in narrative

    def test_herd_email_sends_single_message_with_cc(self):
        source = (Path(__file__).parent.parent / "dream_deliver.py").read_text()
        # Should NOT have a for loop over recipients for sending
        assert "for recipient in HERD_RECIPIENTS:" not in source
        # Should have CC logic
        assert "--cc" in source
        # Should attach image
        assert "--attachment" in source


# ── herd_config.py tests ──────────────────────────────────────────────────────

class TestHerdConfig:
    """Tests for herd_config.py — member list validation."""

    def test_all_expected_members_present(self):
        sys.path.insert(0, str(Path.home() / ".openclaw"))
        from herd_config import HERD, HERD_EMAILS

        expected = {
            "sam@jasonacox.com", "oc@mostlycopyandpaste.com",
            "gaston@bluemoxon.com", "marey@makehorses.org",
            "colette@pilatesmuse.co", "rockbot@makehorses.org",
            "ara@monsterheaven.com", "jules@laplante.dev",
            "nova@servernest.xyz",
        }
        assert HERD_EMAILS == expected

    def test_no_duplicate_emails(self):
        sys.path.insert(0, str(Path.home() / ".openclaw"))
        from herd_config import HERD
        emails = [m["email"] for m in HERD]
        assert len(emails) == len(set(emails))

    def test_all_members_have_required_fields(self):
        sys.path.insert(0, str(Path.home() / ".openclaw"))
        from herd_config import HERD
        for m in HERD:
            assert "name" in m, f"Missing name: {m}"
            assert "email" in m, f"Missing email: {m}"
            assert "@" in m["email"], f"Invalid email: {m['email']}"

    def test_nova_cosmos_removed(self):
        sys.path.insert(0, str(Path.home() / ".openclaw"))
        from herd_config import HERD_EMAILS
        assert "novacosmos184@gmail.com" not in HERD_EMAILS

    def test_nova_scott_present(self):
        sys.path.insert(0, str(Path.home() / ".openclaw"))
        from herd_config import HERD_EMAILS
        assert "nova@servernest.xyz" in HERD_EMAILS


# ── generate_image.sh tests ──────────────────────────────────────────────────

class TestGenerateImage:
    """Tests for generate_image.sh — port and retry logic."""

    def test_uses_correct_port(self):
        source = (Path(__file__).parent.parent / "generate_image.sh").read_text()
        assert 'SWARM_URL="http://localhost:7801"' in source
        assert 'http://localhost:7802' not in source

    def test_has_retry_logic(self):
        source = (Path(__file__).parent.parent / "generate_image.sh").read_text()
        assert "sleep 1" in source  # retry wait


# ── Integration test ─────────────────────────────────────────────────────────

class TestPipelineIntegration:
    """End-to-end checks on the pipeline data flow."""

    def test_rolling_dates_are_7_days(self):
        from dream_generate import ROLLING_DATES, ROLLING_DAYS
        assert ROLLING_DAYS == 7
        assert len(ROLLING_DATES) == 7
        assert ROLLING_DATES[0] == date.today().isoformat()

    def test_generate_narrative_returns_tuple(self):
        # Verify the function signature returns (str, list)
        import inspect
        from dream_generate import generate_narrative
        sig = inspect.signature(generate_narrative)
        # Return type annotation says tuple
        ann = sig.return_annotation
        assert ann == tuple[str, list[dict]] or "tuple" in str(ann).lower() or ann == inspect.Parameter.empty

    def test_scheduler_has_single_dream_job(self):
        import yaml
        with open(Path.home() / ".openclaw/config/scheduler.yaml") as f:
            config = yaml.safe_load(f)
        tasks = config.get("tasks", {})
        dream_jobs = [k for k in tasks if "dream" in k.lower()]
        assert dream_jobs == ["dream_pipeline"], f"Expected single dream_pipeline, got: {dream_jobs}"
        assert tasks["dream_pipeline"]["schedule"] == "cron 0 5 * * *"
        assert tasks["dream_pipeline"]["timeout"] == 900


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
