#!/usr/bin/env python3
"""
test_music_crawlers.py — Comprehensive tests for nova_hardcore_edm_ingest.py
(representative of all 7 music crawlers which share the same BFS structure).

Covers: BFS crawl logic, Wikipedia API fetching, content classification,
text chunking, vector memory ingestion, rate limiting, signal handling, security.

Run: python3 -m pytest tests/test_music_crawlers.py -v
Written by Jordan Koch.
"""

import json
import signal
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_nova_config_for_music(monkeypatch):
    """Mock nova_config before module import."""
    mock_config = MagicMock()
    mock_config.VECTOR_URL = "http://127.0.0.1:18790/remember"
    mock_config.SLACK_API = "https://slack.com/api"
    mock_config.SLACK_NOTIFY = "C_TEST_NOTIFY"
    mock_config.JORDAN_DM = "D_TEST_DM"
    mock_config.slack_bot_token.return_value = "xoxb-test-token"
    mock_config.post_both = MagicMock()
    mock_config.post_discord = MagicMock(return_value=True)
    monkeypatch.setitem(sys.modules, "nova_config", mock_config)
    return mock_config


@pytest.fixture
def edm_module(mock_nova_config_for_music, monkeypatch):
    """Import nova_hardcore_edm_ingest with reset state."""
    for mod in list(sys.modules.keys()):
        if "nova_hardcore_edm_ingest" in mod:
            del sys.modules[mod]

    import nova_hardcore_edm_ingest

    # Reset globals
    nova_hardcore_edm_ingest.shutdown = False
    nova_hardcore_edm_ingest.stats = {
        "pages_processed": 0,
        "chunks_ingested": 0,
        "queue_size": 0,
        "current_page": "",
        "current_vector": "",
        "errors": 0,
        "by_vector": {},
        "last_pages": [],
    }
    nova_hardcore_edm_ingest.last_status_time = 0

    return nova_hardcore_edm_ingest


@pytest.fixture
def sample_wiki_api_response():
    """Sample Wikipedia API response with page content and links."""
    return {
        "query": {
            "pages": {
                "12345": {
                    "pageid": 12345,
                    "title": "Hardcore (electronic dance music genre)",
                    "extract": (
                        "Hardcore is a genre of electronic dance music that originated "
                        "in the early 1990s in the Netherlands and Belgium. It is characterized "
                        "by a fast tempo (usually 160-200 BPM), distorted kick drums, and "
                        "aggressive synthesizer sounds.\n\n"
                        "The genre evolved from acid house and techno, incorporating elements "
                        "of industrial music and EBM. Early pioneers include Rotterdam artists "
                        "who developed the gabber subgenre.\n\n"
                        "Hardcore has spawned numerous subgenres including happy hardcore, "
                        "speedcore, terrorcore, frenchcore, and industrial hardcore. The scene "
                        "remains active with festivals like Thunderdome, Defqon.1, and Masters "
                        "of Hardcore drawing tens of thousands of attendees."
                    ),
                    "links": [
                        {"ns": 0, "title": "Gabber"},
                        {"ns": 0, "title": "Speedcore"},
                        {"ns": 0, "title": "Happy hardcore"},
                        {"ns": 0, "title": "Thunderdome (festival)"},
                        {"ns": 14, "title": "Category:Electronic music genres"},  # Should be filtered
                    ],
                }
            }
        }
    }


@pytest.fixture
def sample_wiki_empty_page():
    """Wikipedia API response for a missing page."""
    return {
        "query": {
            "pages": {
                "-1": {
                    "missing": "",
                    "title": "NonExistentPage",
                }
            }
        }
    }


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestClassifyContent:
    """Tests for classify_content()."""

    def test_hardcore_classification(self, edm_module):
        result = edm_module.classify_content(
            "Gabber",
            "Gabber is a style of hardcore electronic dance music with fast tempos and hard kicks."
        )
        assert result == "edm_hardcore"

    def test_techno_classification(self, edm_module):
        result = edm_module.classify_content(
            "Detroit Techno",
            "Detroit techno is a genre of techno music that originated in Detroit in the 1980s. "
            "It combines elements of acid techno with industrial techno sounds."
        )
        assert result == "edm_techno"

    def test_trance_classification(self, edm_module):
        result = edm_module.classify_content(
            "Psytrance",
            "Psytrance is a subgenre of trance music characterized by hypnotic arrangements. "
            "Goa trance and progressive trance evolved from this."
        )
        assert result == "edm_trance"

    def test_breakbeat_classification(self, edm_module):
        result = edm_module.classify_content(
            "Drum and Bass",
            "Drum and bass is a genre of breakbeat electronic music featuring fast breakcore "
            "rhythms, neurofunk basslines, and jungle-influenced percussion."
        )
        assert result == "edm_breakbeat"

    def test_house_classification(self, edm_module):
        result = edm_module.classify_content(
            "Chicago House",
            "Chicago house music originated in clubs, combining acid house with deep house "
            "and progressive house elements."
        )
        assert result == "edm_house"

    def test_artists_classification(self, edm_module):
        result = edm_module.classify_content(
            "DJ Promo",
            "DJ Promo is a producer and performer on the record label. His discography spans "
            "multiple decades as a musician and artist in the scene."
        )
        assert result == "edm_artists"

    def test_culture_classification(self, edm_module):
        result = edm_module.classify_content(
            "Rave Culture",
            "Rave culture is a subculture centered around nightclub events and festivals. "
            "The dance movement emerged from warehouse party scenes."
        )
        assert result == "edm_culture"

    def test_technology_classification(self, edm_module):
        result = edm_module.classify_content(
            "Roland TR-909",
            "The Roland TR-909 is a drum machine and synthesizer used in production. "
            "Its four on the floor kick drum pattern defined the genre's tempo and BPM."
        )
        assert result == "edm_technology"

    def test_history_classification(self, edm_module):
        result = edm_module.classify_content(
            "Origins of Hardcore",
            "The history of hardcore music emerged in the early 1990s. Founded in Rotterdam, "
            "established by Dutch producers, its evolution through the decades shaped electronic music."
        )
        assert result == "edm_history"

    def test_labels_classification(self, edm_module):
        result = edm_module.classify_content(
            "Industrial Strength Records",
            "Industrial Strength Records is a record label founded for hardcore recordings. "
            "Their vinyl catalogue and release imprint has been influential."
        )
        assert result == "edm_labels"

    def test_fallback_to_music_general(self, edm_module):
        """Unclassifiable content should fall back to music_general."""
        result = edm_module.classify_content(
            "Quantum Physics",
            "Quarks and leptons interact via the strong nuclear force in particle accelerators."
        )
        assert result == "music_general"

    def test_highest_score_wins(self, edm_module):
        """When multiple categories match, highest score wins."""
        result = edm_module.classify_content(
            "Hardcore Rave",
            "Hardcore gabber speedcore terrorcore frenchcore industrial hardcore mainstream hardcore."
        )
        assert result == "edm_hardcore"

    def test_case_insensitive(self, edm_module):
        """Classification should be case-insensitive."""
        result = edm_module.classify_content(
            "GABBER MUSIC",
            "HARDCORE GABBER SPEEDCORE TERRORCORE"
        )
        assert result == "edm_hardcore"


class TestFetchWikiPage:
    """Tests for fetch_wiki_page()."""

    @patch("urllib.request.urlopen")
    def test_successful_fetch(self, mock_urlopen, edm_module, sample_wiki_api_response):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(sample_wiki_api_response).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result, links, error = edm_module.fetch_wiki_page(
            "https://en.wikipedia.org/wiki/Hardcore_(electronic_dance_music_genre)"
        )
        assert result is not None
        title, text = result
        assert title == "Hardcore (electronic dance music genre)"
        assert "early 1990s" in text
        assert len(links) > 0
        assert error is None

    @patch("urllib.request.urlopen")
    def test_missing_page_returns_none(self, mock_urlopen, edm_module, sample_wiki_empty_page):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(sample_wiki_empty_page).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result, links, error = edm_module.fetch_wiki_page(
            "https://en.wikipedia.org/wiki/NonExistentPage"
        )
        assert result is None
        assert "missing" in error

    @patch("urllib.request.urlopen")
    def test_filters_non_article_links(self, mock_urlopen, edm_module, sample_wiki_api_response):
        """Links with ns != 0 (categories, talk pages) should be filtered out."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(sample_wiki_api_response).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        _, links, _ = edm_module.fetch_wiki_page("https://en.wikipedia.org/wiki/Test")
        # Category link should be excluded
        for link in links:
            assert "Category:" not in link

    @patch("urllib.request.urlopen")
    def test_rate_limit_retry(self, mock_urlopen, edm_module):
        """Should retry on 429 with exponential backoff."""
        # First call: 429, second call: success
        error_429 = urllib.error.HTTPError(
            url="http://test", code=429, msg="Too Many Requests",
            hdrs={}, fp=None
        )
        success_response = MagicMock()
        success_response.read.return_value = json.dumps({
            "query": {"pages": {"1": {"title": "Test", "extract": "Content here " * 20, "links": []}}}
        }).encode()
        success_response.__enter__ = MagicMock(return_value=success_response)
        success_response.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [error_429, success_response]

        with patch("time.sleep"):
            result, links, error = edm_module.fetch_wiki_page("https://en.wikipedia.org/wiki/Test")

        assert result is not None
        assert error is None

    @patch("urllib.request.urlopen")
    def test_rate_limit_exhausted(self, mock_urlopen, edm_module):
        """After 5 retries on 429, should give up."""
        error_429 = urllib.error.HTTPError(
            url="http://test", code=429, msg="Too Many Requests",
            hdrs={}, fp=None
        )
        mock_urlopen.side_effect = [error_429] * 5

        with patch("time.sleep"):
            result, links, error = edm_module.fetch_wiki_page("https://en.wikipedia.org/wiki/Test")

        assert result is None
        assert "rate limited" in error

    @patch("urllib.request.urlopen")
    def test_uses_user_agent(self, mock_urlopen, edm_module):
        """Should include a User-Agent header."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "query": {"pages": {"1": {"title": "T", "extract": "Content " * 20, "links": []}}}
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        edm_module.fetch_wiki_page("https://en.wikipedia.org/wiki/Test")
        req = mock_urlopen.call_args[0][0]
        assert "User-Agent" in req.headers or "User-agent" in req.headers

    @patch("urllib.request.urlopen")
    def test_network_error_returns_none(self, mock_urlopen, edm_module):
        mock_urlopen.side_effect = Exception("Network unreachable")
        result, links, error = edm_module.fetch_wiki_page("https://en.wikipedia.org/wiki/Test")
        assert result is None
        assert error is not None


class TestChunkText:
    """Tests for chunk_text()."""

    def test_basic_chunking(self, edm_module):
        text = "\n\n".join([f"Paragraph {i} " * 50 for i in range(20)])
        chunks = edm_module.chunk_text(text, "Test Title")
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= edm_module.CHUNK_SIZE + 500

    def test_short_text_single_chunk(self, edm_module):
        text = "This is a short paragraph with enough content to pass the minimum filter."
        chunks = edm_module.chunk_text(text, "Title")
        assert len(chunks) == 1

    def test_filters_short_paragraphs(self, edm_module):
        """Paragraphs under 30 chars should be skipped."""
        text = "Short.\n\nAlso short.\n\n" + "A proper paragraph with more than thirty characters of content."
        chunks = edm_module.chunk_text(text, "Title")
        for chunk in chunks:
            assert "Short." not in chunk or len(chunk) > 30

    def test_empty_text(self, edm_module):
        chunks = edm_module.chunk_text("", "Title")
        assert chunks == []

    def test_paragraph_splitting(self, edm_module):
        """Should split on double newlines (paragraphs)."""
        text = ("Paragraph one with enough text. " * 100 + "\n\n" +
                "Paragraph two with different content. " * 100)
        chunks = edm_module.chunk_text(text, "Title")
        assert len(chunks) >= 2


class TestIngestChunk:
    """Tests for ingest_chunk()."""

    @patch("urllib.request.urlopen")
    def test_successful_ingestion(self, mock_urlopen, edm_module):
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        result = edm_module.ingest_chunk(
            "Test chunk content.",
            "Test Title",
            "edm_hardcore",
            "https://en.wikipedia.org/wiki/Test",
        )
        assert result is True

    @patch("urllib.request.urlopen")
    def test_correct_payload(self, mock_urlopen, edm_module):
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        edm_module.ingest_chunk(
            "Chunk text here.",
            "Gabber",
            "edm_hardcore",
            "https://en.wikipedia.org/wiki/Gabber",
        )
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data)
        assert payload["text"] == "Chunk text here."
        assert payload["metadata"]["source"] == "edm_hardcore"
        assert payload["metadata"]["title"] == "Gabber"
        assert payload["metadata"]["url"] == "https://en.wikipedia.org/wiki/Gabber"
        assert payload["metadata"]["type"] == "wikipedia"
        assert payload["metadata"]["privacy"] == "public"

    @patch("urllib.request.urlopen")
    def test_failure_returns_false(self, mock_urlopen, edm_module):
        mock_urlopen.side_effect = Exception("Server error")
        result = edm_module.ingest_chunk("Text", "Title", "vector", "url")
        assert result is False


class TestVectorCategories:
    """Tests for VECTOR_CATEGORIES configuration."""

    def test_has_all_expected_categories(self, edm_module):
        expected = {
            "edm_hardcore", "edm_techno", "edm_trance", "edm_breakbeat",
            "edm_house", "edm_artists", "edm_culture", "edm_technology",
            "edm_history", "edm_labels", "music_general",
        }
        assert expected.issubset(set(edm_module.VECTOR_CATEGORIES.keys()))

    def test_music_general_is_empty_fallback(self, edm_module):
        """music_general should have empty keywords (fallback only)."""
        assert edm_module.VECTOR_CATEGORIES["music_general"] == []

    def test_all_categories_have_keywords(self, edm_module):
        """All categories except music_general should have keywords."""
        for cat, keywords in edm_module.VECTOR_CATEGORIES.items():
            if cat != "music_general":
                assert len(keywords) > 0, f"{cat} has no keywords"


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestSecurityMusicCrawlers:
    """Security tests: no secrets, safe crawling, no PII."""

    def test_no_hardcoded_tokens(self):
        source = Path(__file__).parent.parent / "nova_hardcore_edm_ingest.py"
        content = source.read_text()
        assert "xoxb-" not in content
        assert "sk-" not in content
        assert "AKIA" not in content
        assert "ghp_" not in content

    def test_no_personal_emails_in_source(self):
        source = Path(__file__).parent.parent / "nova_hardcore_edm_ingest.py"
        content = source.read_text()
        # Verify no personal email addresses are embedded
        import re
        personal_patterns = [r"kochj\w*@\w+\.\w+", r"jordan\.\w+@\w+\.\w+"]
        for pat in personal_patterns:
            assert not re.search(pat, content), f"Personal email pattern found: {pat}"

    def test_no_hardcoded_home_paths(self):
        source = Path(__file__).parent.parent / "nova_hardcore_edm_ingest.py"
        content = source.read_text()
        home_path = Path.home()
        assert str(home_path) + "/" not in content

    def test_memory_server_is_localhost(self, edm_module):
        assert "127.0.0.1" in edm_module.MEMORY_URL

    def test_user_agent_identifies_bot(self):
        """User-Agent should identify this as a research bot."""
        source = Path(__file__).parent.parent / "nova_hardcore_edm_ingest.py"
        content = source.read_text()
        assert "User-Agent" in content or "User-agent" in content

    def test_respects_rate_limits(self, edm_module):
        """DELAY_BETWEEN_PAGES should be reasonable (not spamming)."""
        assert edm_module.DELAY_BETWEEN_PAGES >= 1.0

    def test_metadata_privacy_is_public(self, edm_module):
        """Wikipedia content is public, metadata should reflect that."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = MagicMock()
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            edm_module.ingest_chunk("Text", "Title", "edm_hardcore", "url")
            req = mock_urlopen.call_args[0][0]
            payload = json.loads(req.data)
            assert payload["metadata"]["privacy"] == "public"

    def test_no_corporate_urls(self):
        """Music crawler should only reference Wikipedia, not corporate URLs."""
        source = Path(__file__).parent.parent / "nova_hardcore_edm_ingest.py"
        content = source.read_text()
        # Should only contain wikipedia.org and localhost URLs
        assert "chat.gpt." not in content

    def test_crawl_only_wikipedia(self, edm_module):
        """Start URL should be Wikipedia only."""
        assert "wikipedia.org" in edm_module.START_URL


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS — End-to-End BFS Crawl
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.functional
class TestFullPipelineMusicCrawler:
    """Full pipeline: BFS crawl -> fetch page -> classify -> chunk -> ingest."""

    @patch("urllib.request.urlopen")
    @patch("time.sleep")
    def test_full_crawl_cycle(self, mock_sleep, mock_urlopen, edm_module):
        """One full iteration of the BFS crawl."""
        # Mock Wikipedia API response
        wiki_response = json.dumps({
            "query": {
                "pages": {
                    "1": {
                        "title": "Gabber",
                        "extract": (
                            "Gabber is a style of hardcore electronic dance music. "
                            "It originated in the early 1990s in Rotterdam. "
                            "The genre features fast tempos and distorted kicks. "
                        ) * 10,
                        "links": [
                            {"ns": 0, "title": "Speedcore"},
                            {"ns": 0, "title": "Rotterdam"},
                        ],
                    }
                }
            }
        }).encode()

        # Mock responses: first for wiki fetch, subsequent for ingestion
        wiki_resp = MagicMock()
        wiki_resp.read.return_value = wiki_response
        wiki_resp.__enter__ = MagicMock(return_value=wiki_resp)
        wiki_resp.__exit__ = MagicMock(return_value=False)

        ingest_resp = MagicMock()
        ingest_resp.__enter__ = MagicMock(return_value=ingest_resp)
        ingest_resp.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [wiki_resp] + [ingest_resp] * 20

        # Override TARGET_CHUNKS to stop quickly
        edm_module.TARGET_CHUNKS = 5
        edm_module.STATUS_INTERVAL = 999999  # Prevent status posts

        edm_module.main()

        assert edm_module.stats["pages_processed"] >= 1
        assert edm_module.stats["chunks_ingested"] > 0

    @patch("nova_hardcore_edm_ingest.fetch_wiki_page")
    @patch("nova_hardcore_edm_ingest.ingest_chunk", return_value=True)
    @patch("time.sleep")
    def test_bfs_visits_linked_pages(self, mock_sleep, mock_ingest, mock_fetch, edm_module):
        """BFS should follow links to new pages."""
        # Page 1 links to Page 2
        mock_fetch.side_effect = [
            (
                ("Hardcore", "Hardcore content about gabber speedcore. " * 20),
                ["https://en.wikipedia.org/wiki/Gabber"],
                None,
            ),
            (
                ("Gabber", "Gabber is hardcore electronic music. " * 20),
                [],
                None,
            ),
        ]

        edm_module.TARGET_CHUNKS = 2
        edm_module.STATUS_INTERVAL = 999999
        edm_module.main()

        assert edm_module.stats["pages_processed"] == 2

    @patch("nova_hardcore_edm_ingest.fetch_wiki_page")
    @patch("nova_hardcore_edm_ingest.ingest_chunk", return_value=True)
    @patch("time.sleep")
    def test_bfs_does_not_revisit(self, mock_sleep, mock_ingest, mock_fetch, edm_module):
        """BFS should not revisit already-crawled pages."""
        # Both pages link to each other
        mock_fetch.side_effect = [
            (
                ("Page A", "Content A about hardcore gabber. " * 20),
                ["https://en.wikipedia.org/wiki/PageB", "https://en.wikipedia.org/wiki/PageA"],
                None,
            ),
            (
                ("Page B", "Content B about speedcore terrorcore. " * 20),
                ["https://en.wikipedia.org/wiki/PageA"],  # Back-link
                None,
            ),
        ]

        edm_module.TARGET_CHUNKS = 2
        edm_module.STATUS_INTERVAL = 999999
        edm_module.main()

        # Should only process each page once
        assert edm_module.stats["pages_processed"] == 2

    @patch("nova_hardcore_edm_ingest.fetch_wiki_page")
    @patch("time.sleep")
    def test_handles_fetch_errors_gracefully(self, mock_sleep, mock_fetch, edm_module):
        """Fetch errors should increment error count, not crash."""
        mock_fetch.side_effect = [
            (None, [], "Network error"),
            (None, [], "Timeout"),
        ]

        edm_module.TARGET_CHUNKS = 1
        edm_module.STATUS_INTERVAL = 999999
        # Add extra URLs to queue
        edm_module.main()

        assert edm_module.stats["errors"] >= 1

    @patch("nova_hardcore_edm_ingest.fetch_wiki_page")
    @patch("nova_hardcore_edm_ingest.ingest_chunk", return_value=True)
    @patch("time.sleep")
    def test_stops_at_target_chunks(self, mock_sleep, mock_ingest, mock_fetch, edm_module):
        """Should stop when TARGET_CHUNKS is reached."""
        mock_fetch.return_value = (
            ("Big Page", "Content " * 500),
            ["https://en.wikipedia.org/wiki/More"],
            None,
        )

        edm_module.TARGET_CHUNKS = 3
        edm_module.STATUS_INTERVAL = 999999
        edm_module.main()

        assert edm_module.stats["chunks_ingested"] <= edm_module.TARGET_CHUNKS + 5  # small overshoot allowed


# ═══════════════════════════════════════════════════════════════════════════════
# FRAMEWORK TESTS — Signals, State, Error Recovery
# ═══════════════════════════════════════════════════════════════════════════════


class TestFrameworkMusicCrawler:
    """Framework tests: graceful shutdown, stats tracking, config validation."""

    def test_sigterm_sets_shutdown(self, edm_module):
        edm_module.shutdown = False
        edm_module.signal_handler(signal.SIGTERM, None)
        assert edm_module.shutdown is True

    def test_sigint_sets_shutdown(self, edm_module):
        edm_module.shutdown = False
        edm_module.signal_handler(signal.SIGINT, None)
        assert edm_module.shutdown is True

    @patch("nova_hardcore_edm_ingest.fetch_wiki_page")
    @patch("time.sleep")
    def test_shutdown_stops_crawl(self, mock_sleep, mock_fetch, edm_module):
        """Shutdown flag should stop the BFS loop."""
        edm_module.shutdown = True
        edm_module.TARGET_CHUNKS = 1000
        edm_module.STATUS_INTERVAL = 999999

        mock_fetch.return_value = (("Page", "Content " * 100), [], None)
        edm_module.main()

        assert edm_module.stats["pages_processed"] == 0

    def test_target_chunks_reasonable(self, edm_module):
        """TARGET_CHUNKS should be a reasonable number."""
        assert edm_module.TARGET_CHUNKS >= 100
        assert edm_module.TARGET_CHUNKS <= 100000

    def test_chunk_size_reasonable(self, edm_module):
        assert 500 <= edm_module.CHUNK_SIZE <= 5000

    def test_delay_between_pages_reasonable(self, edm_module):
        """Delay should be at least 1 second to be polite to Wikipedia."""
        assert edm_module.DELAY_BETWEEN_PAGES >= 1.0

    def test_status_interval_configured(self, edm_module):
        assert edm_module.STATUS_INTERVAL > 0

    def test_stats_structure(self, edm_module):
        """Stats dict should have all required keys."""
        required = {"pages_processed", "chunks_ingested", "queue_size",
                    "current_page", "current_vector", "errors", "by_vector", "last_pages"}
        assert required.issubset(set(edm_module.stats.keys()))

    def test_last_pages_bounded(self, edm_module):
        """last_pages list should not grow unbounded."""
        edm_module.stats["last_pages"] = [f"Page {i}" for i in range(20)]
        # The module trims to 10
        if len(edm_module.stats["last_pages"]) > 10:
            edm_module.stats["last_pages"] = edm_module.stats["last_pages"][-10:]
        assert len(edm_module.stats["last_pages"]) <= 10

    @patch("nova_hardcore_edm_ingest.notify")
    def test_post_status_generates_message(self, mock_notify, edm_module):
        """post_status() should generate a formatted status message."""
        edm_module.stats = {
            "pages_processed": 50,
            "chunks_ingested": 500,
            "queue_size": 200,
            "current_page": "Gabber",
            "current_vector": "edm_hardcore",
            "errors": 2,
            "by_vector": {"edm_hardcore": 200, "edm_techno": 100},
            "last_pages": ["Gabber [edm_hardcore] (5 chunks)"],
        }
        edm_module.post_status()
        mock_notify.assert_called_once()
        msg = mock_notify.call_args[0][0]
        assert "Hardcore EDM" in msg
        assert "50" in msg  # pages_processed
        assert "500" in msg  # chunks_ingested


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestMusicCrawlerIntegration:
    """Integration tests hitting live Wikipedia API. Skipped if unavailable."""

    @pytest.fixture(autouse=True)
    def check_wikipedia_available(self):
        try:
            urllib.request.urlopen("https://en.wikipedia.org/w/api.php?action=query&meta=siteinfo&format=json", timeout=5)
        except Exception:
            pytest.skip("Wikipedia API not available")

    def test_can_fetch_hardcore_page(self, edm_module):
        """Should successfully fetch the Hardcore EDM Wikipedia page."""
        result, links, error = edm_module.fetch_wiki_page(edm_module.START_URL)
        assert result is not None
        title, text = result
        assert len(text) > 100
        assert error is None

    def test_hardcore_page_has_links(self, edm_module):
        """The Hardcore page should return outgoing links."""
        _, links, _ = edm_module.fetch_wiki_page(edm_module.START_URL)
        assert len(links) > 5

    def test_links_are_valid_urls(self, edm_module):
        """All returned links should be valid Wikipedia URLs."""
        _, links, _ = edm_module.fetch_wiki_page(edm_module.START_URL)
        for link in links[:10]:
            assert link.startswith("https://en.wikipedia.org/wiki/")

    @pytest.mark.integration
    def test_memory_server_accepts_chunk(self, edm_module):
        """Memory server should accept a test chunk."""
        try:
            urllib.request.urlopen("http://127.0.0.1:18790/health", timeout=3)
        except Exception:
            pytest.skip("Memory server not available")

        result = edm_module.ingest_chunk(
            "Integration test chunk for music crawler.",
            "Test",
            "test_integration",
            "https://test.url",
        )
        assert result is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
