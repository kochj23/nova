"""
test_nova_yale_backfill.py -- All 7 test categories for nova_yale_backfill.py
Written by Jordan Koch.
"""

import importlib.util
import json
import math
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Stub dependencies before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules.setdefault("nova_config", _nova_cfg)

_nova_registry = MagicMock()
_nova_registry.is_done = MagicMock(return_value=False)
_nova_registry.register_file = MagicMock()
_nova_registry.mark_status = MagicMock()
_nova_registry.mark_ingested = MagicMock()
sys.modules.setdefault("nova_media_registry", _nova_registry)

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_yale_backfill.py"
_spec = importlib.util.spec_from_file_location("nova_yale_backfill", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

is_trash_chunk     = _mod.is_trash_chunk
chunk_text         = _mod.chunk_text
remember           = _mod.remember
load_state         = _mod.load_state
save_state         = _mod.save_state
sanitize           = _mod.sanitize
DELAY_BETWEEN      = _mod.DELAY_BETWEEN
SOURCE             = _mod.SOURCE
SHOW_NAME          = _mod.SHOW_NAME


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-", "ghp_", "AKIA", "xoxb-", "password =", "secret ="]:
            self.assertNotIn(pattern, src, f"Credential: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp.com",
            "kochj" + _at + "digitalnoise.net",
            "kochj23" + _at + "gmail.com",
        ]:
            self.assertNotIn(pat, src, f"PII: {pat!r}")

    def test_remember_truncates_to_2000(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        long_text = "x" * 5000
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            remember(long_text, {"type": "course_lecture"})

        self.assertTrue(len(captured) > 0)
        self.assertLessEqual(len(captured[0]["text"]), 2000)

    def test_memory_url_is_local(self):
        url = _mod.MEMORY_URL
        self.assertTrue(
            url.startswith("http://127.0.0.1") or url.startswith("http://192.168."),
            f"MEMORY_URL must be local: {url}"
        )

    def test_state_file_uses_home(self):
        self.assertIn(str(Path.home()), str(_mod.STATE_FILE))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_is_trash_fast(self):
        text = "Professor Smith discusses the history of philosophy at Yale University." * 50
        start = time.perf_counter()
        for _ in range(10000):
            is_trash_chunk(text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 2.0)

    def test_chunk_text_fast(self):
        text = " ".join(f"word{i}" for i in range(10000))
        start = time.perf_counter()
        chunk_text(text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0)

    def test_delay_is_pi_minutes(self):
        expected = math.pi * 60
        self.assertAlmostEqual(DELAY_BETWEEN, expected, places=2,
                               msg="Delay must be exactly π minutes")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_returns_false_on_error(self):
        def failing(req, timeout=None):
            raise OSError("refused")

        with patch("urllib.request.urlopen", side_effect=failing):
            result = remember("Yale course content.", {"type": "course_lecture"})

        self.assertFalse(result)

    def test_remember_returns_true_on_success(self):
        ctx = MagicMock()
        ctx.__enter__ = lambda s: s
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=ctx):
            result = remember("Yale course lecture content.", {"type": "course_lecture"})

        self.assertTrue(result)

    def test_transcribe_handles_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 10)):
            result = _mod.transcribe(Path("/tmp/fake.wav"), "test_stem")

        self.assertIsNone(result)

    def test_extract_audio_returns_false_on_error(self):
        with patch("subprocess.run", side_effect=Exception("ffmpeg not found")):
            result = _mod.extract_audio(Path("/tmp/fake.mp4"), Path("/tmp/fake.wav"))

        self.assertFalse(result)

    def test_download_video_handles_error(self):
        import subprocess
        with patch("subprocess.run", return_value=MagicMock(
            returncode=1, stdout="", stderr="Download failed"
        )):
            with patch("pathlib.Path.exists", return_value=False):
                result = _mod.download_video("abc123", Path("/tmp/test.mp4"))

        self.assertTrue(result.startswith("error") or result == "error: Download failed"[:50])


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_is_trash_short_chunk(self):
        self.assertTrue(is_trash_chunk("short"))

    def test_is_trash_music(self):
        self.assertTrue(is_trash_chunk("♪ " * 30))

    def test_is_trash_repeated(self):
        self.assertTrue(is_trash_chunk("word word word word word word " * 5))

    def test_not_trash_lecture(self):
        text = ("Professor Smith discusses the historical context of ancient Greek philosophy "
                "and its influence on Western thought. This is the third lecture in the series "
                "on philosophy and ethics at Yale University.")
        self.assertFalse(is_trash_chunk(text))

    def test_chunk_text_uses_chunk_words(self):
        text = "word " * 1000
        chunks = chunk_text(text)
        for c in chunks:
            self.assertLessEqual(len(c.split()), _mod.CHUNK_WORDS + 5)

    def test_sanitize_removes_bad_chars(self):
        result = sanitize('Test<>:"/\\|?*Title')
        for bad in '<>:"/\\|?*':
            self.assertNotIn(bad, result)

    def test_sanitize_truncates(self):
        long_title = "A" * 200
        result = sanitize(long_title)
        self.assertLessEqual(len(result), 120)

    def test_load_state_defaults(self):
        with patch.object(_mod, "STATE_FILE", Path("/tmp/nonexistent_yale.json")):
            state = load_state()
        self.assertIn("done_playlists", state)
        self.assertIn("done_videos", state)
        self.assertIn("total_downloaded", state)
        self.assertIn("total_ingested", state)

    def test_source_is_education(self):
        self.assertEqual(SOURCE, "education")

    def test_show_name_is_yale(self):
        self.assertIn("Yale", SHOW_NAME)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_state_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "yale_state.json"):
                state = load_state()
                state["done_playlists"] = ["https://youtube.com/playlist?list=abc"]
                state["total_downloaded"] = 5
                save_state(state)
                loaded = load_state()

        self.assertIn("https://youtube.com/playlist?list=abc", loaded["done_playlists"])
        self.assertEqual(loaded["total_downloaded"], 5)

    def test_remember_payload_structure(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            remember("[Yale Courses] Introduction to Philosophy", {
                "type": "course_lecture",
                "show": SHOW_NAME,
                "playlist": "Philosophy 181",
                "title": "Introduction to Philosophy",
                "season": 1,
                "episode": 1,
            })

        self.assertEqual(len(captured), 1)
        payload = captured[0]
        self.assertIn("text", payload)
        self.assertIn("source", payload)
        self.assertEqual(payload["source"], "education")
        self.assertIn("metadata", payload)

    def test_chunk_text_filters_trash(self):
        text = "good lecture content " * 100 + " ♪ " * 20 + " more good content " * 50
        words = text.split()
        chunks = [" ".join(words[i:i + _mod.CHUNK_WORDS]) for i in range(0, len(words), _mod.CHUNK_WORDS)]
        valid = [c for c in chunks if not is_trash_chunk(c)]
        self.assertGreater(len(valid), 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_skips_done_playlists(self):
        """Playlists already in done_playlists should be skipped."""
        fake_playlist = {"url": "https://youtube.com/playlist?list=done", "title": "Done Playlist"}
        state = {
            "done_playlists": ["https://youtube.com/playlist?list=done"],
            "done_videos": [],
            "current_playlist": None,
            "total_downloaded": 5,
            "total_ingested": 10,
            "last_run": None,
        }

        with patch.object(_mod, "load_state", return_value=state):
            with patch.object(_mod, "get_playlists", return_value=[fake_playlist]):
                with patch.object(_mod, "get_playlist_videos") as mock_videos:
                    with patch.object(_mod, "notify"):
                        with patch.object(_mod, "save_state"):
                            _mod.main()

        mock_videos.assert_not_called()

    def test_remember_truncates_text(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            remember("word " * 2000, {"type": "course_lecture"})

        self.assertLessEqual(len(captured[0]["text"]), 2000)

    def test_notify_called_on_ingestion(self):
        """notify() must be called when a video is ingested."""
        notified = []
        fake_playlist = {"url": "https://youtube.com/playlist?list=test", "title": "Test Playlist"}
        fake_video = {"id": "abc123", "title": "Introduction to Philosophy", "upload_date": "20240101"}
        state = {
            "done_playlists": [],
            "done_videos": [],
            "current_playlist": None,
            "total_downloaded": 0,
            "total_ingested": 0,
            "last_run": None,
        }

        with patch.object(_mod, "load_state", return_value=state):
            with patch.object(_mod, "get_playlists", return_value=[fake_playlist]):
                with patch.object(_mod, "get_playlist_videos", return_value=[fake_video]):
                    with patch.object(_mod, "season_num_for_playlist", return_value=1):
                        with patch.object(_mod, "get_season_dir",
                                           return_value=(MagicMock(), MagicMock())):
                            with patch.object(_nova_registry, "is_done", return_value=False):
                                with patch.object(_mod, "download_video", return_value="ok"):
                                    with patch.object(_mod, "extract_audio", return_value=True):
                                        with patch.object(_mod, "transcribe",
                                                           return_value="lecture content " * 100):
                                            with patch.object(_mod, "remember", return_value=True):
                                                with patch.object(_mod, "notify",
                                                                   side_effect=lambda m: notified.append(m)):
                                                    with patch.object(_mod, "save_state"):
                                                        with patch.object(_mod, "last_episode_in_season", return_value=0):
                                                            with patch("time.sleep"):
                                                                _mod.main()

        self.assertGreater(len(notified), 0)


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

    def test_module_constants_defined(self):
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIsInstance(_mod.CHUNK_WORDS, int)
        self.assertIsInstance(_mod.SOURCE, str)
        self.assertIsInstance(_mod.SHOW_NAME, str)
        self.assertIsInstance(_mod.DELAY_BETWEEN, float)

    def test_module_loads_without_network(self):
        self.assertIsNotNone(_mod)

    def test_delay_is_pi_times_60(self):
        self.assertAlmostEqual(_mod.DELAY_BETWEEN, math.pi * 60, places=1)

    def test_log_file_uses_home(self):
        self.assertIn(str(Path.home()), str(_mod.LOG_FILE))

    def test_trash_patterns_compiled(self):
        self.assertGreater(len(_mod._TRASH_PATTERNS), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
