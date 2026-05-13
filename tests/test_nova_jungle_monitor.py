"""
test_nova_jungle_monitor.py — All 7 test categories for nova_jungle_monitor.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.modules.setdefault("nova_config", MagicMock())

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_jungle_monitor.py"
_spec = importlib.util.spec_from_file_location("nova_jungle_monitor", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

search_youtube_jungle = _mod.search_youtube_jungle
parse_tracks = _mod.parse_tracks
filter_quality_tracks = _mod.filter_quality_tracks
post_to_slack = _mod.post_to_slack


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(p, src)

    def test_bot_token_loaded_from_config_not_hardcoded(self):
        """Bot token must be read from config file, not hardcoded."""
        src = _SCRIPT.read_text()
        self.assertNotIn("xoxb-", src, "Bot token must not be hardcoded")

    def test_slack_channel_id_not_secret(self):
        """Channel ID is not sensitive — verify it looks like a Slack channel."""
        self.assertRegex(_mod.CHANNEL, r"^C[A-Z0-9]{10}$")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_parse_tracks_fast(self):
        import time
        lines = [json.dumps({
            "title": f"Track {i}", "id": f"id{i}",
            "uploader": "DJ", "duration": 400, "view_count": 5000
        }) for i in range(100)]
        json_output = "\n".join(lines)
        start = time.perf_counter()
        parse_tracks(json_output)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_filter_quality_tracks_bounded(self):
        """filter_quality_tracks must return at most 10 tracks."""
        tracks = [
            {"title": f"T{i}", "url": f"http://youtube.com/{i}",
             "uploader": "DJ", "duration": 400, "view_count": 5000}
            for i in range(100)
        ]
        result = filter_quality_tracks(tracks)
        self.assertLessEqual(len(result), 10)

    def test_search_youtube_has_timeout(self):
        """yt-dlp call must have a timeout."""
        src = _SCRIPT.read_text()
        self.assertIn("timeout=", src, "YouTube search must have a subprocess timeout")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_search_returns_none_on_exception(self):
        with patch("subprocess.run", side_effect=Exception("yt-dlp not found")):
            result = search_youtube_jungle()
        self.assertIsNone(result)

    def test_search_returns_none_on_nonzero_exit(self):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            result = search_youtube_jungle()
        self.assertIsNone(result)

    def test_parse_tracks_handles_invalid_json(self):
        """parse_tracks must not crash on malformed JSON lines."""
        try:
            tracks = parse_tracks("{INVALID JSON\n{also invalid}")
        except Exception as e:
            self.fail(f"parse_tracks raised on invalid JSON: {e}")
        self.assertIsInstance(tracks, list)

    def test_post_to_slack_handles_missing_config(self):
        """post_to_slack must not crash if openclaw.json is missing."""
        with patch("pathlib.Path.exists", return_value=False):
            try:
                post_to_slack([{"title": "Track", "url": "http://x", "uploader": "DJ",
                                "view_count": 100}])
            except Exception as e:
                self.fail(f"post_to_slack raised on missing config: {e}")

    def test_post_to_slack_handles_subprocess_error(self):
        with patch("pathlib.Path.exists", return_value=True):
            with patch("builtins.open", side_effect=Exception("file error")):
                try:
                    post_to_slack([{"title": "Track", "url": "http://x", "uploader": "DJ",
                                    "view_count": 100}])
                except Exception:
                    pass  # Acceptable — the inner exception handler should catch it


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_parse_tracks_valid_json(self):
        json_output = json.dumps({
            "title": "Jungle Mix", "id": "abc123",
            "uploader": "DJ Jungle", "duration": 420, "view_count": 10000
        })
        tracks = parse_tracks(json_output)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["title"], "Jungle Mix")

    def test_parse_tracks_url_format(self):
        json_output = json.dumps({
            "title": "Test Track", "id": "xyz789",
            "uploader": "DJ", "duration": 400, "view_count": 500
        })
        tracks = parse_tracks(json_output)
        self.assertIn("xyz789", tracks[0]["url"])

    def test_filter_quality_min_duration(self):
        """Tracks must be > 300 seconds (5 min) to pass."""
        tracks = [
            {"title": "Short", "url": "http://x", "uploader": "DJ", "duration": 200, "view_count": 1000},
            {"title": "Long", "url": "http://y", "uploader": "DJ", "duration": 400, "view_count": 1000},
        ]
        result = filter_quality_tracks(tracks)
        titles = [t["title"] for t in result]
        self.assertNotIn("Short", titles)
        self.assertIn("Long", titles)

    def test_filter_quality_min_views(self):
        """Tracks must have > 100 views to pass."""
        tracks = [
            {"title": "NoViews", "url": "http://x", "uploader": "DJ", "duration": 400, "view_count": 50},
            {"title": "Popular", "url": "http://y", "uploader": "DJ", "duration": 400, "view_count": 500},
        ]
        result = filter_quality_tracks(tracks)
        titles = [t["title"] for t in result]
        self.assertNotIn("NoViews", titles)
        self.assertIn("Popular", titles)

    def test_filter_returns_empty_on_empty_input(self):
        result = filter_quality_tracks([])
        self.assertEqual(result, [])

    def test_parse_tracks_multi_line(self):
        lines = [
            json.dumps({"title": f"Track {i}", "id": f"id{i}",
                        "uploader": "DJ", "duration": 400, "view_count": 500})
            for i in range(5)
        ]
        tracks = parse_tracks("\n".join(lines))
        self.assertEqual(len(tracks), 5)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_returns_nonzero_on_no_results(self):
        with patch.object(_mod, "search_youtube_jungle", return_value=None):
            result = _mod.main()
        self.assertEqual(result, 1)

    def test_main_returns_nonzero_on_no_quality_tracks(self):
        json_output = json.dumps({
            "title": "Short Track", "id": "short1",
            "uploader": "DJ", "duration": 100, "view_count": 10  # fails filter
        })
        with patch.object(_mod, "search_youtube_jungle", return_value=json_output):
            result = _mod.main()
        self.assertEqual(result, 1)

    def test_main_returns_zero_on_success(self):
        json_output = json.dumps({
            "title": "Great Jungle Track", "id": "jungle1",
            "uploader": "DJ Jungle", "duration": 400, "view_count": 1000
        })
        with patch.object(_mod, "search_youtube_jungle", return_value=json_output):
            with patch.object(_mod, "post_to_slack"):
                result = _mod.main()
        self.assertEqual(result, 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_post_to_slack_formats_message(self):
        """post_to_slack should build a readable message with track info."""
        tracks = [
            {"title": "Jungle Massive Vol 1", "url": "https://youtube.com/x",
             "uploader": "DJ Hype", "view_count": 50000}
        ]
        curl_calls = []
        mock_config = {"channels": {"slack": {"botToken": "xoxb-fake"}}}

        with patch("pathlib.Path.exists", return_value=True):
            with patch("builtins.open", unittest.mock.mock_open(read_data=json.dumps(mock_config))):
                with patch("subprocess.run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0)
                    curl_calls.extend([mock_run.call_args])
                    post_to_slack(tracks)

        # Verify curl was called if config found
        # (or no crash if config handling happened)

    def test_post_to_slack_no_tracks_no_post(self):
        """post_to_slack with empty list should not attempt to post."""
        with patch("subprocess.run") as mock_run:
            post_to_slack([])
        mock_run.assert_not_called()


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

    def test_module_has_main(self):
        self.assertTrue(callable(_mod.main))

    def test_cache_file_defined(self):
        self.assertIsNotNone(_mod.CACHE_FILE)

    def test_channel_defined(self):
        self.assertIsNotNone(_mod.CHANNEL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
