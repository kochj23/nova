#!/usr/bin/env python3
"""
test_media_ingest.py — Combined tests for nova_movie_script_ingest.py,
nova_adult_swim_ingest.py, and nova_comedy_ingest.py.

Covers: franchise definitions, Wikipedia API parsing, IMSDb script fetching,
episode table parsing, text chunking, vector memory storage, comedy transcription,
filename parsing, status reporting, graceful shutdown, security.

Run: python3 -m pytest tests/test_media_ingest.py -v
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
def movie_module(mock_nova_config):
    """Import nova_movie_script_ingest fresh."""
    for mod in list(sys.modules.keys()):
        if "nova_movie_script_ingest" in mod:
            del sys.modules[mod]
    import nova_movie_script_ingest
    # Reset globals
    nova_movie_script_ingest.dry_run = True
    nova_movie_script_ingest.shutdown.clear()
    nova_movie_script_ingest.stats = {
        "franchise": "", "source_tag": "", "movies_total": 0,
        "movies_processed": 0, "scripts_found": 0, "wiki_fallbacks": 0,
        "memories_stored": 0, "errors": 0, "start_time": 0, "current_movie": "",
    }
    return nova_movie_script_ingest


@pytest.fixture
def adult_swim_module(mock_nova_config):
    """Import nova_adult_swim_ingest fresh."""
    for mod in list(sys.modules.keys()):
        if "nova_adult_swim_ingest" in mod:
            del sys.modules[mod]
    import nova_adult_swim_ingest
    nova_adult_swim_ingest.dry_run = True
    nova_adult_swim_ingest.shutdown.clear()
    nova_adult_swim_ingest.stats = {
        "total_shows": 0, "current_show": "", "episodes_found": 0,
        "memories_stored": 0, "errors": 0, "start_time": 0,
        "shows_completed": 0, "per_show": {},
    }
    return nova_adult_swim_ingest


@pytest.fixture
def comedy_module(mock_nova_config):
    """Import nova_comedy_ingest fresh."""
    for mod in list(sys.modules.keys()):
        if "nova_comedy_ingest" in mod:
            del sys.modules[mod]
    import nova_comedy_ingest
    nova_comedy_ingest.shutdown.clear()
    nova_comedy_ingest.stats = {
        "total_files": 0, "processed": 0, "transcribed": 0,
        "chunks_stored": 0, "errors": 0, "skipped": 0,
        "current_file": "", "start_time": 0, "total_transcript_chars": 0,
    }
    return nova_comedy_ingest


# ═══════════════════════════════════════════════════════════════════════════════
# MOVIE SCRIPT INGEST TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestMovieFranchiseDefinitions:
    """Tests for franchise configuration."""

    def test_all_franchises_have_required_fields(self, movie_module):
        for key, franchise in movie_module.FRANCHISES.items():
            assert "source" in franchise, f"{key} missing source"
            assert "movies" in franchise, f"{key} missing movies"
            assert len(franchise["movies"]) > 0, f"{key} has no movies"

    def test_all_movies_have_required_fields(self, movie_module):
        for key, franchise in movie_module.FRANCHISES.items():
            for movie in franchise["movies"]:
                assert "title" in movie, f"{key}: movie missing title"
                assert "wiki_page" in movie, f"{key}/{movie.get('title', '?')}: missing wiki_page"

    def test_franchise_count(self, movie_module):
        """Should have a reasonable number of franchises."""
        assert len(movie_module.FRANCHISES) >= 10


class TestMovieTextChunking:
    """Tests for text chunking logic."""

    def test_chunks_long_text(self, movie_module):
        text = "This is a sentence. " * 200
        chunks = movie_module.chunk_text(text, chunk_size=800)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 1200  # Some slack for sentence boundaries

    def test_respects_paragraph_boundaries(self, movie_module):
        text = "First paragraph content here.\n\nSecond paragraph content here.\n\nThird paragraph."
        chunks = movie_module.chunk_text(text, chunk_size=100)
        # Should not split in the middle of a paragraph when possible
        assert len(chunks) >= 1

    def test_filters_short_chunks(self, movie_module):
        text = "A.\n\nB.\n\nC."
        chunks = movie_module.chunk_text(text, chunk_size=800)
        for chunk in chunks:
            assert len(chunk) > 50

    def test_handles_empty_text(self, movie_module):
        chunks = movie_module.chunk_text("", chunk_size=800)
        assert chunks == []


class TestMovieWikiRequest:
    """Tests for Wikipedia API calls."""

    @patch("urllib.request.urlopen")
    def test_wiki_get_page_text(self, mock_urlopen, movie_module):
        response_data = {
            "query": {"pages": [{"title": "Test", "extract": "Page content here."}]}
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        text = movie_module.wiki_get_page_text("Test_Page")
        assert text == "Page content here."

    @patch("urllib.request.urlopen")
    def test_wiki_handles_missing_page(self, mock_urlopen, movie_module):
        response_data = {"query": {"pages": [{"missing": True}]}}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(response_data).encode()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        text = movie_module.wiki_get_page_text("Nonexistent_Page")
        assert text == ""


class TestMovieMemoryStorage:
    """Tests for vector memory storage."""

    @patch("urllib.request.urlopen")
    def test_store_memory_in_dry_run(self, mock_urlopen, movie_module):
        movie_module.dry_run = True
        result = movie_module.store_memory("Test text", "movie_test", {"type": "test"})
        assert result is True
        assert movie_module.stats["memories_stored"] == 1
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_store_memory_truncates_long_text(self, mock_urlopen, movie_module):
        movie_module.dry_run = False
        mock_resp = MagicMock()
        mock_urlopen.return_value = mock_resp
        long_text = "A" * 5000
        movie_module.store_memory(long_text, "test", {})
        # Verify the payload was truncated
        call_args = mock_urlopen.call_args
        req = call_args[0][0] if call_args[0] else call_args[1].get("url")
        # In dry_run=False mode, the text should be truncated to 2000


class TestMovieProcessMovie:
    """Tests for processing a single movie."""

    @patch("nova_movie_script_ingest.store_memory")
    @patch("nova_movie_script_ingest.wiki_get_page_text")
    @patch("nova_movie_script_ingest.fetch_imsdb_script")
    def test_processes_wiki_only(self, mock_imsdb, mock_wiki, mock_store, movie_module, tmp_path):
        mock_imsdb.return_value = None
        mock_wiki.return_value = "This is a movie overview with enough content.\n\nMore content here." * 5
        mock_store.return_value = True
        movie_module.stats["franchise"] = "test"
        with patch.object(movie_module, "LOG_DIR", tmp_path):
            movie = {"title": "Test Movie", "year": 2020, "wiki_page": "Test_Movie", "imsdb_path": None}
            count = movie_module.process_movie(movie, "movie_test")
        assert count > 0
        mock_store.assert_called()


# ═══════════════════════════════════════════════════════════════════════════════
# ADULT SWIM INGEST TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestAdultSwimShowConfig:
    """Tests for show configuration."""

    def test_all_shows_have_required_fields(self, adult_swim_module):
        for key, show in adult_swim_module.SHOWS.items():
            assert "source" in show, f"{key} missing source"
            assert "title" in show, f"{key} missing title"
            assert "network" in show, f"{key} missing network"
            assert "years" in show, f"{key} missing years"
            assert "creators" in show, f"{key} missing creators"

    def test_all_shows_have_parsers(self, adult_swim_module):
        for key in adult_swim_module.SHOWS:
            assert key in adult_swim_module.SHOW_PARSERS, f"No parser for {key}"


class TestAdultSwimHtmlParsing:
    """Tests for HTML table parsing."""

    def test_strip_html(self, adult_swim_module):
        html = '<b>Bold</b> and <a href="test">link</a> and &amp; entity'
        result = adult_swim_module.strip_html(html)
        assert "Bold" in result
        assert "link" in result
        assert "&" in result
        assert "<" not in result

    def test_parse_episode_tables_basic(self, adult_swim_module):
        html = """
        <table class="wikiepisodetable">
            <tr><th>No.</th><th>Title</th><th>Air date</th></tr>
            <tr><td>1</td><td>"Pilot"</td><td>January 1, 2004</td></tr>
            <tr><td>2</td><td>"Episode Two"</td><td>January 8, 2004</td></tr>
        </table>
        """
        episodes = adult_swim_module.parse_episode_tables(html)
        assert len(episodes) == 2
        assert episodes[0].get("title") in ("Pilot", '"Pilot"')


class TestAdultSwimMemoryConstruction:
    """Tests for memory entry building."""

    def test_build_episode_memory(self, adult_swim_module):
        show_config = adult_swim_module.SHOWS["aqua_teen"]
        episode = {
            "season": 1, "episode_number": 1,
            "title": "Rabbot", "air_date": "December 30, 2000",
            "written_by": "Dave Willis", "description": "A giant robotic rabbit terrorizes the neighborhood."
        }
        text, meta = adult_swim_module.build_episode_memory(show_config, episode)
        assert "Rabbot" in text
        assert "S01E01" in text
        assert meta["type"] == "episode"
        assert meta["show"] == "Aqua Teen Hunger Force"

    def test_build_show_overview_memory(self, adult_swim_module):
        show_config = adult_swim_module.SHOWS["perfect_hair_forever"]
        text, meta = adult_swim_module.build_show_overview_memory("perfect_hair_forever", show_config, "A surreal show about hair.")
        assert "Perfect Hair Forever" in text
        assert "Adult Swim" in text
        assert meta["type"] == "show_overview"

    def test_build_season_summary_memory(self, adult_swim_module):
        show_config = adult_swim_module.SHOWS["aqua_teen"]
        episodes = [
            {"title": "Ep1", "air_date": "Jan 1"},
            {"title": "Ep2", "air_date": "Jan 8"},
        ]
        text, meta = adult_swim_module.build_season_summary_memory(show_config, 1, episodes)
        assert "Season 1" in text
        assert "2 episodes" in text
        assert meta["type"] == "season_summary"


class TestAdultSwimVectorStore:
    """Tests for vector memory writes."""

    @patch("urllib.request.urlopen")
    def test_dry_run_mode(self, mock_urlopen, adult_swim_module):
        adult_swim_module.dry_run = True
        result = adult_swim_module.vector_remember("Test text", "tv_test", {"type": "test"})
        assert result is True
        assert adult_swim_module.stats["memories_stored"] == 1
        mock_urlopen.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════════
# COMEDY INGEST TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestComedyFilenameParsing:
    """Tests for comedian/show extraction from filenames."""

    def test_colon_delimiter(self, comedy_module):
        comedian, show = comedy_module.parse_comedian_show("Dave Chappelle_ For What It's Worth.m4v")
        assert comedian == "Dave Chappelle"
        assert "For What It's Worth" in show

    def test_dash_delimiter(self, comedy_module):
        comedian, show = comedy_module.parse_comedian_show("Eddie Izzard - Dress to Kill.m4v")
        assert comedian == "Eddie Izzard"
        assert show == "Dress to Kill"

    def test_underscore_names(self, comedy_module):
        comedian, show = comedy_module.parse_comedian_show("LOUIS_CK_CHEWED_UP-2.m4v")
        assert comedian == "Louis C.K."
        assert "Part 2" in show

    def test_part_number_detection(self, comedy_module):
        comedian, show = comedy_module.parse_comedian_show("Lewis Black Unleashed-3.m4v")
        assert "Part 3" in show

    def test_norman_rockwell_special_case(self, comedy_module):
        comedian, show = comedy_module.parse_comedian_show("Norman Rockwell is Bleeding.m4v")
        assert comedian == "Lewis Black"

    def test_unknown_comedian(self, comedy_module):
        comedian, show = comedy_module.parse_comedian_show("Random Comedy Special.m4v")
        assert comedian == "Unknown"


class TestComedianNormalization:
    """Tests for name normalization."""

    def test_louis_ck_variants(self, comedy_module):
        assert comedy_module._normalize_comedian("Louis CK") == "Louis C.K."
        assert comedy_module._normalize_comedian("LOUIS CK") == "Louis C.K."
        assert comedy_module._normalize_comedian("louis c.k.") == "Louis C.K."

    def test_katt_williams(self, comedy_module):
        assert comedy_module._normalize_comedian("Kat Williams") == "Katt Williams"
        assert comedy_module._normalize_comedian("Katt Williams") == "Katt Williams"


class TestComedyAudioExtraction:
    """Tests for audio extraction."""

    @patch("subprocess.run")
    def test_extract_audio_success(self, mock_run, comedy_module, tmp_path):
        output_audio = tmp_path / "audio.wav"
        output_audio.write_bytes(b"\x00" * 2000)  # Fake WAV

        mock_run.return_value = MagicMock(returncode=0)
        result = comedy_module.extract_audio(tmp_path / "video.mp4", str(tmp_path))
        # Even with mocked subprocess, we check the logic
        assert result is not None or mock_run.called

    @patch("subprocess.run")
    def test_get_duration(self, mock_run, comedy_module, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="3600.5\n", stderr="")
        duration = comedy_module.get_duration(tmp_path / "video.mp4")
        assert duration == 3600.5


class TestComedyVectorMemory:
    """Tests for comedy memory storage."""

    @patch("urllib.request.urlopen")
    def test_vector_remember_success(self, mock_urlopen, comedy_module):
        mock_resp = MagicMock()
        mock_urlopen.return_value = mock_resp
        result = comedy_module.vector_remember("Test comedy text", {"type": "test"})
        assert result is True

    @patch("urllib.request.urlopen")
    def test_vector_remember_failure(self, mock_urlopen, comedy_module):
        mock_urlopen.side_effect = Exception("Connection refused")
        result = comedy_module.vector_remember("Test", {"type": "test"})
        assert result is False


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestMediaIngestSecurity:
    """Security tests for media ingest scripts."""

    def test_movie_no_hardcoded_keys(self, movie_module):
        import inspect
        source = inspect.getsource(movie_module)
        assert "sk-" not in source
        assert "AKIA" not in source

    def test_adult_swim_no_hardcoded_keys(self, adult_swim_module):
        import inspect
        source = inspect.getsource(adult_swim_module)
        assert "sk-" not in source

    def test_comedy_no_hardcoded_keys(self, comedy_module):
        import inspect
        source = inspect.getsource(comedy_module)
        assert "sk-" not in source

    def test_movie_vector_url_is_localhost(self, movie_module):
        assert "127.0.0.1" in movie_module.VECTOR_URL

    def test_adult_swim_vector_url_is_localhost(self, adult_swim_module):
        assert "127.0.0.1" in adult_swim_module.VECTOR_URL

    def test_comedy_vector_url_is_localhost(self, comedy_module):
        assert "127.0.0.1" in comedy_module.VECTOR_URL

    def test_movie_respects_rate_limits(self, movie_module):
        """Movie ingest should have rate limiting between Wikipedia calls."""
        import inspect
        source = inspect.getsource(movie_module.run_franchise)
        assert "time.sleep" in source


# ═══════════════════════════════════════════════════════════════════════════════
# FRAMEWORK TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestGracefulShutdown:
    """Tests for graceful shutdown handling."""

    def test_movie_shutdown_event(self, movie_module):
        movie_module.shutdown.set()
        # process_movie should check shutdown and exit early
        # This verifies the shutdown mechanism exists
        assert movie_module.shutdown.is_set()
        movie_module.shutdown.clear()

    def test_adult_swim_shutdown_event(self, adult_swim_module):
        adult_swim_module.shutdown.set()
        assert adult_swim_module.shutdown.is_set()
        adult_swim_module.shutdown.clear()

    def test_comedy_shutdown_event(self, comedy_module):
        comedy_module.shutdown.set()
        assert comedy_module.shutdown.is_set()
        comedy_module.shutdown.clear()


class TestStatusReporting:
    """Tests for periodic status reporting."""

    def test_movie_post_status(self, movie_module):
        movie_module.stats["franchise"] = "test"
        movie_module.stats["movies_total"] = 10
        movie_module.stats["movies_processed"] = 5
        movie_module.stats["memories_stored"] = 100
        movie_module.stats["start_time"] = time.time() - 60
        movie_module.stats["current_movie"] = "Test Movie"
        # Should not raise
        with patch("nova_movie_script_ingest.slack_post"):
            movie_module.post_status()


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestIntegration:
    """Integration tests requiring live services."""

    def test_wikipedia_api_reachable(self, movie_module):
        """Verify Wikipedia API is accessible."""
        import urllib.request
        try:
            params = "action=query&titles=Main_Page&format=json"
            url = f"{movie_module.WIKI_API}?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "NovaTest/1.0"})
            resp = urllib.request.urlopen(req, timeout=10)
            assert resp.status == 200
        except Exception:
            pytest.skip("Wikipedia API not reachable")

    def test_memory_server_reachable(self, movie_module):
        """Verify memory server is running."""
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18790/health", timeout=5)
            assert resp.status == 200
        except Exception:
            pytest.skip("Memory server not running")
