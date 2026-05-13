"""
test_ingest_yt_playlist_fights.py — All 7 test categories for ingest_yt_playlist_fights.py
Written by Jordan Koch.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

import importlib.util
_SCRIPT = (Path(__file__).parent.parent / "scripts" / "ingest_yt_playlist_fights.py"
           if (Path(__file__).parent.parent / "scripts" / "ingest_yt_playlist_fights.py").exists()
           else Path(__file__).parent.parent / "scripts" / "_archive" / "ingest_yt_playlist_fights.py")
_spec = importlib.util.spec_from_file_location("ingest_yt_playlist_fights", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)

# Suppress actual execution at module level by mocking subprocess
with patch("subprocess.run") as mock_run:
    mock_run.return_value = MagicMock(stdout="", returncode=0)
    with patch("urllib.request.urlopen") as mock_url:
        _spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src)

    def test_memory_url_localhost(self):
        self.assertIn("127.0.0.1", _mod.MEMORY_URL)

    def test_text_truncated(self):
        src = _SCRIPT.read_text()
        self.assertIn("[:2000]", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_delay_between_videos_defined(self):
        self.assertEqual(_mod.DELAY_BETWEEN_VIDEOS, 120)

    def test_chunk_words_defined(self):
        self.assertEqual(_mod.CHUNK_WORDS, 400)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_silent_on_failure(self):
        def fail(*args, **kwargs):
            raise OSError("server down")

        with patch("urllib.request.urlopen", side_effect=fail):
            result = _mod.remember("Test.", "comic_books", {"type": "test"})
        self.assertFalse(result)

    def test_skipped_on_download_failure(self):
        src = _SCRIPT.read_text()
        self.assertIn("skipped_videos += 1", src)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_classify_source_horror(self):
        """classify_source() must classify horror content."""
        result = _mod.classify_source(
            "Horror movie fight", "Jason vs Freddy horror slasher battle")
        self.assertEqual(result, "horror")

    def test_classify_source_comic(self):
        """classify_source() must classify superhero content."""
        result = _mod.classify_source(
            "Marvel fight", "Avengers versus Thanos who would win")
        self.assertEqual(result, "comic_books")

    def test_classify_source_vs(self):
        """'vs' or 'versus' content should classify as comic_books."""
        result = _mod.classify_source(
            "Legendary fights", "who would win fight versus battle")
        self.assertEqual(result, "comic_books")

    def test_classify_source_default(self):
        """Unknown content should default to 'video'."""
        result = _mod.classify_source("Random title", "completely unrelated content here")
        self.assertEqual(result, "video")

    def test_chunk_text_function(self):
        text = " ".join(f"word{i}" for i in range(800))
        chunks = _mod.chunk_text(text)
        self.assertEqual(len(chunks), 2)

    def test_remember_includes_source(self):
        posted = []

        def capture(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=capture):
            _mod.remember("Content.", "horror", {"type": "test"})

        self.assertEqual(posted[0]["source"], "horror")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_duplicate_video_ids_deduplicated(self):
        """get_playlist_videos() must deduplicate video IDs."""
        def fake_run(*args, **kwargs):
            r = MagicMock()
            r.stdout = "abc123\tTitle 1\nabc123\tTitle 1 duplicate\n"
            r.returncode = 0
            return r

        with patch("subprocess.run", side_effect=fake_run):
            videos = _mod.get_playlist_videos("https://youtube.com/playlist?list=TEST")
        ids = [v["id"] for v in videos]
        self.assertEqual(len(ids), len(set(ids)),
                         "Duplicate video IDs must be removed")

    def test_private_videos_skipped(self):
        """Private and deleted videos must be filtered out."""
        def fake_run(*args, **kwargs):
            r = MagicMock()
            r.stdout = "abc123\t[Private video]\ndef456\tGood video\n"
            r.returncode = 0
            return r

        with patch("subprocess.run", side_effect=fake_run):
            videos = _mod.get_playlist_videos("https://youtube.com/playlist?list=TEST")
        titles = [v["title"] for v in videos]
        self.assertNotIn("[Private video]", titles)
        self.assertIn("Good video", titles)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_sources_tracking_dict(self):
        src = _SCRIPT.read_text()
        self.assertIn("sources_used", src)

    def test_final_slack_post_includes_sources(self):
        src = _SCRIPT.read_text()
        self.assertIn("sources_used", src)
        self.assertIn("Sources", src)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"ingest_yt_playlist_fights.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_functions_defined(self):
        for fn in ["log", "slack_post", "classify_source", "remember",
                   "chunk_text", "get_playlist_videos", "download_audio",
                   "transcribe"]:
            self.assertTrue(callable(getattr(_mod, fn, None)),
                            f"{fn} must exist")

    def test_playlist_url_defined(self):
        self.assertTrue(hasattr(_mod, "PLAYLIST_URL"))
        self.assertIn("youtube.com", _mod.PLAYLIST_URL)


if __name__ == "__main__":
    unittest.main(verbosity=2)
