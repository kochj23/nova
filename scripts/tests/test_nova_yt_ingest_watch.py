#!/usr/bin/env python3
"""Tests for nova_yt_ingest_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). yt-dlp, the detached capture worker and PG are mocked.
Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_yt_ingest_watch.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("ytwatch", SCRIPTS / "nova_yt_ingest_watch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


yw = _load()
yw.log = lambda *a, **k: None
yw.dispatch = MagicMock()          # never spawn a detached capture worker from a test


class _Cur:
    def __init__(self, seen):
        self.seen = seen; self.sql = []; self._last = ""; self.rowcount = 0

    def execute(self, sql, params=None):
        self.sql.append((sql, params)); self._last = sql

    def fetchall(self):
        return [(v,) for v in self.seen]


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _main(seen, vids, live=None, argv=("x",), channels=None):
    cur = _Cur(seen)
    with patch.object(yw, "_db", return_value=_Conn(cur)), patch.object(yw, "recent_ids", return_value=vids), \
         patch.object(yw, "vid_live_status", side_effect=lambda v: (live or {}).get(v, "not_live")), \
         patch.object(yw, "CHANNELS", channels or [{"key": "chan", "url": "u", "vector": "fishbowl"}]), \
         patch.object(sys, "argv", list(argv)):
        yw.main()
    return [(s, p) for s, p in cur.sql if s.startswith("INSERT INTO yt_ingest_seen")]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_parameterized_and_titles_bounded(self):
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))
        ins = _main({"old"}, [("new1", "NA", "x'); --" + "t" * 500)])
        params = ins[0][1]
        self.assertEqual(len(params[2]), 200)
        self.assertNotIn("x');", ins[0][0])

    def test_ytdlp_called_as_argv(self):
        self.assertNotIn("shell=True", SRC)
        with patch.object(yw.subprocess, "run", return_value=SimpleNamespace(stdout="", stderr="", returncode=0)) as r:
            yw.recent_ids("https://www.youtube.com/@x; rm -rf ~")
        self.assertEqual(r.call_args[0][0][-1], "https://www.youtube.com/@x; rm -rf ~")


class TestPerformance(unittest.TestCase):
    def test_parse_10k_listing_lines_fast(self):
        out = "\n".join(f"id{i}\tnot_live\tTitle {i}" for i in range(10_000))
        with patch.object(yw.subprocess, "run", return_value=SimpleNamespace(stdout=out, stderr="", returncode=0)):
            t0 = time.perf_counter()
            vids = yw.recent_ids("u")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(vids), 10_000)
        self.assertIn('f"1:{RECENT}"', SRC)             # the real listing is capped at RECENT per channel


class TestRetry(unittest.TestCase):
    def test_listing_failure_fails_open(self):
        # recent_ids / vid_live_status — 3 yt-dlp attempts each with backoff; then []/"" (logged)
        with patch.object(yw.subprocess, "run", side_effect=subprocess.TimeoutExpired("yt-dlp", 180)) as r, \
                patch.object(yw.time, "sleep"):
            self.assertEqual(yw.recent_ids("u"), [])
            self.assertEqual(yw.vid_live_status("v"), "")
        self.assertEqual(r.call_count, 6)

    def test_channel_with_no_listing_skipped(self):
        self.assertEqual(_main(set(), []), [])


class TestUnit(unittest.TestCase):
    def test_recent_ids_parsing(self):
        out = "a1\tis_live\tLive show\n\tbad\tline\nshort\nb2\tnot_live\tTitle\twith\ttabs\n"
        with patch.object(yw.subprocess, "run", return_value=SimpleNamespace(stdout=out, stderr="", returncode=0)):
            self.assertEqual(yw.recent_ids("u"), [("a1", "is_live", "Live show"), ("b2", "not_live", "Title\twith\ttabs")])

    def test_vid_live_status_first_line(self):
        with patch.object(yw.subprocess, "run", return_value=SimpleNamespace(stdout="was_live\nextra\n")):
            self.assertEqual(yw.vid_live_status("v"), "was_live")


class TestIntegration(unittest.TestCase):
    def test_state_in_pg_table(self):
        cur = _Cur(set())
        yw.ensure(cur)
        self.assertIn("CREATE TABLE IF NOT EXISTS yt_ingest_seen", cur.sql[0][0])
        self.assertIn("PRIMARY KEY (channel, video_id)", cur.sql[0][0])
        # Watches and Friends (2026-10-06): watch-news channels -> horology, the drama scene -> fishbowl
        self.assertTrue(all(c["vector"] in ("fishbowl", "horology") for c in yw.CHANNELS))
        self.assertEqual({c["key"]: c["vector"] for c in yw.CHANNELS}["tpgentleman"], "fishbowl")  # Farrer


class TestFunctional(unittest.TestCase):
    def test_first_run_seeds_but_captures_live(self):
        ins = _main(set(), [("v1", "NA", "old"), ("v2", "NA", "live now")], live={"v2": "is_live"})
        self.assertEqual([p[3] for _, p in ins], ["seeded", "queued_live"])

    def test_new_videos_queued_upcoming_skipped(self):
        ins = _main({"v1"}, [("v1", "NA", "seen"), ("v2", "NA", "new"), ("v3", "NA", "soon"), ("v4", "NA", "live")],
                    live={"v3": "is_upcoming", "v4": "is_live"})
        self.assertEqual([(p[1], p[3]) for _, p in ins], [("v2", "queued"), ("v4", "queued_live")])
        yw.dispatch.assert_not_called()                 # queued for the capture-runner, never spawned here

    def test_seed_flag_reseeds(self):
        ins = _main({"v1"}, [("v9", "NA", "x")], argv=("x", "--seed"))
        self.assertEqual(ins[0][1][3], "seeded")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # main() lists ~30 YouTube channels and writes PG, so the smoke is an import only
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_yt_ingest_watch"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
