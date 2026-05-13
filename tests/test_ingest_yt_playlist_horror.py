"""
test_ingest_yt_playlist_horror.py — All 7 test categories for ingest_yt_playlist_horror.py
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
_SCRIPT = (Path(__file__).parent.parent / "scripts" / "ingest_yt_playlist_horror.py"
           if (Path(__file__).parent.parent / "scripts" / "ingest_yt_playlist_horror.py").exists()
           else Path(__file__).parent.parent / "scripts" / "_archive" / "ingest_yt_playlist_horror.py")
_spec = importlib.util.spec_from_file_location("ingest_yt_playlist_horror", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)

with patch("subprocess.run") as mock_run:
    mock_run.return_value = MagicMock(stdout="", returncode=0)
    with patch("urllib.request.urlopen"):
        _spec.loader.exec_module(_mod)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src)

    def test_memory_url_localhost(self):
        self.assertIn("127.0.0.1", _mod.MEMORY_URL)

    def test_text_truncated(self):
        src = _SCRIPT.read_text()
        self.assertIn("[:2000]", src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_delay_defined(self):
        self.assertEqual(_mod.DELAY_BETWEEN_VIDEOS, 120)

    def test_chunk_words_defined(self):
        self.assertEqual(_mod.CHUNK_WORDS, 400)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_silent_on_failure(self):
        def fail(*args, **kwargs):
            raise OSError("down")

        with patch("urllib.request.urlopen", side_effect=fail):
            result = _mod.remember("Horror transcript chunk.", {"type": "test"})
        self.assertFalse(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_chunk_text_function(self):
        text = " ".join(f"word{i}" for i in range(800))
        chunks = _mod.chunk_text(text)
        self.assertEqual(len(chunks), 2)

    def test_chunk_text_filters_short(self):
        chunks = _mod.chunk_text("short")
        self.assertEqual(len(chunks), 0)

    def test_remember_posts_youtube_transcript_source(self):
        posted = []

        def capture(req, timeout=None):
            posted.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=capture):
            _mod.remember("[YouTube: Horror] Content.", {"video_id": "xyz"})

        self.assertEqual(posted[0]["source"], "youtube_transcript")

    def test_get_playlist_videos_filters_private(self):
        def fake_run(*args, **kwargs):
            r = MagicMock()
            r.stdout = "id1\t[Private video]\nid2\tHorror video\n"
            r.returncode = 0
            return r

        with patch("subprocess.run", side_effect=fake_run):
            videos = _mod.get_playlist_videos("https://youtube.com/playlist?list=X")
        self.assertEqual(len(videos), 1)
        self.assertEqual(videos[0]["title"], "Horror video")

    def test_playlist_url_is_youtube(self):
        self.assertIn("youtube.com", _mod.PLAYLIST_URL)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_audio_cleanup_after_processing(self):
        src = _SCRIPT.read_text()
        self.assertIn("audio_path.unlink", src)

    def test_status_posted_periodically(self):
        src = _SCRIPT.read_text()
        self.assertIn("300", src)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_final_completion_post(self):
        src = _SCRIPT.read_text()
        self.assertIn("Ingestion Complete", src)

    def test_chunk_metadata_structure(self):
        src = _SCRIPT.read_text()
        self.assertIn('"chunk"', src)
        self.assertIn('"playlist"', src)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"ingest_yt_playlist_horror.py has syntax errors: {e}")

    def test_module_loads(self):
        self.assertIsNotNone(_mod)

    def test_functions_defined(self):
        for fn in ["log", "slack_post", "remember", "chunk_text",
                   "get_playlist_videos", "download_audio", "transcribe"]:
            self.assertTrue(callable(getattr(_mod, fn, None)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
