#!/usr/bin/env python3
"""7-category gap tests for nova_yt_ingest_watch.py (2026-10-06..07 changes: watch-news channels on
/videos, Farrer = Fishbowl, vod_pending re-queue every 2 h) plus the yt-dlp / PG retries added here.
yt-dlp, PG and the capture worker are mocked. Base suite: test_nova_yt_ingest_watch.py.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_yt_ingest_watch_7cat.py
"""
import importlib.util
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location("ytwatch_7cat", SCRIPTS / "nova_yt_ingest_watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


yw = _load()
SRC = (SCRIPTS / "nova_yt_ingest_watch.py").read_text()


def cp(out="", rc=0, err=""):
    return SimpleNamespace(stdout=out, returncode=rc, stderr=err)


class _Base(unittest.TestCase):
    def setUp(self):
        for p in (patch.object(yw, "log"), patch.object(yw.time, "sleep"), patch.object(yw, "dispatch")):
            m = p.start(); self.addCleanup(p.stop)
        self.log = yw.log
        self.sleep = yw.time.sleep


class Cur:
    def __init__(self, seen=(), requeued=0):
        self.seen, self.rowcount, self.sql = list(seen), requeued, []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return [(v,) for v in self.seen]


class TestSecurity(_Base):
    def test_ytdlp_argv_list_never_shell(self):
        with patch.object(yw.subprocess, "run", return_value=cp("live")) as r:
            yw.vid_live_status("x; rm -rf ~")
        argv = r.call_args.args[0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[-1], "https://www.youtube.com/watch?v=x; rm -rf ~")
        self.assertFalse(r.call_args.kwargs.get("shell"))

    def test_listing_is_anonymous(self):
        self.assertNotIn("--cookies", SRC)

    def test_queue_inserts_are_parameterised(self):
        self.assertNotRegex(SRC, r'execute\(f["\']')


class TestPerformance(_Base):
    def test_every_ytdlp_call_has_timeout(self):
        with patch.object(yw.subprocess, "run", return_value=cp("a\tnot_live\tt")) as r:
            yw.recent_ids("u"); yw.vid_live_status("v")
        self.assertEqual([c.kwargs["timeout"] for c in r.call_args_list], [180, 90])

    def test_retry_total_backoff_bounded(self):
        with patch.object(yw.subprocess, "run", return_value=cp("", 1)):
            yw.recent_ids("u")
        self.assertLessEqual(sum(c.args[0] for c in self.sleep.call_args_list), 10)


class TestRetry(_Base):
    def test_listing_retried_after_failure(self):
        with patch.object(yw.subprocess, "run", side_effect=[cp("", 1, "HTTP 429"), cp("a1\tnot_live\tT")]) as r:
            self.assertEqual(yw.recent_ids("u"), [("a1", "not_live", "T")])
        self.assertEqual(r.call_count, 2)

    def test_live_status_retried_after_timeout(self):
        with patch.object(yw.subprocess, "run", side_effect=[subprocess.TimeoutExpired("y", 90), cp("is_live\n")]):
            self.assertEqual(yw.vid_live_status("v"), "is_live")

    def test_listing_final_failure_logged(self):
        with patch.object(yw.subprocess, "run", return_value=cp("", 1, "ERROR: blocked")) as r:
            self.assertEqual(yw.recent_ids("u"), [])
        self.assertEqual(r.call_count, 3)
        self.assertTrue(any("blocked" in c.args[0] for c in self.log.call_args_list))

    def test_pg_connect_retried_then_raises(self):
        conn = MagicMock()
        with patch.object(yw.psycopg2, "connect", side_effect=[psycopg2.OperationalError("x"), conn]):
            self.assertIs(yw._db(), conn)
        with patch.object(yw.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")) as c, \
                self.assertRaises(psycopg2.OperationalError):
            yw._db()
        self.assertEqual(c.call_count, 3)

    def test_vod_pending_requeued_after_two_hours(self):
        cur = Cur(requeued=3)
        with patch.object(yw, "_db", return_value=SimpleNamespace(cursor=lambda: cur, close=lambda: None)), \
                patch.object(yw, "CHANNELS", []), patch.object(sys, "argv", ["x"]):
            yw.main()
        upd = [s for s, _ in cur.sql if s.startswith("UPDATE yt_ingest_seen SET status='queued'")]
        self.assertEqual(len(upd), 1)
        self.assertIn("vod_pending", upd[0])
        self.assertIn("interval '2 hours'", upd[0])


class TestUnit(_Base):
    def test_watch_news_channels_list_a_tab_that_exists(self):
        horo = [c for c in yw.CHANNELS if c["vector"] == "horology"]
        self.assertGreaterEqual(len(horo), 25)
        self.assertTrue(all(c["url"].endswith(("/videos", "/streams")) for c in horo))
        for key in ("wristchick", "greymarketpod"):   # no streams tab (2026-10-06): must watch /videos
            self.assertTrue(next(c for c in yw.CHANNELS if c["key"] == key)["url"].endswith("/videos"))

    def test_channel_keys_unique(self):
        keys = [c["key"] for c in yw.CHANNELS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_farrer_is_not_watch_news(self):
        for c in yw.CHANNELS:
            if "farrer" in c["key"].lower() or "timepiecegentleman" in c["url"].lower():
                self.assertEqual(c["vector"], "fishbowl")

    def test_ytdlp_returns_none_when_every_attempt_raises(self):
        with patch.object(yw.subprocess, "run", side_effect=OSError("no binary")):
            self.assertIsNone(yw._ytdlp(["x"], timeout=1))


class TestIntegration(_Base):
    def test_runner_maps_queue_channel_to_vector(self):
        spec = importlib.util.spec_from_file_location("runner_for_watch", SCRIPTS / "nova_yt_capture_runner.py")
        cr = importlib.util.module_from_spec(spec); spec.loader.exec_module(cr)
        for c in yw.CHANNELS:
            self.assertEqual(cr.VECTORS[c["key"]], c["vector"])


class TestFunctional(_Base):
    def test_new_upcoming_skipped_live_flagged_vod_queued(self):
        cur = Cur(seen=["old"])
        vids = [("old", "NA", "o"), ("up", "NA", "u"), ("lv", "NA", "l"), ("vod", "NA", "v")]
        status = {"up": "is_upcoming", "lv": "is_live", "vod": "not_live"}
        with patch.object(yw, "_db", return_value=SimpleNamespace(cursor=lambda: cur, close=lambda: None)), \
                patch.object(yw, "recent_ids", return_value=vids), \
                patch.object(yw, "vid_live_status", side_effect=status.get), \
                patch.object(yw, "CHANNELS", [{"key": "c", "url": "u", "vector": "horology"}]), \
                patch.object(sys, "argv", ["x"]):
            yw.main()
        ins = [p for s, p in cur.sql if s.startswith("INSERT")]
        self.assertEqual([(p[1], p[3]) for p in ins], [("lv", "queued_live"), ("vod", "queued")])

    def test_listing_outage_skips_channel_without_crash(self):
        cur = Cur(seen=["a"])
        with patch.object(yw, "_db", return_value=SimpleNamespace(cursor=lambda: cur, close=lambda: None)), \
                patch.object(yw.subprocess, "run", return_value=cp("", 1, "down")), \
                patch.object(yw, "CHANNELS", [{"key": "c", "url": "u", "vector": "fishbowl"}]), \
                patch.object(sys, "argv", ["x"]):
            yw.main()
        self.assertFalse(any(s.startswith("INSERT") for s, _ in cur.sql))


class TestFrame(unittest.TestCase):
    def test_import_side_effect_free_and_entrypoints(self):
        self.assertTrue(callable(yw.main) and callable(yw._ytdlp) and callable(yw._db))

    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPTS / "nova_yt_ingest_watch.py")],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
