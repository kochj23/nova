"""
test_nova_yt_new_episodes.py — All 7 test categories for nova_yt_new_episodes.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import re
import sys
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Stub deps
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_registry = MagicMock()
sys.modules["nova_media_registry"] = _registry

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_yt_new_episodes.py"
_spec = importlib.util.spec_from_file_location("nova_yt_new_episodes", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

sanitize = _mod.sanitize
normalize = _mod.normalize
is_on_disk = _mod.is_on_disk
next_episode_single = _mod.next_episode_single
disk_titles = _mod.disk_titles


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_", "Jkoogie"]:
            self.assertNotIn(pat, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_cookies_file_permissions_set_600(self):
        """Cookies file must be chmod 600 after refresh."""
        src = _SCRIPT.read_text()
        self.assertIn("0o600", src, "Cookie file must be set to 0o600")

    def test_yt_dlp_uses_absolute_path(self):
        src = _SCRIPT.read_text()
        self.assertIn("/opt/homebrew/bin/yt-dlp", src)

    def test_cookies_stored_in_home_cache(self):
        self.assertIn(str(Path.home()), str(_mod.YT_COOKIES_FILE))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_sanitize_fast_on_long_titles(self):
        title = "A" * 500 + "<>:\"\\|?*"
        start = time.perf_counter()
        for _ in range(10000):
            sanitize(title)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)

    def test_sanitize_truncates_to_120(self):
        result = sanitize("A" * 200)
        self.assertLessEqual(len(result), 120)

    def test_normalize_fast(self):
        start = time.perf_counter()
        for _ in range(10000):
            normalize("Jeopardy! - Best Moments #123")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_delay_between_downloads_defined(self):
        self.assertGreater(_mod.DELAY_BETWEEN, 0)

    def test_recent_videos_check_count(self):
        self.assertGreater(_mod.RECENT_VIDEOS_CHECK, 0)
        self.assertLessEqual(_mod.RECENT_VIDEOS_CHECK, 50)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_download_video_returns_skip_on_already_downloaded(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "already been downloaded"
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result):
            with patch.object(_mod, "_cookies_args", return_value=[]):
                result = _mod.download_video("dQw4w9WgXcQ", Path("/tmp/test.mp4"))
        self.assertEqual(result, "skip")

    def test_download_video_returns_error_on_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "HTTP Error 403: Forbidden"
        with patch("subprocess.run", return_value=mock_result):
            with patch.object(_mod, "_cookies_args", return_value=[]):
                result = _mod.download_video("testid", Path("/tmp/test.mp4"))
        self.assertTrue(result.startswith("error:"))

    def test_cookies_args_falls_back_if_file_missing(self):
        """_cookies_args must attempt to refresh if file missing."""
        with patch.object(_mod, "YT_COOKIES_FILE", Path("/nonexistent/cookies.txt")):
            with patch.object(_mod, "_refresh_cookies_from_browser", return_value=False) as mock_refresh:
                args = _mod._cookies_args()
        mock_refresh.assert_called_once()

    def test_get_recent_videos_returns_empty_on_subprocess_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        with patch("subprocess.run", return_value=mock_result):
            result = _mod.get_recent_videos("https://youtube.com/@test")
        self.assertEqual(result, [])


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_sanitize_removes_special_chars(self):
        result = sanitize('Title: "Bad/Chars" <here>')
        for bad in ['<', '>', ':', '"', '/', '\\', '|', '?', '*']:
            self.assertNotIn(bad, result)

    def test_sanitize_collapses_whitespace(self):
        result = sanitize("Title   with   spaces")
        self.assertNotIn("   ", result)

    def test_normalize_lowercases(self):
        result = normalize("Jeopardy!")
        self.assertEqual(result, normalize("jeopardy!"))

    def test_normalize_strips_punctuation(self):
        result = normalize("test: value!")
        self.assertNotIn(":", result)
        self.assertNotIn("!", result)

    def test_is_on_disk_exact_match(self):
        on_disk = {"jeopardy best moments", "wheel of fortune classic"}
        self.assertTrue(is_on_disk("Jeopardy! Best Moments", on_disk))

    def test_is_on_disk_fuzzy_match(self):
        on_disk = {"the best jeopardy answers ever recorded"}
        self.assertTrue(is_on_disk("Best Jeopardy Answers", on_disk))

    def test_is_on_disk_no_match(self):
        on_disk = {"wheel of fortune classic"}
        self.assertFalse(is_on_disk("Something Completely Different", on_disk))

    def test_next_episode_single_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sn, ep = next_episode_single(Path(tmpdir))
        self.assertEqual(sn, 1)
        self.assertEqual(ep, 1)  # 0 + 1

    def test_next_episode_single_increments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ep_file = Path(tmpdir) / "Season 01" / "Show - S01E005 - Title.mp4"
            ep_file.parent.mkdir()
            ep_file.touch()
            sn, ep = next_episode_single(Path(tmpdir))
        self.assertEqual(sn, 1)
        self.assertEqual(ep, 6)

    def test_disk_titles_extracts_stems(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            season_dir = Path(tmpdir) / "Season 01"
            season_dir.mkdir()
            (season_dir / "Show - S01E001 - Great Episode.mp4").touch()
            titles = disk_titles(Path(tmpdir))
        self.assertTrue(len(titles) > 0)
        self.assertTrue(any("great episode" in t for t in titles))

    def test_channels_registry_not_empty(self):
        self.assertGreater(len(_mod.CHANNELS), 10)

    def test_all_channels_have_required_fields(self):
        for key, cfg in _mod.CHANNELS.items():
            self.assertIn("name", cfg, f"Channel {key} missing 'name'")
            self.assertIn("url", cfg, f"Channel {key} missing 'url'")
            self.assertIn("mode", cfg, f"Channel {key} missing 'mode'")
            self.assertIn(cfg["mode"], ("single", "year", "playlists"),
                          f"Channel {key} has invalid mode: {cfg['mode']}")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_download_one_success_calls_registry(self):
        """_download_one must register file in media registry on success."""
        with tempfile.TemporaryDirectory() as tmpdir:
            season_dir = Path(tmpdir)
            video = {"id": "abc123", "title": "Test Episode", "upload_date": "20260101"}
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = ""
            mock_result.stderr = ""
            with patch("subprocess.run", return_value=mock_result):
                with patch.object(_mod, "_cookies_args", return_value=[]):
                    with patch.object(_mod, "BASE_DIR", Path(tmpdir)):
                        results = []
                        _mod._download_one(video, "TestShow", 1, 1, season_dir, results)
        _registry.register_file.assert_called()
        _registry.mark_status.assert_called()

    def test_process_channel_up_to_date(self):
        """process_channel must do nothing if all videos are on disk."""
        with tempfile.TemporaryDirectory() as tmpdir:
            show_dir = Path(tmpdir) / "TestShow"
            show_dir.mkdir()
            # Fake video already on disk
            season = show_dir / "Season 01"
            season.mkdir()
            ep = season / "TestShow - S01E0001 - Existing Video.mp4"
            ep.touch()

            videos = [{"id": "abc", "title": "Existing Video", "upload_date": "20260101"}]
            with patch.object(_mod, "get_recent_videos", return_value=videos):
                with patch.object(_mod, "BASE_DIR", Path(tmpdir)):
                    results = []
                    _mod.process_channel("test", {"name": "TestShow", "url": "https://yt.com/@test", "mode": "single"}, results)
        # No downloads since it's already on disk
        self.assertEqual(len(results), 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_scan_new_recordings_skips_small_files(self):
        """scan_new_recordings must skip files < 5MB."""
        with tempfile.TemporaryDirectory() as tmpdir:
            small_file = Path(tmpdir) / "tiny.mp4"
            small_file.write_bytes(b"x" * 100)  # tiny
            with patch.object(_mod, "VIDEO_ROOT", Path(tmpdir)):
                with patch.object(_mod, "BASE_DIR", Path(tmpdir) / "TVShows"):
                    results = _mod.scan_new_recordings(since_days=30)
        self.assertEqual(len(results), 0)

    def test_scan_new_recordings_skips_yt_managed_files(self):
        """scan_new_recordings must skip files in BASE_DIR (YT-managed)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tv_shows = Path(tmpdir) / "TVShows"
            tv_shows.mkdir()
            yt_ep = tv_shows / "TestShow - S01E001 - Test.mp4"
            yt_ep.write_bytes(b"x" * 10_000_000)  # 10MB
            with patch.object(_mod, "VIDEO_ROOT", Path(tmpdir)):
                with patch.object(_mod, "BASE_DIR", tv_shows):
                    results = _mod.scan_new_recordings(since_days=30)
        # Should be empty — YT-managed file excluded
        self.assertEqual(len(results), 0)

    def test_video_exts_set(self):
        self.assertIn(".mp4", _mod.VIDEO_EXTS)
        self.assertIn(".mkv", _mod.VIDEO_EXTS)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_constants_defined(self):
        self.assertIsInstance(_mod.BASE_DIR, Path)
        self.assertIsInstance(_mod.YT_DLP, str)
        self.assertIsInstance(_mod.CHANNELS, dict)
        self.assertIsInstance(_mod.DELAY_BETWEEN, int)
        self.assertIsInstance(_mod.RECENT_VIDEOS_CHECK, int)
        self.assertIsInstance(_mod.VIDEO_EXTS, set)

    def test_functions_exist(self):
        for fn in ("sanitize", "normalize", "disk_titles", "is_on_disk",
                   "get_recent_videos", "get_playlists", "get_playlist_videos",
                   "next_episode_single", "next_episode_year", "next_episode_playlist",
                   "download_video", "process_channel", "_download_one",
                   "scan_new_recordings", "sync_subscriptions", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")

    def test_channels_have_youtube_urls(self):
        for key, cfg in _mod.CHANNELS.items():
            self.assertIn("youtube.com", cfg["url"],
                          f"Channel {key} URL doesn't look like YouTube: {cfg['url']}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
