#!/usr/bin/env python3
"""Tests for nova_tv_retry_failed.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_tv_retry_failed.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_tv_retry_failed_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tv = _load()
_VOCAB = ("engine torque camshaft rebuilt weekend figures improved considerably team took strip sunday "
          "garage builder explained valve timing pressure oil cooling exhaust intake manifold carburetor "
          "dyno pulls numbers looked strong afternoon crew tested again before lunch finished painting").split()


def _speech(n, seed=1):
    import random
    r = random.Random(seed)
    return " ".join(r.choice(_VOCAB) for _ in range(n)) + "."


SPEECH = ("The engine was rebuilt over the weekend and the torque figures improved considerably "
          "after the new camshaft went in so the team took it to the drag strip on Sunday. ")


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _resp(text):
    return _Resp(json.dumps({"choices": [{"message": {"content": text}}]}).encode())


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ps = [patch.object(tv, "LOG_FILE", Path(self.tmp.name) / "tv.log"),
                   patch.object(tv, "WORK_DIR", Path(self.tmp.name) / "work"),
                   patch.object(tv, "notify"), patch.object(tv.time, "sleep"),
                   patch.object(tv, "_get_openrouter_key", return_value="sk-test"),
                   patch.object(tv.urllib.request, "urlopen", side_effect=OSError("offline"))]
        started = [p.start() for p in self.ps]
        self.notify, self.sleep, self.urlopen = started[2], started[3], started[5]
        self._r = redirect_stdout(io.StringIO())
        self._r.__enter__()
        self.wav = Path(self.tmp.name) / "a.wav"
        self.wav.write_bytes(b"RIFF" + b"\0" * 2000)

    def tearDown(self):
        self._r.__exit__(None, None, None)
        for p in self.ps:
            p.stop()
        self.tmp.cleanup()


class TestSecurity(_Base):
    def test_no_hardcoded_key(self):
        self.assertNotRegex(SRC, r"sk-or-[A-Za-z0-9]")
        self.assertIn('"nova-openrouter-api-key"', SRC)

    def test_sql_parameterized(self):
        cur = MagicMock()
        with patch("psycopg2.connect", return_value=MagicMock(cursor=MagicMock(return_value=cur))):
            tv.update_status("/x'; --", "ingested", 1, 2, "tv")
        sql, params = cur.execute.call_args[0]
        self.assertIn("WHERE file_path = %s", sql)
        self.assertEqual(params[-1], "/x'; --")
        self.assertIn('type=int, default=0', SRC)  # the only interpolated value (LIMIT) is an int

    def test_ingest_marked_local_only(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = _Resp(b"{}")
        tv.remember("text", "automotive", {})
        self.assertEqual(json.loads(self.urlopen.call_args[0][0].data)["privacy"], "local-only")


class TestPerformance(unittest.TestCase):
    def test_chunk_and_trash_on_large_transcript(self):
        # 10 chunks (4k words): the repeated-phrase regex is ~quadratic per chunk, ~0.1s each
        text = _speech(4_000)
        t0 = time.perf_counter()
        chunks = tv.chunk_text(text)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(len(chunks), 10)


class TestRetry(_Base):
    def test_transcribe_retries_429_then_succeeds(self):
        err = urllib.error.HTTPError(tv.OPENROUTER_URL, 429, "slow", {}, io.BytesIO(b""))
        self.urlopen.side_effect = [err, err, _resp(SPEECH)]
        self.assertEqual(tv.transcribe_segment(self.wav), SPEECH.strip())
        self.assertEqual(self.urlopen.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_transcribe_4xx_no_retry_and_exhaustion(self):
        self.urlopen.side_effect = urllib.error.HTTPError(tv.OPENROUTER_URL, 400, "bad", {}, io.BytesIO(b"nope"))
        self.assertIsNone(tv.transcribe_segment(self.wav))
        self.assertEqual(self.urlopen.call_count, 1)
        self.urlopen.reset_mock()
        self.urlopen.side_effect = OSError("reset")
        self.assertIsNone(tv.transcribe_segment(self.wav))
        self.assertEqual(self.urlopen.call_count, tv.MAX_RETRIES)


class TestUnit(unittest.TestCase):
    def test_trash_detection(self):
        self.assertTrue(tv.is_trash_chunk("too short"))
        self.assertTrue(tv.is_trash_chunk("♪ " + "la " * 40))
        self.assertTrue(tv.is_trash_chunk("go go go go " * 10))
        self.assertFalse(tv.is_trash_chunk(_speech(80)))

    def test_classify_source(self):
        self.assertEqual(tv.classify_source("Good Eats", "", ""), "cooking")
        self.assertEqual(tv.classify_source("Some Show", "", "the rifle cartridge"), "military_history")
        self.assertEqual(tv.classify_source("Unknown", "", "weather today"), "television")


class TestIntegration(_Base):
    def test_retriable_query_and_limit(self):
        cur = MagicMock()
        cur.fetchall.return_value = [(1, "/v.mkv", "Show", "Ep")]
        with patch("psycopg2.connect", return_value=MagicMock(cursor=MagicMock(return_value=cur))):
            rows = tv.get_retriable_files(5)
        sql = cur.execute.call_args[0][0]
        self.assertIn("FROM media_ingest_state", sql)
        self.assertIn("status IN ('no_transcript', 'audio_failed')", sql)
        self.assertTrue(sql.rstrip().endswith("LIMIT 5"))
        self.assertEqual(rows, [{"id": 1, "file_path": "/v.mkv", "show": "Show", "title": "Ep"}])

    def test_remember_uses_shared_truncation(self):
        self.urlopen.side_effect = None
        self.urlopen.return_value = _Resp(b"{}")
        with patch.object(tv.nova_config, "truncate_at_boundary", return_value="T") as tr:
            self.assertTrue(tv.remember("long", "s", {}))
        tr.assert_called_once_with("long")


class TestFunctional(_Base):
    def test_process_file_golden_path(self):
        entry = {"file_path": "/v/Engine Masters/ep1.mkv", "show": "Engine Masters", "title": "Ep 1"}
        with patch.object(tv, "file_has_audio", return_value=True), \
             patch.object(tv, "extract_audio_segments", return_value=[self.wav]), \
             patch.object(tv, "transcribe_segment", return_value=_speech(1200)), \
             patch.object(tv, "remember", return_value=True) as rem, patch.object(tv, "update_status") as us:
            out = tv.process_file(entry, Path(self.tmp.name))
        self.assertEqual(out["status"], "ingested")
        self.assertEqual(us.call_args[0][1:], ("ingested", out["chunks"], out["words"], "automotive"))
        self.assertTrue(rem.call_args[0][0].startswith("[Engine Masters] "))
        self.assertFalse(self.wav.exists())

    def test_process_file_skip_and_empty(self):
        entry = {"file_path": "/nope.mkv", "show": None, "title": None}
        self.assertEqual(tv.process_file(entry, Path(self.tmp.name))["status"], "skip")
        with patch.object(tv, "file_has_audio", return_value=True), \
             patch.object(tv, "extract_audio_segments", return_value=[self.wav]), \
             patch.object(tv, "transcribe_segment", return_value=None), patch.object(tv, "update_status") as us:
            self.assertEqual(tv.process_file(entry, Path(self.tmp.name))["status"], "no_transcript")
        self.assertEqual(us.call_args[0][1], "no_transcript")

    def test_main_dry_run_and_full(self):
        files = [{"id": 1, "file_path": "/a", "show": "s", "title": "t"}]
        with patch.object(sys, "argv", ["x", "--dry-run"]), patch.object(tv, "get_retriable_files", return_value=files), \
             patch.object(tv, "file_has_audio", return_value=True), patch.object(tv, "process_file") as pf:
            tv.main()
        pf.assert_not_called()
        self.notify.assert_not_called()
        with patch.object(sys, "argv", ["x"]), patch.object(tv, "get_retriable_files", return_value=files), \
             patch.object(tv, "process_file", return_value={"status": "ingested", "chunks": 4}):
            tv.main()
        self.assertIn("Ingested: 1 files (4 chunks)", self.notify.call_args.kwargs["body"])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)


if __name__ == "__main__":
    unittest.main()
