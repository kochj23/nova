#!/usr/bin/env python3
"""
test_daily_news_ingest.py — Comprehensive tests for nova_daily_news_ingest.py.

Covers: HDHomeRun recording, MLX Whisper transcription, text chunking,
Ollama summarization, memory server ingestion, signal handling, security.

Run: python3 -m pytest tests/test_daily_news_ingest.py -v
Written by Jordan Koch.
"""

import json
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_nova_config_for_news(monkeypatch):
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
def news_module(mock_nova_config_for_news, tmp_path, monkeypatch):
    """Import nova_daily_news_ingest with mocked paths."""
    for mod in list(sys.modules.keys()):
        if "nova_daily_news_ingest" in mod:
            del sys.modules[mod]

    import nova_daily_news_ingest

    # Override paths
    nova_daily_news_ingest.WORK_DIR = tmp_path / "daily-news"
    nova_daily_news_ingest.LOG_FILE = str(tmp_path / "test.log")
    nova_daily_news_ingest.shutdown = False

    return nova_daily_news_ingest


@pytest.fixture
def sample_transcript():
    """A realistic news broadcast transcript snippet."""
    return (
        "Good evening, I'm David Ono. Breaking news tonight out of downtown Los Angeles "
        "where a five-alarm fire has broken out at a commercial building on 7th and Grand. "
        "Fire crews from LAFD Station 9 are on scene battling the blaze. No injuries reported "
        "so far. In other news, the mayor announced a new initiative to address homelessness "
        "in the city, committing 500 million dollars over the next three years. The program "
        "will focus on supportive housing and mental health services. Meanwhile, traffic on "
        "the 405 freeway is backed up for 7 miles due to a multi-vehicle collision near the "
        "Getty Center exit. CHP has closed two lanes and expects delays through the evening "
        "commute. Coming up after the break, your seven-day forecast with meteorologist "
        "Leslie Lopez. Temperatures expected to reach triple digits this weekend. "
    ) * 10  # Make it long enough for chunking


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestRecordAudio:
    """Tests for record_audio()."""

    @patch("subprocess.run")
    def test_successful_recording(self, mock_run, news_module, tmp_path):
        news_module.WORK_DIR = tmp_path

        def create_wav(*args, **kwargs):
            # Simulate ffmpeg creating the output file
            wav_files = list(tmp_path.glob("kabc_news_*.wav"))
            if not wav_files:
                # Create a simulated output
                now = datetime.now()
                filename = f"kabc_news_{now.strftime('%Y%m%d_%H%M')}.wav"
                (tmp_path / filename).write_bytes(b"\x00" * 200000)
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = create_wav
        result = news_module.record_audio(60)
        assert result is not None
        assert result.exists()
        assert result.stat().st_size > 100000

    @patch("subprocess.run")
    def test_recording_failure_returns_none(self, mock_run, news_module, tmp_path):
        news_module.WORK_DIR = tmp_path
        mock_run.return_value = MagicMock(returncode=1, stderr="No signal from tuner")
        result = news_module.record_audio(60)
        assert result is None

    @patch("subprocess.run")
    def test_recording_timeout_returns_none(self, mock_run, news_module, tmp_path):
        news_module.WORK_DIR = tmp_path
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=120)
        result = news_module.record_audio(60)
        assert result is None

    @patch("subprocess.run")
    def test_recording_too_small_returns_none(self, mock_run, news_module, tmp_path):
        news_module.WORK_DIR = tmp_path

        def create_small_wav(*args, **kwargs):
            now = datetime.now()
            filename = f"kabc_news_{now.strftime('%Y%m%d_%H%M')}.wav"
            (tmp_path / filename).write_bytes(b"\x00" * 50)  # Too small
            return MagicMock(returncode=0, stderr="")

        mock_run.side_effect = create_small_wav
        result = news_module.record_audio(60)
        assert result is None

    @patch("subprocess.run")
    def test_uses_correct_hdhr_stream_url(self, mock_run, news_module, tmp_path):
        news_module.WORK_DIR = tmp_path
        mock_run.return_value = MagicMock(returncode=1, stderr="")
        news_module.record_audio(1800)
        cmd = mock_run.call_args[0][0]
        # Should include channel in URL
        assert news_module.HDHR_STREAM in cmd[3] or "192.168.1.89" in cmd[3]
        assert news_module.CHANNEL in cmd[3]

    @patch("subprocess.run")
    def test_uses_correct_duration(self, mock_run, news_module, tmp_path):
        news_module.WORK_DIR = tmp_path
        mock_run.return_value = MagicMock(returncode=1, stderr="")
        news_module.record_audio(1800)
        cmd = mock_run.call_args[0][0]
        assert "1800" in cmd

    @patch("subprocess.run")
    def test_creates_work_dir_if_missing(self, mock_run, news_module, tmp_path):
        new_dir = tmp_path / "new" / "subdir"
        news_module.WORK_DIR = new_dir
        mock_run.return_value = MagicMock(returncode=1, stderr="")
        news_module.record_audio(60)
        assert new_dir.exists()


class TestTranscribeNews:
    """Tests for transcribe()."""

    @patch("subprocess.run")
    def test_successful_transcription(self, mock_run, news_module, tmp_path):
        news_module.WORK_DIR = tmp_path
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        wav_path = tmp_path / "kabc_news_20260501_1700.wav"
        wav_path.write_bytes(b"\x00" * 1000)
        txt_path = wav_path.with_suffix(".txt")
        txt_path.write_text("Breaking news tonight from downtown Los Angeles.")

        result = news_module.transcribe(wav_path)
        assert result == "Breaking news tonight from downtown Los Angeles."

    @patch("subprocess.run")
    def test_whisper_failure_returns_empty(self, mock_run, news_module, tmp_path):
        news_module.WORK_DIR = tmp_path
        mock_run.return_value = MagicMock(returncode=1, stderr="Model error")
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"\x00" * 1000)
        result = news_module.transcribe(wav_path)
        assert result == ""

    @patch("subprocess.run")
    def test_whisper_timeout_returns_empty(self, mock_run, news_module, tmp_path):
        news_module.WORK_DIR = tmp_path
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="mlx_whisper", timeout=600)
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"\x00" * 1000)
        result = news_module.transcribe(wav_path)
        assert result == ""

    @patch("subprocess.run")
    def test_uses_english_language(self, mock_run, news_module, tmp_path):
        """KABC is English-language, should use --language en."""
        news_module.WORK_DIR = tmp_path
        mock_run.return_value = MagicMock(returncode=0, stderr="")
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"\x00" * 1000)
        news_module.transcribe(wav_path)
        cmd = mock_run.call_args[0][0]
        assert "--language" in cmd
        assert "en" in cmd


class TestChunkTextNews:
    """Tests for chunk_text()."""

    def test_basic_chunking(self, news_module, sample_transcript):
        chunks = news_module.chunk_text(sample_transcript)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= news_module.CHUNK_SIZE + 200

    def test_short_text_single_chunk(self, news_module):
        text = "A single sentence news broadcast that is long enough to pass the minimum filter threshold of fifty chars."
        chunks = news_module.chunk_text(text)
        assert len(chunks) == 1

    def test_filters_short_chunks(self, news_module):
        """Chunks under 50 chars should be filtered out."""
        text = "Hi. Ok. " + "A proper sentence that is definitely longer than fifty characters here."
        chunks = news_module.chunk_text(text)
        for chunk in chunks:
            assert len(chunk) > 50

    def test_empty_text(self, news_module):
        chunks = news_module.chunk_text("")
        assert chunks == []

    def test_handles_newlines(self, news_module):
        text = ("Breaking news from downtown\nFire crews responding\nNo injuries reported. " * 30)
        chunks = news_module.chunk_text(text)
        for chunk in chunks:
            assert "\n" not in chunk


class TestIngestChunksNews:
    """Tests for ingest_chunks()."""

    @patch("urllib.request.urlopen")
    def test_successful_ingestion(self, mock_urlopen, news_module):
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        chunks = ["News chunk one with sufficient length.", "News chunk two with enough text."]
        result = news_module.ingest_chunks(chunks, "2026-05-01T17:00:00", "2026-05-01")
        assert result == 2

    @patch("urllib.request.urlopen")
    def test_correct_metadata_structure(self, mock_urlopen, news_module):
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
        news_module.ingest_chunks(
            ["Test news chunk content."],
            "2026-05-01T17:00:00",
            "2026-05-01",
        )
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data)
        assert payload["metadata"]["source"] == "daily_news"
        assert payload["metadata"]["channel"] == news_module.CHANNEL
        assert payload["metadata"]["channel_name"] == news_module.CHANNEL_NAME
        assert payload["metadata"]["type"] == "news_broadcast"
        assert payload["metadata"]["privacy"] == "public"
        assert payload["metadata"]["date"] == "2026-05-01"

    @patch("urllib.request.urlopen")
    def test_failure_does_not_crash(self, mock_urlopen, news_module):
        mock_urlopen.side_effect = Exception("Connection refused")
        result = news_module.ingest_chunks(["Chunk text."], "timestamp", "date")
        assert result == 0


class TestSummarizeNews:
    """Tests for summarize_news()."""

    @patch("urllib.request.urlopen")
    def test_successful_summary(self, mock_urlopen, news_module):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "response": "- Fire in downtown LA\n- Mayor announces homelessness plan"
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = news_module.summarize_news("Transcript text...", "17:00", "2026-05-01")
        assert "Fire" in result or "downtown" in result or "-" in result

    @patch("urllib.request.urlopen")
    def test_summary_failure_returns_fallback(self, mock_urlopen, news_module):
        mock_urlopen.side_effect = Exception("Ollama unavailable")
        result = news_module.summarize_news("Transcript text...", "17:00", "2026-05-01")
        assert "unavailable" in result.lower()

    @patch("urllib.request.urlopen")
    def test_calls_ollama_on_localhost(self, mock_urlopen, news_module):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"response": "Summary"}).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        news_module.summarize_news("Text", "17:00", "2026-05-01")
        req = mock_urlopen.call_args[0][0]
        assert "127.0.0.1:11434" in req.full_url

    @patch("urllib.request.urlopen")
    def test_uses_qwen3_coder_model(self, mock_urlopen, news_module):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"response": "Summary"}).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        news_module.summarize_news("Text", "17:00", "2026-05-01")
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data)
        assert payload["model"] == "qwen3-coder:30b"

    @patch("urllib.request.urlopen")
    def test_truncates_transcript_for_ollama(self, mock_urlopen, news_module):
        """Transcript sent to Ollama should be truncated to 6000 chars."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"response": "Short"}).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        long_text = "A" * 20000
        news_module.summarize_news(long_text, "17:00", "2026-05-01")
        req = mock_urlopen.call_args[0][0]
        payload = json.loads(req.data)
        # The prompt includes the transcript truncated at 6000
        assert len(payload["prompt"]) < 7000


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestSecurityDailyNews:
    """Security tests: no secrets, localhost-only services, no PII."""

    def test_no_hardcoded_tokens(self):
        source = Path(__file__).parent.parent / "nova_daily_news_ingest.py"
        content = source.read_text()
        assert "xoxb-" not in content
        assert "sk-" not in content
        assert "AKIA" not in content
        assert "ghp_" not in content

    def test_memory_server_is_localhost(self, news_module):
        assert "127.0.0.1" in news_module.MEMORY_URL

    def test_ollama_is_localhost(self):
        source = Path(__file__).parent.parent / "nova_daily_news_ingest.py"
        content = source.read_text()
        assert "127.0.0.1:11434" in content

    def test_hdhr_is_local_network(self, news_module):
        assert "192.168." in news_module.HDHR_STREAM

    def test_no_personal_emails(self):
        source = Path(__file__).parent.parent / "nova_daily_news_ingest.py"
        content = source.read_text()
        # Verify no personal email addresses are embedded
        import re
        personal_patterns = [r"kochj\w*@\w+\.\w+", r"jordan\.\w+@\w+\.\w+"]
        for pat in personal_patterns:
            assert not re.search(pat, content), f"Personal email pattern found: {pat}"

    def test_no_hardcoded_home_paths(self):
        source = Path(__file__).parent.parent / "nova_daily_news_ingest.py"
        content = source.read_text()
        home_path = Path.home()
        assert str(home_path) + "/" not in content

    def test_metadata_privacy_is_public(self, news_module):
        """News broadcasts are public content, metadata should reflect that."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__ = MagicMock()
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            news_module.ingest_chunks(["Test chunk."], "ts", "date")
            req = mock_urlopen.call_args[0][0]
            payload = json.loads(req.data)
            assert payload["metadata"]["privacy"] == "public"

    def test_no_sensitive_data_in_notify_calls(self, news_module, mock_nova_config_for_news):
        """Notifications should not contain tokens or credentials."""
        news_module.notify("Test notification message")
        if mock_nova_config_for_news.post_both.called:
            msg = mock_nova_config_for_news.post_both.call_args[0][0]
            assert "xoxb-" not in msg
            assert "token" not in msg.lower() or "Token" not in msg


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS — End-to-End Pipeline
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.functional
class TestFullPipelineNews:
    """Full pipeline: HDHomeRun -> ffmpeg -> whisper -> chunk -> summarize -> ingest."""

    @patch("urllib.request.urlopen")
    @patch("nova_daily_news_ingest.summarize_news", return_value="- Top story summary")
    @patch("nova_daily_news_ingest.transcribe")
    @patch("nova_daily_news_ingest.record_audio")
    def test_full_pipeline(self, mock_record, mock_transcribe, mock_summarize,
                           mock_urlopen, news_module, tmp_path, sample_transcript):
        news_module.WORK_DIR = tmp_path

        # Mock recording
        wav_path = tmp_path / "kabc_news_20260501_1700.wav"
        wav_path.write_bytes(b"\x00" * 200000)
        mock_record.return_value = wav_path

        # Mock transcription
        mock_transcribe.return_value = sample_transcript

        # Mock memory server
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        news_module.main()

        # Verify recording was attempted
        mock_record.assert_called_once_with(news_module.RECORD_DURATION)
        # Verify transcription
        mock_transcribe.assert_called_once()
        # Verify summarization
        mock_summarize.assert_called_once()
        # Verify ingestion happened
        assert mock_urlopen.called

    @patch("nova_daily_news_ingest.record_audio", return_value=None)
    def test_recording_failure_aborts_gracefully(self, mock_record, news_module,
                                                 tmp_path, mock_nova_config_for_news):
        news_module.WORK_DIR = tmp_path
        news_module.main()
        # Should notify about failure
        notify_calls = mock_nova_config_for_news.post_both.call_args_list
        failure_notified = any("Failed" in str(c) or "failed" in str(c) for c in notify_calls)
        assert failure_notified

    @patch("nova_daily_news_ingest.transcribe", return_value="")
    @patch("nova_daily_news_ingest.record_audio")
    def test_transcription_failure_aborts(self, mock_record, mock_transcribe,
                                         news_module, tmp_path):
        news_module.WORK_DIR = tmp_path
        wav_path = tmp_path / "test.wav"
        wav_path.write_bytes(b"\x00" * 200000)
        mock_record.return_value = wav_path
        news_module.main()
        # WAV should be cleaned up even on failure
        assert not wav_path.exists()

    @patch("urllib.request.urlopen")
    @patch("nova_daily_news_ingest.transcribe")
    @patch("nova_daily_news_ingest.record_audio")
    def test_transcript_saved_to_file(self, mock_record, mock_transcribe, mock_urlopen,
                                      news_module, tmp_path, sample_transcript):
        """Transcript should be saved to a file for archival."""
        news_module.WORK_DIR = tmp_path
        wav_path = tmp_path / "kabc_news_20260501_1700.wav"
        wav_path.write_bytes(b"\x00" * 200000)
        mock_record.return_value = wav_path
        mock_transcribe.return_value = sample_transcript
        mock_urlopen.return_value.__enter__ = MagicMock()
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        news_module.main()

        # Check transcript file was created
        transcript_files = list(tmp_path.glob("*_transcript.txt"))
        assert len(transcript_files) == 1
        assert transcript_files[0].read_text() == sample_transcript


# ═══════════════════════════════════════════════════════════════════════════════
# FRAMEWORK TESTS — Signals, Error Recovery, Logging
# ═══════════════════════════════════════════════════════════════════════════════


class TestFrameworkDailyNews:
    """Framework tests: graceful shutdown, error recovery, config constants."""

    def test_sigterm_sets_shutdown(self, news_module):
        news_module.shutdown = False
        news_module.signal_handler(signal.SIGTERM, None)
        assert news_module.shutdown is True

    def test_sigint_sets_shutdown(self, news_module):
        news_module.shutdown = False
        news_module.signal_handler(signal.SIGINT, None)
        assert news_module.shutdown is True

    def test_record_duration_is_30_min(self, news_module):
        assert news_module.RECORD_DURATION == 1800

    def test_channel_is_kabc(self, news_module):
        assert news_module.CHANNEL == "7.1"
        assert "KABC" in news_module.CHANNEL_NAME

    def test_chunk_size_reasonable(self, news_module):
        assert 500 <= news_module.CHUNK_SIZE <= 5000

    def test_memory_url_uses_async(self, news_module):
        """Memory URL should use async mode for non-blocking ingestion."""
        assert "async=1" in news_module.MEMORY_URL

    def test_logging_configured(self, news_module):
        assert news_module.log is not None
        news_module.log.info("Test log message")


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestDailyNewsIntegration:
    """Integration tests hitting live services. Skipped if unavailable."""

    @pytest.fixture(autouse=True)
    def check_services(self):
        import urllib.request
        try:
            urllib.request.urlopen("http://192.168.1.89:5004/lineup.json", timeout=3)
        except Exception:
            pytest.skip("HDHomeRun at 192.168.1.89 not available")

    def test_hdhr_accessible(self):
        """HDHomeRun should be reachable and responding."""
        import urllib.request
        resp = urllib.request.urlopen("http://192.168.1.89:5004/lineup.json", timeout=5)
        assert resp.status == 200

    def test_hdhr_has_kabc_channel(self):
        """HDHomeRun should have channel 7.1 (KABC) in lineup."""
        import urllib.request
        resp = urllib.request.urlopen("http://192.168.1.89:5004/lineup.json", timeout=5)
        lineup = json.loads(resp.read())
        channels = [ch.get("GuideNumber") for ch in lineup]
        assert "7.1" in channels

    @pytest.mark.integration
    def test_ollama_reachable(self):
        """Ollama should be running on localhost."""
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5)
            assert resp.status == 200
        except Exception:
            pytest.skip("Ollama not available at 127.0.0.1:11434")

    @pytest.mark.integration
    def test_memory_server_reachable(self):
        """Memory server should be running."""
        import urllib.request
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:18790/health", timeout=5)
            assert resp.status == 200
        except Exception:
            pytest.skip("Memory server not available at 127.0.0.1:18790")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
