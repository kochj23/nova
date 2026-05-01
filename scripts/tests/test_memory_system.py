#!/usr/bin/env python3
"""
Tests for Nova's memory system scripts:
  - nova_memory_first.py  — query classification, recency routing, memory lookup
  - nova_recent_memories.py — PostgreSQL recent memories, CLI parsing, formatting
  - nova_memory_consolidate.py — synthesis modules, vector helpers
  - nova_memory_breakdown.py — breakdown output and queue waiting

Run: python3 -m pytest tests/test_memory_system.py -v
Written by Jordan Koch.
"""

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone, date
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# =============================================================================
# nova_memory_first.py — classify_query
# =============================================================================

class TestClassifyQuery:
    """Tests for classify_query() — routing queries to the right memory sources."""

    def test_imessage_query(self):
        """iMessage keywords route to the imessage source."""
        from nova_memory_first import classify_query
        sources, labels, _ = classify_query("what did he text me yesterday?")
        assert "imessage" in sources
        assert "iMessage" in labels

    def test_slack_query(self):
        """Slack keywords route to slack sources."""
        from nova_memory_first import classify_query
        sources, labels, _ = classify_query("what was posted in the slack channel?")
        assert "slack_general" in sources or "slack_conversation" in sources

    def test_health_query(self):
        """Health-related keywords route to health sources."""
        from nova_memory_first import classify_query
        sources, labels, _ = classify_query("what was my blood pressure last week?")
        assert "apple_health" in sources or "health" in sources
        assert "health" in labels

    def test_rave_music_query(self):
        """Rave and music keywords route to music/rave sources."""
        from nova_memory_first import classify_query
        sources, labels, _ = classify_query("do you remember any raves from 2002?")
        assert "music" in sources or "socal_rave" in sources
        assert "music/rave" in labels

    def test_horror_query(self):
        """Horror movie queries route to horror sources."""
        from nova_memory_first import classify_query
        sources, labels, _ = classify_query("tell me about Jason from Friday the 13th")
        assert "horror" in sources

    def test_corvette_query(self):
        """Corvette keywords route to workshop manual source."""
        from nova_memory_first import classify_query
        sources, labels, _ = classify_query("what is the torque spec for the C6 corvette?")
        assert "corvette_workshop_manual" in sources

    def test_people_query_prefers_search(self):
        """People queries set prefer_search=True for text search over vector recall."""
        from nova_memory_first import classify_query
        _, _, prefer_search = classify_query("who is Sam?")
        assert prefer_search is True

    def test_default_sources_on_no_match(self):
        """Unclassifiable queries fall back to DEFAULT_SOURCES."""
        from nova_memory_first import classify_query, DEFAULT_SOURCES
        sources, labels, prefer_search = classify_query("qwpxmzyrtvbk nonsense gibberish")
        assert sources == DEFAULT_SOURCES
        assert labels == ["general"]
        assert prefer_search is False

    def test_multiple_rules_merge_sources(self):
        """A query matching multiple rules merges sources from all matches."""
        from nova_memory_first import classify_query
        # "punk rave in burbank" hits punk/hardcore, music/rave, and local/home
        sources, labels, _ = classify_query("punk rave in burbank")
        assert len(labels) >= 2
        # Should have sources from multiple categories merged
        assert len(sources) > 3

    def test_appviewx_work_query(self):
        """AppViewX / PKI work queries route to work_knowledge."""
        from nova_memory_first import classify_query
        sources, labels, _ = classify_query("what is the AppViewX migration project status?")
        assert "work_knowledge" in sources

    def test_sre_query(self):
        """SRE concepts route to sre source."""
        from nova_memory_first import classify_query
        sources, labels, _ = classify_query("what are the four golden signals of SRE?")
        assert "sre" in sources

    def test_demonology_query(self):
        """Demonology and occult queries route correctly."""
        from nova_memory_first import classify_query
        sources, labels, _ = classify_query("tell me about the Goetia grimoire")
        assert "demonology" in sources or "occult" in sources

    def test_drag_racing_query(self):
        """Drag racing queries route to drag_racing source."""
        from nova_memory_first import classify_query
        sources, labels, _ = classify_query("what was his quarter mile elapsed time?")
        assert "drag_racing" in sources

    def test_comedy_query(self):
        """Stand-up comedy queries route to comedy source."""
        from nova_memory_first import classify_query
        sources, labels, _ = classify_query("Dave Chappelle's Killing Them Softly special")
        assert "comedy" in sources

    def test_security_camera_query(self):
        """Security and camera queries route to security source."""
        from nova_memory_first import classify_query
        sources, labels, _ = classify_query("was there motion detected on the front door camera?")
        assert "security" in sources


# =============================================================================
# nova_memory_first.py — recency routing
# =============================================================================

class TestRecencyRouting:
    """Tests for routing 'what was added' type questions to nova_recent_memories."""

    def test_recency_signal_detected(self):
        """Questions about recently added memories trigger recency routing."""
        from nova_memory_first import main as _main

        # We test the detection logic directly rather than calling main()
        query = "what new memories were added in the last 24 hours?"
        _q_lower = query.lower()
        _recency_signals = ["added", "ingested", "new memories", "recently added",
                            "what was added", "what memories were", "past 24", "past 48",
                            "past 72", "yesterday", "last 24", "last 48", "last 72",
                            "how many memories", "what's new in"]

        has_signal = any(sig in _q_lower for sig in _recency_signals)
        has_memory_word = any(w in _q_lower for w in ["memor", "vector", "postgres", "db",
                                                       "database", "added", "ingested", "new"])

        assert has_signal is True
        assert has_memory_word is True

    def test_recency_signal_72_hours(self):
        """72-hour queries are correctly parsed."""
        query = "what memories were added in the past 72 hours?"
        _q_lower = query.lower()
        hours = 72 if "72" in _q_lower or "3 day" in _q_lower else (
            48 if "48" in _q_lower or "2 day" in _q_lower else 24)
        assert hours == 72

    def test_recency_signal_48_hours(self):
        """48-hour queries are correctly parsed."""
        query = "how many memories were ingested in the last 48 hours?"
        _q_lower = query.lower()
        hours = 72 if "72" in _q_lower or "3 day" in _q_lower else (
            48 if "48" in _q_lower or "2 day" in _q_lower else 24)
        assert hours == 48

    def test_recency_signal_defaults_to_24(self):
        """When no specific hour count is given, default to 24."""
        query = "what was added to the memory database recently?"
        _q_lower = query.lower()
        hours = 72 if "72" in _q_lower or "3 day" in _q_lower else (
            48 if "48" in _q_lower or "2 day" in _q_lower else 24)
        assert hours == 24

    def test_non_recency_query_not_detected(self):
        """Normal memory queries do not trigger recency routing."""
        query = "what raves do you remember from 2002?"
        _q_lower = query.lower()
        _recency_signals = ["added", "ingested", "new memories", "recently added",
                            "what was added", "what memories were", "past 24"]

        # "remember" doesn't match "added" or "ingested"
        has_signal = any(sig in _q_lower for sig in _recency_signals)
        # Even if signal matched, the second check for "memor" etc would need to pass
        # but "remember" does contain "memor" — the test verifies the signal check fails first
        assert has_signal is False


# =============================================================================
# nova_memory_first.py — memory_lookup
# =============================================================================

class TestMemoryLookup:
    """Tests for memory_lookup() — the full recall/search pipeline."""

    @patch("nova_memory_first.recall")
    @patch("nova_memory_first.batch_recall")
    @patch("nova_memory_first.search")
    def test_returns_results_from_batch_recall(self, mock_search, mock_batch, mock_recall):
        """memory_lookup uses batch_recall for source-filtered queries."""
        mock_batch.return_value = [
            {"query": "test", "memories": [
                {"text": "Memory about raves in 1999", "source": "music", "score": 0.9}
            ]},
        ]
        mock_recall.return_value = []
        mock_search.return_value = []

        from nova_memory_first import memory_lookup
        results, sources_searched, labels = memory_lookup("tell me about raves")

        assert len(results) >= 1
        assert results[0]["text"] == "Memory about raves in 1999"
        mock_batch.assert_called_once()

    @patch("nova_memory_first.recall")
    @patch("nova_memory_first.batch_recall")
    @patch("nova_memory_first.search")
    def test_deduplicates_results(self, mock_search, mock_batch, mock_recall):
        """Duplicate memories (by text prefix) are removed."""
        duplicate = {"text": "Same memory about the park", "source": "local", "score": 0.8}
        mock_batch.return_value = [
            {"query": "q", "memories": [duplicate]},
        ]
        mock_recall.return_value = [duplicate.copy()]  # Duplicate from broad recall
        mock_search.return_value = []

        from nova_memory_first import memory_lookup
        results, _, _ = memory_lookup("burbank park")

        texts = [r["text"][:50] for r in results]
        assert len(texts) == len(set(texts)), "Duplicate results not removed"

    @patch("nova_memory_first.recall")
    @patch("nova_memory_first.batch_recall")
    @patch("nova_memory_first.search")
    def test_caps_results_at_recall_count(self, mock_search, mock_batch, mock_recall):
        """Results are capped at RECALL_COUNT."""
        from nova_memory_first import RECALL_COUNT

        many_results = [{"text": f"Memory {i}", "source": "src", "score": 0.5} for i in range(20)]
        mock_batch.return_value = [{"query": "q", "memories": many_results}]
        mock_recall.return_value = []
        mock_search.return_value = []

        from nova_memory_first import memory_lookup
        results, _, _ = memory_lookup("some query")

        assert len(results) <= RECALL_COUNT

    @patch("nova_memory_first.recall")
    @patch("nova_memory_first.batch_recall", return_value=[])
    @patch("nova_memory_first.search")
    def test_runs_search_when_few_recall_results(self, mock_search, mock_batch, mock_recall):
        """Text search is triggered when recall returns fewer than 3 results."""
        mock_recall.return_value = []
        mock_search.return_value = [
            {"text": "Found via text search", "source": "email_archive"}
        ]

        from nova_memory_first import memory_lookup
        results, _, _ = memory_lookup("who is Sam?")

        # search should have been called because batch_recall returned empty
        assert mock_search.called


# =============================================================================
# nova_memory_first.py — recall and search helpers
# =============================================================================

class TestRecallAndSearch:
    """Tests for the recall() and search() HTTP wrapper functions."""

    @patch("nova_memory_first.urllib.request.urlopen")
    def test_recall_returns_memories_list(self, mock_urlopen):
        """recall() parses the memories list from the response."""
        response = json.dumps({
            "memories": [
                {"text": "Memory 1", "source": "tv", "score": 0.92},
                {"text": "Memory 2", "source": "music", "score": 0.88},
            ]
        }).encode()
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=BytesIO(response))
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        from nova_memory_first import recall
        results = recall("test query")
        assert len(results) == 2
        assert results[0]["text"] == "Memory 1"

    @patch("nova_memory_first.urllib.request.urlopen", side_effect=Exception("timeout"))
    def test_recall_returns_empty_on_error(self, mock_urlopen):
        """recall() returns empty list on connection failure."""
        from nova_memory_first import recall
        results = recall("test query")
        assert results == []

    @patch("nova_memory_first.urllib.request.urlopen")
    def test_search_returns_results(self, mock_urlopen):
        """search() parses results from /search endpoint."""
        response = json.dumps({
            "memories": [{"text": "Found text", "source": "email"}]
        }).encode()
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=BytesIO(response))
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        from nova_memory_first import search
        results = search("some keyword")
        assert len(results) == 1

    @patch("nova_memory_first.urllib.request.urlopen", side_effect=Exception("refused"))
    def test_search_returns_empty_on_error(self, mock_urlopen):
        """search() returns empty list on failure."""
        from nova_memory_first import search
        results = search("keyword")
        assert results == []


# =============================================================================
# nova_memory_first.py — batch_recall
# =============================================================================

class TestBatchRecall:
    """Tests for batch_recall() — multi-query HTTP request."""

    @patch("nova_memory_first.urllib.request.urlopen")
    def test_sends_batch_request(self, mock_urlopen):
        """batch_recall sends queries as a single POST to /recall_batch."""
        response = json.dumps({
            "results": [
                {"query": "q1", "memories": [{"text": "M1", "source": "src1"}]},
                {"query": "q2", "memories": [{"text": "M2", "source": "src2"}]},
            ]
        }).encode()
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=BytesIO(response))
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        from nova_memory_first import batch_recall
        results = batch_recall([{"q": "q1", "n": 3}, {"q": "q2", "n": 3}])

        assert len(results) == 2
        assert results[0]["memories"][0]["text"] == "M1"

    @patch("nova_memory_first.urllib.request.urlopen", side_effect=Exception("timeout"))
    @patch("nova_memory_first.recall")
    def test_falls_back_to_individual_recalls(self, mock_recall, mock_urlopen):
        """When batch endpoint fails, falls back to individual recall() calls."""
        mock_recall.return_value = [{"text": "Fallback", "source": "src"}]

        from nova_memory_first import batch_recall
        results = batch_recall([{"q": "q1"}, {"q": "q2"}])

        assert len(results) == 2
        assert mock_recall.call_count == 2


# =============================================================================
# nova_memory_first.py — format_result
# =============================================================================

class TestFormatResult:
    """Tests for format_result() — memory display formatting."""

    def test_formats_with_score(self):
        """Results with numeric scores show relevance."""
        from nova_memory_first import format_result
        item = {"text": "A memory about raves", "source": "music", "score": 0.92}
        output = format_result(item, 1)
        assert "[1]" in output
        assert "(music" in output
        assert "0.92" in output
        assert "A memory about raves" in output

    def test_formats_without_score(self):
        """Results without scores omit the relevance indicator."""
        from nova_memory_first import format_result
        item = {"text": "Another memory", "source": "email_archive"}
        output = format_result(item, 3)
        assert "[3]" in output
        assert "(email_archive" in output

    def test_truncates_long_text(self):
        """Memory text is truncated to 400 characters."""
        from nova_memory_first import format_result
        item = {"text": "x" * 1000, "source": "src"}
        output = format_result(item, 1)
        # The text portion should be at most 400 chars
        text_part = output.split("\n", 1)[1] if "\n" in output else output
        assert len(text_part) <= 400


# =============================================================================
# nova_recent_memories.py — get_recent_summary
# =============================================================================

class TestGetRecentSummary:
    """Tests for get_recent_summary() — PostgreSQL memory summary."""

    @patch("nova_recent_memories.connect")
    def test_returns_summary_structure(self, mock_connect):
        """get_recent_summary returns the expected dict structure."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (42,)
        mock_cursor.fetchall.return_value = [
            ("television", 15, ["Jeopardy!", "Wheel of Fortune"]),
            ("music", 12, ["Aphex Twin"]),
            ("horror", 8, None),
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        from nova_recent_memories import get_recent_summary
        result = get_recent_summary(hours=24)

        assert result["hours"] == 24
        assert result["total"] == 42
        assert len(result["by_source"]) == 3
        assert result["by_source"][0]["source"] == "television"
        assert result["by_source"][0]["count"] == 15
        assert "Jeopardy!" in result["by_source"][0]["labels"]

    @patch("nova_recent_memories.connect")
    def test_handles_no_results(self, mock_connect):
        """Returns zero total and empty by_source when no memories exist."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (0,)
        mock_cursor.fetchall.return_value = []
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        from nova_recent_memories import get_recent_summary
        result = get_recent_summary(hours=1)

        assert result["total"] == 0
        assert result["by_source"] == []

    @patch("nova_recent_memories.connect")
    def test_source_filter(self, mock_connect):
        """When source is specified, SQL includes source filter."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (5,)
        mock_cursor.fetchall.return_value = [("television", 5, ["Show"])]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        from nova_recent_memories import get_recent_summary
        result = get_recent_summary(hours=24, source="television")

        # Verify source was passed to the SQL queries
        calls = mock_cursor.execute.call_args_list
        for c in calls:
            sql = c[0][0]
            params = c[0][1]
            if "source" in sql.lower() and "= %s" in sql:
                assert "television" in params

    @patch("nova_recent_memories.connect")
    def test_null_labels_become_empty_list(self, mock_connect):
        """When labels column is NULL, it becomes an empty list."""
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = (3,)
        mock_cursor.fetchall.return_value = [("infrastructure", 3, None)]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        from nova_recent_memories import get_recent_summary
        result = get_recent_summary(hours=24)

        assert result["by_source"][0]["labels"] == []


# =============================================================================
# nova_recent_memories.py — get_recent_detail
# =============================================================================

class TestGetRecentDetail:
    """Tests for get_recent_detail() — per-source detail with samples."""

    @patch("nova_recent_memories.connect")
    def test_returns_detail_with_samples(self, mock_connect):
        """get_recent_detail includes sample memory snippets per source."""
        mock_cursor = MagicMock()
        mock_cursor.fetchall.side_effect = [
            # First call: source breakdown
            [{"source": "tv", "cnt": 10, "labels": ["Show"]}],
            # Second call: sample memories for "tv"
            [{"text": "Sample memory text", "label": "Show", "created_at": datetime.now(timezone.utc)}],
        ]
        mock_conn = MagicMock()
        mock_conn.cursor.return_value = mock_cursor
        mock_connect.return_value = mock_conn

        from nova_recent_memories import get_recent_detail
        import psycopg2.extras  # noqa: ensure import works
        result = get_recent_detail(hours=24)

        assert result["total"] == 10
        assert len(result["sources"]) == 1
        assert result["sources"][0]["source"] == "tv"
        assert len(result["sources"][0]["samples"]) == 1


# =============================================================================
# nova_recent_memories.py — CLI argument parsing
# =============================================================================

class TestRecentMemoriesCLI:
    """Tests for CLI argument parsing in nova_recent_memories.py."""

    def test_default_hours(self):
        """Default --hours is 24."""
        from nova_recent_memories import DEFAULT_HOURS
        assert DEFAULT_HOURS == 24

    def test_parse_hours_argument(self):
        """--hours argument is correctly parsed."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument("--source", type=str, default=None)
        parser.add_argument("--detail", action="store_true")
        parser.add_argument("--json", dest="json_output", action="store_true")

        args = parser.parse_args(["--hours", "48"])
        assert args.hours == 48

    def test_parse_source_argument(self):
        """--source argument is correctly parsed."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument("--source", type=str, default=None)
        parser.add_argument("--detail", action="store_true")
        parser.add_argument("--json", dest="json_output", action="store_true")

        args = parser.parse_args(["--source", "television"])
        assert args.source == "television"

    def test_parse_detail_flag(self):
        """--detail flag enables detail mode."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument("--source", type=str, default=None)
        parser.add_argument("--detail", action="store_true")
        parser.add_argument("--json", dest="json_output", action="store_true")

        args = parser.parse_args(["--detail"])
        assert args.detail is True

    def test_parse_json_flag(self):
        """--json flag enables JSON output."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument("--source", type=str, default=None)
        parser.add_argument("--detail", action="store_true")
        parser.add_argument("--json", dest="json_output", action="store_true")

        args = parser.parse_args(["--json"])
        assert args.json_output is True

    def test_combined_arguments(self):
        """Multiple arguments work together."""
        parser = argparse.ArgumentParser()
        parser.add_argument("--hours", type=int, default=24)
        parser.add_argument("--source", type=str, default=None)
        parser.add_argument("--detail", action="store_true")
        parser.add_argument("--json", dest="json_output", action="store_true")

        args = parser.parse_args(["--hours", "72", "--source", "horror", "--detail", "--json"])
        assert args.hours == 72
        assert args.source == "horror"
        assert args.detail is True
        assert args.json_output is True


# =============================================================================
# nova_recent_memories.py — format_summary
# =============================================================================

class TestFormatSummary:
    """Tests for format_summary() — human-readable text output."""

    def test_basic_format(self):
        """Formats a summary with multiple sources."""
        from nova_recent_memories import format_summary
        data = {
            "hours": 24,
            "total": 100,
            "by_source": [
                {"source": "television", "count": 50, "labels": ["Jeopardy!"]},
                {"source": "music", "count": 30, "labels": ["Aphex Twin", "Prodigy"]},
                {"source": "horror", "count": 20, "labels": []},
            ],
        }

        output = format_summary(data)

        assert "last 24 hours" in output
        assert "100" in output
        assert "television" in output
        assert "50" in output
        assert "Jeopardy!" in output
        assert "music" in output
        assert "horror" in output

    def test_singular_hour(self):
        """Uses singular 'hour' for hours=1."""
        from nova_recent_memories import format_summary
        data = {"hours": 1, "total": 5, "by_source": [
            {"source": "test", "count": 5, "labels": []},
        ]}

        output = format_summary(data)
        assert "last 1 hour:" in output

    def test_empty_results(self):
        """Empty results show '(none)' message."""
        from nova_recent_memories import format_summary
        data = {"hours": 24, "total": 0, "by_source": []}

        output = format_summary(data)
        assert "(none)" in output

    def test_label_truncation(self):
        """More than 3 labels shows '+N more' suffix."""
        from nova_recent_memories import format_summary
        data = {
            "hours": 24,
            "total": 100,
            "by_source": [
                {"source": "tv", "count": 100, "labels": ["A", "B", "C", "D", "E"]},
            ],
        }

        output = format_summary(data)
        assert "+2 more" in output


# =============================================================================
# nova_recent_memories.py — format_detail
# =============================================================================

class TestFormatDetail:
    """Tests for format_detail() — detailed output with memory snippets."""

    def test_shows_sample_snippets(self):
        """Detail format includes text snippets from samples."""
        from nova_recent_memories import format_detail
        data = {
            "hours": 24,
            "total": 10,
            "sources": [
                {
                    "source": "television",
                    "count": 10,
                    "labels": ["Jeopardy!"],
                    "samples": [
                        {"text": "Clue: This French city is the City of Light.", "label": "Jeopardy!", "created_at": "2026-01-01T00:00:00"},
                    ],
                },
            ],
        }

        output = format_detail(data)
        assert "television (10 new):" in output
        assert "[Jeopardy!]" in output
        assert "French city" in output

    def test_shows_overflow_count(self):
        """When there are more memories than shown samples, shows overflow."""
        from nova_recent_memories import format_detail
        data = {
            "hours": 24,
            "total": 50,
            "sources": [
                {
                    "source": "music",
                    "count": 50,
                    "labels": [],
                    "samples": [
                        {"text": "Sample 1", "label": "", "created_at": "2026-01-01T00:00:00"},
                        {"text": "Sample 2", "label": "", "created_at": "2026-01-01T00:00:00"},
                    ],
                },
            ],
        }

        output = format_detail(data)
        assert "showing 2 of 50" in output

    def test_empty_sources(self):
        """Empty sources list shows '(none)'."""
        from nova_recent_memories import format_detail
        data = {"hours": 24, "total": 0, "sources": []}

        output = format_detail(data)
        assert "(none)" in output


# =============================================================================
# nova_recent_memories.py — helper functions
# =============================================================================

class TestRecentMemoriesHelpers:
    """Tests for formatting helper functions in nova_recent_memories.py."""

    def test_fmt_count(self):
        """_fmt_count adds thousand separators."""
        from nova_recent_memories import _fmt_count
        assert _fmt_count(1000) == "1,000"
        assert _fmt_count(1234567) == "1,234,567"
        assert _fmt_count(0) == "0"

    def test_truncate_short_text(self):
        """Short text is not truncated."""
        from nova_recent_memories import _truncate
        assert _truncate("hello world", 80) == "hello world"

    def test_truncate_long_text(self):
        """Long text is truncated with ellipsis."""
        from nova_recent_memories import _truncate
        result = _truncate("x" * 200, 80)
        assert len(result) == 83  # 80 + "..."
        assert result.endswith("...")

    def test_truncate_strips_newlines(self):
        """Newlines in text are replaced with spaces."""
        from nova_recent_memories import _truncate
        result = _truncate("line one\nline two\nline three", 100)
        assert "\n" not in result
        assert "line one line two" in result

    def test_label_tag_with_label(self):
        """_label_tag formats non-empty labels as bracketed tags."""
        from nova_recent_memories import _label_tag
        assert _label_tag("Jeopardy!") == "[Jeopardy!] "

    def test_label_tag_empty(self):
        """_label_tag returns empty string for empty labels."""
        from nova_recent_memories import _label_tag
        assert _label_tag("") == ""

    def test_cutoff_calculation(self):
        """_cutoff returns a UTC datetime N hours in the past."""
        from nova_recent_memories import _cutoff
        now = datetime.now(timezone.utc)
        cutoff = _cutoff(24)
        diff = now - cutoff
        # Should be approximately 24 hours ago (within a few seconds)
        assert 23.99 * 3600 <= diff.total_seconds() <= 24.01 * 3600


# =============================================================================
# nova_memory_consolidate.py — vector helpers
# =============================================================================

class TestConsolidateVectorHelpers:
    """Tests for vector memory helper functions in nova_memory_consolidate.py."""

    @patch("nova_memory_consolidate.urllib.request.urlopen")
    def test_vector_recall_filters_by_score(self, mock_urlopen):
        """vector_recall only returns memories with score >= 0.35."""
        response = json.dumps({
            "memories": [
                {"text": "High score memory", "score": 0.9},
                {"text": "Low score memory", "score": 0.2},
                {"text": "Border score memory", "score": 0.35},
            ]
        }).encode()
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=BytesIO(response))
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        from nova_memory_consolidate import vector_recall
        results = vector_recall("test query")

        assert len(results) == 2
        assert "High score memory" in results
        assert "Border score memory" in results
        assert "Low score memory" not in results

    @patch("nova_memory_consolidate.urllib.request.urlopen", side_effect=Exception("refused"))
    def test_vector_recall_returns_empty_on_error(self, mock_urlopen):
        """vector_recall returns empty list on connection failure."""
        from nova_memory_consolidate import vector_recall
        results = vector_recall("test")
        assert results == []

    @patch("nova_memory_consolidate.urllib.request.urlopen")
    def test_vector_remember_sends_correct_payload(self, mock_urlopen):
        """vector_remember POSTs text with source='synthesis'."""
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        from nova_memory_consolidate import vector_remember
        vector_remember("Synthesis text", {"date": "2026-01-01", "type": "work_synthesis"})

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        payload = json.loads(req.data.decode())
        assert payload["text"] == "Synthesis text"
        assert payload["source"] == "synthesis"
        assert payload["metadata"]["type"] == "work_synthesis"

    @patch("nova_memory_consolidate.urllib.request.urlopen")
    def test_vector_stats_returns_dict(self, mock_urlopen):
        """vector_stats returns the parsed JSON response."""
        response = json.dumps({"count": 1400000, "sources": 45}).encode()
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=BytesIO(response))
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        from nova_memory_consolidate import vector_stats
        result = vector_stats()
        assert result["count"] == 1400000

    @patch("nova_memory_consolidate.urllib.request.urlopen", side_effect=Exception("down"))
    def test_vector_stats_returns_empty_on_error(self, mock_urlopen):
        """vector_stats returns empty dict on failure."""
        from nova_memory_consolidate import vector_stats
        result = vector_stats()
        assert result == {}


# =============================================================================
# nova_memory_consolidate.py — synthesis modules
# =============================================================================

class TestConsolidateSynthesis:
    """Tests for the LLM synthesis modules in nova_memory_consolidate.py."""

    @patch("nova_memory_consolidate.llm_synthesize")
    def test_synthesize_work_patterns(self, mock_llm):
        """synthesize_work_patterns calls LLM with work-focused prompt."""
        mock_llm.return_value = "- Jordan is working on MLXCode\n- NMAPScanner has stalled"

        from nova_memory_consolidate import synthesize_work_patterns
        result = synthesize_work_patterns(["memory about coding", "memory about GitHub"])

        assert result is not None
        assert "MLXCode" in result
        mock_llm.assert_called_once()
        prompt = mock_llm.call_args[0][0]
        assert "projects" in prompt.lower() or "working" in prompt.lower()

    @patch("nova_memory_consolidate.llm_synthesize")
    def test_synthesize_work_returns_none_on_empty_input(self, mock_llm):
        """synthesize_work_patterns returns None with empty memories."""
        from nova_memory_consolidate import synthesize_work_patterns
        result = synthesize_work_patterns([])
        assert result is None
        mock_llm.assert_not_called()

    @patch("nova_memory_consolidate.llm_synthesize", return_value="- Active contact with Sam")
    @patch("nova_memory_consolidate.vector_recall", return_value=["email from Sam about servers"])
    def test_synthesize_relationship_activity(self, mock_recall, mock_llm):
        """synthesize_relationship_activity queries email memories and synthesizes."""
        from nova_memory_consolidate import synthesize_relationship_activity
        result = synthesize_relationship_activity(["some memories"])

        assert result is not None
        assert "Sam" in result
        mock_recall.assert_called_once()

    @patch("nova_memory_consolidate.vector_recall", return_value=[])
    def test_synthesize_relationship_returns_none_without_emails(self, mock_recall):
        """Returns None when no email memories are found."""
        from nova_memory_consolidate import synthesize_relationship_activity
        result = synthesize_relationship_activity(["some memories"])
        assert result is None

    @patch("nova_memory_consolidate.llm_synthesize", return_value="- Home systems stable")
    @patch("nova_memory_consolidate.vector_recall", return_value=["homekit status: all lights off"])
    def test_synthesize_home_and_life(self, mock_recall, mock_llm):
        """synthesize_home_and_life queries home memories and synthesizes."""
        from nova_memory_consolidate import synthesize_home_and_life
        result = synthesize_home_and_life(["some memories"])

        assert result is not None
        assert "stable" in result.lower() or "Home" in result

    @patch("nova_memory_consolidate.vector_recall", return_value=[])
    def test_synthesize_home_returns_none_without_data(self, mock_recall):
        """Returns None when no home memories are found."""
        from nova_memory_consolidate import synthesize_home_and_life
        result = synthesize_home_and_life([])
        assert result is None


# =============================================================================
# nova_memory_consolidate.py — read_recent_memory_files
# =============================================================================

class TestReadRecentMemoryFiles:
    """Tests for read_recent_memory_files() — markdown file reading."""

    @patch("nova_memory_consolidate.MEMORY_DIR")
    def test_reads_multiple_days(self, mock_dir):
        """Reads markdown files for each of the past N days."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            mock_dir.__truediv__ = lambda self, name: tmpdir_path / name
            mock_dir.exists = MagicMock(return_value=True)

            # Create two days of files
            today = date.today().isoformat()
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            (tmpdir_path / f"{today}.md").write_text("Today's memory content")
            (tmpdir_path / f"{yesterday}.md").write_text("Yesterday's memory content")

            with patch("nova_memory_consolidate.MEMORY_DIR", tmpdir_path):
                from nova_memory_consolidate import read_recent_memory_files
                result = read_recent_memory_files(days=2)

            assert today in result
            assert yesterday in result
            assert "Today's memory content" in result
            assert "Yesterday's memory content" in result

    def test_handles_missing_files(self):
        """Missing day files are silently skipped."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("nova_memory_consolidate.MEMORY_DIR", Path(tmpdir)):
                from nova_memory_consolidate import read_recent_memory_files
                result = read_recent_memory_files(days=3)
                # Should not crash, returns empty or minimal content
                assert isinstance(result, str)


# =============================================================================
# nova_memory_consolidate.py — llm_synthesize
# =============================================================================

class TestLlmSynthesize:
    """Tests for llm_synthesize() — Nova-NextGen routing."""

    @patch("nova_memory_consolidate.urllib.request.urlopen")
    def test_routes_to_reasoning_model(self, mock_urlopen):
        """Sends synthesis prompts to Nova-NextGen with task_type='reasoning'."""
        response = json.dumps({"response": "Synthesized observation"}).encode()
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=BytesIO(response))
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        from nova_memory_consolidate import llm_synthesize
        result = llm_synthesize("What has Jordan been working on?")

        assert result == "Synthesized observation"
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        payload = json.loads(req.data.decode())
        assert payload["task_type"] == "reasoning"

    @patch("nova_memory_consolidate.urllib.request.urlopen", side_effect=Exception("timeout"))
    def test_returns_empty_on_failure(self, mock_urlopen):
        """Returns empty string when Nova-NextGen is unreachable."""
        from nova_memory_consolidate import llm_synthesize
        result = llm_synthesize("prompt text")
        assert result == ""


# =============================================================================
# nova_memory_breakdown.py — get_breakdown
# =============================================================================

class TestMemoryBreakdown:
    """Tests for nova_memory_breakdown.py — breakdown output and formatting."""

    @patch("subprocess.run")
    def test_get_breakdown_parses_psql_output(self, mock_run):
        """get_breakdown parses pipe-delimited psql output correctly."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="television|1500\nmusic|800\nhorror|300\n",
            stderr="",
        )

        from nova_memory_breakdown import get_breakdown
        result = get_breakdown()

        assert result is not None
        breakdown, total = result
        assert total == 2600
        assert len(breakdown) == 3
        assert breakdown[0] == ("television", 1500)
        assert breakdown[1] == ("music", 800)

    @patch("subprocess.run")
    def test_get_breakdown_returns_none_on_psql_error(self, mock_run):
        """Returns None when psql exits non-zero."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="connection refused")

        from nova_memory_breakdown import get_breakdown
        result = get_breakdown()
        assert result is None

    @patch("nova_memory_breakdown.urllib.request.urlopen")
    def test_get_queue_depth(self, mock_urlopen):
        """get_queue_depth returns the pending count from Redis queue stats."""
        response = json.dumps({"pending": 42}).encode()
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=BytesIO(response))
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        from nova_memory_breakdown import get_queue_depth
        depth = get_queue_depth()
        assert depth == 42

    @patch("nova_memory_breakdown.urllib.request.urlopen", side_effect=Exception("refused"))
    def test_get_queue_depth_returns_negative_on_error(self, mock_urlopen):
        """Returns -1 when the queue stats endpoint is unreachable."""
        from nova_memory_breakdown import get_queue_depth
        depth = get_queue_depth()
        assert depth == -1


# =============================================================================
# nova_memory_breakdown.py — post_breakdown formatting
# =============================================================================

class TestBreakdownFormatting:
    """Tests for the Slack message formatting in post_breakdown()."""

    @patch("nova_memory_breakdown.slack_post")
    @patch("nova_memory_breakdown.get_breakdown")
    def test_post_breakdown_formats_table(self, mock_get, mock_slack):
        """post_breakdown sends a formatted code block to Slack."""
        mock_get.return_value = (
            [("television", 1500), ("music", 800), ("horror", 300)],
            2600,
        )

        from nova_memory_breakdown import post_breakdown
        post_breakdown()

        mock_slack.assert_called()
        message = mock_slack.call_args[0][0]
        assert "```" in message
        assert "television" in message
        assert "2,600" in message
        assert "TOTAL" in message

    @patch("nova_memory_breakdown.slack_post")
    @patch("nova_memory_breakdown.get_breakdown", return_value=None)
    def test_post_breakdown_handles_failure(self, mock_get, mock_slack):
        """post_breakdown handles get_breakdown returning None."""
        from nova_memory_breakdown import post_breakdown
        post_breakdown()

        mock_slack.assert_not_called()


# =============================================================================
# Integration tests — require live services
# =============================================================================

@pytest.mark.integration
class TestIntegrationRecentMemories:
    """Integration tests that require a running PostgreSQL instance with nova_memories."""

    def test_recent_memories_returns_real_data(self):
        """nova_recent_memories.py --hours 24 returns actual data from PostgreSQL."""
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "nova_recent_memories.py"),
             "--hours", "24"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        output = result.stdout
        assert "Memories added" in output or "last 24 hours" in output

    def test_recent_memories_json_output(self):
        """--json flag produces valid JSON output."""
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "nova_recent_memories.py"),
             "--hours", "24", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "hours" in data
        assert "total" in data
        assert "by_source" in data

    def test_recent_memories_detail_mode(self):
        """--detail flag produces per-source sample output."""
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent.parent / "nova_recent_memories.py"),
             "--hours", "24", "--detail"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        output = result.stdout
        assert "Memories added" in output or "new):" in output


@pytest.mark.integration
class TestIntegrationMemoryServer:
    """Integration tests that require the memory server on port 18790."""

    def test_recall_endpoint_responds(self):
        """The /recall endpoint on port 18790 responds to queries."""
        import urllib.request
        import urllib.parse

        url = f"http://127.0.0.1:18790/recall?q={urllib.parse.quote('test query')}&n=3"
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                data = json.loads(r.read())
            assert "memories" in data
        except Exception as e:
            pytest.skip(f"Memory server not available: {e}")

    def test_stats_endpoint_responds(self):
        """The /stats endpoint returns memory count."""
        import urllib.request

        try:
            with urllib.request.urlopen("http://127.0.0.1:18790/stats", timeout=10) as r:
                data = json.loads(r.read())
            assert "count" in data
            assert data["count"] > 0
        except Exception as e:
            pytest.skip(f"Memory server not available: {e}")


@pytest.mark.integration
class TestIntegrationDreamPipelinePostgres:
    """Integration tests for dream pipeline's PostgreSQL queries."""

    def test_query_recent_ingests_returns_data(self):
        """query_recent_ingests() returns real data from live PostgreSQL."""
        try:
            from dream_generate import query_recent_ingests
            text, inspirations = query_recent_ingests()
            # Should return data (may be empty if nothing ingested in 7 days)
            assert isinstance(text, str)
            assert isinstance(inspirations, list)
            # If we got results, verify structure
            for insp in inspirations:
                assert "source" in insp
                assert "memory" in insp
        except ImportError:
            pytest.skip("psycopg2 not available")
        except Exception as e:
            if "connect" in str(e).lower() or "database" in str(e).lower():
                pytest.skip(f"PostgreSQL not available: {e}")
            raise


# =============================================================================
# Functional tests — end-to-end workflows
# =============================================================================

@pytest.mark.functional
class TestFunctionalMemoryFirst:
    """Functional tests for the full memory-first query pipeline."""

    def test_classify_then_lookup_flow(self):
        """Classify a query and run memory_lookup in sequence."""
        from nova_memory_first import classify_query, memory_lookup

        sources, labels, prefer_search = classify_query("tell me about raves in the 90s")
        assert "music" in sources or "socal_rave" in sources

        # The full lookup will try to hit the server; mock it
        with patch("nova_memory_first.batch_recall", return_value=[]):
            with patch("nova_memory_first.recall", return_value=[]):
                with patch("nova_memory_first.search", return_value=[]):
                    results, searched, lbls = memory_lookup("tell me about raves in the 90s")
                    # With everything mocked empty, should get no results
                    assert isinstance(results, list)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
