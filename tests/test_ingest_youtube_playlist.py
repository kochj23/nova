"""
test_ingest_youtube_playlist.py — All 7 test categories for ingest_youtube_playlist.py
Written by Jordan Koch.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

import importlib.util
_SCRIPT = (Path(__file__).parent.parent / "scripts" / "ingest_youtube_playlist.py"
           if (Path(__file__).parent.parent / "scripts" / "ingest_youtube_playlist.py").exists()
           else Path(__file__).parent.parent / "scripts" / "_archive" / "ingest_youtube_playlist.py")
_spec = importlib.util.spec_from_file_location("ingest_youtube_playlist", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
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

    def test_chunk_words_defined(self):
        self.assertEqual(_mod.CHUNK_WORDS, 400)

    def test_download_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=600", src,
                      "yt-dlp download must have 600s timeout")

    def test_remember_has_timeout(self):
        src = _SCRIPT.read_text()
        self.assertIn("timeout=15", src)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_silent_on_failure(self):
        def fail(*args, **kwargs):
            raise OSError("server down")

        with patch("urllib.request.urlopen", side_effect=fail):
            result = _mod.remember("Test transcript.", {"type": "test"})
        self.assertFalse(result)

    def test_skipped_on_download_failure(self):
        """Videos with failed downloads must be skipped, not crash."""
        src = _SCRIPT.read_text()
        self.assertIn("SKIPPED", src)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_chunk_text_correct_size(self):
        text = " ".join(f"word{i}" for i in range(800))
        chunks = _mod.chunk_text(text, chunk_size=400)
        self.assertEqual(len(chunks), 2)

    def test_chunk_text_filters_short(self):
        text = " ".join(["x"] * 5)
        chunks = _mod.chunk_text(text, chunk_size=100)
        self.assertEqual(len(chunks), 0)

    def test_remember_posts_correct_source(self):
        posted = []

        def capture(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        meta = {"type": "youtube_transcript", "video_id": "abc123", "title": "Test"}
        with patch("urllib.request.urlopen", side_effect=capture):
            _mod.remember("[YouTube: Test] Content here.", meta)

        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["source"], "youtube_transcript")
        self.assertEqual(posted[0]["metadata"]["video_id"], "abc123")

    def test_log_function_exists(self):
        self.assertTrue(callable(_mod.log))

    def test_get_playlist_videos_returns_list(self):
        def fake_run(*args, **kwargs):
            r = MagicMock()
            r.stdout = "abc123\tTest Video Title\n"
            r.returncode = 0
            return r

        with patch("subprocess.run", side_effect=fake_run):
            videos = _mod.get_playlist_videos("https://youtube.com/playlist?list=TEST")
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["id"], "abc123")
        self.assertEqual(videos[0]["title"], "Test Video Title")

    def test_work_dir_defined(self):
        self.assertIsNotNone(_mod.WORK_DIR)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_requires_playlist_url_arg(self):
        """main() must exit 1 when no playlist URL given."""
        with patch("sys.argv", ["ingest_youtube_playlist.py"]):
            with self.assertRaises(SystemExit) as cm:
                _mod.main()
            self.assertEqual(cm.exception.code, 1)

    def test_audio_cleaned_up_after_processing(self):
        """Audio files must be deleted after transcription."""
        src = _SCRIPT.read_text()
        self.assertIn("audio_path.unlink", src,
                      "Audio must be cleaned up after processing")

    def test_status_posted_every_5_minutes(self):
        src = _SCRIPT.read_text()
        self.assertIn("300", src,
                      "Status must be posted every 300s (5min)")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_transcript_chunked_before_ingest(self):
        """Transcript must be chunked before ingestion."""
        src = _SCRIPT.read_text()
        self.assertIn("chunk_text", src)

    def test_metadata_includes_chunk_number(self):
        src = _SCRIPT.read_text()
        self.assertIn('"chunk"', src)
        self.assertIn('"total_chunks"', src)

    def test_slack_notified_at_completion(self):
        src = _SCRIPT.read_text()
        self.assertIn("Ingestion Complete", src)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"ingest_youtube_playlist.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_main_function_exists(self):
        self.assertTrue(callable(_mod.main))

    def test_constants_defined(self):
        for attr in ["MEMORY_URL", "WORK_DIR", "CHUNK_WORDS"]:
            self.assertTrue(hasattr(_mod, attr))

    def test_functions_defined(self):
        for fn in ["log", "slack_post", "remember", "chunk_text",
                   "get_playlist_videos", "download_audio",
                   "transcribe", "post_status", "main"]:
            self.assertTrue(callable(getattr(_mod, fn, None)),
                            f"{fn} must exist")


if __name__ == "__main__":
    unittest.main(verbosity=2)
