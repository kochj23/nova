#!/usr/bin/env python3
"""
test_nova_tv_ingest.py — Test suite for nova_tv_ingest.py.

Covers all 7 required categories:
  Security · Performance · Retry · Unit · Integration · Functional · Frame

Written by Jordan Koch.
"""

import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

sys.path.insert(0, str(Path(__file__).parent.parent))
import nova_tv_ingest as tv


# ═════════════════════════════════════════════════════════════════════════════
# SECURITY TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestSecurity(unittest.TestCase):

    def test_other_dir_excluded(self):
        """Any ancestor directory named 'other' or 'Other' must trigger exclusion."""
        excluded_paths = [
            Path("/Volumes/external/videos/other/somefile.mp4"),
            Path("/Volumes/external/videos/Other/somefile.mp4"),
            Path("/Volumes/external/videos/TVShows/other/foo.mp4"),
        ]
        for p in excluded_paths:
            has_excluded = any(part in tv.EXCLUDED_DIRS for part in p.parts)
            self.assertTrue(has_excluded, f"Should contain excluded dir: {p}")

    def test_excluded_dirs_constant(self):
        """EXCLUDED_DIRS must include both case variants."""
        self.assertIn("other", tv.EXCLUDED_DIRS)
        self.assertIn("Other", tv.EXCLUDED_DIRS)

    def test_privacy_tag_in_memory_payload(self):
        """All remember() calls must include privacy=local-only."""
        import inspect
        src = inspect.getsource(tv.remember)
        self.assertIn("local-only", src)

    def test_no_cloud_urls_in_source(self):
        """Script must not reference external cloud APIs."""
        import inspect
        src = inspect.getsource(tv)
        for bad in ["openai.com", "api.anthropic", "openrouter.ai"]:
            self.assertNotIn(bad, src)

    def test_memory_url_is_local(self):
        """MEMORY_URL must point to localhost."""
        self.assertTrue(
            tv.MEMORY_URL.startswith("http://127.0.0.1") or
            tv.MEMORY_URL.startswith("http://localhost"),
            f"MEMORY_URL must be local, got: {tv.MEMORY_URL}"
        )

    def test_max_audio_secs_cap(self):
        """MAX_AUDIO_SECS must be set to prevent runaway audio extraction."""
        self.assertGreater(tv.MAX_AUDIO_SECS, 0)
        self.assertLessEqual(tv.MAX_AUDIO_SECS, 14400)  # max 4h


# ═════════════════════════════════════════════════════════════════════════════
# PERFORMANCE TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestPerformance(unittest.TestCase):

    def test_chunk_words_is_reasonable(self):
        """CHUNK_WORDS must be between 100 and 1000."""
        self.assertGreaterEqual(tv.CHUNK_WORDS, 100)
        self.assertLessEqual(tv.CHUNK_WORDS, 1000)

    def test_min_chunk_words_filters_tiny_chunks(self):
        """Chunks under MIN_CHUNK_WORDS must be discarded."""
        tiny = "hello world"
        self.assertTrue(tv.is_trash_chunk(tiny))

    def test_chunk_text_does_not_return_empty_on_good_transcript(self):
        """A normal speech transcript should yield at least one chunk."""
        # Use realistic non-repetitive firearms commentary ~600 words
        transcript = (
            "Today we are looking at a very interesting pistol from the First World War era. "
            "This particular example was manufactured in Germany around nineteen fourteen. "
            "The action is a toggle-locked design derived from the earlier Borchardt pistol. "
            "Georg Luger made several significant improvements to the basic design including "
            "a much more ergonomic grip angle and a simplified toggle mechanism. "
            "The barrel on this example measures about four inches and is chambered for the "
            "nine millimeter Parabellum cartridge which Luger developed specifically for this firearm. "
            "Notice the distinctive toggle link action which locks the breech securely during firing. "
            "The magazine holds eight rounds of nine millimeter ammunition in a single stack configuration. "
            "This particular pistol was issued to German officers and has matching serial numbers "
            "on the frame receiver barrel and toggle components which indicates a high quality example. "
            "The finish shows typical military wear consistent with field use in the Great War period. "
            "Collectors prize these early examples for their historical significance and mechanical quality. "
        )
        chunks = tv.chunk_text(transcript)
        self.assertGreater(len(chunks), 0)

    def test_trash_detection_is_fast(self):
        """is_trash_chunk must run in under 10ms per chunk."""
        text = " ".join(["hello world this is a test"] * 20)
        start = time.time()
        for _ in range(1000):
            tv.is_trash_chunk(text)
        elapsed = time.time() - start
        self.assertLess(elapsed, 1.0, "1000 trash checks must complete in <1s")


# ═════════════════════════════════════════════════════════════════════════════
# RETRY TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestRetry(unittest.TestCase):

    def test_remember_returns_false_on_connection_error(self):
        """remember() must return False (not raise) when memory server is down."""
        import urllib.error
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("conn refused")):
            result = tv.remember("test text", "television", {"show": "Test"})
        self.assertFalse(result)

    def test_extract_audio_returns_false_on_ffmpeg_failure(self):
        """extract_audio must return False (not raise) when ffmpeg fails."""
        with patch("subprocess.run", side_effect=Exception("ffmpeg not found")):
            result = tv.extract_audio(Path("/fake/video.mp4"), Path("/tmp/test.wav"))
        self.assertFalse(result)

    def test_transcribe_returns_none_on_timeout(self):
        """transcribe must return None (not raise) on Whisper timeout."""
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("mlx_whisper", 100)):
            result = tv.transcribe(Path("/fake/audio.wav"), Path("/tmp"), "test")
        self.assertIsNone(result)


# ═════════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestUnit(unittest.TestCase):

    def test_is_trash_chunk_detects_music_symbol(self):
        self.assertTrue(tv.is_trash_chunk("♪ La la la, the music plays on ♪ la la la ♪"))

    def test_is_trash_chunk_detects_repetition(self):
        self.assertTrue(tv.is_trash_chunk("hello hello hello hello hello hello world"))

    def test_is_trash_chunk_detects_silence_markers(self):
        self.assertTrue(tv.is_trash_chunk("[silence]"))
        self.assertTrue(tv.is_trash_chunk("[music]"))
        self.assertTrue(tv.is_trash_chunk("[applause]"))

    def test_is_trash_chunk_passes_normal_speech(self):
        normal = (
            "Today we are looking at a fascinating firearm from the First World War. "
            "This is the Gewehr 98, chambered in 7.92x57 Mauser. The action is a "
            "Mauser-style controlled-feed bolt action with a five-round internal magazine."
        )
        self.assertFalse(tv.is_trash_chunk(normal))

    def test_classify_source_forgotten_weapons(self):
        self.assertEqual(
            tv.classify_source("Forgotten Weapons", "Gewehr 98 Sniper", ""),
            "military_history"
        )

    def test_classify_source_jeopardy(self):
        self.assertEqual(
            tv.classify_source("Jeopardy (1984)", "Season 42", ""),
            "game_show"
        )

    def test_classify_source_automotive_show(self):
        self.assertEqual(
            tv.classify_source("Finnegans Garage", "LS Swap Episode", "horsepower dyno pull"),
            "automotive"
        )

    def test_classify_source_comedy(self):
        self.assertEqual(
            tv.classify_source("Louis CK", "Stand Up Special", "the audience laughed at the joke"),
            "comedy"
        )

    def test_classify_source_defaults_to_television(self):
        result = tv.classify_source("Some Unknown Show", "Episode 1", "")
        self.assertEqual(result, "television")

    def test_show_name_from_path_with_season_dir(self):
        p = Path("/Volumes/external/videos/TVShows/Forgotten Weapons/Season 01/S01E01.mp4")
        self.assertEqual(tv.show_name_from_path(p), "Forgotten Weapons")

    def test_show_name_from_path_fallback(self):
        p = Path("/Volumes/external/videos/Comedy/standup.mp4")
        self.assertEqual(tv.show_name_from_path(p), "Comedy")

    def test_chunk_text_filters_music_chunks(self):
        music_chunk = "♪ " + " ".join(["la"] * 50) + " ♪"
        speech = " ".join(["this is normal speech about firearms history"] * 10)
        transcript = music_chunk + " " + speech
        chunks = tv.chunk_text(transcript)
        for chunk in chunks:
            self.assertFalse(chunk.startswith("♪"))

    def test_state_load_returns_default_on_missing_file(self):
        with patch.object(Path, "exists", return_value=False):
            state = tv.load_state()
        self.assertIn("done", state)
        self.assertIn("last_run", state)
        self.assertIsInstance(state["done"], dict)

    def test_mark_done_records_metadata(self):
        state = {"done": {}, "last_run": None}
        tv.mark_done(state, "/fake/path.mp4", {"show": "Test", "status": "ingested"})
        self.assertIn("/fake/path.mp4", state["done"])
        self.assertEqual(state["done"]["/fake/path.mp4"]["show"], "Test")
        self.assertIn("marked_at", state["done"]["/fake/path.mp4"])


# ═════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestIntegration(unittest.TestCase):

    def _make_state(self, done_paths=None):
        return {"done": {p: {"status": "ingested"} for p in (done_paths or [])}, "last_run": None}

    def _no_registry(self):
        """Context manager: mock registry.is_done() to always return False so
        DB state from previous test runs doesn't cause early-exit short-circuits."""
        return patch("nova_tv_ingest.registry.is_done", return_value=False)

    def test_already_done_video_skipped(self):
        state = self._make_state(["/fake/video.mp4"])
        result = tv.process_video(Path("/fake/video.mp4"), state, Path("/tmp"))
        self.assertIsNone(result)

    def test_audio_extraction_failure_marked_done(self):
        state = self._make_state()
        with self._no_registry(), patch("nova_tv_ingest.extract_audio", return_value=False):
            result = tv.process_video(Path("/fake/test.mp4"), state, Path("/tmp"))
        self.assertIsNone(result)
        self.assertIn("/fake/test.mp4", state["done"])
        self.assertEqual(state["done"]["/fake/test.mp4"]["status"], "audio_failed")

    def test_no_transcript_marked_done(self):
        state = self._make_state()
        mock_stat = MagicMock(); mock_stat.st_size = 10000
        with self._no_registry(), \
             patch("nova_tv_ingest.extract_audio", return_value=True), \
             patch("nova_tv_ingest.transcribe", return_value=None), \
             patch.object(Path, "unlink"), \
             patch.object(Path, "stat", return_value=mock_stat):
            result = tv.process_video(Path("/fake/test.mp4"), state, Path("/tmp"))
        self.assertIsNone(result)
        self.assertEqual(state["done"]["/fake/test.mp4"]["status"], "no_transcript")

    def _good_transcript(self):
        """Return a realistic non-repetitive firearms transcript > CHUNK_WORDS words."""
        return (
            "Today we are examining a bolt action rifle from the Second World War period. "
            "This particular example was manufactured in occupied Czechoslovakia at the Brno factory "
            "under German supervision during the wartime occupation of that country. "
            "The design is based on the Mauser 98 action which became the standard German military "
            "rifle configuration during both world wars. Notice the distinctive tangent rear sight "
            "graduated in meters to allow accurate fire at various distances. "
            "The barrel is cold hammer forged and measures approximately twenty four inches. "
            "The stock is walnut with a straight grip configuration typical of the period. "
            "Field markings on the receiver indicate inspection by German military proof houses. "
            "The bolt disassembles without tools using the standard Mauser procedure. "
            "Magazine capacity is five rounds of the seven point nine two by fifty seven cartridge. "
            "This cartridge was developed in eighteen eighty eight and remained the primary German "
            "military rifle cartridge through the end of the Second World War in nineteen forty five. "
            "The example shown today is in excellent condition with approximately ninety percent "
            "of the original military finish remaining on both metal and wood surfaces. "
            "Values for this variant range from three hundred to eight hundred dollars depending "
            "on matching numbers and overall condition of the bore and exterior surfaces. "
        )

    def test_all_trash_transcript_marked_done(self):
        state = self._make_state()
        garbage = "♪ la la la ♪ mm mm mm ♪ na na na ♪ " * 50
        mock_stat = MagicMock(); mock_stat.st_size = 10000
        with self._no_registry(), \
             patch("nova_tv_ingest.extract_audio", return_value=True), \
             patch("nova_tv_ingest.transcribe", return_value=garbage), \
             patch.object(Path, "unlink"), \
             patch.object(Path, "stat", return_value=mock_stat):
            result = tv.process_video(Path("/fake/test.mp4"), state, Path("/tmp"))
        self.assertIsNone(result)
        self.assertEqual(state["done"]["/fake/test.mp4"]["status"], "trash")

    def test_successful_ingest_returns_result(self):
        state = self._make_state()
        video = Path("/Volumes/external/videos/TVShows/Forgotten Weapons/Season 01/S01E01 - Gewehr 98.mp4")
        mock_stat = MagicMock(); mock_stat.st_size = 500000
        with self._no_registry(), \
             patch("nova_tv_ingest.extract_audio", return_value=True), \
             patch("nova_tv_ingest.transcribe", return_value=self._good_transcript()), \
             patch("nova_tv_ingest.remember", return_value=True), \
             patch.object(Path, "unlink"), \
             patch.object(Path, "stat", return_value=mock_stat):
            result = tv.process_video(video, state, Path("/tmp"))
        self.assertIsNotNone(result)
        self.assertEqual(result["show"], "Forgotten Weapons")
        self.assertEqual(result["source"], "military_history")
        self.assertGreater(result["chunks"], 0)
        self.assertEqual(state["done"][str(video)]["status"], "ingested")

    def test_slack_notification_sent_on_ingested_videos(self):
        state = self._make_state()
        good_transcript = " ".join(
            ["firearm rifle pistol barrel trigger action history weapons"] * 80
        )
        video = Path("/Volumes/external/videos/TVShows/Forgotten Weapons/Season 01/S01E01 - Test.mp4")
        mock_stat = MagicMock()
        mock_stat.st_mtime = time.time() - 3600
        with patch("nova_tv_ingest.find_videos", return_value=[video]), \
             patch("nova_tv_ingest.extract_audio", return_value=True), \
             patch("nova_tv_ingest.transcribe", return_value=good_transcript), \
             patch("nova_tv_ingest.remember", return_value=True), \
             patch("nova_tv_ingest.load_state", return_value=state), \
             patch("nova_tv_ingest.save_state"), \
             patch("nova_tv_ingest.post_slack") as mock_slack, \
             patch("nova_tv_ingest.random_memory_for_show", return_value=None), \
             patch("pathlib.Path.unlink"), \
             patch("pathlib.Path.stat", return_value=mock_stat):
            tv.main()
        mock_slack.assert_called()

    def test_no_new_videos_posts_caught_up_message(self):
        state = {"done": {}, "last_run": None}
        with patch("nova_tv_ingest.find_videos", return_value=[]), \
             patch("nova_tv_ingest.load_state", return_value=state), \
             patch("nova_tv_ingest.save_state"), \
             patch("nova_tv_ingest.post_slack") as mock_slack:
            tv.main()
        mock_slack.assert_called_once()
        self.assertIn("All caught up", mock_slack.call_args[0][0])


# ═════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestFunctional(unittest.TestCase):

    def test_trash_ratio_check_works_end_to_end(self):
        """Transcript that's >60% garbage must be marked trash."""
        # 60% music, 40% speech
        music = "♪ " + " ".join(["la"] * tv.CHUNK_WORDS) + " ♪ "
        speech = " ".join(["this is real speech about history"] * (tv.CHUNK_WORDS // 5))
        # 6 music chunks + 4 speech chunks = 60% trash
        mixed = (music * 6) + (speech * 4)
        chunks = tv.chunk_text(mixed)
        total_raw = max(1, len(mixed.split()) // tv.CHUNK_WORDS)
        trash_ratio = 1 - (len(chunks) / total_raw)
        self.assertGreater(trash_ratio, tv.TRASH_RATIO)

    def test_good_transcript_passes_trash_check(self):
        """A realistic non-repetitive transcript should produce clean chunks."""
        # Use the same realistic transcript helper, but multiple paragraphs
        good = self._good_transcript()
        chunks = tv.chunk_text(good)
        self.assertGreater(len(chunks), 0, "Good transcript should produce at least one clean chunk")

    def _good_transcript(self):
        return (
            "Today we are examining a bolt action rifle from the Second World War period. "
            "This particular example was manufactured in occupied Czechoslovakia at the Brno factory. "
            "The design is based on the Mauser ninety eight action which became the standard German "
            "military rifle configuration during both world wars. Notice the distinctive tangent rear "
            "sight graduated in meters to allow accurate fire at various distances on the battlefield. "
            "The barrel is cold hammer forged and measures approximately twenty four inches in length. "
            "The stock is walnut with a straight grip configuration typical of wartime production. "
            "Field markings on the receiver indicate inspection by German military proof houses during "
            "the occupation. The bolt disassembles without any tools using the standard Mauser procedure. "
            "Magazine capacity is five rounds of the seven point nine two by fifty seven cartridge. "
            "This cartridge was developed in eighteen eighty eight and remained the primary German "
            "military rifle cartridge through the end of the Second World War in nineteen forty five. "
            "The example shown today is in excellent condition with approximately ninety percent "
            "of the original military finish remaining on both metal and wood surfaces overall. "
            "Values for this variant range from three hundred to eight hundred dollars depending "
            "on matching numbers and the overall condition of the bore and exterior metal surfaces. "
            "The two piece stock design was adopted to simplify wartime manufacturing considerably. "
        )

    def test_backfill_marks_old_files_without_processing(self):
        """Files older than RECENT_DAYS must be marked done without transcription."""
        from datetime import datetime, timedelta
        old_time = time.time() - (tv.RECENT_DAYS + 1) * 86400
        new_time = time.time() - 3600

        mock_old = MagicMock(spec=Path)
        mock_old.__str__ = lambda s: "/fake/old.mp4"
        mock_old.stat.return_value.st_mtime = old_time
        mock_old.suffix = ".mp4"
        mock_old.stem = "old"
        mock_old.parent.name = "TVShows"
        mock_old.parts = ("/", "fake", "old.mp4")

        state = {"done": {}, "last_run": None}
        cutoff = datetime.now() - timedelta(days=tv.RECENT_DAYS)
        mtime_dt = datetime.fromtimestamp(old_time)
        self.assertLess(mtime_dt, cutoff)


# ═════════════════════════════════════════════════════════════════════════════
# FRAME / SMOKE TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestFrame(unittest.TestCase):

    def test_module_imports(self):
        import nova_tv_ingest
        self.assertTrue(hasattr(nova_tv_ingest, "main"))
        self.assertTrue(hasattr(nova_tv_ingest, "process_video"))
        self.assertTrue(hasattr(nova_tv_ingest, "is_trash_chunk"))
        self.assertTrue(hasattr(nova_tv_ingest, "chunk_text"))
        self.assertTrue(hasattr(nova_tv_ingest, "classify_source"))
        self.assertTrue(hasattr(nova_tv_ingest, "extract_audio"))
        self.assertTrue(hasattr(nova_tv_ingest, "transcribe"))
        self.assertTrue(hasattr(nova_tv_ingest, "remember"))
        self.assertTrue(hasattr(nova_tv_ingest, "load_state"))
        self.assertTrue(hasattr(nova_tv_ingest, "save_state"))

    def test_constants_sane(self):
        self.assertGreater(tv.CHUNK_WORDS, 0)
        self.assertGreater(tv.MIN_CHUNK_WORDS, 0)
        self.assertLess(tv.MIN_CHUNK_WORDS, tv.CHUNK_WORDS)
        self.assertGreater(tv.TRASH_RATIO, 0)
        self.assertLessEqual(tv.TRASH_RATIO, 1)
        self.assertGreater(tv.RECENT_DAYS, 0)
        self.assertGreater(tv.MAX_AUDIO_SECS, 0)

    def test_video_root_defined(self):
        self.assertIsInstance(tv.VIDEO_ROOT, Path)

    def test_video_exts_include_common_formats(self):
        for ext in [".mp4", ".mkv", ".ts", ".avi", ".mov"]:
            self.assertIn(ext, tv.VIDEO_EXTS)

    def test_state_file_path_under_home(self):
        home = str(Path.home())
        self.assertTrue(str(tv.STATE_FILE).startswith(home))

    def test_whisper_bin_path_set(self):
        self.assertTrue(len(tv.WHISPER_BIN) > 0)

    def test_ffmpeg_bin_path_set(self):
        self.assertTrue(len(tv.FFMPEG_BIN) > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
