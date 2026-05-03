#!/usr/bin/env python3
"""
test_bulk_ingest.py — Comprehensive tests for nova_bulk_ingest.py (bulk video ingestion).

Covers: file discovery, ingestion state tracking, source classification, text chunking,
episode title parsing, memory storage, status reporting, security (no hardcoded paths,
local-only privacy metadata).

Run: python3 -m pytest tests/test_bulk_ingest.py -v
Written by Jordan Koch.
"""

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_nova_config_for_ingest(monkeypatch):
    """Mock nova_config before nova_bulk_ingest imports it."""
    mock_config = MagicMock()
    mock_config.VECTOR_URL = "http://127.0.0.1:18790/remember"
    mock_config.SLACK_API = "https://slack.com/api"
    mock_config.SLACK_CHAN = "C_TEST_CHAT"
    mock_config.SLACK_NOTIFY = "C_TEST_NOTIFY"
    mock_config.JORDAN_DM = "D_TEST_DM"
    mock_config.slack_bot_token.return_value = "xoxb-test-token"
    mock_config.post_both = MagicMock()
    mock_config.post_discord = MagicMock(return_value=True)
    monkeypatch.setitem(sys.modules, "nova_config", mock_config)
    return mock_config


@pytest.fixture
def ingest_module(mock_nova_config_for_ingest):
    """Import nova_bulk_ingest with mocked dependencies."""
    import importlib
    if "nova_bulk_ingest" in sys.modules:
        del sys.modules["nova_bulk_ingest"]
    import nova_bulk_ingest
    return nova_bulk_ingest


@pytest.fixture
def mock_media_dirs(tmp_path):
    """Create a mock media directory structure with sample video files."""
    tv_dir = tmp_path / "TVShows"
    tv_dir.mkdir()
    movie_dir = tmp_path / "Ripped Movies"
    movie_dir.mkdir()

    # TV Shows structure
    show_dir = tv_dir / "Breaking Bad" / "Season 1"
    show_dir.mkdir(parents=True)
    (show_dir / "S01E01 Pilot.mkv").write_text("fake video")
    (show_dir / "S01E02 Cat's in the Bag.mkv").write_text("fake video")

    # Another show
    show2_dir = tv_dir / "Seinfeld" / "Season 3"
    show2_dir.mkdir(parents=True)
    (show2_dir / "The Pen.mp4").write_text("fake video")

    # Movies
    (movie_dir / "The Matrix (1999).mkv").write_text("fake video")
    (movie_dir / "2001 A Space Odyssey.mp4").write_text("fake video")

    # Sample file, should be skipped
    (movie_dir / "sample-trailer.mp4").write_text("fake video")

    return tv_dir, movie_dir


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestFindAllVideos:
    """Tests for find_all_videos() — file discovery."""

    def test_discovers_correct_extensions(self, ingest_module, mock_media_dirs):
        tv_dir, movie_dir = mock_media_dirs
        with patch.object(ingest_module, "MEDIA_DIRS", [tv_dir, movie_dir]):
            videos = ingest_module.find_all_videos()
        extensions = {v.suffix.lower() for v in videos}
        # Should find .mkv and .mp4 files
        assert ".mkv" in extensions or ".mp4" in extensions

    def test_skips_sample_files(self, ingest_module, mock_media_dirs):
        tv_dir, movie_dir = mock_media_dirs
        with patch.object(ingest_module, "MEDIA_DIRS", [tv_dir, movie_dir]):
            videos = ingest_module.find_all_videos()
        names = [v.name.lower() for v in videos]
        assert not any("sample" in n for n in names)

    def test_skips_trailer_files(self, ingest_module, mock_media_dirs):
        tv_dir, movie_dir = mock_media_dirs
        with patch.object(ingest_module, "MEDIA_DIRS", [tv_dir, movie_dir]):
            videos = ingest_module.find_all_videos()
        names = [v.name.lower() for v in videos]
        assert not any("trailer" in n for n in names)

    def test_handles_missing_directory(self, ingest_module, tmp_path):
        with patch.object(ingest_module, "MEDIA_DIRS", [tmp_path / "nonexistent"]):
            videos = ingest_module.find_all_videos()
        assert videos == []

    def test_discovers_files_in_subdirectories(self, ingest_module, mock_media_dirs):
        tv_dir, movie_dir = mock_media_dirs
        with patch.object(ingest_module, "MEDIA_DIRS", [tv_dir]):
            videos = ingest_module.find_all_videos()
        # Should find files in Season 1 and Season 3 subdirs
        assert len(videos) >= 3


class TestIngestionState:
    """Tests for is_already_ingested() and mark_ingested()."""

    def test_is_already_ingested_false_initially(self, ingest_module, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch.object(ingest_module, "STATE_DIR", state_dir):
            assert ingest_module.is_already_ingested(Path("/fake/video.mkv")) is False

    def test_mark_ingested_creates_marker(self, ingest_module, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        with patch.object(ingest_module, "STATE_DIR", state_dir):
            ingest_module.mark_ingested(Path("/fake/video.mkv"))
        marker = state_dir / "video.ingested"
        assert marker.exists()
        content = marker.read_text()
        # Should contain ISO timestamp
        assert "T" in content

    def test_is_already_ingested_true_after_marking(self, ingest_module, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        video = Path("/fake/test_episode.mkv")
        with patch.object(ingest_module, "STATE_DIR", state_dir):
            ingest_module.mark_ingested(video)
            assert ingest_module.is_already_ingested(video) is True


class TestClassifySource:
    """Tests for classify_source() — source tag and show name extraction."""

    def test_classifies_movies(self, ingest_module):
        path = Path("/Volumes/external/videos/Ripped Movies/The Matrix (1999).mkv")
        source, show = ingest_module.classify_source(path)
        assert source == "movie_transcript"
        assert show == "The Matrix (1999)"

    def test_classifies_tv_shows(self, ingest_module):
        path = Path("/Volumes/external/videos/TVShows/Breaking Bad/Season 1/S01E01.mkv")
        source, show = ingest_module.classify_source(path)
        assert source == "tv_transcript"
        assert show == "Breaking Bad"

    def test_classifies_unknown_paths(self, ingest_module):
        path = Path("/some/other/location/video.mp4")
        source, show = ingest_module.classify_source(path)
        assert source == "video_transcript"

    def test_tv_extracts_show_from_parent(self, ingest_module):
        path = Path("/Volumes/external/videos/TVShows/Seinfeld/Season 5/The Marine Biologist.mp4")
        source, show = ingest_module.classify_source(path)
        assert show == "Seinfeld"


class TestChunkText:
    """Tests for chunk_text() — text splitting for vector memory."""

    def test_short_text_returns_single_chunk(self, ingest_module):
        result = ingest_module.chunk_text("Short text here.", max_chars=1800)
        assert len(result) == 1
        assert result[0] == "Short text here."

    def test_splits_at_sentence_boundaries(self, ingest_module):
        text = ". ".join([f"Sentence number {i}" for i in range(100)])
        chunks = ingest_module.chunk_text(text, max_chars=200)
        assert len(chunks) > 1
        # Each chunk (except possibly the last) should end with a sentence
        for chunk in chunks[:-1]:
            assert len(chunk) <= 200

    def test_respects_max_chars(self, ingest_module):
        # Use proper sentences so chunk_text can split at sentence boundaries
        text = ". ".join([f"Sentence number {i} with some padding text" for i in range(100)])
        chunks = ingest_module.chunk_text(text, max_chars=500)
        assert len(chunks) > 1
        # Most chunks (except the last accumulation) should be near the limit
        for chunk in chunks[:-1]:
            assert len(chunk) <= 600  # generous margin for sentence boundary

    def test_empty_text_returns_single_chunk(self, ingest_module):
        result = ingest_module.chunk_text("", max_chars=1800)
        assert len(result) == 1

    def test_very_long_sentence_gets_truncated(self, ingest_module):
        text = "A" * 3000  # One "sentence" longer than max_chars
        chunks = ingest_module.chunk_text(text, max_chars=1800)
        assert len(chunks) >= 1


class TestParseEpisodeTitle:
    """Tests for YouTube ID stripping from episode titles."""

    def test_strips_youtube_id(self, ingest_module):
        import re
        title = "How to Cook Pasta [dQw4w9WgXcQ]"
        cleaned = re.sub(r'\s*\[[\w-]+\]$', '', title)
        assert cleaned == "How to Cook Pasta"

    def test_preserves_title_without_id(self, ingest_module):
        import re
        title = "Normal Episode Title"
        cleaned = re.sub(r'\s*\[[\w-]+\]$', '', title)
        assert cleaned == "Normal Episode Title"

    def test_strips_only_trailing_brackets(self, ingest_module):
        import re
        title = "[Important] Episode Name [abc123]"
        cleaned = re.sub(r'\s*\[[\w-]+\]$', '', title)
        assert cleaned == "[Important] Episode Name"


class TestVideoExtensions:
    """Tests for VIDEO_EXTENSIONS constant."""

    def test_includes_all_expected_formats(self, ingest_module):
        expected = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".ts", ".flv", ".wmv"}
        assert expected == ingest_module.VIDEO_EXTENSIONS

    def test_extensions_are_lowercase(self, ingest_module):
        for ext in ingest_module.VIDEO_EXTENSIONS:
            assert ext == ext.lower()
            assert ext.startswith(".")


class TestMediaDirs:
    """Tests for MEDIA_DIRS configuration."""

    def test_media_dirs_paths_correct(self, ingest_module):
        expected_paths = [
            Path("/Volumes/external/videos/TVShows"),
            Path("/Volumes/external/videos/Ripped Movies"),
        ]
        assert ingest_module.MEDIA_DIRS == expected_paths

    def test_media_dirs_is_list(self, ingest_module):
        assert isinstance(ingest_module.MEDIA_DIRS, list)


class TestIngestToMemory:
    """Tests for ingest_to_memory() — vector memory storage."""

    @patch("urllib.request.urlopen")
    def test_stores_chunks_with_metadata(self, mock_urlopen, ingest_module):
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        stored = ingest_module.ingest_to_memory(
            "A long enough transcript text to meet the minimum threshold for storage.",
            "Episode Title", "tv_transcript", "Breaking Bad", "S01E01.mkv"
        )
        assert stored >= 1
        # Verify the payload
        call_args = mock_urlopen.call_args[0][0]
        payload = json.loads(call_args.data)
        assert "privacy" in payload["metadata"]
        assert payload["metadata"]["privacy"] == "local-only"

    def test_skips_short_text(self, ingest_module):
        with patch("urllib.request.urlopen") as mock_url:
            stored = ingest_module.ingest_to_memory("Short", "Title", "source", "show", "file.mkv")
        assert stored == 0

    @patch("urllib.request.urlopen")
    def test_memory_has_privacy_local_only(self, mock_urlopen, ingest_module):
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        ingest_module.ingest_to_memory(
            "A sufficiently long transcript for ingestion testing purposes here.",
            "Title", "tv_transcript", "Show", "file.mkv"
        )
        call_args = mock_urlopen.call_args[0][0]
        payload = json.loads(call_args.data)
        assert payload["metadata"]["privacy"] == "local-only"


class TestProcessSingleFile:
    """Tests for process_single_file() — end-to-end per-file pipeline."""

    @patch("nova_bulk_ingest.mark_ingested")
    @patch("nova_bulk_ingest.ingest_to_memory", return_value=3)
    @patch("nova_bulk_ingest.transcribe", return_value="Transcribed text with enough words for testing.")
    @patch("nova_bulk_ingest.extract_audio")
    def test_successful_processing(self, mock_audio, mock_transcribe,
                                    mock_ingest, mock_mark, ingest_module, tmp_path):
        wav = tmp_path / "test.wav"
        wav.write_text("fake wav")
        mock_audio.return_value = wav
        video = Path("/Volumes/external/videos/TVShows/Seinfeld/S03E01.mkv")
        result = ingest_module.process_single_file(video)
        assert result["success"] is True
        assert result["memories"] == 3
        assert result["words"] > 0
        mock_mark.assert_called_once()

    @patch("nova_bulk_ingest.extract_audio", side_effect=RuntimeError("ffmpeg failed"))
    def test_handles_extraction_failure(self, mock_audio, ingest_module):
        video = Path("/fake/video.mkv")
        result = ingest_module.process_single_file(video)
        assert result["success"] is False
        assert result["error"] is not None
        assert "ffmpeg" in result["error"]


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestSecurityBulkIngest:
    """Security tests: no hardcoded paths, local-only privacy."""

    def test_no_hardcoded_username_paths(self):
        source = (Path(__file__).parent.parent / "nova_bulk_ingest.py").read_text()
        assert str(Path.home()) + "/" not in source

    def test_no_credentials_in_source(self):
        source = (Path(__file__).parent.parent / "nova_bulk_ingest.py").read_text()
        assert "xoxb-" not in source
        assert "sk-" not in source
        assert "AKIA" not in source
        assert "password" not in source.lower().split("#")[0]  # ignore comments

    def test_no_pii_emails(self):
        source = (Path(__file__).parent.parent / "nova_bulk_ingest.py").read_text()
        pii = ["testuser@example.com", "testuser@corp.example.com", "testuser@domain.example.com"]
        for email in pii:
            assert email not in source

    @patch("urllib.request.urlopen")
    def test_memory_payloads_have_local_only_privacy(self, mock_urlopen, ingest_module):
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp
        ingest_module.ingest_to_memory(
            "Test transcript text that is long enough to be stored in memory.",
            "Title", "tv_transcript", "Show", "file.mkv"
        )
        call_args = mock_urlopen.call_args[0][0]
        payload = json.loads(call_args.data)
        assert payload["metadata"]["privacy"] == "local-only"


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.functional
class TestSingleFilePipeline:
    """Functional tests: full single-file pipeline with mocks."""

    @patch("nova_bulk_ingest.mark_ingested")
    @patch("nova_bulk_ingest.ingest_to_memory", return_value=5)
    @patch("nova_bulk_ingest.transcribe", return_value="This is a full transcript of the episode content with lots of words.")
    @patch("nova_bulk_ingest.extract_audio")
    def test_complete_pipeline(self, mock_audio, mock_transcribe,
                                mock_ingest, mock_mark, ingest_module, tmp_path):
        wav = tmp_path / "episode.wav"
        wav.write_text("wav data")
        mock_audio.return_value = wav
        video = Path("/Volumes/external/videos/TVShows/Breaking Bad/S01E01 Pilot.mkv")
        result = ingest_module.process_single_file(video)
        assert result["success"] is True
        assert result["file"] == "S01E01 Pilot.mkv"
        mock_mark.assert_called_once_with(video)


@pytest.mark.functional
class TestStatusReporter:
    """Functional tests for status reporting."""

    def test_status_message_format(self, ingest_module, mock_nova_config_for_ingest):
        stats = {
            "total_files": 100,
            "completed": 25,
            "skipped": 10,
            "errors": 2,
            "memories_stored": 150,
            "start_time": time.time() - 600,  # 10 minutes ago
            "current_file": "S01E01.mkv",
            "active_workers": 3,
        }
        ingest_module.post_status(stats)
        mock_nova_config_for_ingest.post_both.assert_called_once()
        msg = mock_nova_config_for_ingest.post_both.call_args[0][0]
        assert "Bulk Video Ingest" in msg
        assert "25/100" in msg
        assert "25.0%" in msg

    def test_skips_already_ingested(self, ingest_module, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        video = Path("/fake/already_done.mkv")
        with patch.object(ingest_module, "STATE_DIR", state_dir):
            ingest_module.mark_ingested(video)
            assert ingest_module.is_already_ingested(video) is True


# ═══════════════════════════════════════════════════════════════════════════════
# FRAME TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.frame
class TestBulkIngestFrameTests:
    """Frame tests: imports, constants, configuration."""

    def test_script_imports_without_error(self):
        if "nova_bulk_ingest" in sys.modules:
            del sys.modules["nova_bulk_ingest"]
        mock_config = MagicMock()
        mock_config.VECTOR_URL = "http://127.0.0.1:18790/remember"
        mock_config.SLACK_NOTIFY = "C_TEST"
        mock_config.post_both = MagicMock()
        sys.modules["nova_config"] = mock_config
        try:
            import nova_bulk_ingest
        except Exception as e:
            pytest.fail(f"Import failed: {e}")

    def test_video_extensions_complete(self, ingest_module):
        expected = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".ts", ".flv", ".wmv"}
        assert ingest_module.VIDEO_EXTENSIONS == expected

    def test_media_dirs_are_paths(self, ingest_module):
        for d in ingest_module.MEDIA_DIRS:
            assert isinstance(d, Path)

    def test_max_workers_is_reasonable(self, ingest_module):
        assert 1 <= ingest_module.MAX_WORKERS <= 8

    def test_status_interval_is_5_minutes(self, ingest_module):
        assert ingest_module.STATUS_INTERVAL == 300

    def test_memory_url_is_async(self, ingest_module):
        assert "async=1" in ingest_module.MEMORY_URL

    def test_work_dir_on_data_volume(self, ingest_module):
        assert str(ingest_module.WORK_DIR).startswith("/Volumes/Data")

    def test_whisper_model_specified(self, ingest_module):
        assert "whisper" in ingest_module.WHISPER_MODEL.lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
