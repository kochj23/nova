#!/usr/bin/env python3
"""Tests for nova_jungle_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). yt-dlp and the curl Slack post are mocked; HOME points at a tempdir.
Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_jungle_monitor.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("jungle", SCRIPTS / "nova_jungle_monitor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


jm = _load()
jm.log = lambda *a, **k: None


def _line(i, dur=400, views=500):
    return json.dumps({"id": f"v{i}", "title": f"Track {i}", "uploader": f"DJ {i}", "duration": dur,
                       "view_count": views})


class _Home:
    """A temp HOME carrying an openclaw.json with a fake bot token."""
    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        p = Path(self.td.name) / ".openclaw"; p.mkdir()
        (p / "openclaw.json").write_text(json.dumps({"channels": {"slack": {"botToken": "xoxb-fake-for-test"}}}))
        self.patch = patch.object(jm.Path, "home", return_value=Path(self.td.name))
        self.patch.start()
        return self

    def __exit__(self, *a):
        self.patch.stop(); self.td.cleanup()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"xox[bp]-\d")

    def test_subprocess_uses_argv_not_shell(self):
        self.assertNotIn("shell=True", SRC)
        with _Home(), patch.object(jm.subprocess, "run") as run:
            jm.post_to_slack([{"title": "t; rm -rf ~", "uploader": "u", "url": "x", "view_count": 1}])
        argv = run.call_args[0][0]
        self.assertEqual(argv[0], "curl")
        self.assertIn("rm -rf", json.loads(argv[-1])["text"])     # hostile title stays inside the JSON body

    def test_no_token_means_no_post(self):
        with tempfile.TemporaryDirectory() as td, patch.object(jm.Path, "home", return_value=Path(td)), \
             patch.object(jm.subprocess, "run") as run:
            jm.post_to_slack([{"title": "t", "uploader": "u", "url": "x", "view_count": 1}])
        run.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_parse_and_filter_10k_fast(self):
        blob = "\n".join(_line(i, dur=200 + i % 400, views=i) for i in range(10_000))
        t0 = time.perf_counter()
        q = jm.filter_quality_tracks(jm.parse_tracks(blob))
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(q), 10)


class TestRetry(unittest.TestCase):
    def test_search_fails_open(self):
        # RETRY GAP: search_youtube_jungle — one yt-dlp call; timeout/error returns None, never raises
        calls = []

        def boom(*a, **k):
            calls.append(1); raise subprocess.TimeoutExpired("yt-dlp", 30)

        with patch.object(jm.subprocess, "run", side_effect=boom):
            self.assertIsNone(jm.search_youtube_jungle())
        self.assertEqual(len(calls), 1)
        with patch.object(jm.subprocess, "run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="x")):
            self.assertIsNone(jm.search_youtube_jungle())

    def test_slack_post_error_swallowed(self):
        with _Home(), patch.object(jm.subprocess, "run", side_effect=OSError("no curl")):
            self.assertIsNone(jm.post_to_slack([{"title": "t", "uploader": "u", "url": "x", "view_count": 1}]))


class TestUnit(unittest.TestCase):
    def test_parse_tracks_edges(self):
        self.assertEqual(jm.parse_tracks(""), [])
        t = jm.parse_tracks(json.dumps({"id": "abc"}))
        self.assertEqual(t[0]["url"], "https://youtube.com/watch?v=abc")
        self.assertEqual(t[0]["title"], "Unknown")
        self.assertEqual(len(jm.parse_tracks(_line(1) + "\n{broken")), 1)   # stops at bad JSON, keeps good

    def test_filter_bounds(self):
        tr = [{"duration": 300, "view_count": 500}, {"duration": 301, "view_count": 100},
              {"duration": 301, "view_count": 101}, {}]
        self.assertEqual(jm.filter_quality_tracks(tr), [{"duration": 301, "view_count": 101}])


class TestIntegration(unittest.TestCase):
    def test_posts_to_notifications_channel_top_five(self):
        tracks = jm.parse_tracks("\n".join(_line(i) for i in range(8)))
        with _Home(), patch.object(jm.subprocess, "run") as run:
            jm.post_to_slack(jm.filter_quality_tracks(tracks))
        body = json.loads(run.call_args[0][0][-1])
        self.assertEqual(body["channel"], "C0ATAF7NZG9")
        self.assertIn("5. **Track 4**", body["text"])
        self.assertNotIn("Track 5", body["text"])


class TestFunctional(unittest.TestCase):
    def test_main_golden_path(self):
        out = "\n".join(_line(i) for i in range(3))
        with patch.object(jm, "search_youtube_jungle", return_value=out), \
             patch.object(jm, "post_to_slack") as post:
            self.assertEqual(jm.main(), 0)
        self.assertEqual(len(post.call_args[0][0]), 3)

    def test_main_nothing_found(self):
        with patch.object(jm, "search_youtube_jungle", return_value=None), patch.object(jm, "post_to_slack") as post:
            self.assertEqual(jm.main(), 1)
        with patch.object(jm, "search_youtube_jungle", return_value=_line(1, dur=60)), \
             patch.object(jm, "post_to_slack") as post2:
            self.assertEqual(jm.main(), 1)
        post.assert_not_called(); post2.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # main() shells out to yt-dlp and posts to Slack, so the smoke is an import only
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_jungle_monitor"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
