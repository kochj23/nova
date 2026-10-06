#!/usr/bin/env python3
"""Tests for nova_yt_liked_download.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

yt-dlp/find/ffprobe, Slack, the clock and the RNG are mocked; every directory and state/log file
points into a tempdir. The main loop is driven to its own 'all caught up' exit."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_yt_liked_download.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_yt_liked_download_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


yt = _load()
yt.nova_config = types.SimpleNamespace(post_both=mock.MagicMock())


def _cp(rc=0, out="", err=""):
    return mock.Mock(returncode=rc, stdout=out, stderr=err)


class _Env(unittest.TestCase):
    def setUp(self):
        yt.nova_config.post_both.reset_mock(side_effect=True)
        yt._TVSHOWS_INDEX = None
        self.td = tempfile.TemporaryDirectory()
        r = Path(self.td.name)
        self.liked, self.music = r / "Liked", r / "Music"
        self.liked.mkdir()
        ps = {"liked": mock.patch.object(yt, "LIKED_DIR", self.liked),
              "music": mock.patch.object(yt, "MUSIC_DIR", self.music),
              "tv": mock.patch.object(yt, "TVSHOWS_DIR", r / "TV"),
              "ytd": mock.patch.object(yt, "YOUTUBE_DIR", r / "yt"),
              "cookies": mock.patch.object(yt, "YT_COOKIES_FILE", r / "cookies.txt"),
              "log": mock.patch.object(yt, "LOG_FILE", r / "yt.log"),
              "state": mock.patch.object(yt, "STATE_FILE", r / "cache" / "state.json"),
              "run": mock.patch.object(yt.subprocess, "run", return_value=_cp()),
              "sleep": mock.patch.object(yt.time, "sleep"),
              "out": mock.patch("sys.stdout", new_callable=io.StringIO)}
        self.m = {k: p.start() for k, p in ps.items()}
        self.root = r
        self.addCleanup(lambda: ([p.stop() for p in ps.values()], self.td.cleanup()))


class TestSecurity(_Env):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"SAPISID|__Secure-")              # no cookie values embedded

    def test_sanitize_blocks_path_traversal(self):
        s = yt.sanitize("../../etc/passwd: <evil>|\x00‮")
        self.assertTrue(all(c not in s for c in '<>:"/\\|?*'))
        p = yt.LIKED_DIR / f"{yt.sanitize('../../x')}.mp4"
        self.assertEqual(p.parent, self.liked)

    def test_downloader_argv_list_and_fresh_cookie_file(self):
        (self.root / "cookies.txt").write_text("# netscape")
        yt.download_video("abc123", "A title")
        argv = self.m["run"].call_args[0][0]
        self.assertEqual(argv[0], yt.YT_DLP)
        self.assertEqual(argv[1:3], ["--cookies", str(self.root / "cookies.txt")])
        self.assertEqual(argv[-1], "https://www.youtube.com/watch?v=abc123")
        self.assertNotIn("shell", self.m["run"].call_args[1])


class TestPerformance(_Env):
    def test_tvshows_index_is_built_once(self):
        self.m["run"].return_value = _cp(0, "\n".join(f"/tv/show{i}-vid{i}.mp4" for i in range(10_000)))
        state = {"downloaded": [], "skipped": [], "failed": []}
        t0 = time.perf_counter()
        for i in range(200):
            yt.is_already_downloaded(f"zz{i}", f"t{i}", state)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(self.m["run"].call_count, 1)


class TestRetry(_Env):
    def test_metadata_and_index_failures_fail_open(self):
        # RETRY GAP: fetch_metadata / _build_tvshows_index — one subprocess try; {} / empty index on failure
        self.m["run"].side_effect = subprocess.TimeoutExpired("yt-dlp", 60)
        self.assertEqual(yt.fetch_metadata("x"), {})
        self.assertEqual(yt._build_tvshows_index(), set())

    def test_slack_failure_logged_not_raised(self):
        yt.nova_config.post_both.side_effect = OSError("slack down")
        yt.notify("hi")
        self.assertIn("Slack error: slack down", self.m["out"].getvalue())


class TestUnit(_Env):
    def test_sanitize_edges(self):
        self.assertEqual(yt.sanitize(""), "untitled")
        self.assertEqual(yt.sanitize("日本"), "untitled")
        self.assertEqual(len(yt.sanitize("a" * 400)), 150)
        self.assertEqual(yt.sanitize("  Hello   World  "), "Hello World")

    def test_is_music(self):
        self.assertTrue(yt.is_music({"categories": ["Music"]}))
        self.assertTrue(yt.is_music({"uploader": "ArtistVEVO"}))
        self.assertTrue(yt.is_music({"channel": "Band - Topic"}))
        self.assertFalse(yt.is_music({"uploader": "Some Vlogger", "categories": None}))

    def test_download_result_codes(self):
        self.m["run"].return_value = _cp(1, err="Join this channel to get access")
        self.assertEqual(yt.download_video("a", "Title one"), "members-only")
        self.m["run"].return_value = _cp(1, out="has already been downloaded")
        self.assertEqual(yt.download_video("a", "Title two"), "skip")
        self.m["run"].return_value = _cp(1, err="HTTP 403")
        self.assertTrue(yt.download_video("a", "Title three").startswith("error:"))
        (self.liked / "Exists.mp4").write_text("x")
        self.assertEqual(yt.download_video("a", "Exists"), "skip")


class TestIntegration(_Env):
    def test_liked_list_parse_and_dedup_against_state_and_dirs(self):
        self.m["run"].return_value = _cp(0, "id1\tFirst Video\tChan\nid2\tSecond\nbad-line\n")
        vids = yt.get_liked_videos()
        self.assertEqual(vids, [{"id": "id1", "title": "First Video", "uploader": "Chan"},
                                {"id": "id2", "title": "Second", "uploader": "Unknown"}])
        yt._TVSHOWS_INDEX = {"show-id9.mp4"}
        (self.liked / "First Video.mp4").write_text("x")
        st = {"downloaded": ["id2"], "skipped": [], "failed": []}
        self.assertTrue(yt.is_already_downloaded("id1", "First Video", st))
        self.assertTrue(yt.is_already_downloaded("id2", "x", st))
        self.assertTrue(yt.is_already_downloaded("id9", "zzz", st))
        self.assertFalse(yt.is_already_downloaded("id3", "Brand New", st))

    def test_state_roundtrip(self):
        self.assertEqual(yt.load_state(), {"downloaded": [], "skipped": [], "failed": []})
        yt.save_state({"downloaded": ["a"], "skipped": [], "failed": []})
        self.assertEqual(yt.load_state()["downloaded"], ["a"])


class TestFunctional(_Env):
    def test_main_downloads_batch_then_exits_caught_up(self):
        def run(argv, **k):
            if "--flat-playlist" in argv:
                return _cp(0, "v1\tCat Video\tCats\nv2\tSong\tBandVEVO\n")
            if "--dump-json" in argv:
                vid = argv[-1].split("v=")[1]
                return _cp(0, json.dumps({"uploader": "BandVEVO", "title": "Song"} if vid == "v2" else {"uploader": "Cats"}))
            if argv[0] == "find":
                return _cp(0, "")
            return _cp(0)
        self.m["run"].side_effect = run
        with mock.patch.object(yt.random, "randint", return_value=4), \
                mock.patch.object(yt.random, "sample", side_effect=lambda p, n: list(p)[:n]), \
                mock.patch.object(yt, "_apply_id3_tags") as id3:
            yt.main()                                          # pre-fix this busy-spun for 30 min
        st = json.loads((self.root / "cache" / "state.json").read_text())
        self.assertEqual(sorted(st["downloaded"]), ["v1", "v2"])
        id3.assert_called_once()
        posts = [c[0][0] for c in yt.nova_config.post_both.call_args_list]
        self.assertIn("all caught up", posts[-1])

    def test_progress_message_shape(self):
        yt._send_progress([f"u — t{i}" for i in range(7)], 2, 1, [1, 2, 3])
        msg = yt.nova_config.post_both.call_args[0][0]
        self.assertIn("7 downloaded, 3 remaining", msg)
        self.assertIn("...and 2 more this session", msg)
        self.assertIn(":warning: 1 errors", msg)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_yt_liked_download"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
