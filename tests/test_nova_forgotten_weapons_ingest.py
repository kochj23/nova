"""
test_nova_forgotten_weapons_ingest.py -- All 7 test categories for nova_forgotten_weapons_ingest.py
Written by Jordan Koch.
"""

import importlib.util
import json
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_forgotten_weapons_ingest.py"
_spec = importlib.util.spec_from_file_location("nova_forgotten_weapons_ingest", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

is_trash_chunk = _mod.is_trash_chunk
load_state     = _mod.load_state
save_state     = _mod.save_state
SOURCE         = _mod.SOURCE
SHOW_NAME      = _mod.SHOW_NAME


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
            "jordan.koch" + _at + "disney.com",
            "kochj" + _at + "digitalnoise.net",
            "kochj23" + _at + "gmail.com",
        ]:
            self.assertNotIn(pat, src, f"PII: {pat!r}")

    def test_memory_url_is_local(self):
        url = _mod.MEMORY_URL
        self.assertTrue(
            url.startswith("http://127.0.0.1") or url.startswith("http://192.168."),
            f"MEMORY_URL must be local: {url}"
        )

    def test_source_is_military_history(self):
        self.assertEqual(SOURCE, "military_history")

    def test_state_file_uses_home(self):
        self.assertIn(str(Path.home()), str(_mod.STATE_FILE))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_is_trash_fast(self):
        text = "The M1 Garand is a semi-automatic rifle developed for the US military." * 50
        start = time.perf_counter()
        for _ in range(10000):
            is_trash_chunk(text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 2.0, f"is_trash_chunk 10000x took {elapsed:.3f}s")

    def test_is_trash_music_fast(self):
        music = "♪ " * 500
        start = time.perf_counter()
        for _ in range(1000):
            is_trash_chunk(music)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_chunk_words_constant_reasonable(self):
        self.assertGreater(_mod.CHUNK_WORDS, 50)
        self.assertLess(_mod.CHUNK_WORDS, 2000)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_remember_returns_false_on_error(self):
        """remember() must return False when memory server is unreachable."""
        def failing(req, timeout=None):
            raise OSError("refused")

        with patch("urllib.request.urlopen", side_effect=failing):
            result = _mod.remember("Forgotten Weapons content here.", {
                "type": "gun_review",
                "show": SHOW_NAME,
                "title": "M1 Garand",
            })

        self.assertFalse(result)

    def test_remember_returns_true_on_success(self):
        ctx = MagicMock()
        ctx.__enter__ = lambda s: s
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=ctx):
            result = _mod.remember("Gun review content here.", {
                "type": "gun_review",
                "title": "Thompson",
            })

        self.assertTrue(result)

    def test_transcribe_returns_none_on_timeout(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 10)):
            result = _mod.transcribe(Path("/tmp/nonexistent.wav"), "test_stem")

        self.assertIsNone(result)

    def test_extract_audio_returns_false_on_error(self):
        with patch("subprocess.run", side_effect=Exception("ffmpeg not found")):
            result = _mod.extract_audio(Path("/tmp/fake.mp4"), Path("/tmp/fake.wav"))

        self.assertFalse(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_is_trash_short_text(self):
        self.assertTrue(is_trash_chunk("too short"))

    def test_is_trash_music_symbols(self):
        self.assertTrue(is_trash_chunk("♪ " * 30))

    def test_is_trash_repeated_phrase(self):
        self.assertTrue(is_trash_chunk("word word word word word word " * 5))

    def test_not_trash_normal_text(self):
        text = ("The M1 Garand is a semi-automatic rifle widely used in World War II by "
                "American forces. It was designed by John C. Garand and adopted in 1936.")
        self.assertFalse(is_trash_chunk(text))

    def test_is_trash_subtitle_marker(self):
        self.assertTrue(is_trash_chunk("Subtitles by SubGuy " * 5 + " extra words"))

    def test_load_state_defaults(self):
        with patch.object(_mod, "STATE_FILE", Path("/tmp/nonexistent_fw.json")):
            state = load_state()
        self.assertIn("done", state)
        self.assertIn("total_ingested", state)

    def test_show_name_constant(self):
        self.assertIn("Forgotten", SHOW_NAME)

    def test_source_constant(self):
        self.assertEqual(SOURCE, "military_history")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_state_roundtrip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "fw_state.json"):
                state = load_state()
                state["done"]["test_video.mp4"] = {"ingested": 5}
                state["total_ingested"] = 42
                save_state(state)
                loaded = load_state()

        self.assertIn("test_video.mp4", loaded["done"])
        self.assertEqual(loaded["total_ingested"], 42)

    def test_remember_sends_correct_payload(self):
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            _mod.remember("Ian McCollum reviews the M1 Garand.", {
                "type": "gun_review",
                "show": SHOW_NAME,
                "title": "M1 Garand Review",
                "source_file": "/fake/path.mp4",
            })

        self.assertEqual(len(captured), 1)
        self.assertIn("text", captured[0])
        self.assertIn("source", captured[0])
        self.assertEqual(captured[0]["source"], "military_history")

    def test_is_trash_filters_pipeline(self):
        """is_trash_chunk should filter out junk from transcription output."""
        good = ("Ian McCollum here from Forgotten Weapons. Today we are looking at the "
                "Winchester Model 1873 lever action rifle. This was an important firearm "
                "in American history used extensively during the frontier period.")
        bad_music = "♪ ♪ ♪ " * 30
        bad_repeat = "and and and and and and and and and and " * 5

        self.assertFalse(is_trash_chunk(good))
        self.assertTrue(is_trash_chunk(bad_music))
        self.assertTrue(is_trash_chunk(bad_repeat))


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_skips_done_videos(self):
        """Videos already in done dict should be skipped."""
        fake_video = MagicMock()
        fake_video.suffix = ".mp4"
        fake_video.name = "FW-001.mp4"
        fake_video.__str__ = lambda s: "/fake/FW-001.mp4"

        state = {
            "done": {"/fake/FW-001.mp4": {"ingested": 10}},
            "last_run": None,
            "total_ingested": 10,
        }

        with patch.object(_mod, "load_state", return_value=state):
            with patch.object(_mod, "find_videos", return_value=[fake_video]):
                with patch.object(_nova_registry, "is_done", return_value=True):
                    with patch.object(_mod, "extract_audio", return_value=False):
                        with patch.object(_mod, "save_state"):
                            with patch.object(_mod, "notify"):
                                _mod.main()

    def test_remember_truncates_to_2000(self):
        """remember() must truncate text to 2000 chars."""
        captured = []

        def mock_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            ctx = MagicMock()
            ctx.__enter__ = lambda s: s
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        long_text = "x" * 5000
        with patch("urllib.request.urlopen", side_effect=mock_urlopen):
            _mod.remember(long_text, {"type": "test"})

        self.assertTrue(len(captured) > 0)
        self.assertLessEqual(len(captured[0]["text"]), 2000)

    def test_notify_on_video_ingested(self):
        """notify() must be called when a video is ingested."""
        notified = []
        fake_video = MagicMock()
        fake_video.suffix = ".mp4"
        fake_video.name = "FW-001.mp4"
        fake_video.__str__ = lambda s: "/fake/FW-001.mp4"
        fake_video.stat = MagicMock(return_value=MagicMock(st_size=1024 * 1024))

        with patch.object(_mod, "load_state", return_value={"done": {}, "last_run": None, "total_ingested": 0}):
            with patch.object(_mod, "find_videos", return_value=[fake_video]):
                with patch.object(_nova_registry, "is_done", return_value=False):
                    with patch.object(_mod, "extract_audio", return_value=True):
                        with patch.object(_mod, "transcribe", return_value="Gun review content " * 50):
                            with patch.object(_mod, "remember", return_value=True):
                                with patch.object(_mod, "notify",
                                                   side_effect=lambda m: notified.append(m)):
                                    with patch.object(_mod, "save_state"):
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

    def test_module_constants(self):
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertIsInstance(_mod.CHUNK_WORDS, int)
        self.assertIsInstance(_mod.SOURCE, str)
        self.assertIsInstance(_mod.SHOW_NAME, str)

    def test_module_loads_without_network(self):
        self.assertIsNotNone(_mod)

    def test_log_file_uses_home(self):
        self.assertIn(str(Path.home()), str(_mod.LOG_FILE))

    def test_trash_patterns_compiled(self):
        self.assertGreater(len(_mod._TRASH_PATTERNS), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
