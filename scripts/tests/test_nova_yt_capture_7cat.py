#!/usr/bin/env python3
"""7-category gap tests for nova_yt_capture.py + nova_yt_capture_runner.py (2026-10-06..08 changes):
the shared mlx_whisper advisory lock, vod_pending parking, per-channel vectors, and the retries
added for memory-server writes, status UPDATEs and the runner's PG connect. Everything external
(PG, memory server, yt-dlp, whisper) is mocked. Base coverage: test_nova_yt_capture.py and
test_nova_yt_capture_runner.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_yt_capture_7cat.py
"""
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


yt = _load("yt_capture_7cat", SCRIPTS / "nova_yt_capture.py")
cr = _load("yt_capture_runner_7cat", SCRIPTS / "nova_yt_capture_runner.py")


class _Quiet(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        r = redirect_stdout(self.out); r.__enter__(); self.addCleanup(r.__exit__, None, None, None)
        p = patch.object(yt.time, "sleep"); self.sleep = p.start(); self.addCleanup(p.stop)


class TestSecurity(_Quiet):
    def test_status_sql_is_parameterised(self):
        conn = MagicMock()
        hostile = "x'; UPDATE yt_ingest_seen SET status='pwned'; --"
        with patch.object(yt.psycopg2, "connect", return_value=conn):
            yt.setstatus(hostile, "ingested")
        sql, params = conn.cursor.return_value.execute.call_args.args
        self.assertNotIn("pwned", sql)
        self.assertEqual(params, ("ingested", hostile))

    def test_whisper_lock_uses_fixed_lock_id_bound_param(self):
        conn = MagicMock()
        with patch.object(yt.psycopg2, "connect", return_value=conn):
            with yt.whisper_lock():
                pass
        conn.cursor.return_value.execute.assert_called_once_with("SELECT pg_advisory_lock(%s)", (yt.WHISPER_LOCK,))

    def test_remembered_chunks_marked_private(self):
        ok = MagicMock(); ok.__enter__.return_value = ok
        with patch.object(yt.urllib.request, "urlopen", return_value=ok) as u:
            yt.remember("t", {"vector": "fishbowl"})
        body = json.loads(u.call_args.args[0].data)
        self.assertEqual(body["metadata"]["privacy"], "private")


class TestPerformance(_Quiet):
    def test_lock_connect_has_timeout(self):
        with patch.object(yt.psycopg2, "connect", return_value=MagicMock()) as c:
            with yt.whisper_lock():
                pass
        self.assertEqual(c.call_args.kwargs.get("connect_timeout"), 5)

    def test_retry_backoff_bounded(self):
        with patch.object(yt.urllib.request, "urlopen", side_effect=OSError("x")):
            yt.remember("t", {"vector": "v"})
        self.assertLessEqual(sum(c.args[0] for c in self.sleep.call_args_list), 10)

    def test_status_connect_has_timeout(self):
        with patch.object(yt.psycopg2, "connect", return_value=MagicMock()) as c:
            yt.setstatus("v", "s")
        self.assertEqual(c.call_args.kwargs.get("connect_timeout"), 10)


class TestRetry(_Quiet):
    def test_remember_recovers_after_blip(self):
        ok = MagicMock(); ok.__enter__.return_value = ok
        with patch.object(yt.urllib.request, "urlopen", side_effect=[OSError("restart"), ok]) as u:
            self.assertTrue(yt.remember("t", {"vector": "v"}))
        self.assertEqual(u.call_count, 2)
        self.sleep.assert_called_once_with(2)

    def test_status_update_recovers_after_blip(self):
        conn = MagicMock()
        with patch.object(yt.psycopg2, "connect", side_effect=[OSError("failover"), conn]):
            self.assertTrue(yt._pg_update("UPDATE x SET y=%s", (1,)))
        conn.close.assert_called_once()

    def test_status_update_final_failure_logged(self):
        with patch.object(yt.psycopg2, "connect", side_effect=OSError("down")) as c:
            self.assertFalse(yt._pg_update("UPDATE x", ()))
        self.assertEqual(c.call_count, 3)
        self.assertIn("status update failed after 3 tries", self.out.getvalue())

    def test_vod_pending_requeue_counts_attempts(self):
        conn = MagicMock()
        with patch.object(yt.psycopg2, "connect", return_value=conn):
            yt.retry_or_empty("v1")
        sql, params = conn.cursor.return_value.execute.call_args.args
        self.assertIn("'vod_pending'", sql)
        self.assertEqual(params, (yt.MAX_VOD_ATTEMPTS, "v1"))

    def test_runner_connect_retried(self):
        conn = MagicMock()
        with patch.object(cr.psycopg2, "connect", side_effect=[OSError("x"), conn]), \
                patch.object(cr.time, "sleep") as sl, redirect_stdout(io.StringIO()):
            self.assertIs(cr._connect(), conn)
        sl.assert_called_once_with(5)

    def test_runner_connect_gives_up_after_three(self):
        with patch.object(cr.psycopg2, "connect", side_effect=OSError("x")) as c, \
                patch.object(cr.time, "sleep"), redirect_stdout(io.StringIO()), self.assertRaises(OSError):
            cr._connect()
        self.assertEqual(c.call_count, 3)


class TestUnit(_Quiet):
    def test_whisper_lock_fails_open_when_pg_down(self):
        with patch.object(yt.psycopg2, "connect", side_effect=OSError("down")):
            with yt.whisper_lock() as lk:
                self.assertIsNone(lk.c)

    def test_whisper_lock_released_by_closing_session(self):
        conn = MagicMock()
        with patch.object(yt.psycopg2, "connect", return_value=conn):
            with yt.whisper_lock():
                conn.close.assert_not_called()
        conn.close.assert_called_once()

    def test_runner_vectors_per_channel(self):
        horo = [c["key"] for c in cr.CHANNELS if c["vector"] == "horology"]
        self.assertTrue(horo)
        self.assertEqual(cr.VECTORS[horo[0]], "horology")


class TestIntegration(_Quiet):
    def test_transcribe_holds_lock_around_whisper(self):
        order = []

        class Lock:
            def __enter__(self):
                order.append("lock")

            def __exit__(self, *a):
                order.append("unlock")
        with patch.object(yt, "whisper_lock", Lock), \
                patch.object(yt, "_transcribe", side_effect=lambda w, s: order.append("whisper") or "txt"):
            self.assertEqual(yt.transcribe(Path("a.wav"), "a"), "txt")
        self.assertEqual(order, ["lock", "whisper", "unlock"])

    def test_subs_audio_shares_the_same_lock(self):
        self.assertIn("from nova_yt_capture import whisper_lock", (SCRIPTS / "nova_yt_subs_audio.py").read_text())


class TestFunctional(_Quiet):
    def test_memory_server_blip_mid_stream_still_ingested(self):
        """One chunk needs a retry; every chunk is still stored and the row ends 'ingested'."""
        ok = MagicMock(); ok.__enter__.return_value = ok
        statuses = []
        with patch.object(sys, "argv", ["x", "vid1", "horology"]), \
                patch.object(yt, "meta_of", return_value=("T", "Chan")), \
                patch.object(yt, "download", return_value=(Path("a.mp4"), None)), \
                patch.object(yt, "to_wav", return_value=Path("a.wav")), \
                patch.object(yt, "transcribe", return_value="para one\n\npara two"), \
                patch.object(yt, "setstatus", side_effect=lambda v, s: statuses.append(s)), \
                patch.object(yt, "slack"), patch.object(yt, "file_video_to_plex") as plex, \
                patch.object(yt.urllib.request, "urlopen", side_effect=[OSError("blip"), ok]):
            yt.main()
        self.assertEqual(statuses[-1], "ingested")
        plex.assert_not_called()   # watch-news (horology) is transcript-only

    def test_undownloadable_vod_parked_not_emptied(self):
        with patch.object(sys, "argv", ["x", "vid2", "fishbowl"]), \
                patch.object(yt, "meta_of", return_value=("T", "Chan")), \
                patch.object(yt, "download", return_value=(None, None)), \
                patch.object(yt, "setstatus") as st, patch.object(yt, "retry_or_empty") as roe, \
                patch.object(yt, "slack") as sl, patch.object(yt, "file_video_to_plex"):
            yt.main()
        roe.assert_called_once_with("vid2")
        self.assertNotIn("empty", [c.args[1] for c in st.call_args_list])
        sl.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_both_compile_and_expose_entrypoints(self):
        for p in ("nova_yt_capture.py", "nova_yt_capture_runner.py"):
            r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPTS / p)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(callable(yt.main) and callable(cr.main) and callable(cr._connect))


if __name__ == "__main__":
    unittest.main()
