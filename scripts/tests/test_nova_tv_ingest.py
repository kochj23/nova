#!/usr/bin/env python3
"""
test_nova_tv_ingest.py — Test suite for nova_tv_ingest.py.

Covers all 7 required categories:
  Security · Performance · Retry · Unit · Integration · Functional · Frame

Current contract (2026): state lives in PostgreSQL (nova_ops.media_ingest_state,
no JSON state file / no RECENT_DAYS window), audio is extracted as 5-minute WAV
segments, transcription goes through OpenRouter (key from macOS Keychain), memory
chunks stay on the in-fleet memory server tagged privacy=local-only, and all
notifications go through the nova_notify bus via post_slack(). Every DB / network
/ ffmpeg boundary is mocked here.

Written by Jordan Koch.
"""

import contextlib
import json
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import nova_tv_ingest as tv


# ── Helpers ──────────────────────────────────────────────────────────────────

def _fake_db(rows=None):
    """A fake psycopg2 connection whose cursor returns `rows` from fetchall()."""
    cur = MagicMock()
    cur.fetchall.return_value = list(rows or [])
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def _fake_psycopg2(existing_chunks=0):
    """Fake `psycopg2` module for the nova_memories dedup pre-check in process_video."""
    mod = MagicMock()
    mod.connect.return_value.cursor.return_value.fetchone.return_value = (existing_chunks,)
    return mod


def _segments(n=1):
    segs = []
    for i in range(n):
        seg = MagicMock(spec=Path)
        seg.stem = f"seg{i:05d}"
        segs.append(seg)
    return segs


def _make_state(done_paths=None):
    return {"done": {p: True for p in (done_paths or [])}}


def _good_transcript():
    """Realistic non-repetitive transcript > CHUNK_WORDS words."""
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
    ) * 3


@contextlib.contextmanager
def _pipeline(existing_chunks=0, segments=None, transcript=None, remember_ok=True):
    """Mock every external boundary process_video()/main() touch.

    Yields a dict of the mocks: db cursor, registry, remember, post_slack, extract.
    """
    conn, cur = _fake_db()
    with patch.object(tv, "_db", return_value=conn), \
         patch.object(tv, "registry") as registry, \
         patch.dict(sys.modules, {"psycopg2": _fake_psycopg2(existing_chunks)}), \
         patch.object(tv, "extract_audio_segments",
                      return_value=_segments(1) if segments is None else segments) as extract, \
         patch.object(tv, "transcribe", return_value=transcript) as transcribe, \
         patch.object(tv, "remember", return_value=remember_ok) as remember, \
         patch.object(tv, "random_memory_for_show", return_value=None), \
         patch.object(tv, "post_slack") as post_slack, \
         patch.object(tv, "log"):
        yield {"cur": cur, "registry": registry, "extract": extract, "transcribe": transcribe,
               "remember": remember, "post_slack": post_slack}


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

    def test_find_videos_prunes_excluded_dirs(self):
        """os.walk pruning: nothing under other/Other is ever yielded."""
        walk = [
            ("/Volumes/external/videos", ["TVShows", "Other", "other"], []),
            ("/Volumes/external/videos/TVShows", [], ["a.mp4", "notes.txt", "b.MKV"]),
        ]
        with patch.object(tv.os, "walk", return_value=walk):
            found = tv.find_videos()
        names = sorted(p.name for p in found)
        self.assertEqual(names, ["a.mp4", "b.MKV"])
        # pruning happens in-place on the dirs list of the root entry
        self.assertEqual(walk[0][1], ["TVShows"])

    def test_excluded_dirs_constant(self):
        """EXCLUDED_DIRS must include both case variants."""
        self.assertIn("other", tv.EXCLUDED_DIRS)
        self.assertIn("Other", tv.EXCLUDED_DIRS)

    def test_privacy_tag_in_memory_payload(self):
        """All remember() calls must include privacy=local-only."""
        import inspect
        src = inspect.getsource(tv.remember)
        self.assertIn("local-only", src)

    def test_remember_payload_is_local_only_long_term(self):
        resp = MagicMock()
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=resp) as uo:
            ok = tv.remember("some text", "television", {"show": "X"})
        self.assertTrue(ok)
        req = uo.call_args[0][0]
        self.assertEqual(req.full_url, tv.MEMORY_URL)
        payload = json.loads(req.data)
        self.assertEqual(payload["privacy"], "local-only")
        self.assertEqual(payload["tier"], "long_term")
        self.assertEqual(payload["source"], "television")

    def test_no_llm_vendor_apis_in_source(self):
        """Memory storage never goes to a vendor API; the only cloud hop is the
        OpenRouter transcription endpoint (see test_openrouter_key_from_keychain)."""
        import inspect
        src = inspect.getsource(tv)
        for bad in ["openai.com", "api.anthropic"]:
            self.assertNotIn(bad, src)
        self.assertTrue(tv.OPENROUTER_URL.startswith("https://openrouter.ai/"))

    def test_openrouter_key_from_keychain(self):
        """OpenRouter key is pulled from the macOS Keychain at runtime — never hardcoded."""
        import inspect
        src = inspect.getsource(tv)
        self.assertNotIn("sk-or-", src)
        self.assertNotIn("Bearer sk-", src)
        self.assertIn("find-generic-password", src)
        tv._openrouter_key = None
        with patch.object(tv.subprocess, "check_output", return_value="kc-secret\n") as co:
            self.assertEqual(tv._get_openrouter_key(), "kc-secret")
            self.assertEqual(tv._get_openrouter_key(), "kc-secret")   # cached
        self.assertEqual(co.call_count, 1)
        self.assertIn("security", co.call_args[0][0][0])
        tv._openrouter_key = None

    def test_memory_url_is_in_fleet(self):
        """MEMORY_URL must be the in-fleet memory server (service DNS or loopback), plain
        http on the LAN — never a public host."""
        self.assertTrue(
            tv.MEMORY_URL.startswith("http://memory-server.digitalnoise.net:18790/") or
            tv.MEMORY_URL.startswith("http://127.0.0.1:18790/") or
            tv.MEMORY_URL.startswith("http://localhost:18790/"),
            f"MEMORY_URL must be the fleet memory server, got: {tv.MEMORY_URL}"
        )
        self.assertTrue(tv.MEMORY_URL.endswith("/remember"))

    def test_max_audio_secs_cap(self):
        """MAX_AUDIO_SECS must be set to prevent runaway audio extraction."""
        self.assertGreater(tv.MAX_AUDIO_SECS, 0)
        self.assertLessEqual(tv.MAX_AUDIO_SECS, 14400)  # max 4h

    def test_db_state_is_local_nova_ops(self):
        import inspect
        src = inspect.getsource(tv._db)
        self.assertIn('dbname="nova_ops"', src)
        self.assertIn('host="localhost"', src)
        self.assertNotIn("password", src)


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
        chunks = tv.chunk_text(_good_transcript())
        self.assertGreater(len(chunks), 0)

    def test_trash_detection_is_fast(self):
        """is_trash_chunk must run in under 1ms per chunk."""
        text = " ".join(["hello world this is a test"] * 20)
        start = time.time()
        for _ in range(1000):
            tv.is_trash_chunk(text)
        elapsed = time.time() - start
        self.assertLess(elapsed, 1.0, "1000 trash checks must complete in <1s")

    def test_segment_size_bounded_for_api_payload(self):
        """5-min 16kHz mono PCM ≈ 9.6MB — must stay under the ~15MB request cap."""
        bytes_per_seg = tv.SEGMENT_SECS * 16000 * 2
        self.assertLess(bytes_per_seg, 15 * 1024 * 1024)
        self.assertEqual(tv.MAX_AUDIO_SECS % tv.SEGMENT_SECS, 0)

    def test_concurrency_limits_sane(self):
        self.assertGreater(tv.MAX_WORKERS, 0)
        self.assertGreater(tv.MAX_FFMPEG, 0)
        self.assertLessEqual(tv.MAX_FFMPEG, tv.MAX_WORKERS)
        self.assertEqual(tv._FFMPEG_SEM._value, tv.MAX_FFMPEG)


# ═════════════════════════════════════════════════════════════════════════════
# RETRY TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestRetry(unittest.TestCase):

    def setUp(self):
        self.wav = Path(__file__).parent / "_tv_ingest_test.wav"
        self.wav.write_bytes(b"RIFF" + b"\x00" * 64)
        self.addCleanup(lambda: self.wav.unlink(missing_ok=True))
        tv._openrouter_key = "test-key"
        self.addCleanup(lambda: setattr(tv, "_openrouter_key", None))

    def test_remember_returns_false_on_connection_error(self):
        """remember() must return False (not raise) when memory server is down."""
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("conn refused")):
            result = tv.remember("test text", "television", {"show": "Test"})
        self.assertFalse(result)

    def test_extract_audio_segments_returns_empty_on_ffmpeg_failure(self):
        """extract_audio_segments must return [] (not raise) when ffmpeg is missing."""
        with patch("subprocess.run", side_effect=Exception("ffmpeg not found")):
            result = tv.extract_audio_segments(Path("/fake/video.mp4"), Path("/tmp"), "stem")
        self.assertEqual(result, [])

    def test_extract_audio_segments_honors_duration_and_cap(self, ):
        """Segments are cut every SEGMENT_SECS up to min(duration, MAX_AUDIO_SECS)."""
        probe = MagicMock(stderr="  Duration: 00:12:30.00, start: 0.0")
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return probe
        with patch("subprocess.run", side_effect=fake_run), \
             patch.object(Path, "exists", return_value=True), \
             patch.object(Path, "stat", return_value=MagicMock(st_size=50000)):
            segs = tv.extract_audio_segments(Path("/fake/v.mp4"), Path("/tmp/work"), "stem")
        # 750s -> starts at 0,300,600 = 3 segments (+1 probe call)
        self.assertEqual(len(segs), 3)
        self.assertEqual(len(calls), 4)
        self.assertEqual(calls[1][calls[1].index("-ss") + 1], "0")
        self.assertEqual(calls[3][calls[3].index("-ss") + 1], "600")

    def test_transcribe_returns_none_on_timeout_after_retries(self):
        """Network timeouts are retried MAX_RETRIES times, then None (never raise)."""
        with patch("urllib.request.urlopen", side_effect=TimeoutError()) as uo, \
             patch.object(tv.time, "sleep") as slp:
            result = tv.transcribe(self.wav, Path("/tmp"), "test")
        self.assertIsNone(result)
        self.assertEqual(uo.call_count, tv.MAX_RETRIES)
        self.assertEqual(slp.call_count, tv.MAX_RETRIES)

    def test_transcribe_retries_429_and_5xx(self):
        err = urllib.error.HTTPError(tv.OPENROUTER_URL, 429, "rate", None, None)
        err.read = lambda: b"slow down"
        with patch("urllib.request.urlopen", side_effect=err) as uo, \
             patch.object(tv.time, "sleep"):
            self.assertIsNone(tv.transcribe(self.wav, Path("/tmp"), "t"))
        self.assertEqual(uo.call_count, tv.MAX_RETRIES)

    def test_transcribe_does_not_retry_4xx(self):
        err = urllib.error.HTTPError(tv.OPENROUTER_URL, 400, "bad", None, None)
        err.read = lambda: b"bad request"
        with patch("urllib.request.urlopen", side_effect=err) as uo, \
             patch.object(tv.time, "sleep"):
            self.assertIsNone(tv.transcribe(self.wav, Path("/tmp"), "t"))
        self.assertEqual(uo.call_count, 1)

    def test_transcribe_success_and_empty_sentinel(self):
        def resp_with(text):
            r = MagicMock()
            r.read.return_value = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
            r.__enter__ = MagicMock(return_value=r)
            r.__exit__ = MagicMock(return_value=False)
            return r
        good = "This is a perfectly normal transcript of spoken words from the episode."
        with patch("urllib.request.urlopen", return_value=resp_with(good)) as uo:
            self.assertEqual(tv.transcribe(self.wav, Path("/tmp"), "t"), good)
        req = uo.call_args[0][0]
        self.assertEqual(req.get_header("Authorization"), "Bearer test-key")
        body = json.loads(req.data)
        self.assertEqual(body["model"], tv.OPENROUTER_MODEL)
        self.assertEqual(body["messages"][0]["content"][0]["input_audio"]["format"], "wav")
        with patch("urllib.request.urlopen", return_value=resp_with("EMPTY")):
            self.assertIsNone(tv.transcribe(self.wav, Path("/tmp"), "t"))

    def test_transcribe_returns_none_on_unreadable_wav(self):
        with patch("urllib.request.urlopen") as uo:
            self.assertIsNone(tv.transcribe(Path("/nonexistent/x.wav"), Path("/tmp"), "t"))
        uo.assert_not_called()

    def test_mark_done_db_failure_does_not_raise(self):
        """A PG hiccup must not kill a worker — in-memory state still updated."""
        state = _make_state()
        with patch.object(tv, "_db", side_effect=Exception("pg down")), patch.object(tv, "log"):
            tv.mark_done(state, "/fake/path.mp4", {"status": "ingested"})
        self.assertTrue(state["done"]["/fake/path.mp4"])


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

    def test_classify_source_local_news_beats_content_keywords(self):
        """LA-market stations file under local_news even if the transcript mentions
        'combat'/'engine' — news is checked FIRST (2026-08-12 fix)."""
        self.assertEqual(tv.classify_source("KTLA 5 Morning News", "Ep 1", "engine fire combat"),
                         "local_news")
        self.assertEqual(tv.classify_source("NBC Nightly News", "Ep 1", "a century of war"),
                         "news")

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

    def test_load_state_reads_tracked_paths_from_pg(self):
        conn, cur = _fake_db(rows=[("/a.mp4",), ("/b.mkv",)])
        with patch.object(tv, "_db", return_value=conn), patch.object(tv, "log"):
            state = tv.load_state()
        self.assertEqual(state, {"done": {"/a.mp4": True, "/b.mkv": True}})
        self.assertIn("media_ingest_state", cur.execute.call_args[0][0])

    def test_load_state_empty_table(self):
        conn, _ = _fake_db(rows=[])
        with patch.object(tv, "_db", return_value=conn), patch.object(tv, "log"):
            state = tv.load_state()
        self.assertEqual(state, {"done": {}})

    def test_mark_done_records_metadata(self):
        state = _make_state()
        conn, cur = _fake_db()
        with patch.object(tv, "_db", return_value=conn):
            tv.mark_done(state, "/fake/path.mp4",
                         {"show": "Test", "title": "Ep", "status": "ingested",
                          "chunks": 4, "words": 1200, "source": "television"})
        self.assertTrue(state["done"]["/fake/path.mp4"])
        sql, params = cur.execute.call_args[0]
        self.assertIn("INSERT INTO media_ingest_state", sql)
        self.assertIn("ON CONFLICT (file_path)", sql)
        self.assertEqual(params, ("/fake/path.mp4", "Test", "Ep", "ingested", 4, 1200, "television"))

    def test_mark_done_defaults_status_unknown(self):
        conn, cur = _fake_db()
        with patch.object(tv, "_db", return_value=conn):
            tv.mark_done(_make_state(), "/x.mp4", {})
        self.assertEqual(cur.execute.call_args[0][1][3], "unknown")


# ═════════════════════════════════════════════════════════════════════════════
# INTEGRATION TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestIntegration(unittest.TestCase):

    VIDEO = Path("/Volumes/external/videos/TVShows/Forgotten Weapons/Season 01/S01E01 - Gewehr 98.mp4")

    def test_already_done_video_skipped(self):
        state = _make_state(["/fake/video.mp4"])
        with _pipeline() as m:
            result = tv.process_video(Path("/fake/video.mp4"), state, Path("/tmp"))
        self.assertIsNone(result)
        m["registry"].register_file.assert_not_called()
        m["extract"].assert_not_called()

    def test_registered_in_media_registry(self):
        state = _make_state()
        with _pipeline(segments=[]) as m:
            tv.process_video(self.VIDEO, state, Path("/tmp"))
        m["registry"].register_file.assert_called_once_with(
            str(self.VIDEO), show_name="Forgotten Weapons", title="S01E01 - Gewehr 98",
            ingest_script="nova_tv_ingest.py")

    def test_existing_memories_short_circuit_as_already_known(self):
        """nova_memories is the source of truth: chunks already there -> no ffmpeg."""
        state = _make_state()
        with _pipeline(existing_chunks=7) as m:
            result = tv.process_video(self.VIDEO, state, Path("/tmp"))
        self.assertIsNone(result)
        self.assertTrue(state["done"][str(self.VIDEO)])
        self.assertEqual(m["cur"].execute.call_args[0][1][3], "already_known")
        m["registry"].mark_ingested.assert_called_once_with(str(self.VIDEO), 7, "")
        m["extract"].assert_not_called()

    def test_audio_extraction_failure_marked_done(self):
        state = _make_state()
        with _pipeline(segments=[]) as m:
            result = tv.process_video(Path("/fake/test.mp4"), state, Path("/tmp"))
        self.assertIsNone(result)
        self.assertIn("/fake/test.mp4", state["done"])
        self.assertEqual(m["cur"].execute.call_args[0][1][3], "audio_failed")
        m["registry"].mark_status.assert_called_once_with("/fake/test.mp4", "audio_failed")

    def test_no_transcript_marked_done(self):
        state = _make_state()
        segs = _segments(2)
        with _pipeline(segments=segs, transcript=None) as m:
            result = tv.process_video(Path("/fake/test.mp4"), state, Path("/tmp"))
        self.assertIsNone(result)
        self.assertEqual(m["cur"].execute.call_args[0][1][3], "no_transcript")
        m["registry"].mark_status.assert_called_once_with("/fake/test.mp4", "no_transcript")
        for seg in segs:   # temp WAVs are always cleaned up
            seg.unlink.assert_called_once_with(missing_ok=True)

    def test_all_trash_transcript_marked_done(self):
        state = _make_state()
        garbage = "♪ la la la ♪ mm mm mm ♪ na na na ♪ " * 50
        with _pipeline(transcript=garbage) as m:
            result = tv.process_video(Path("/fake/test.mp4"), state, Path("/tmp"))
        self.assertIsNone(result)
        self.assertEqual(m["cur"].execute.call_args[0][1][3], "trash")
        m["registry"].mark_status.assert_called_once_with("/fake/test.mp4", "trash")
        m["remember"].assert_not_called()

    def test_successful_ingest_returns_result(self):
        state = _make_state()
        with _pipeline(transcript=_good_transcript()) as m:
            result = tv.process_video(self.VIDEO, state, Path("/tmp"), next_title="S01E02")
        self.assertIsNotNone(result)
        self.assertEqual(result["show"], "Forgotten Weapons")
        self.assertEqual(result["source"], "military_history")
        self.assertGreater(result["chunks"], 0)
        self.assertEqual(result["chunks"], m["remember"].call_count)
        self.assertTrue(state["done"][str(self.VIDEO)])
        self.assertEqual(m["cur"].execute.call_args[0][1][3], "ingested")
        m["registry"].mark_ingested.assert_called_once_with(str(self.VIDEO), result["chunks"],
                                                            "military_history")
        # memory metadata carries the source_file used for dedup
        meta = m["remember"].call_args[0][2]
        self.assertEqual(meta["source_file"], str(self.VIDEO))
        self.assertEqual(meta["type"], "tv_transcript")
        # per-episode notification with next-up pointer
        m["post_slack"].assert_called_once()
        msg = m["post_slack"].call_args[0][0]
        self.assertIn("Forgotten Weapons", msg)
        self.assertIn("S01E02", msg)

    def test_all_chunks_duplicate_is_already_known(self):
        """remember() False for every chunk = memory server already had them."""
        state = _make_state()
        with _pipeline(transcript=_good_transcript(), remember_ok=False) as m:
            result = tv.process_video(self.VIDEO, state, Path("/tmp"))
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(m["cur"].execute.call_args[0][1][3], "already_known")
        m["registry"].mark_ingested.assert_not_called()
        m["post_slack"].assert_not_called()

    def test_slack_notification_sent_on_ingested_videos(self):
        video = Path("/Volumes/external/videos/TVShows/Forgotten Weapons/Season 01/S01E01 - Test.mp4")
        with _pipeline(transcript=_good_transcript()) as m, \
             patch.object(tv, "find_videos", return_value=[video]), \
             patch.object(tv, "load_state", return_value=_make_state()), \
             patch.object(tv, "WORK_DIR", MagicMock(spec=Path)):
            tv.main()
        msgs = [c[0][0] for c in m["post_slack"].call_args_list]
        self.assertTrue(any("TV Ingest starting" in x for x in msgs))
        self.assertTrue(any("TV Ingest Complete" in x and "1 episodes ingested" in x for x in msgs))
        self.assertTrue(any("Forgotten Weapons" in x for x in msgs))

    def test_no_new_videos_posts_caught_up_message(self):
        with _pipeline() as m, \
             patch.object(tv, "find_videos", return_value=[]), \
             patch.object(tv, "load_state", return_value=_make_state()), \
             patch.object(tv, "WORK_DIR", MagicMock(spec=Path)):
            tv.main()
        m["post_slack"].assert_called_once()
        self.assertIn("All caught up", m["post_slack"].call_args[0][0])

    def test_post_slack_routes_to_notify_bus(self):
        with patch.object(tv, "notify") as notify:
            tv.post_slack(":tv: *TV Ingest — today*\nline two")
        notify.assert_called_once()
        title, kwargs = notify.call_args[0][0], notify.call_args[1]
        self.assertEqual(title, "TV Ingest — today")
        self.assertEqual(kwargs["body"], "line two")
        self.assertEqual(kwargs["level"], "info")
        self.assertEqual(kwargs["category"], "tv")

    def test_post_slack_never_raises(self):
        with patch.object(tv, "notify", side_effect=RuntimeError("bus down")), patch.object(tv, "log"):
            tv.post_slack("hello")


# ═════════════════════════════════════════════════════════════════════════════
# FUNCTIONAL TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestFunctional(unittest.TestCase):

    def test_trash_ratio_check_works_end_to_end(self):
        """Transcript that's >60% garbage must be marked trash."""
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
        chunks = tv.chunk_text(_good_transcript())
        self.assertGreater(len(chunks), 0, "Good transcript should produce at least one clean chunk")

    def test_main_processes_only_untracked_files(self):
        """No time window any more: everything not in media_ingest_state is processed,
        everything tracked is skipped — regardless of file age."""
        tracked = Path("/Volumes/external/videos/TVShows/A/Season 01/old.mp4")
        fresh = Path("/Volumes/external/videos/TVShows/A/Season 01/new.mp4")
        with _pipeline(segments=[]) as m, \
             patch.object(tv, "find_videos", return_value=[tracked, fresh]), \
             patch.object(tv, "load_state", return_value=_make_state([str(tracked)])), \
             patch.object(tv, "WORK_DIR", MagicMock(spec=Path)):
            tv.main()
        m["registry"].register_file.assert_called_once()
        self.assertEqual(m["registry"].register_file.call_args[0][0], str(fresh))
        start_msg = m["post_slack"].call_args_list[0][0][0]
        self.assertIn("1 videos to process", start_msg)
        self.assertIn("1 already tracked", start_msg)

    def test_worker_exception_is_recorded_as_error_not_fatal(self):
        video = Path("/Volumes/external/videos/TVShows/A/Season 01/boom.mp4")
        with _pipeline() as m, \
             patch.object(tv, "process_video", side_effect=RuntimeError("worker died")), \
             patch.object(tv, "find_videos", return_value=[video]), \
             patch.object(tv, "load_state", return_value=_make_state()), \
             patch.object(tv, "WORK_DIR", MagicMock(spec=Path)):
            tv.main()   # must not raise
        self.assertEqual(m["cur"].execute.call_args[0][1][3], "error")
        final = m["post_slack"].call_args_list[-1][0][0]
        self.assertIn("1 errors", final)


# ═════════════════════════════════════════════════════════════════════════════
# FRAME / SMOKE TESTS
# ═════════════════════════════════════════════════════════════════════════════

class TestFrame(unittest.TestCase):

    def test_module_imports(self):
        import nova_tv_ingest
        for name in ("main", "process_video", "is_trash_chunk", "chunk_text", "classify_source",
                     "extract_audio_segments", "transcribe", "remember", "load_state",
                     "mark_done", "find_videos", "show_name_from_path", "post_slack"):
            self.assertTrue(hasattr(nova_tv_ingest, name), name)

    def test_legacy_api_is_gone(self):
        """JSON state file / recency window / local whisper were retired for PG state,
        full-library sweeps and OpenRouter transcription."""
        for gone in ("STATE_FILE", "RECENT_DAYS", "WHISPER_BIN", "extract_audio", "save_state"):
            self.assertFalse(hasattr(tv, gone), gone)

    def test_constants_sane(self):
        self.assertGreater(tv.CHUNK_WORDS, 0)
        self.assertGreater(tv.MIN_CHUNK_WORDS, 0)
        self.assertLess(tv.MIN_CHUNK_WORDS, tv.CHUNK_WORDS)
        self.assertGreater(tv.TRASH_RATIO, 0)
        self.assertLessEqual(tv.TRASH_RATIO, 1)
        self.assertGreater(tv.MAX_AUDIO_SECS, 0)
        self.assertGreater(tv.SEGMENT_SECS, 0)
        self.assertGreaterEqual(tv.MAX_RETRIES, 1)

    def test_video_root_defined(self):
        self.assertIsInstance(tv.VIDEO_ROOT, Path)

    def test_video_exts_include_common_formats(self):
        for ext in [".mp4", ".mkv", ".ts", ".avi", ".mov"]:
            self.assertIn(ext, tv.VIDEO_EXTS)

    def test_work_dir_on_data_volume_not_main_ssd(self):
        self.assertTrue(str(tv.WORK_DIR).startswith("/Volumes/Data/"), tv.WORK_DIR)

    def test_log_file_path_under_home(self):
        self.assertTrue(str(tv.LOG_FILE).startswith(str(Path.home())))

    def test_openrouter_model_set(self):
        self.assertTrue(len(tv.OPENROUTER_MODEL) > 0)

    def test_ffmpeg_bin_path_set(self):
        self.assertTrue(len(tv.FFMPEG_BIN) > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
