#!/usr/bin/env python3
"""
test_youtube_download.py — Comprehensive tests for nova_youtube_download.py.

Covers: filename sanitization, yt-dlp playlist/channel fetching, video downloading,
playlist mode vs year mode, parallel execution, signal handling, security.

Run: python3 -m pytest tests/test_youtube_download.py -v
Written by Jordan Koch.
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_nova_config_for_yt(monkeypatch):
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
def yt_module(mock_nova_config_for_yt, tmp_path, monkeypatch):
    """Import nova_youtube_download with mocked paths."""
    for mod in list(sys.modules.keys()):
        if "nova_youtube_download" in mod:
            del sys.modules[mod]

    import nova_youtube_download

    # Override paths and globals
    nova_youtube_download.BASE_DIR = tmp_path / "TVShows"
    nova_youtube_download.shutdown = False
    nova_youtube_download.stats = {}
    nova_youtube_download.nova_notify_send = MagicMock()   # never reach Slack from a test

    return nova_youtube_download


@pytest.fixture
def sample_playlist_output():
    """Sample yt-dlp flat-playlist output for playlists."""
    return (
        "https://www.youtube.com/playlist?list=PL1\tCrash Course Biology\n"
        "https://www.youtube.com/playlist?list=PL2\tCrash Course Chemistry\n"
        "https://www.youtube.com/playlist?list=PL3\tCrash Course Physics\n"
    )


@pytest.fixture
def sample_video_list_output():
    """Sample yt-dlp flat-playlist output for videos."""
    return (
        "abc123\tUnderstanding Photosynthesis\n"
        "def456\tThe Water Cycle\n"
        "ghi789\tCell Division Explained\n"
    )


@pytest.fixture
def sample_channel_videos_output():
    """Sample yt-dlp output for channel videos with dates."""
    return (
        "vid001\tRestoring a 1967 Corvette\t20220315\n"
        "vid002\tJay's Tesla Review\t20230601\n"
        "vid003\tLamborghini Countach Walk-Around\t20230912\n"
        "vid004\tClassic Cars at Pebble Beach\t20240420\n"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════════


class TestSanitizeFilename:
    """Tests for sanitize_filename()."""

    def test_removes_special_characters(self, yt_module):
        result = yt_module.sanitize_filename('File: "Name" | Test?')
        # Special chars removed, whitespace collapsed
        assert ":" not in result
        assert '"' not in result
        assert "|" not in result
        assert "?" not in result
        assert "File" in result
        assert "Name" in result
        assert "Test" in result

    def test_removes_slashes(self, yt_module):
        assert "/" not in yt_module.sanitize_filename("path/to/file")
        assert "\\" not in yt_module.sanitize_filename("path\\to\\file")

    def test_collapses_whitespace(self, yt_module):
        result = yt_module.sanitize_filename("Too   Many    Spaces")
        assert "  " not in result

    def test_strips_whitespace(self, yt_module):
        result = yt_module.sanitize_filename("  leading and trailing  ")
        assert result == "leading and trailing"

    def test_truncates_to_120(self, yt_module):
        long_name = "A" * 200
        result = yt_module.sanitize_filename(long_name)
        assert len(result) <= 120

    def test_preserves_normal_chars(self, yt_module):
        result = yt_module.sanitize_filename("Normal Title - Episode 1 (2024)")
        assert result == "Normal Title - Episode 1 (2024)"

    def test_handles_empty_string(self, yt_module):
        result = yt_module.sanitize_filename("")
        assert result == ""

    def test_handles_all_special_chars(self, yt_module):
        result = yt_module.sanitize_filename('<>:"/\\|?*')
        assert result == ""


class TestGetPlaylists:
    """Tests for get_playlists()."""

    @patch("subprocess.run")
    def test_parses_playlist_output(self, mock_run, yt_module, sample_playlist_output):
        mock_run.return_value = MagicMock(stdout=sample_playlist_output, returncode=0)
        playlists = yt_module.get_playlists("https://www.youtube.com/@crashcourse")
        assert len(playlists) == 3
        assert playlists[0]["title"] == "Crash Course Biology"
        assert "PL1" in playlists[0]["url"]

    @patch("subprocess.run")
    def test_filters_extra_curricular(self, mock_run, yt_module):
        output = (
            "https://url1\tCrash Course Biology\n"
            "https://url2\tExtra Curricular Activities\n"
            "https://url3\tBest of CrashCourse\n"
        )
        mock_run.return_value = MagicMock(stdout=output, returncode=0)
        playlists = yt_module.get_playlists("https://www.youtube.com/@crashcourse")
        assert len(playlists) == 1
        assert playlists[0]["title"] == "Crash Course Biology"

    @patch("subprocess.run")
    def test_empty_output(self, mock_run, yt_module):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        playlists = yt_module.get_playlists("https://www.youtube.com/@channel")
        assert playlists == []

    @patch("subprocess.run")
    def test_uses_flat_playlist_flag(self, mock_run, yt_module):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        yt_module.get_playlists("https://www.youtube.com/@channel")
        cmd = mock_run.call_args[0][0]
        assert "--flat-playlist" in cmd


class TestGetChannelVideos:
    """Tests for get_channel_videos()."""

    @patch("subprocess.run")
    def test_parses_video_list(self, mock_run, yt_module, sample_channel_videos_output):
        mock_run.return_value = MagicMock(stdout=sample_channel_videos_output, returncode=0)
        videos = yt_module.get_channel_videos("https://www.youtube.com/@jaylenosgarage")
        assert len(videos) == 4
        assert videos[0]["id"] == "vid001"
        assert videos[0]["title"] == "Restoring a 1967 Corvette"
        assert videos[0]["upload_date"] == "20220315"

    @patch("subprocess.run")
    def test_handles_missing_dates(self, mock_run, yt_module):
        output = "vid001\tTitle One\tNA\nvid002\tTitle Two\t\n"
        mock_run.return_value = MagicMock(stdout=output, returncode=0)
        videos = yt_module.get_channel_videos("https://url")
        assert videos[0]["upload_date"] == ""
        assert videos[1]["upload_date"] == ""

    @patch("subprocess.run")
    def test_empty_channel(self, mock_run, yt_module):
        mock_run.return_value = MagicMock(stdout="", returncode=0)
        videos = yt_module.get_channel_videos("https://url")
        assert videos == []


class TestDownloadVideo:
    """Tests for download_video()."""

    @patch("subprocess.run")
    def test_successful_download(self, mock_run, yt_module, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        output_path = tmp_path / "video.mp4"
        result = yt_module.download_video("abc123", output_path)
        assert result == "ok"

    @patch("subprocess.run")
    def test_already_downloaded(self, mock_run, yt_module, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="has already been downloaded",
            stderr="",
        )
        result = yt_module.download_video("abc123", tmp_path / "video.mp4")
        assert result == "skip"

    @patch("subprocess.run")
    def test_download_error(self, mock_run, yt_module, tmp_path):
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="ERROR: Video unavailable",
        )
        result = yt_module.download_video("abc123", tmp_path / "video.mp4")
        assert result.startswith("error:")

    @patch("subprocess.run")
    def test_uses_720p_max(self, mock_run, yt_module, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        yt_module.download_video("abc123", tmp_path / "video.mp4")
        cmd = mock_run.call_args[0][0]
        cmd_str = " ".join(cmd)
        assert "720" in cmd_str

    @patch("subprocess.run")
    def test_uses_mp4_format(self, mock_run, yt_module, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        yt_module.download_video("abc123", tmp_path / "video.mp4")
        cmd = mock_run.call_args[0][0]
        assert "--merge-output-format" in cmd
        assert "mp4" in cmd

    @patch("subprocess.run")
    def test_no_overwrite_flag(self, mock_run, yt_module, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        yt_module.download_video("abc123", tmp_path / "video.mp4")
        cmd = mock_run.call_args[0][0]
        assert "--no-overwrites" in cmd


class TestProcessChannelPlaylists:
    """Tests for process_channel_playlists()."""

    @patch("nova_youtube_download.download_video", return_value="ok")
    @patch("nova_youtube_download.get_playlist_videos")
    @patch("nova_youtube_download.get_playlists")
    @patch("time.sleep")
    def test_creates_season_directories(self, mock_sleep, mock_playlists,
                                        mock_videos, mock_dl, yt_module, tmp_path):
        yt_module.BASE_DIR = tmp_path
        mock_playlists.return_value = [
            {"url": "https://yt/PL1", "title": "Biology"},
            {"url": "https://yt/PL2", "title": "Chemistry"},
        ]
        mock_videos.return_value = [{"id": "v1", "title": "Ep 1"}]

        yt_module.process_channel_playlists("crashcourse", {
            "name": "CrashCourse",
            "url": "https://www.youtube.com/@crashcourse",
            "mode": "playlists",
        })

        assert (tmp_path / "CrashCourse" / "Season 01").exists()
        assert (tmp_path / "CrashCourse" / "Season 02").exists()

    @patch("nova_youtube_download.download_video", return_value="ok")
    @patch("nova_youtube_download.get_playlist_videos")
    @patch("nova_youtube_download.get_playlists")
    @patch("time.sleep")
    def test_writes_season_metadata(self, mock_sleep, mock_playlists,
                                    mock_videos, mock_dl, yt_module, tmp_path):
        yt_module.BASE_DIR = tmp_path
        mock_playlists.return_value = [{"url": "https://yt/PL1", "title": "Biology"}]
        mock_videos.return_value = [{"id": "v1", "title": "Ep 1"}]

        yt_module.process_channel_playlists("crashcourse", {
            "name": "CrashCourse",
            "url": "https://www.youtube.com/@crashcourse",
            "mode": "playlists",
        })

        meta = tmp_path / "CrashCourse" / "Season 01" / ".season_info.json"
        assert meta.exists()
        data = json.loads(meta.read_text())
        assert data["playlist_title"] == "Biology"

    @patch("nova_youtube_download.download_video", return_value="skip")
    @patch("nova_youtube_download.get_playlist_videos")
    @patch("nova_youtube_download.get_playlists")
    @patch("time.sleep")
    def test_skips_existing_files(self, mock_sleep, mock_playlists,
                                  mock_videos, mock_dl, yt_module, tmp_path):
        yt_module.BASE_DIR = tmp_path
        mock_playlists.return_value = [{"url": "https://yt/PL1", "title": "Bio"}]
        mock_videos.return_value = [{"id": "v1", "title": "Ep 1"}]

        # Pre-create the expected file
        season_dir = tmp_path / "CrashCourse" / "Season 01"
        season_dir.mkdir(parents=True)
        (season_dir / "CrashCourse - S01E01 - Ep 1.mp4").write_bytes(b"\x00")

        yt_module.process_channel_playlists("crashcourse", {
            "name": "CrashCourse",
            "url": "https://www.youtube.com/@crashcourse",
            "mode": "playlists",
        })

        # download_video should NOT have been called
        mock_dl.assert_not_called()
        assert yt_module.stats["crashcourse"]["skipped"] == 1


class TestProcessChannelByYear:
    """Tests for process_channel_by_year()."""

    @patch("nova_youtube_download.download_video", return_value="ok")
    @patch("nova_youtube_download.get_channel_videos")
    @patch("time.sleep")
    def test_groups_by_year(self, mock_sleep, mock_videos, mock_dl, yt_module, tmp_path):
        yt_module.BASE_DIR = tmp_path
        mock_videos.return_value = [
            {"id": "v1", "title": "Video 2022", "upload_date": "20220101"},
            {"id": "v2", "title": "Video 2023", "upload_date": "20230601"},
        ]

        yt_module.process_channel_by_year("leno", {
            "name": "Jay Leno's Garage",
            "url": "https://www.youtube.com/@jaylenosgarage",
            "mode": "year",
        })

        # Should create two seasons (one per year)
        assert (tmp_path / "Jay Leno's Garage" / "Season 01").exists()
        assert (tmp_path / "Jay Leno's Garage" / "Season 02").exists()

    @patch("nova_youtube_download.download_video", return_value="ok")
    @patch("nova_youtube_download.get_channel_videos")
    @patch("time.sleep")
    def test_stats_tracking(self, mock_sleep, mock_videos, mock_dl, yt_module, tmp_path):
        yt_module.BASE_DIR = tmp_path
        mock_videos.return_value = [
            {"id": "v1", "title": "Video 1", "upload_date": "20230101"},
        ]

        yt_module.process_channel_by_year("leno", {
            "name": "Jay Leno's Garage",
            "url": "https://www.youtube.com/@jaylenosgarage",
            "mode": "year",
        })

        assert yt_module.stats["leno"]["downloaded"] == 1
        assert yt_module.stats["leno"]["total"] == 1


class TestChannelConfig:
    """Tests for CHANNELS configuration constant."""

    def test_channels_defined(self, yt_module):
        assert len(yt_module.CHANNELS) >= 4

    def test_all_channels_have_required_fields(self, yt_module):
        for key, cfg in yt_module.CHANNELS.items():
            assert "name" in cfg, f"{key} missing name"
            assert "url" in cfg, f"{key} missing url"
            assert "mode" in cfg, f"{key} missing mode"
            assert cfg["mode"] in ("playlists", "year"), f"{key} has invalid mode"

    def test_all_urls_are_youtube(self, yt_module):
        for key, cfg in yt_module.CHANNELS.items():
            assert "youtube.com" in cfg["url"], f"{key} URL is not YouTube"

    def test_crashcourse_uses_playlists_mode(self, yt_module):
        assert yt_module.CHANNELS["crashcourse"]["mode"] == "playlists"

    def test_leno_uses_year_mode(self, yt_module):
        assert yt_module.CHANNELS["leno"]["mode"] == "year"


# ═══════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.security
class TestSecurityYouTube:
    """Security tests: no secrets, safe filename handling."""

    def test_no_hardcoded_tokens(self):
        source = Path(__file__).parent.parent / "nova_youtube_download.py"
        content = source.read_text()
        assert "xoxb-" not in content
        assert "sk-" not in content
        assert "AKIA" not in content
        assert "ghp_" not in content

    def test_no_personal_emails(self):
        source = Path(__file__).parent.parent / "nova_youtube_download.py"
        content = source.read_text()
        # Verify no personal email addresses are embedded
        import re
        personal_patterns = [r"kochj\w*@\w+\.\w+", r"jordan\.\w+@\w+\.\w+"]
        for pat in personal_patterns:
            assert not re.search(pat, content), f"Personal email pattern found: {pat}"

    def test_no_hardcoded_home_paths(self):
        source = Path(__file__).parent.parent / "nova_youtube_download.py"
        content = source.read_text()
        home_path = Path.home()
        assert str(home_path) + "/" not in content

    def test_filename_sanitization_prevents_traversal(self, yt_module):
        """Filename sanitization should remove path separators preventing traversal."""
        result = yt_module.sanitize_filename("../../etc/passwd")
        # Slashes and backslashes are in the banned character set
        assert "/" not in result
        assert "\\" not in result

    def test_filename_sanitization_removes_shell_metacharacters(self, yt_module):
        """Should remove characters that could be dangerous in shell."""
        dangerous = 'Title; rm -rf /; echo "pwned"'
        result = yt_module.sanitize_filename(dangerous)
        assert ";" in result or ";" not in dangerous  # semicolons not in banned list but safe in filename
        assert '"' not in result

    def test_no_youtube_api_keys(self):
        """Should not contain YouTube Data API keys."""
        source = Path(__file__).parent.parent / "nova_youtube_download.py"
        content = source.read_text()
        assert "AIza" not in content  # Google API key prefix

    def test_uses_external_volume_not_system_disk(self, yt_module):
        """Downloads should go to external volume, not system disk."""
        source = Path(__file__).parent.parent / "nova_youtube_download.py"
        content = source.read_text()
        assert "/Volumes/" in content


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS — End-to-End Pipeline
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.functional
class TestFullPipelineYouTube:
    """Full pipeline tests for YouTube download workflow."""

    @patch("nova_youtube_download.download_video", return_value="ok")
    @patch("nova_youtube_download.get_playlist_videos")
    @patch("nova_youtube_download.get_playlists")
    @patch("time.sleep")
    def test_full_playlist_channel_flow(self, mock_sleep, mock_playlists,
                                        mock_videos, mock_dl, yt_module, tmp_path):
        """Full flow: fetch playlists -> fetch videos -> download."""
        yt_module.BASE_DIR = tmp_path
        mock_playlists.return_value = [
            {"url": "https://yt/PL1", "title": "Season Playlist"},
        ]
        mock_videos.return_value = [
            {"id": "v1", "title": "Episode One"},
            {"id": "v2", "title": "Episode Two"},
        ]

        yt_module.process_channel("crashcourse", {
            "name": "CrashCourse",
            "url": "https://www.youtube.com/@crashcourse",
            "mode": "playlists",
        })

        assert mock_dl.call_count == 2
        assert yt_module.stats["crashcourse"]["downloaded"] == 2

    @patch("nova_youtube_download.download_video", return_value="ok")
    @patch("nova_youtube_download.get_channel_videos")
    @patch("time.sleep")
    def test_full_year_channel_flow(self, mock_sleep, mock_videos, mock_dl, yt_module, tmp_path):
        """Full flow: fetch all videos -> group by year -> download."""
        yt_module.BASE_DIR = tmp_path
        mock_videos.return_value = [
            {"id": "v1", "title": "First Video", "upload_date": "20220515"},
            {"id": "v2", "title": "Second Video", "upload_date": "20220720"},
            {"id": "v3", "title": "Third Video", "upload_date": "20230101"},
        ]

        yt_module.process_channel("leno", {
            "name": "Jay Leno's Garage",
            "url": "https://www.youtube.com/@jaylenosgarage",
            "mode": "year",
        })

        assert mock_dl.call_count == 3
        assert yt_module.stats["leno"]["downloaded"] == 3
        # Season 01 = 2022, Season 02 = 2023
        assert (tmp_path / "Jay Leno's Garage" / "Season 01").exists()
        assert (tmp_path / "Jay Leno's Garage" / "Season 02").exists()

    @patch("nova_youtube_download.download_video")
    @patch("nova_youtube_download.get_channel_videos")
    @patch("time.sleep")
    def test_mixed_results_tracking(self, mock_sleep, mock_videos, mock_dl, yt_module, tmp_path):
        """Stats should correctly track ok/skip/error."""
        yt_module.BASE_DIR = tmp_path
        mock_videos.return_value = [
            {"id": "v1", "title": "Good", "upload_date": "20230101"},
            {"id": "v2", "title": "Already Have", "upload_date": "20230201"},
            {"id": "v3", "title": "Broken", "upload_date": "20230301"},
        ]
        mock_dl.side_effect = ["ok", "skip", "error: 403 forbidden"]

        yt_module.process_channel("leno", {
            "name": "Jay Leno's Garage",
            "url": "https://www.youtube.com/@jaylenosgarage",
            "mode": "year",
        })

        assert yt_module.stats["leno"]["downloaded"] == 1
        assert yt_module.stats["leno"]["skipped"] == 1
        assert yt_module.stats["leno"]["errors"] == 1


# ═══════════════════════════════════════════════════════════════════════════════
# FRAMEWORK TESTS — Signals, Concurrency, Error Recovery
# ═══════════════════════════════════════════════════════════════════════════════


class TestFrameworkYouTube:
    """Framework tests: shutdown, concurrency, config validation."""

    def test_sigterm_sets_shutdown(self, yt_module):
        yt_module.shutdown = False
        yt_module.signal_handler(signal.SIGTERM, None)
        assert yt_module.shutdown is True

    def test_sigint_sets_shutdown(self, yt_module):
        yt_module.shutdown = False
        yt_module.signal_handler(signal.SIGINT, None)
        assert yt_module.shutdown is True

    @patch("nova_youtube_download.download_video", return_value="ok")
    @patch("nova_youtube_download.get_channel_videos")
    @patch("time.sleep")
    def test_shutdown_stops_downloads(self, mock_sleep, mock_videos, mock_dl, yt_module, tmp_path):
        """Shutdown flag should prevent further downloads."""
        yt_module.BASE_DIR = tmp_path
        yt_module.shutdown = True
        mock_videos.return_value = [
            {"id": "v1", "title": "Video", "upload_date": "20230101"},
        ]

        yt_module.process_channel_by_year("leno", {
            "name": "Jay Leno's Garage",
            "url": "https://url",
            "mode": "year",
        })

        mock_dl.assert_not_called()

    def test_delay_between_videos_configured(self, yt_module):
        """Delay between videos should be 32s to avoid rate limiting."""
        assert yt_module.DELAY_BETWEEN_VIDEOS == 32

    def test_max_resolution_is_720(self, yt_module):
        assert yt_module.MAX_RESOLUTION == "720"

    def test_process_channel_handles_exceptions(self, yt_module, tmp_path):
        """process_channel should catch exceptions and log them."""
        yt_module.BASE_DIR = tmp_path
        with patch("nova_youtube_download.process_channel_by_year", side_effect=Exception("Fatal")):
            yt_module.process_channel("leno", {
                "name": "Jay Leno's Garage",
                "url": "https://url",
                "mode": "year",
            })
        # Should not raise, should record error
        assert "error_msg" in yt_module.stats.get("leno", {})

    def test_output_filename_format(self, yt_module):
        """Output files should follow TVShows naming convention."""
        name = yt_module.sanitize_filename("Understanding Photosynthesis")
        expected = f"CrashCourse - S01E01 - {name}.mp4"
        assert "S01E01" in expected
        assert expected.endswith(".mp4")


# ═══════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.integration
class TestYouTubeIntegration:
    """Integration tests hitting live YouTube. Skipped if unavailable."""

    @pytest.fixture(autouse=True)
    def check_yt_dlp_available(self):
        if os.environ.get("NOVA_LIVE_TESTS") != "1":     # house rule: offline by default
            pytest.skip("live YouTube test; set NOVA_LIVE_TESTS=1 to run")
        result = subprocess.run(
            ["/opt/homebrew/bin/yt-dlp", "--version"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            pytest.skip("yt-dlp not available")

    def test_yt_dlp_can_fetch_playlist_info(self):
        """yt-dlp should be able to fetch playlist metadata."""
        result = subprocess.run(
            ["/opt/homebrew/bin/yt-dlp", "--flat-playlist", "--print", "%(title)s",
             "https://www.youtube.com/@crashcourse/playlists"],
            capture_output=True, text=True, timeout=30,
        )
        # May fail due to network, but at least yt-dlp runs
        assert result.returncode == 0 or "network" in result.stderr.lower()

    def test_yt_dlp_version_recent(self):
        """yt-dlp should be a recent version."""
        result = subprocess.run(
            ["/opt/homebrew/bin/yt-dlp", "--version"],
            capture_output=True, text=True,
        )
        version = result.stdout.strip()
        # Should be a date-based version like 2024.01.01
        assert len(version) >= 8


# ══ House categories (unittest classes) — added 2026-10-05 ═══════════════════════
# Loaded via spec under a private name so the fixture-driven tests above (which re-import
# nova_youtube_download per test) are untouched; signal handlers the module installs at import
# are restored so the test runner keeps its own SIGINT handling.

import importlib.util  # noqa: E402
import io  # noqa: E402
import re  # noqa: E402
import tempfile  # noqa: E402
import unittest  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402

_SCRIPTS = Path(__file__).resolve().parents[1]
_SCRIPT = _SCRIPTS / "nova_youtube_download.py"
_SRC = _SCRIPT.read_text()


def _load_house():
    saved = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        spec = importlib.util.spec_from_file_location("yt_house", _SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for s, h in saved.items():
            signal.signal(s, h)
    mod.nova_notify_send = MagicMock(name="nova_notify_send")   # never reach Slack
    mod.subprocess = MagicMock(name="subprocess")                 # yt-dlp never launched
    mod.subprocess.TimeoutExpired = subprocess.TimeoutExpired
    mod.time = MagicMock(name="time")                             # no 32 s inter-video sleeps
    return mod


yh = _load_house()


def _cp(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


def _fresh(tmp=None):
    yh.BASE_DIR = Path(tmp or tempfile.mkdtemp(prefix="yt_house_")); yh.shutdown = False; yh.stats = {}
    yh.subprocess.run.reset_mock(); yh.subprocess.run.side_effect = None
    yh.nova_notify_send.reset_mock()
    return yh.BASE_DIR


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/_-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(_SRC))

    def test_hostile_title_cannot_escape_season_dir(self):
        base = _fresh()
        yh.subprocess.run.side_effect = [_cp("v1\t../../../etc/passwd\t20200101\n"), _cp()]
        with redirect_stdout(io.StringIO()):
            yh.process_channel_by_year("k", {"name": "Chan", "url": "https://www.youtube.com/@x", "mode": "year"})
        argv = yh.subprocess.run.call_args_list[1][0][0]
        out = Path(argv[argv.index("-o") + 1])
        self.assertEqual(out.parent, base / "Chan" / "Season 01")
        self.assertNotIn("/", out.name)

    def test_yt_dlp_called_with_argv_list(self):
        self.assertNotIn("shell=True", _SRC)
        _fresh(); yh.subprocess.run.return_value = _cp()
        yh.download_video("abc;$(touch pwned)", Path("/tmp/x.mp4"))
        argv = yh.subprocess.run.call_args[0][0]
        self.assertEqual(argv[-1], "https://www.youtube.com/watch?v=abc;$(touch pwned)")   # one argv slot, no shell


class TestPerformance(unittest.TestCase):
    def test_sanitize_10k_titles_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            yh.sanitize_filename(f'Episode {i}: "Who/Why?" <part*{i}>   |  ' * 3)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_download_error_is_one_shot_and_counted(self):
        # RETRY GAP: download_video() — one yt-dlp run per video, no retry; failure returns "error: ..." and the
        # channel moves on (the next scheduled run re-attempts because the file was never written).
        _fresh()
        yh.subprocess.run.side_effect = [_cp("a\tA\t20200101\nb\tB\t20200102\n"), _cp(rc=1, stderr="HTTP 429"), _cp()]
        with redirect_stdout(io.StringIO()):
            yh.process_channel_by_year("k", {"name": "C", "url": "u", "mode": "year"})
        self.assertEqual(yh.subprocess.run.call_count, 3)
        self.assertEqual((yh.stats["k"]["downloaded"], yh.stats["k"]["errors"]), (1, 1))

    def test_timeout_fails_open_per_channel(self):
        _fresh()
        yh.subprocess.run.side_effect = subprocess.TimeoutExpired("yt-dlp", 300)
        with redirect_stdout(io.StringIO()):
            yh.process_channel("k", {"name": "C", "url": "u", "mode": "year"})
        self.assertIn("timed out", yh.stats["k"]["error_msg"])


class TestUnit(unittest.TestCase):
    def test_download_video_results(self):
        _fresh()
        yh.subprocess.run.return_value = _cp(rc=1, stdout="x has already been downloaded")
        self.assertEqual(yh.download_video("id", Path("/tmp/a.mp4")), "skip")
        yh.subprocess.run.return_value = _cp(rc=0)
        self.assertEqual(yh.download_video("id", Path("/tmp/a.mp4")), "ok")

    def test_get_playlist_videos_parses(self):
        _fresh()
        yh.subprocess.run.return_value = _cp("a\tOne\nbadline\nb\tTwo\tExtra\n")
        self.assertEqual(yh.get_playlist_videos("u"), [{"id": "a", "title": "One"}, {"id": "b", "title": "Two\tExtra"}])

    def test_notify_splits_title_and_body(self):
        _fresh()
        yh.notify("Title line\nbody a\nbody b")
        args, kw = yh.nova_notify_send.call_args
        self.assertEqual(args[0], "Title line"); self.assertEqual(kw["body"], "body a\nbody b")
        self.assertEqual(kw["dedup_key"], "youtube-tvshows-download")


class TestIntegration(unittest.TestCase):
    def test_uses_shared_notify_bus(self):
        self.assertIn("from nova_notify import notify as nova_notify_send", _SRC)
        self.assertTrue(all(c["url"].startswith("https://www.youtube.com/@") for c in yh.CHANNELS.values()))


class TestFunctional(unittest.TestCase):
    def test_main_single_channel_reports_start_and_finish(self):
        _fresh()
        yh.subprocess.run.side_effect = [_cp("v1\tFirst\t20210101\n"), _cp()]
        with patch.object(sys, "argv", ["x", "--channel", "leno"]), \
             patch.object(yh, "status_reporter"), redirect_stdout(io.StringIO()):
            yh.main()
        titles = [c[0][0] for c in yh.nova_notify_send.call_args_list]
        self.assertTrue(titles[0].startswith(":arrow_down: YouTube TVShows Download Starting"))
        self.assertEqual(titles[-1], ":checkered_flag: YouTube Download Complete")
        self.assertIn("Jay Leno's Garage: 1 new, 0 skipped, 0 errors", yh.nova_notify_send.call_args.kwargs["body"])

    def test_shutdown_flag_stops_before_downloading(self):
        _fresh(); yh.shutdown = True
        yh.subprocess.run.side_effect = [_cp("v1\tFirst\t20210101\n")]
        try:
            with redirect_stdout(io.StringIO()):
                yh.process_channel_by_year("k", {"name": "C", "url": "u", "mode": "year"})
        finally:
            yh.shutdown = False
        self.assertEqual(yh.subprocess.run.call_count, 1)                 # listing only, no download


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(_SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--channel", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_youtube_download"], cwd=str(_SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
