#!/usr/bin/env python3
"""Tests for nova_yt_capture.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

WORK and the Plex library live in a tempdir; yt-dlp / ffmpeg / whisper (subprocess.run), the memory
server (urlopen), PG (psycopg2.connect) and Slack (a stub nova_config) are mocked in every test."""
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
SCRIPT = SCRIPTS / "nova_yt_capture.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


yt = _load("nova_yt_capture_t", SCRIPT)


def _chat_line(author, msg, cid="UC1", amount=None):
    r = {"authorName": {"simpleText": author}, "authorExternalChannelId": cid,
         "message": {"runs": [{"text": msg}]}}
    key = "liveChatTextMessageRenderer"
    if amount:
        r["purchaseAmountText"] = {"simpleText": amount}
        key = "liveChatPaidMessageRenderer"
    return json.dumps({"replayChatItemAction": {"actions": [{"addChatItemAction": {"item": {key: r}}}]}})


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        self.cfg = types.ModuleType("nova_config")
        self.cfg.post_both, self.cfg.SLACK_FEED = MagicMock(), "C_FEED"
        boom = MagicMock(side_effect=AssertionError("unmocked outbound"))
        for p in (patch.object(yt, "WORK", root / "work"), patch.object(yt, "PLEX_YT", root / "videos/youtube/Fishbowl"),
                  patch.object(yt.subprocess, "run", boom), patch.object(yt.urllib.request, "urlopen", boom),
                  patch.object(yt.psycopg2, "connect", boom), patch.dict(sys.modules, {"nova_config": self.cfg})):
            p.start()
            self.addCleanup(p.stop)
        (root / "work").mkdir()
        self.root = root
        self.out = io.StringIO()
        r = redirect_stdout(self.out)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)

    def test_memories_are_marked_private_and_paths_sanitized(self):
        with patch.object(yt.urllib.request, "urlopen", MagicMock()) as uo:
            yt.remember("t", {"vector": "fishbowl"})
        self.assertEqual(json.loads(uo.call_args.args[0].data)["metadata"]["privacy"], "private")
        (self.root / "videos/youtube").mkdir(parents=True)
        (self.root / "work" / "abc.mp4").write_bytes(b"v" * 10)
        yt.file_video_to_plex("abc", "../../etc", "a/b\\c; rm", "abc")
        filed = list((self.root / "videos/youtube/Fishbowl").rglob("*.mp4"))
        self.assertEqual(len(filed), 1)
        self.assertTrue(str(filed[0].resolve()).startswith(str((self.root / "videos/youtube/Fishbowl").resolve())))

    def test_commenter_sql_parameterized(self):
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        with patch.object(yt.psycopg2, "connect", return_value=conn):
            yt.record_commenters("chan'; --", "v", {"UC1": {"name": "x'--", "messages": 1, "superchats": 0,
                                                             "superchat_total": 0.0}})
        sql, params = cur.execute.call_args.args
        self.assertNotIn("x'--", sql)
        self.assertEqual(params[1], "x'--")


class TestPerformance(_Base):
    def test_chunk_and_dedupe_large_text_fast(self):
        text = "\n\n".join(f"Paragraph {i} about the stream and what was said." for i in range(10_000))
        t0 = time.perf_counter()
        parts = yt.chunk(text)
        yt._dedupe_loops(text.replace("\n\n", " "))
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertTrue(all(len(p) <= yt.CHUNK for p in parts))


class TestRetry(_Base):
    def test_remember_failure_retries_then_reports(self):
        # remember/urlopen — 3 attempts per chunk with backoff; False (logged) after the last
        with patch.object(yt.urllib.request, "urlopen", side_effect=OSError("down")) as uo, \
                patch.object(yt.time, "sleep"):
            self.assertFalse(yt.remember("t", {"vector": "v"}))
        self.assertEqual(uo.call_count, 3)
        self.assertIn("remember failed after 3 tries", self.out.getvalue())

    def test_status_and_meta_fail_open(self):
        # setstatus retries 3x then logs; meta_of falls back to empty defaults
        with patch.object(yt.psycopg2, "connect", side_effect=OSError("pg down")), patch.object(yt.time, "sleep"):
            yt.setstatus("v", "x")
        with patch.object(yt.subprocess, "run", side_effect=subprocess.TimeoutExpired("yt-dlp", 120)):
            self.assertEqual(yt.meta_of("v"), ("", ""))
        self.assertIn("status update failed", self.out.getvalue())


class TestUnit(_Base):
    def test_dedupe_loops(self):
        self.assertEqual(yt._dedupe_loops("Hi. Hi. Hi. Hi. Bye."), "Hi. Hi. Bye.")
        self.assertEqual(yt._dedupe_loops("our hearts " * 30), "")
        self.assertEqual(yt._dedupe_loops(""), "")

    def test_chunk_edges(self):
        self.assertEqual(yt.chunk(""), [])
        self.assertEqual(yt.chunk("x" * 3100, size=1500), ["x" * 1500, "x" * 1500, "x" * 100])
        self.assertEqual(yt.chunk("a\n\nb"), ["a\n\nb"])

    def test_parse_chat_superchats_and_tallies(self):
        f = self.root / "c.live_chat.json"
        f.write_text("\n".join([_chat_line("Amy", "hello"), _chat_line("Amy", "take my money", amount="$1,005.50"),
                                "not json", _chat_line("Bo", "hey", cid="UC2")]))
        text, who = yt.parse_chat(f)
        self.assertEqual(text.splitlines(), ["Amy: hello", "[SUPERCHAT $1,005.50] Amy: take my money", "Bo: hey"])
        self.assertEqual(who["UC1"], {"name": "Amy", "messages": 1, "superchats": 1, "superchat_total": 1005.5})
        self.assertEqual(yt.parse_chat(None), ("", {}))


class TestIntegration(_Base):
    def test_download_builds_ytdlp_argv_and_finds_outputs(self):
        def fake_run(argv, **kw):
            (self.root / "work" / "vid1.mp4").write_bytes(b"v")
            (self.root / "work" / "vid1.live_chat.json").write_text("{}")
            return subprocess.CompletedProcess(argv, 0, "", "")
        with patch.object(yt.subprocess, "run", side_effect=fake_run) as run:
            audio, chat = yt.download("https://www.youtube.com/watch?v=vid1", True, "vid1")
        argv = run.call_args.args[0]
        self.assertEqual(argv[0], yt.YT_DLP)
        self.assertIn("--live-from-start", argv)
        self.assertEqual((audio.name, chat.name), ("vid1.mp4", "vid1.live_chat.json"))


class TestFunctional(_Base):
    def test_golden_path_stores_chunks_files_plex_and_posts(self):
        (self.root / "videos/youtube").mkdir(parents=True)
        stored = []

        def fake_download(url, live, stem):
            (self.root / "work" / f"{stem}.mp4").write_bytes(b"v" * 100)
            c = self.root / "work" / f"{stem}.live_chat.json"
            c.write_text(_chat_line("Amy", "first!"))
            return self.root / "work" / f"{stem}.mp4", c
        with patch.object(sys, "argv", ["x", "abc123", "fishbowl", "--live"]), \
                patch.object(yt, "setstatus") as st, patch.object(yt, "meta_of", return_value=("Late Show", "Chan")), \
                patch.object(yt, "download", side_effect=fake_download), \
                patch.object(yt, "to_wav", return_value=self.root / "work" / "abc123.wav"), \
                patch.object(yt, "transcribe", return_value="We talked about radios."), \
                patch.object(yt, "record_commenters") as rc, \
                patch.object(yt, "remember", side_effect=lambda t, m: stored.append((t, m)) or True):
            yt.main()
        self.assertEqual([m["part"] for _, m in stored], ["transcript", "chat"])
        self.assertTrue(stored[0][0].startswith("[Fishbowl stream — Chan — Late Show] (transcript)"))
        self.assertEqual(st.call_args_list[-1].args, ("abc123", "ingested"))
        self.assertEqual(list((self.root / "videos/youtube/Fishbowl/Chan").iterdir())[0].name, "Late Show [abc123].mp4")
        self.assertEqual(list((self.root / "work").iterdir()), [])
        self.assertIn("Fishbowl memory ingested", self.cfg.post_both.call_args.args[0])
        self.assertEqual(rc.call_args.args[0], "Chan")

    def test_undownloadable_vod_is_parked_for_retry_and_stays_quiet(self):
        # a long replay is often not downloadable yet -> vod_pending (retried every 2 h), not empty
        with patch.object(sys, "argv", ["x", "zzz", "fishbowl"]), patch.object(yt, "setstatus") as st, \
                patch.object(yt, "retry_or_empty") as ro, \
                patch.object(yt, "meta_of", return_value=("", "")), patch.object(yt, "download", return_value=(None, None)):
            yt.main()
        ro.assert_called_once_with("zzz")
        self.assertNotIn(("zzz", "empty"), [c.args for c in st.call_args_list])
        self.cfg.post_both.assert_not_called()

    def test_live_recording_with_nothing_is_empty_not_retried(self):
        with patch.object(sys, "argv", ["x", "zzz", "fishbowl", "--live"]), patch.object(yt, "setstatus") as st, \
                patch.object(yt, "retry_or_empty") as ro, \
                patch.object(yt, "meta_of", return_value=("", "")), patch.object(yt, "download", return_value=(None, None)):
            yt.main()
        ro.assert_not_called()
        self.assertEqual(st.call_args_list[-1].args, ("zzz", "empty"))


class TestFrame(unittest.TestCase):
    def test_no_args_prints_usage(self):
        # no --help; with too few args main() prints usage and exits 1 before any download
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("usage: nova_yt_capture.py", r.stdout)
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
