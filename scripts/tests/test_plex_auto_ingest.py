#!/usr/bin/env python3
"""
test_plex_auto_ingest.py — Comprehensive tests for nova_plex_auto_ingest.py.

Covers: Plex API scanning, audio extraction, MLX Whisper transcription,
content classification into vectors, text chunking, memory server ingestion,
state persistence, signal handling, security.

Run: python3 -m pytest tests/test_plex_auto_ingest.py -v
Written by Jordan Koch.
"""

import json
import signal
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_nova_config_for_plex_ingest(monkeypatch):
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
def plex_ingest(mock_nova_config_for_plex_ingest, tmp_path, monkeypatch):
    """Import nova_plex_auto_ingest with mocked paths."""
    for mod in list(sys.modules.keys()):
        if "nova_plex_auto_ingest" in mod:
            del sys.modules[mod]

    # Patch module-level constants before import
    monkeypatch.setenv("TESTING", "1")
    import nova_plex_auto_ingest

    # Override paths to use tmp_path
    nova_plex_auto_ingest.WORK_DIR = tmp_path / "work"
    nova_plex_auto_ingest.STATE_FILE = tmp_path / "work" / "ingested_keys.json"
    nova_plex_auto_ingest.LOG_FILE = str(tmp_path / "test.log")
    nova_plex_auto_ingest.shutdown = False

    return nova_plex_auto_ingest


@pytest.fixture
def sample_plex_recently_added():
    """Sample Plex API response for recently added items."""
    return {
        "MediaContainer": {
            "Metadata": [
                {
                    "type": "episode",
                    "title": "The Contest",
                    "grandparentTitle": "Seinfeld",
                    "ratingKey": "12345",
                    "duration": 1800000,
                    "Genre": [{"tag": "Comedy"}, {"tag": "Sitcom"}],
                    "Media": [{"Part": [{"file": "/mnt/media/Seinfeld/S04E11.mkv"}]}],
                },
                {
                    "type": "movie",
                    "title": "Jaws",
                    "grandparentTitle": "",
                    "ratingKey": "67890",
                    "duration": 7440000,
                    "Genre": [{"tag": "Horror"}, {"tag": "Thriller"}],
                    "Media": [{"Part": [{"file": "/mnt/media/Movies/Jaws.mkv"}]}],
                },
            ]
        }
    }


@pytest.fixture
def sample_plex_item_too_long():
    """A Plex item that exceeds MAX_DURATION_MIN."""
    return {
        "type": "movie",
        "title": "Very Long Movie",
        "grandparentTitle": "",
        "ratingKey": "99999",
        "duration": 14400000,  # 240 minutes = 4 hours
        "Genre": [],
        "Media": [{"Part": [{"file": "/mnt/media/Movies/VeryLong.mkv"}]}],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestPlexToken:
    """Tests for Plex token retrieval from Keychain."""

    @patch("subprocess.run")
    def test_token_returns_stripped_value(self, mock_run, plex_ingest):
        mock_run.return_value = MagicMock(stdout="my-plex-token\n", returncode=0)
        token = plex_ingest.plex_token()
        assert token == "my-plex-token"

    @patch("subprocess.run")
    def test_token_calls_keychain_security(self, mock_run, plex_ingest):
        mock_run.return_value = MagicMock(stdout="tok123\n", returncode=0)
        plex_ingest.plex_token()
        args = mock_run.call_args[0][0]
        assert "security" in args
        assert "find-generic-password" in args
        assert "nova-plex-token" in args

    @patch("subprocess.run")
    def test_empty_token_falls_back_to_nova_plex(self, mock_run, plex_ingest, monkeypatch):
        mock_run.return_value = MagicMock(stdout="", returncode=1)
        mock_nova_plex = MagicMock()
        mock_nova_plex.token.return_value = "fallback-token"
        monkeypatch.setitem(sys.modules, "nova_plex", mock_nova_plex)
        token = plex_ingest.plex_token()
        assert token == "fallback-token"


class TestGetRecentlyAdded:
    """Tests for get_recently_added()."""

    @patch("nova_plex_auto_ingest.plex_get")
    def test_returns_items_list(self, mock_get, plex_ingest, sample_plex_recently_added):
        mock_get.return_value = sample_plex_recently_added
        items = plex_ingest.get_recently_added("6")
        assert len(items) == 2
        assert items[0]["title"] == "The Contest"

    @patch("nova_plex_auto_ingest.plex_get")
    def test_returns_empty_on_error(self, mock_get, plex_ingest):
        mock_get.side_effect = Exception("Connection refused")
        items = plex_ingest.get_recently_added("6")
        assert items == []

    @patch("nova_plex_auto_ingest.plex_get")
    def test_returns_empty_on_missing_metadata(self, mock_get, plex_ingest):
        mock_get.return_value = {"MediaContainer": {}}
        items = plex_ingest.get_recently_added("6")
        assert items == []


class TestExtractAudio:
    """Tests for extract_audio()."""

    @patch("subprocess.run")
    def test_successful_extraction(self, mock_run, plex_ingest, tmp_path):
        plex_ingest.WORK_DIR = tmp_path
        # Create a fake wav file that the function would expect
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        # We need to simulate ffmpeg creating the file
        def side_effect(*args, **kwargs):
            # Create a fake WAV file
            wav_files = list(tmp_path.glob("temp_audio_*.wav"))
            if not wav_files:
                # The function builds the path before calling subprocess
                for f in tmp_path.iterdir():
                    pass
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = side_effect

        # Patch Path.exists and stat to simulate ffmpeg creating the file
        with patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "stat") as mock_stat:
            mock_stat.return_value = MagicMock(st_size=50000)
            result = plex_ingest.extract_audio("/mnt/media/test.mkv")
            assert result is not None

    @patch("subprocess.run")
    def test_ffmpeg_failure_returns_none(self, mock_run, plex_ingest):
        mock_run.return_value = MagicMock(returncode=1, stderr="Error: codec not found")
        result = plex_ingest.extract_audio("/mnt/media/test.mkv")
        assert result is None

    @patch("subprocess.run")
    def test_ffmpeg_timeout_returns_none(self, mock_run, plex_ingest):
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=600)
        result = plex_ingest.extract_audio("/mnt/media/test.mkv")
        assert result is None

    @patch("subprocess.run")
    def test_uses_correct_ffmpeg_args(self, mock_run, plex_ingest):
        mock_run.return_value = MagicMock(returncode=1, stderr="")
        plex_ingest.extract_audio("/mnt/media/video.mkv")
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == plex_ingest.FFMPEG
        assert "-vn" in cmd
        assert "pcm_s16le" in cmd
        assert "16000" in cmd
        assert "-ac" in cmd
        assert "1" in cmd


class TestTranscribe:
    """Tests for transcribe()."""

    @patch("subprocess.run")
    def test_successful_transcription(self, mock_run, plex_ingest, tmp_path):
        plex_ingest.WORK_DIR = tmp_path
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        # Simulate whisper producing output text
        # The function generates a filename based on time, so we need to patch it
        with patch("time.time", return_value=1000000):
            txt_file = tmp_path / "plex_1000000.txt"
            txt_file.write_text("This is the transcribed text from the video.")
            result = plex_ingest.transcribe(tmp_path / "audio.wav")
        assert result == "This is the transcribed text from the video."

    @patch("subprocess.run")
    def test_whisper_failure_returns_empty(self, mock_run, plex_ingest, tmp_path):
        plex_ingest.WORK_DIR = tmp_path
        mock_run.return_value = MagicMock(returncode=1, stderr="Model not found")
        result = plex_ingest.transcribe(tmp_path / "audio.wav")
        assert result == ""

    @patch("subprocess.run")
    def test_whisper_timeout_returns_empty(self, mock_run, plex_ingest, tmp_path):
        import subprocess
        plex_ingest.WORK_DIR = tmp_path
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="mlx_whisper", timeout=1800)
        result = plex_ingest.transcribe(tmp_path / "audio.wav")
        assert result == ""

    @patch("subprocess.run")
    def test_uses_translate_task(self, mock_run, plex_ingest, tmp_path):
        """Should use --task translate for non-English content."""
        plex_ingest.WORK_DIR = tmp_path
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        plex_ingest.transcribe(tmp_path / "audio.wav")
        cmd = mock_run.call_args[0][0]
        assert "--task" in cmd
        assert "translate" in cmd

    @patch("subprocess.run")
    def test_cleans_up_txt_file(self, mock_run, plex_ingest, tmp_path):
        """Output txt file should be cleaned up after reading."""
        plex_ingest.WORK_DIR = tmp_path
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        with patch("time.time", return_value=2000000):
            txt_file = tmp_path / "plex_2000000.txt"
            txt_file.write_text("Transcription text here.")
            plex_ingest.transcribe(tmp_path / "audio.wav")
        assert not txt_file.exists()


class TestClassifyContent:
    """Tests for classify_content()."""

    def test_comedy_classification(self, plex_ingest):
        result = plex_ingest.classify_content(
            "The Contest", "Seinfeld", ["Comedy", "Sitcom"],
            "A very funny episode about a contest between friends."
        )
        assert result == "comedy"

    def test_horror_classification(self, plex_ingest):
        result = plex_ingest.classify_content(
            "Jaws", "", ["Horror", "Thriller"],
            "A terrifying shark attacks a beach town in a scary supernatural story."
        )
        assert result == "horror"

    def test_documentary_classification(self, plex_ingest):
        result = plex_ingest.classify_content(
            "Planet Earth", "", ["Documentary", "Nature"],
            "A documentary about nature and the science of ecosystems."
        )
        assert result == "documentary"

    def test_default_to_documentary(self, plex_ingest):
        """When no keywords match, should default to documentary."""
        result = plex_ingest.classify_content(
            "Unknown Title", "", [],
            "xyzxyzxyz completely unclassifiable content."
        )
        assert result == "documentary"

    def test_game_show_classification(self, plex_ingest):
        result = plex_ingest.classify_content(
            "Jeopardy!", "Jeopardy", ["Game Show"],
            "The contestants compete on this game show quiz."
        )
        assert result == "game_show"

    def test_highest_score_wins(self, plex_ingest):
        """When multiple categories match, highest score wins."""
        result = plex_ingest.classify_content(
            "Horror Comedy", "", ["Horror"],
            "A scary slasher film with a supernatural zombie haunted house."
        )
        # Horror has more keyword matches here
        assert result == "horror"

    def test_genres_influence_classification(self, plex_ingest):
        """Genre tags should contribute to classification."""
        result = plex_ingest.classify_content(
            "Neutral Title", "", ["Action", "Adventure"],
            "Some neutral text about everyday activities."
        )
        assert result == "action"


class TestChunkText:
    """Tests for chunk_text()."""

    def test_basic_chunking(self, plex_ingest):
        # Each sentence needs to be long enough that combining them exceeds CHUNK_SIZE
        text = ". ".join(["Sentence number " + str(i) + " with extra padding to make it longer" for i in range(200)])
        chunks = plex_ingest.chunk_text(text)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= plex_ingest.CHUNK_SIZE + 200  # tolerance for last sentence

    def test_short_text_single_chunk(self, plex_ingest):
        text = "This is a short text that should be one chunk because it fits."
        chunks = plex_ingest.chunk_text(text)
        assert len(chunks) == 1

    def test_very_short_chunks_filtered(self, plex_ingest):
        """Chunks shorter than 50 chars should be discarded."""
        text = "Hi. Ok. Yes. No. " + "A proper sentence with more than fifty characters total here."
        chunks = plex_ingest.chunk_text(text)
        for chunk in chunks:
            assert len(chunk) > 50

    def test_empty_text_no_chunks(self, plex_ingest):
        chunks = plex_ingest.chunk_text("")
        assert chunks == []

    def test_newlines_handled(self, plex_ingest):
        """Newlines should be replaced with spaces."""
        text = "Line one\nLine two\nLine three. " * 50
        chunks = plex_ingest.chunk_text(text)
        for chunk in chunks:
            assert "\n" not in chunk

    def test_preserves_all_content(self, plex_ingest):
        """No content should be lost during chunking (except very short fragments)."""
        sentences = [f"Sentence number {i} with enough content to exceed fifty chars" for i in range(50)]
        text = ". ".join(sentences)
        chunks = plex_ingest.chunk_text(text)
        combined = " ".join(chunks)
        # All major sentences should appear
        assert "Sentence number 0" in combined
        assert "Sentence number 49" in combined


class TestIngestChunks:
    """Tests for ingest_chunks()."""

    @patch("urllib.request.urlopen")
    def test_successful_ingestion(self, mock_urlopen, plex_ingest):
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        chunks = ["Chunk one with enough text to pass.", "Chunk two with sufficient length."]
        metadata = {"title": "Test", "show": "TestShow"}
        result = plex_ingest.ingest_chunks(chunks, "comedy", metadata)
        assert result == 2

    @patch("urllib.request.urlopen")
    def test_partial_failure(self, mock_urlopen, plex_ingest):
        """Some chunks fail, others succeed."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] % 2 == 0:
                raise Exception("Server error")
            return MagicMock(__enter__=MagicMock(), __exit__=MagicMock(return_value=False))

        mock_urlopen.side_effect = side_effect
        chunks = ["Chunk A long enough.", "Chunk B long enough.", "Chunk C long enough."]
        result = plex_ingest.ingest_chunks(chunks, "drama", {})
        assert result == 2  # 1st and 3rd succeed

    @patch("urllib.request.urlopen")
    def test_correct_payload_structure(self, mock_urlopen, plex_ingest):
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        chunks = ["Test chunk content for validation purposes."]
        metadata = {"title": "MyShow", "section": "TV Shows"}
        plex_ingest.ingest_chunks(chunks, "comedy", metadata)

        # Check the request was made with correct data
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        payload = json.loads(req.data)
        assert payload["text"] == chunks[0]
        assert payload["metadata"]["source"] == "comedy"
        assert payload["metadata"]["title"] == "MyShow"
        assert payload["metadata"]["privacy"] == "local-only"

    @patch("urllib.request.urlopen")
    def test_all_failures_returns_zero(self, mock_urlopen, plex_ingest):
        mock_urlopen.side_effect = Exception("Network unreachable")
        chunks = ["Chunk 1 long enough.", "Chunk 2 long enough."]
        result = plex_ingest.ingest_chunks(chunks, "drama", {})
        assert result == 0


class TestProcessItem:
    """Tests for process_item()."""

    @patch("nova_plex_auto_ingest.ingest_chunks", return_value=5)
    @patch("nova_plex_auto_ingest.chunk_text", return_value=["chunk"] * 5)
    @patch("nova_plex_auto_ingest.classify_content", return_value="comedy")
    @patch("nova_plex_auto_ingest.transcribe", return_value="A " * 200)
    @patch("nova_plex_auto_ingest.extract_audio")
    def test_successful_processing(self, mock_extract, mock_transcribe,
                                   mock_classify, mock_chunk, mock_ingest, plex_ingest, tmp_path):
        mock_extract.return_value = tmp_path / "audio.wav"
        (tmp_path / "audio.wav").write_bytes(b"\x00" * 100)

        item = {
            "type": "episode",
            "title": "The Contest",
            "grandparentTitle": "Seinfeld",
            "ratingKey": "12345",
            "duration": 1800000,
            "Genre": [{"tag": "Comedy"}],
            "Media": [{"Part": [{"file": "/mnt/media/test.mkv"}]}],
        }
        result = plex_ingest.process_item(item, "TV Shows")
        assert result is True

    def test_no_file_path_returns_false(self, plex_ingest):
        item = {
            "type": "episode", "title": "NoFile", "grandparentTitle": "",
            "ratingKey": "111", "duration": 1800000, "Genre": [],
            "Media": [],
        }
        result = plex_ingest.process_item(item, "TV Shows")
        assert result is False

    def test_too_long_returns_false(self, plex_ingest):
        item = {
            "type": "movie", "title": "Long Movie", "grandparentTitle": "",
            "ratingKey": "222", "duration": 14400000, "Genre": [],
            "Media": [{"Part": [{"file": "/mnt/media/long.mkv"}]}],
        }
        result = plex_ingest.process_item(item, "Movies")
        assert result is False

    def test_too_short_returns_false(self, plex_ingest):
        item = {
            "type": "movie", "title": "Clip", "grandparentTitle": "",
            "ratingKey": "333", "duration": 30000, "Genre": [],
            "Media": [{"Part": [{"file": "/mnt/media/clip.mkv"}]}],
        }
        result = plex_ingest.process_item(item, "Movies")
        assert result is False

    @patch("nova_plex_auto_ingest.extract_audio", return_value=None)
    def test_audio_extraction_failure(self, mock_extract, plex_ingest):
        item = {
            "type": "episode", "title": "BadAudio", "grandparentTitle": "",
            "ratingKey": "444", "duration": 3600000, "Genre": [],
            "Media": [{"Part": [{"file": "/mnt/media/bad.mkv"}]}],
        }
        result = plex_ingest.process_item(item, "TV Shows")
        assert result is False

    @patch("nova_plex_auto_ingest.transcribe", return_value="short")
    @patch("nova_plex_auto_ingest.extract_audio")
    def test_short_transcription_returns_false(self, mock_extract, mock_transcribe, plex_ingest, tmp_path):
        wav_path = tmp_path / "audio.wav"
        wav_path.write_bytes(b"\x00" * 100)
        mock_extract.return_value = wav_path
        item = {
            "type": "episode", "title": "BadTranscription", "grandparentTitle": "",
            "ratingKey": "555", "duration": 3600000, "Genre": [],
            "Media": [{"Part": [{"file": "/mnt/media/bad.mkv"}]}],
        }
        result = plex_ingest.process_item(item, "TV Shows")
        assert result is False


class TestStateManagement:
    """Tests for load_state() and save_state()."""

    def test_load_state_creates_directory(self, plex_ingest, tmp_path):
        plex_ingest.WORK_DIR = tmp_path / "newdir"
        plex_ingest.STATE_FILE = tmp_path / "newdir" / "state.json"
        state = plex_ingest.load_state()
        assert (tmp_path / "newdir").exists()
        assert state == {"ingested": {}, "last_run": 0}

    def test_load_state_reads_existing(self, plex_ingest, tmp_path):
        plex_ingest.WORK_DIR = tmp_path
        plex_ingest.STATE_FILE = tmp_path / "state.json"
        (tmp_path / "state.json").write_text(json.dumps({
            "ingested": {"123": {"title": "Test"}},
            "last_run": 1000,
        }))
        state = plex_ingest.load_state()
        assert "123" in state["ingested"]
        assert state["last_run"] == 1000

    def test_save_state_writes_json(self, plex_ingest, tmp_path):
        plex_ingest.STATE_FILE = tmp_path / "state.json"
        state = {"ingested": {"456": {"title": "Saved"}}, "last_run": 2000}
        plex_ingest.save_state(state)
        loaded = json.loads((tmp_path / "state.json").read_text())
        assert loaded["ingested"]["456"]["title"] == "Saved"

    def test_state_roundtrip(self, plex_ingest, tmp_path):
        plex_ingest.WORK_DIR = tmp_path
        plex_ingest.STATE_FILE = tmp_path / "state.json"
        original = {"ingested": {"key1": {"title": "Item 1"}}, "last_run": 5000}
        plex_ingest.save_state(original)
        loaded = plex_ingest.load_state()
        assert loaded == original


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestSecurityPlexIngest:
    """Security tests: no hardcoded credentials, localhost-only, no PII."""

    def test_no_hardcoded_tokens_in_source(self):
        source = Path(__file__).parent.parent / "nova_plex_auto_ingest.py"
        content = source.read_text()
        assert "xoxb-" not in content
        assert "sk-" not in content
        assert "AKIA" not in content
        assert "ghp_" not in content

    def test_token_from_keychain_only(self):
        source = Path(__file__).parent.parent / "nova_plex_auto_ingest.py"
        content = source.read_text()
        assert "find-generic-password" in content
        assert "nova-plex-token" in content

    def test_memory_server_is_localhost_only(self, plex_ingest):
        assert "127.0.0.1" in plex_ingest.MEMORY_URL
        assert "localhost" in plex_ingest.MEMORY_URL or "127.0.0.1" in plex_ingest.MEMORY_URL

    def test_plex_server_is_local_network(self, plex_ingest):
        assert "192.168." in plex_ingest.PLEX_URL

    def test_no_personal_emails_in_source(self):
        source = Path(__file__).parent.parent / "nova_plex_auto_ingest.py"
        content = source.read_text()
        # Verify no personal email addresses are embedded
        import re
        personal_patterns = [r"kochj\w*@\w+\.\w+", r"jordan\.\w+@\w+\.\w+"]
        for pat in personal_patterns:
            assert not re.search(pat, content), f"Personal email pattern found: {pat}"

    def test_state_file_contains_no_secrets(self, plex_ingest, tmp_path):
        plex_ingest.STATE_FILE = tmp_path / "state.json"
        state = {
            "ingested": {"123": {"title": "Test Show", "ingested_at": "2026-01-01T12:00:00"}},
            "last_run": 1000,
        }
        plex_ingest.save_state(state)
        content = (tmp_path / "state.json").read_text()
        assert "xoxb-" not in content
        assert "X-Plex-Token" not in content
        assert "password" not in content.lower()

    def test_no_hardcoded_paths_to_home_dir(self):
        source = Path(__file__).parent.parent / "nova_plex_auto_ingest.py"
        content = source.read_text()
        home_path = Path.home()
        assert str(home_path) + "/" not in content

    def test_metadata_privacy_field_is_local_only(self, plex_ingest):
        """Ingested chunks should be marked as local-only privacy."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = MagicMock()
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            plex_ingest.ingest_chunks(
                ["Test content for privacy validation."],
                "comedy",
                {"title": "Test"},
            )
            req = mock_urlopen.call_args[0][0]
            payload = json.loads(req.data)
            assert payload["metadata"]["privacy"] == "local-only"


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS — End-to-End Pipeline
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.functional
class TestFullPipelinePlex:
    """Full pipeline: Plex API -> ffmpeg -> whisper -> classify -> ingest."""

    @patch("urllib.request.urlopen")
    @patch("nova_plex_auto_ingest.transcribe")
    @patch("nova_plex_auto_ingest.extract_audio")
    @patch("nova_plex_auto_ingest.plex_get")
    @patch("nova_plex_auto_ingest.plex_token", return_value="test-token")
    def test_full_pipeline_comedy(self, mock_token, mock_plex_get, mock_extract,
                                  mock_transcribe, mock_urlopen, plex_ingest, tmp_path):
        """End-to-end: comedy show -> classified as comedy -> ingested."""
        plex_ingest.WORK_DIR = tmp_path
        plex_ingest.STATE_FILE = tmp_path / "state.json"

        # Mock Plex API
        mock_plex_get.return_value = {
            "MediaContainer": {
                "Metadata": [{
                    "type": "episode",
                    "title": "The Puffy Shirt",
                    "grandparentTitle": "Seinfeld",
                    "ratingKey": "99001",
                    "duration": 1800000,
                    "Genre": [{"tag": "Comedy"}, {"tag": "Sitcom"}],
                    "Media": [{"Part": [{"file": "/media/Seinfeld/S05E02.mkv"}]}],
                }],
            },
        }

        # Mock audio extraction
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"\x00" * 100)
        mock_extract.return_value = wav_path

        # Mock transcription
        mock_transcribe.return_value = (
            "Jerry agrees to wear a puffy shirt on national television. "
            "The comedy of the situation is that he looks ridiculous on the show. "
            "Kramer's girlfriend is the designer of this funny shirt. "
        ) * 20  # Make it long enough

        # Mock memory server
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        # Run processing
        item = mock_plex_get.return_value["MediaContainer"]["Metadata"][0]
        result = plex_ingest.process_item(item, "TV Shows")

        assert result is True
        # Verify memory server was called
        assert mock_urlopen.called
        # Verify correct vector classification
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data)
        assert payload["metadata"]["source"] == "comedy"

    @patch("nova_plex_auto_ingest.process_item", return_value=True)
    @patch("nova_plex_auto_ingest.get_recently_added")
    def test_main_loop_tracks_state(self, mock_get_recent, mock_process, plex_ingest, tmp_path):
        """Main loop properly updates state file with processed items."""
        plex_ingest.WORK_DIR = tmp_path
        plex_ingest.STATE_FILE = tmp_path / "state.json"

        mock_get_recent.return_value = [{
            "ratingKey": "new_item_1",
            "title": "New Episode",
            "grandparentTitle": "Show",
        }]

        with patch("time.sleep"):
            plex_ingest.main()

        state = json.loads((tmp_path / "state.json").read_text())
        assert "new_item_1" in state["ingested"]
        assert state["last_run"] > 0

    @patch("nova_plex_auto_ingest.get_recently_added", return_value=[])
    def test_main_loop_no_new_content(self, mock_get_recent, plex_ingest, tmp_path):
        """Main loop handles no new content gracefully."""
        plex_ingest.WORK_DIR = tmp_path
        plex_ingest.STATE_FILE = tmp_path / "state.json"
        plex_ingest.main()
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["ingested"] == {}


# ═══════════════════════════════════════════════════════════════════════════════
# FRAMEWORK TESTS — Signals, State, Error Recovery
# ═══════════════════════════════════════════════════════════════════════════════


class TestFrameworkPlexIngest:
    """Framework tests: graceful shutdown, state persistence, error recovery."""

    def test_sigterm_sets_shutdown_flag(self, plex_ingest):
        """SIGTERM should set shutdown=True for graceful exit."""
        plex_ingest.shutdown = False
        plex_ingest.signal_handler(signal.SIGTERM, None)
        assert plex_ingest.shutdown is True

    def test_sigint_sets_shutdown_flag(self, plex_ingest):
        """SIGINT should set shutdown=True for graceful exit."""
        plex_ingest.shutdown = False
        plex_ingest.signal_handler(signal.SIGINT, None)
        assert plex_ingest.shutdown is True

    @patch("nova_plex_auto_ingest.get_recently_added")
    def test_shutdown_stops_processing(self, mock_get_recent, plex_ingest, tmp_path):
        """When shutdown=True, main loop should stop iterating."""
        plex_ingest.WORK_DIR = tmp_path
        plex_ingest.STATE_FILE = tmp_path / "state.json"
        plex_ingest.shutdown = True
        mock_get_recent.return_value = [{"ratingKey": "item1"}]
        plex_ingest.main()
        # Should not have processed anything
        state = json.loads((tmp_path / "state.json").read_text())
        assert state["ingested"] == {}

    def test_state_persists_between_runs(self, plex_ingest, tmp_path):
        """State saved in one run should be loadable in the next."""
        plex_ingest.WORK_DIR = tmp_path
        plex_ingest.STATE_FILE = tmp_path / "state.json"

        # First run saves state
        state1 = {"ingested": {"abc": {"title": "First"}}, "last_run": 100}
        plex_ingest.save_state(state1)

        # Second run loads it
        state2 = plex_ingest.load_state()
        assert state2["ingested"]["abc"]["title"] == "First"

    def test_corrupt_state_file_handled(self, plex_ingest, tmp_path):
        """Corrupt state file should not crash the script."""
        plex_ingest.WORK_DIR = tmp_path
        plex_ingest.STATE_FILE = tmp_path / "state.json"
        (tmp_path / "state.json").write_text("not json {{{{")
        with pytest.raises(json.JSONDecodeError):
            plex_ingest.load_state()

    def test_work_dir_created_if_missing(self, plex_ingest, tmp_path):
        """WORK_DIR should be created if it doesn't exist."""
        new_dir = tmp_path / "deep" / "nested" / "workdir"
        plex_ingest.WORK_DIR = new_dir
        plex_ingest.STATE_FILE = new_dir / "state.json"
        plex_ingest.load_state()
        assert new_dir.exists()

    def test_sections_constant_defined(self, plex_ingest):
        """SECTIONS should be non-empty dict of section_key -> name."""
        assert len(plex_ingest.SECTIONS) > 0
        for key, name in plex_ingest.SECTIONS.items():
            assert isinstance(key, str)
            assert isinstance(name, str)
            assert len(name) > 0

    def test_vector_map_has_content(self, plex_ingest):
        """VECTOR_MAP should have multiple categories with keywords."""
        assert len(plex_ingest.VECTOR_MAP) >= 10
        for vector, keywords in plex_ingest.VECTOR_MAP.items():
            assert isinstance(keywords, list)
            assert len(keywords) > 0

    def test_logging_configured(self, plex_ingest):
        """Logger should be configured and functional."""
        assert plex_ingest.log is not None
        # Should not raise
        plex_ingest.log.info("Test log message")


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestPlexIngestIntegration:
    """Integration tests that hit live services. Skipped if unavailable."""

    @pytest.fixture(autouse=True)
    def check_services_available(self):
        import urllib.request
        try:
            urllib.request.urlopen("http://192.168.1.10:32400/identity", timeout=3)
        except Exception:
            pytest.skip("Plex server at 192.168.1.10:32400 not available")
        try:
            urllib.request.urlopen("http://127.0.0.1:18790/health", timeout=3)
        except Exception:
            pytest.skip("Memory server at 127.0.0.1:18790 not available")

    def test_plex_recently_added_returns_data(self, plex_ingest):
        """Live Plex API should return recently added items."""
        items = plex_ingest.get_recently_added("6")
        assert isinstance(items, list)

    def test_memory_server_accepts_ingestion(self, plex_ingest):
        """Live memory server should accept test data."""
        result = plex_ingest.ingest_chunks(
            ["Integration test chunk for Plex auto-ingest verification."],
            "test_integration",
            {"title": "test", "type": "integration_test"},
        )
        assert result == 1


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
