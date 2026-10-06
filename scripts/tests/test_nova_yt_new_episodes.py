#!/usr/bin/env python3
"""Tests for nova_yt_new_episodes.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

yt-dlp, osascript, Slack and the media registry are stubbed at load; every show/cache/log path is a
tempdir, so nothing touches the NAS, the browser cookie jar or the network."""
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


yt = _load("nova_yt_new_episodes_t", SCRIPTS / "nova_yt_new_episodes.py")
_TMP = Path(tempfile.mkdtemp())
yt.BASE_DIR = _TMP / "youtube"
yt.VIDEO_ROOT = _TMP / "videos"
yt.CHANNELS_CACHE = _TMP / "yt_channels.json"
yt.YT_COOKIES_FILE = _TMP / "yt_cookies.txt"
yt.LOG_FILE = _TMP / "yt.log"
yt.nova_config = types.SimpleNamespace(post_both=MagicMock())
yt.registry = MagicMock()
yt.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=RuntimeError("unmocked subprocess")))
SRC = (SCRIPTS / "nova_yt_new_episodes.py").read_text()


def _cp(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _q():
    return redirect_stdout(io.StringIO())


def _reset():
    yt.nova_config.post_both.reset_mock()
    yt.registry.reset_mock()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_hostile_title_cannot_escape_season_dir(self):
        s = yt.sanitize('../../etc/passwd: <evil> "x" | y? *' + "z" * 300)
        self.assertNotIn("/", s)
        self.assertNotIn("\\", s)
        self.assertLessEqual(len(s), 120)

    def test_hijacked_handle_titles_skipped(self):
        out = "a1\tNormal Garage Build\t20260101\nb2\t深夜のカスタムカー改造\t20260102\n"
        with patch.object(yt.subprocess, "run", return_value=_cp(0, out)), _q():
            vids = yt.get_recent_videos("https://www.youtube.com/@x")
        self.assertEqual([v["id"] for v in vids], ["a1"])

    def test_refreshed_cookie_file_is_private(self):
        def run(cmd, **k):
            yt.YT_COOKIES_FILE.write_text("# cookies")
            return _cp(0)
        yt.YT_COOKIES_FILE.unlink(missing_ok=True)
        with patch.object(yt.subprocess, "run", side_effect=run), _q():
            self.assertTrue(yt._refresh_cookies_from_browser())
        self.assertEqual(yt.YT_COOKIES_FILE.stat().st_mode & 0o777, 0o600)


class TestPerformance(unittest.TestCase):
    def test_is_on_disk_10k_titles(self):
        on_disk = {yt.normalize(f"episode number {i} the big build") for i in range(300)}
        t0 = time.perf_counter()
        for i in range(10_000):
            yt.is_on_disk(f"Brand new video {i} about engines", on_disk)
        self.assertLess(time.perf_counter() - t0, 5.0)


class TestRetry(unittest.TestCase):
    def test_stale_cookies_refresh_then_fallback_to_browser(self):
        yt.YT_COOKIES_FILE.write_text("old")
        old = time.time() - 7 * 3600
        os.utime(yt.YT_COOKIES_FILE, (old, old))
        with patch.object(yt.subprocess, "run", return_value=_cp(1, "", "denied")) as run, _q():
            args = yt._cookies_args()
        self.assertEqual(run.call_count, 1)                                 # one osascript refresh attempt
        self.assertEqual(args, ["--cookies-from-browser", "chrome"])
        os.utime(yt.YT_COOKIES_FILE, None)
        self.assertEqual(yt._cookies_args(), ["--cookies", str(yt.YT_COOKIES_FILE)])

    def test_subscription_sync_failure_falls_back(self):
        # RETRY GAP: sync_subscriptions / download_video — one yt-dlp attempt each; hardcoded CHANNELS
        # and the next weekly run are the fallback
        with patch.object(yt, "_cookies_args", return_value=[]), \
                patch.object(yt.subprocess, "run", side_effect=OSError("no yt-dlp")), _q():
            self.assertIs(yt.sync_subscriptions(), yt.CHANNELS)

    def test_download_failure_notifies_and_registers_error(self):
        _reset()
        sd = yt.BASE_DIR / "Show" / "Season 01"
        sd.mkdir(parents=True, exist_ok=True)
        res = []
        with patch.object(yt, "download_video", return_value="error: HTTP 403"), _q():
            yt._download_one({"id": "v", "title": "T"}, "Show", 1, 3, sd, res)
        self.assertEqual(res[0]["status"], "error")
        self.assertIn("download failed", yt.nova_config.post_both.call_args[0][0])
        yt.registry.mark_status.assert_called_with(str(sd / "Show - S01E0003 - T.mp4"), "error",
                                                   error_msg="error: HTTP 403")


class TestUnit(unittest.TestCase):
    def test_normalize_and_is_on_disk(self):
        self.assertEqual(yt.normalize("Hello,  World!"), "hello world")
        self.assertFalse(yt.is_on_disk("a an the", {"anything"}))          # no meaningful words
        self.assertTrue(yt.is_on_disk("Rebuilding the Engine Block", {"rebuilding engine block part 1"}))

    def test_looks_foreign(self):
        self.assertFalse(yt._looks_foreign("MIDNIGHT MOD build"))
        self.assertTrue(yt._looks_foreign("MOD 深夜カスタム"))
        self.assertFalse(yt._looks_foreign("1234 !!"))

    def test_episode_numbering(self):
        d = _TMP / "num"
        (d / "Season 01").mkdir(parents=True, exist_ok=True)
        (d / "Season 01" / "X - S01E0007 - a.mp4").write_text("")
        (d / "Season 01" / "X - S02E0099 - b.mp4").write_text("")
        self.assertEqual(yt.next_episode_single(d), (1, 8))
        (d / "Season 01" / ".season_info.json").write_text(json.dumps({"year": "2025", "season_number": 1}))
        self.assertEqual(yt.season_for_year(d, "2025"), 1)
        self.assertEqual(yt.season_for_year(d, "2026"), 2)


class TestIntegration(unittest.TestCase):
    def test_download_command_and_success_registers_file(self):
        _reset()
        yt.YT_COOKIES_FILE.write_text("fresh")
        with patch.object(yt.subprocess, "run", return_value=_cp(0)) as run, \
                patch.object(Path, "mkdir"):
            self.assertEqual(yt.download_video("abc123", _TMP / "out.mp4"), "ok")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], yt.YT_DLP)
        self.assertIn("--no-playlist", cmd)
        self.assertEqual(cmd[-1], "https://www.youtube.com/watch?v=abc123")
        sd = yt.BASE_DIR / "Reg" / "Season 01"
        sd.mkdir(parents=True, exist_ok=True)
        with patch.object(yt, "download_video", return_value="ok"), patch.object(yt.time, "sleep"), _q():
            yt._download_one({"id": "v", "title": "Good"}, "Reg", 1, 1, sd, [])
        yt.registry.register_file.assert_called_once()
        yt.registry.mark_status.assert_called_with(str(sd / "Reg - S01E0001 - Good.mp4"), "downloaded")

    def test_subscription_sync_adds_new_channels(self):
        _reset()
        first = next(iter(yt.CHANNELS.values()))["url"].rsplit("@", 1)[-1]
        out = f"UC1\t@{first}\tKnown\nUC2\t@NewChan\tNew Channel\nNA\tx\ty\n"
        with patch.object(yt, "_cookies_args", return_value=[]), \
                patch.object(yt.subprocess, "run", return_value=_cp(0, out)), _q():
            merged = yt.sync_subscriptions()
        self.assertEqual(merged["newchan"]["url"], "https://www.youtube.com/@NewChan")
        self.assertEqual(len(merged), len(yt.CHANNELS) + 1)
        self.assertEqual(json.loads(yt.CHANNELS_CACHE.read_text())["added_this_run"], ["New Channel"])


class TestFunctional(unittest.TestCase):
    def test_main_single_channel_golden_path(self):
        _reset()
        chans = {"t": {"name": "TestShow", "url": "https://www.youtube.com/@t", "mode": "single"}}
        sd = yt.BASE_DIR / "TestShow" / "Season 01"
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "TestShow - S01E0001 - Old Engine Rebuild.mp4").write_text("")
        vids = [{"id": "n2", "title": "Second Brand Gearbox", "upload_date": ""},
                {"id": "n1", "title": "First Shiny Turbocharger", "upload_date": ""},
                {"id": "o", "title": "Old Engine Rebuild", "upload_date": ""}]

        def fake_dl(vid, out):
            out.write_text("")                                      # file lands -> next ep number advances
            return "ok"
        with patch.object(yt, "sync_subscriptions", return_value=chans), \
                patch.object(yt, "get_recent_videos", return_value=vids), \
                patch.object(yt, "download_video", side_effect=fake_dl), \
                patch.object(yt, "scan_new_recordings", return_value=[]), patch.object(yt.time, "sleep"), _q():
            yt.main()
        names = sorted(p.name for p in sd.glob("*.mp4"))
        self.assertIn("TestShow - S01E0002 - First Shiny Turbocharger.mp4", names)   # oldest-first
        self.assertIn("TestShow - S01E0003 - Second Brand Gearbox.mp4", names)
        self.assertIn("2 new episode(s) downloaded", yt.nova_config.post_both.call_args[0][0])

    def test_channel_crash_reported_and_run_continues(self):
        _reset()
        chans = {"a": {"name": "A", "url": "u", "mode": "single"}, "b": {"name": "B", "url": "u", "mode": "single"}}
        with patch.object(yt, "sync_subscriptions", return_value=chans), \
                patch.object(yt, "process_channel", side_effect=[RuntimeError("boom"), None]) as pc, \
                patch.object(yt, "scan_new_recordings", return_value=[]), _q():
            yt.main()
        self.assertEqual(pc.call_count, 2)
        msgs = [c[0][0] for c in yt.nova_config.post_both.call_args_list]
        self.assertTrue(any("*A* — check failed" in m for m in msgs))
        self.assertIn("all 2 channels up to date", msgs[-1])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_yt_new_episodes"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
