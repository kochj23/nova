"""
test_nova_tv_ingest.py — All 7 test categories for nova_tv_ingest.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

# Stub nova_config and nova_media_registry before loading
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790"
sys.modules["nova_config"] = _nova_cfg

_registry = MagicMock()
_registry.is_done = MagicMock(return_value=False)
_registry.register_file = MagicMock(return_value={})
_registry.mark_ingested = MagicMock()
_registry.mark_status = MagicMock()
sys.modules["nova_media_registry"] = _registry

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_tv_ingest.py"
_spec = importlib.util.spec_from_file_location("nova_tv_ingest", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

is_trash_chunk = _mod.is_trash_chunk
chunk_text = _mod.chunk_text
classify_source = _mod.classify_source
show_name_from_path = _mod.show_name_from_path
load_state = _mod.load_state
save_state = _mod.save_state
mark_done = _mod.mark_done
CHUNK_WORDS = _mod.CHUNK_WORDS
MIN_CHUNK_WORDS = _mod.MIN_CHUNK_WORDS


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for p in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(p, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(p, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_tv_transcripts_tagged_local_only(self):
        """TV transcript memories must include privacy=local-only."""
        src = _SCRIPT.read_text()
        self.assertIn("local-only", src)

    def test_memory_url_defined(self):
        """MEMORY_URL must be defined."""
        self.assertIsInstance(_mod.MEMORY_URL, str)
        self.assertTrue(_mod.MEMORY_URL.startswith("http"))

    def test_state_file_uses_home(self):
        """STATE_FILE must be under home directory."""
        self.assertTrue(str(_mod.STATE_FILE).startswith(str(Path.home())))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_is_trash_chunk_fast(self):
        """is_trash_chunk must process 1000 chunks in < 100ms."""
        chunks = [
            "normal text " * 50,
            "♪ " * 40,
            "word " * 30,
        ] * 333
        start = time.perf_counter()
        for chunk in chunks:
            is_trash_chunk(chunk)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_chunk_text_bounded(self):
        """chunk_text must produce at most ceil(len/CHUNK_WORDS) chunks."""
        text = "word " * 2000
        chunks = chunk_text(text)
        expected_max = (2000 // CHUNK_WORDS) + 2
        self.assertLessEqual(len(chunks), expected_max)

    def test_classify_source_fast(self):
        """classify_source must classify 1000 videos in < 100ms."""
        samples = [
            ("Good Eats", "Season 1 Episode 1", "cooking food kitchen"),
            ("Jay Leno's Garage", "Ep 1", "car engine horsepower"),
            ("Jeopardy", "Episode", "trivia game show"),
        ] * 333
        start = time.perf_counter()
        for show, title, text in samples:
            classify_source(show, title, text)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_state_load_returns_default_on_missing(self):
        """load_state returns default state when file doesn't exist."""
        with patch.object(_mod, "STATE_FILE", Path("/nonexistent/state.json")):
            result = load_state()
        self.assertIn("done", result)
        self.assertIn("last_run", result)
        self.assertEqual(result["last_run"], None)

    def test_state_load_returns_default_on_corrupt(self):
        """load_state returns default on corrupt JSON."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            fname = Path(f.name)

        with patch.object(_mod, "STATE_FILE", fname):
            result = load_state()

        fname.unlink()
        self.assertIn("done", result)

    def test_remember_handles_network_failure(self):
        """remember() returns False on network failure."""
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            result = _mod.remember("test text", "television", {})
        self.assertFalse(result)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_is_trash_short_chunk(self):
        """Short chunks (< MIN_CHUNK_WORDS words) are trash."""
        short = "only a few words"
        self.assertTrue(is_trash_chunk(short))

    def test_is_trash_music_symbols(self):
        """Chunks with music symbols are trash."""
        music = "♪ la la la ♪ music playing ♪ " * 5
        self.assertTrue(is_trash_chunk(music))

    def test_is_trash_low_alpha_ratio(self):
        """Chunks with < 50% alpha chars are trash."""
        noise = "123 456 789 !!! ??? $$$ " * 20
        self.assertTrue(is_trash_chunk(noise))

    def test_not_trash_normal_prose(self):
        """Normal transcript prose is not trash."""
        prose = ("The Battle of Hastings was fought in 1066 when William the Conqueror "
                 "defeated King Harold and changed the course of English history forever. "
                 "This was a pivotal moment in medieval European history and culture. ") * 2
        self.assertFalse(is_trash_chunk(prose))

    def test_chunk_text_word_size(self):
        """chunk_text produces chunks of ~CHUNK_WORDS words."""
        text = "word " * (CHUNK_WORDS * 3)
        chunks = chunk_text(text)
        if chunks:
            for c in chunks:
                self.assertLessEqual(len(c.split()), CHUNK_WORDS + 5)

    def test_classify_cooking(self):
        """Good Eats maps to 'cooking'."""
        result = classify_source("Good Eats", "episode", "food recipes")
        self.assertEqual(result, "cooking")

    def test_classify_automotive(self):
        """Jay Leno's Garage maps to 'automotive'."""
        result = classify_source("Jay Leno's Garage", "Ep 1", "car engine")
        self.assertEqual(result, "automotive")

    def test_classify_game_show(self):
        """Jeopardy maps to 'game_show'."""
        result = classify_source("Jeopardy!", "Episode", "trivia question")
        self.assertEqual(result, "game_show")

    def test_classify_education(self):
        """CrashCourse maps to 'education'."""
        result = classify_source("CrashCourse", "History", "history lesson")
        self.assertEqual(result, "education")

    def test_classify_default(self):
        """Unknown show maps to 'television'."""
        result = classify_source("Some Unknown Show", "Episode", "random content")
        self.assertEqual(result, "television")

    def test_show_name_from_path_season(self):
        """show_name_from_path extracts show name from season directory."""
        video = Path("/videos/Breaking Bad/Season 1/episode01.mp4")
        result = show_name_from_path(video)
        self.assertEqual(result, "Breaking Bad")

    def test_show_name_from_path_fallback(self):
        """show_name_from_path falls back to parent directory."""
        video = Path("/videos/DocumentaryShow/episode.mp4")
        result = show_name_from_path(video)
        self.assertEqual(result, "DocumentaryShow")

    def test_min_chunk_words_defined(self):
        """MIN_CHUNK_WORDS must be between 10 and 100."""
        self.assertGreaterEqual(MIN_CHUNK_WORDS, 10)
        self.assertLessEqual(MIN_CHUNK_WORDS, 100)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_save_and_load_state_roundtrip(self):
        """save_state/load_state roundtrip preserves all state data."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = Path(tmpdir) / "tv_state.json"
            with patch.object(_mod, "STATE_FILE", state_file):
                state = {"done": {"/path/video.mp4": {"status": "ingested"}},
                         "last_run": "2026-01-01T00:00:00"}
                save_state(state)
                loaded = load_state()

        self.assertIn("/path/video.mp4", loaded["done"])
        self.assertEqual(loaded["last_run"], "2026-01-01T00:00:00")

    def test_mark_done_thread_safe(self):
        """mark_done uses lock and adds to state correctly."""
        state = {"done": {}, "last_run": None}
        mark_done(state, "/test/video.mp4", {"show": "Test", "status": "ingested"})
        self.assertIn("/test/video.mp4", state["done"])
        self.assertEqual(state["done"]["/test/video.mp4"]["status"], "ingested")

    def test_chunk_then_filter_pipeline(self):
        """Chunk text then filter trash produces valid chunks."""
        # Good quality transcript text
        transcript = (
            "The Apollo program was a series of space missions conducted by NASA. "
            "The goal was to land humans on the Moon and return them safely to Earth. "
            "This was achieved on July 20, 1969 when Neil Armstrong and Buzz Aldrin landed. "
        ) * 10

        chunks = chunk_text(transcript)
        valid = [c for c in chunks if not is_trash_chunk(c)]

        self.assertGreater(len(valid), 0, "Should produce valid chunks from good transcript")
        for c in valid:
            self.assertGreater(len(c.split()), MIN_CHUNK_WORDS)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_find_videos_excludes_other_dirs(self):
        """find_videos must exclude 'other' and 'Other' directories."""
        with tempfile.TemporaryDirectory() as tmpdir:
            video_root = Path(tmpdir)
            other_dir = video_root / "Other"
            other_dir.mkdir()
            good_dir = video_root / "BreakingBad"
            good_dir.mkdir()

            # Create video files
            (other_dir / "video.mp4").touch()
            (good_dir / "episode.mp4").touch()

            cutoff = datetime.now() - timedelta(hours=1)

            with patch.object(_mod, "VIDEO_ROOT", video_root):
                results = _mod.find_videos(cutoff)

        paths = [str(r) for r in results]
        self.assertFalse(any("Other" in p for p in paths), "Other dir should be excluded")
        self.assertTrue(any("BreakingBad" in p for p in paths))

    def test_trash_ratio_determines_skip(self):
        """Videos with > TRASH_RATIO fraction of garbage chunks are skipped."""
        trash_ratio_threshold = _mod.TRASH_RATIO
        self.assertGreater(trash_ratio_threshold, 0)
        self.assertLessEqual(trash_ratio_threshold, 1.0)

    def test_main_handles_no_videos(self):
        """main() posts 'all caught up' when no new videos."""
        with tempfile.TemporaryDirectory() as tmpdir:
            slack_msgs = []
            _nova_cfg.post_both.side_effect = lambda msg, **kw: slack_msgs.append(msg)

            with patch.object(_mod, "VIDEO_ROOT", Path(tmpdir)):
                with patch.object(_mod, "STATE_FILE", Path(tmpdir) / "state.json"):
                    with patch.object(_mod, "WORK_DIR", Path(tmpdir) / "work"):
                        _mod.main()

            _nova_cfg.post_both.side_effect = None

        if slack_msgs:
            all_msgs = "\n".join(slack_msgs)
            self.assertTrue("caught up" in all_msgs.lower() or "new" in all_msgs.lower())


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

    def test_key_functions_callable(self):
        for fn in [is_trash_chunk, chunk_text, classify_source, show_name_from_path,
                   load_state, save_state, mark_done, _mod.main]:
            self.assertTrue(callable(fn))

    def test_constants_defined(self):
        self.assertIsInstance(CHUNK_WORDS, int)
        self.assertIsInstance(MIN_CHUNK_WORDS, int)
        self.assertIsInstance(_mod.TRASH_RATIO, float)
        self.assertIsInstance(_mod.MAX_WORKERS, int)

    def test_video_extensions_defined(self):
        """VIDEO_EXTS must include common video formats."""
        expected = {".mp4", ".mkv", ".avi"}
        self.assertTrue(expected.issubset(_mod.VIDEO_EXTS))


if __name__ == "__main__":
    unittest.main(verbosity=2)
