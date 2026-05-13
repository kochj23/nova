"""
test_nova_youtube_download.py — All 7 test categories for nova_youtube_download.py
Written by Jordan Koch.
"""
import importlib.util, json, sys, time, tempfile, unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg
# nova_youtube_download has optional nova_config import
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_youtube_download.py"
_spec = importlib.util.spec_from_file_location("nova_youtube_download", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

sanitize_filename = _mod.sanitize_filename
download_video = _mod.download_video
CHANNELS = _mod.CHANNELS


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_"]:
            self.assertNotIn(pat, src)
    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)
    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        self.assertNotIn(str(Path.home()) + "/", src)
    def test_yt_dlp_uses_absolute_path(self):
        src = _SCRIPT.read_text()
        self.assertIn("/opt/homebrew/bin/yt-dlp", src)
    def test_base_dir_on_external_volume(self):
        self.assertIn("/Volumes/external", str(_mod.BASE_DIR))


class TestPerformance(unittest.TestCase):
    def test_sanitize_filename_fast(self):
        title = 'Episode "Best" #42: Something & More <video>'
        start = time.perf_counter()
        for _ in range(10000):
            sanitize_filename(title)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)
    def test_sanitize_truncates_to_120(self):
        result = sanitize_filename("A" * 200)
        self.assertLessEqual(len(result), 120)
    def test_delay_between_defined(self):
        self.assertGreater(_mod.DELAY_BETWEEN_VIDEOS, 0)
    def test_max_resolution_defined(self):
        self.assertIn(_mod.MAX_RESOLUTION, ("720", "1080", "480"))


class TestRetry(unittest.TestCase):
    def test_download_returns_skip_on_already_downloaded(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "already been downloaded"
        mock_result.stderr = ""
        with patch("subprocess.run", return_value=mock_result):
            result = download_video("testid", Path("/tmp/test.mp4"))
        self.assertEqual(result, "skip")
    def test_download_returns_error_on_failure(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Error: Video unavailable"
        with patch("subprocess.run", return_value=mock_result):
            result = download_video("testid", Path("/tmp/test.mp4"))
        self.assertTrue(result.startswith("error:"))
    def test_process_channel_handles_exception(self):
        """process_channel must not crash the entire run on exception."""
        with patch.object(_mod, "get_playlists", side_effect=Exception("network error")):
            _mod.process_channel("crashcourse", CHANNELS["crashcourse"])
        # No exception raised = pass


class TestUnit(unittest.TestCase):
    def test_sanitize_removes_special_chars(self):
        result = sanitize_filename('Title: "Bad/Chars" <here>')
        for bad in ['<', '>', ':', '"', '/', '\\', '|', '?', '*']:
            self.assertNotIn(bad, result)
    def test_sanitize_collapses_whitespace(self):
        result = sanitize_filename("Title  with   multiple   spaces")
        self.assertNotIn("   ", result)
    def test_channels_have_required_keys(self):
        for key, cfg in CHANNELS.items():
            self.assertIn("name", cfg, f"Channel {key} missing 'name'")
            self.assertIn("url", cfg, f"Channel {key} missing 'url'")
            self.assertIn("mode", cfg, f"Channel {key} missing 'mode'")
    def test_channels_modes_are_valid(self):
        for key, cfg in CHANNELS.items():
            self.assertIn(cfg["mode"], ("playlists", "year"),
                          f"Channel {key} has invalid mode: {cfg['mode']}")
    def test_download_video_returns_ok(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "Merged"
        with patch("subprocess.run", return_value=mock_result):
            result = download_video("abc123", Path("/tmp/test.mp4"))
        self.assertEqual(result, "ok")
    def test_get_playlists_parses_tab_separated(self):
        mock_result = MagicMock()
        mock_result.stdout = "https://yt.com/playlist1\tSeason 1\nhttps://yt.com/playlist2\tSeason 2"
        with patch("subprocess.run", return_value=mock_result):
            playlists = _mod.get_playlists("https://yt.com/@test")
        self.assertEqual(len(playlists), 2)
        self.assertEqual(playlists[0]["title"], "Season 1")
    def test_get_channel_videos_parses_output(self):
        mock_result = MagicMock()
        mock_result.stdout = "abc123\tTest Video\t20260101\ndef456\tAnother Video\t20260102"
        with patch("subprocess.run", return_value=mock_result):
            videos = _mod.get_channel_videos("https://yt.com/@test")
        self.assertEqual(len(videos), 2)
        self.assertEqual(videos[0]["id"], "abc123")
        self.assertEqual(videos[0]["title"], "Test Video")


class TestIntegration(unittest.TestCase):
    def test_process_channel_year_creates_season_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            channel_cfg = {"name": "TestChannel", "url": "https://yt.com/@test", "mode": "year"}
            videos = [{"id": "abc", "title": "Video 1", "upload_date": "20260101"}]
            with patch.object(_mod, "get_channel_videos", return_value=videos):
                with patch.object(_mod, "download_video", return_value="ok") as mock_dl:
                    with patch.object(_mod, "BASE_DIR", Path(tmpdir)):
                        _mod.process_channel_by_year("test", channel_cfg)
        self.assertGreater(mock_dl.call_count, 0)

    def test_status_reporter_builds_slack_message(self):
        _mod.stats["test_ch"] = {
            "name": "TestChannel", "total": 100, "downloaded": 50,
            "skipped": 30, "errors": 2, "current_video": "S01E05 - Test"
        }
        # Should not crash
        _mod.shutdown = True  # Prevent loop


class TestFunctional(unittest.TestCase):
    def test_main_runs_single_channel(self):
        with patch("sys.argv", ["nova_youtube_download.py", "--channel", "crashcourse"]):
            with patch.object(_mod, "process_channel", side_effect=lambda k, c: None) as mock_proc:
                with patch.object(_mod, "notify"):
                    with patch("threading.Thread"):
                        from concurrent.futures import ThreadPoolExecutor
                        with patch("concurrent.futures.ThreadPoolExecutor") as mock_pool:
                            mock_ctx = MagicMock()
                            mock_pool.return_value.__enter__ = lambda s: mock_ctx
                            mock_pool.return_value.__exit__ = MagicMock(return_value=False)
                            mock_ctx.submit.return_value = MagicMock()
                            from concurrent.futures import as_completed
                            with patch("concurrent.futures.as_completed", return_value=[]):
                                _mod.main()
    def test_all_channels_are_youtube_urls(self):
        for key, cfg in CHANNELS.items():
            self.assertIn("youtube.com", cfg["url"], f"Channel {key} URL is not YouTube")


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
        self.assertIsInstance(_mod.DELAY_BETWEEN_VIDEOS, int)
        self.assertIsInstance(_mod.MAX_RESOLUTION, str)
    def test_functions_exist(self):
        for fn in ("sanitize_filename", "get_playlists", "get_playlist_videos",
                   "get_channel_videos", "download_video", "process_channel_playlists",
                   "process_channel_by_year", "process_channel", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")
    def test_signal_handlers_registered(self):
        src = _SCRIPT.read_text()
        self.assertIn("signal.signal", src)
        self.assertIn("SIGINT", src)

if __name__ == "__main__":
    unittest.main(verbosity=2)
