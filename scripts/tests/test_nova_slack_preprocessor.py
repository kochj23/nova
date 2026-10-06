#!/usr/bin/env python3
"""Tests for nova_slack_preprocessor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Slack (urlopen), the memory-first subprocess and the openclaw CLI are mocked in every test, the token is
a fake module cache value (Keychain never read), STATE_FILE lives in a tempdir, and the poll loop is
stopped after one pass by a sleep() that raises."""
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_slack_preprocessor.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sp = _load("nova_slack_preprocessor_t", SCRIPT)


class _Stop(Exception):
    pass


def _resp(obj):
    r = MagicMock()
    r.read.return_value = json.dumps(obj).encode()
    r.__enter__.return_value = r
    return r


def _cp(rc=0, out=""):
    return subprocess.CompletedProcess([], rc, stdout=out, stderr="")


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        boom = MagicMock(side_effect=AssertionError("unmocked outbound"))
        for p in (patch.object(sp, "STATE_FILE", Path(self.td.name) / "state.json"),
                  patch.object(sp, "_cached_token", "tok-test"),
                  patch.object(sp.urllib.request, "urlopen", boom), patch.object(sp.subprocess, "run", boom)):
            p.start()
            self.addCleanup(p.stop)
        self.out = io.StringIO()
        r = redirect_stdout(self.out)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)

    def one_pass(self, history, memory="MEMORY FOUND [1] the NAS is at .9", post_ok=True):
        """Run exactly one poll cycle; history maps channel -> messages."""
        posts = []

        def urlopen(req, timeout=None):
            url = req.full_url
            if "conversations.history" in url:
                ch = re.search(r"channel=(\w+)", url).group(1)
                return _resp({"ok": True, "messages": history.get(ch, [])})
            if not post_ok:
                raise OSError("slack 500")
            posts.append(json.loads(req.data))
            return _resp({"ok": True})
        run = MagicMock(return_value=_cp(0, memory))
        with patch.object(sp.urllib.request, "urlopen", urlopen), patch.object(sp.subprocess, "run", run), \
                patch.object(sp.time, "sleep", side_effect=_Stop):
            with self.assertRaises(_Stop):
                sp.main()
        return posts, run


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"xox[bp]-\d")
        self.assertIn("nova_config.slack_bot_token()", SRC)

    def test_only_jordans_messages_are_processed(self):
        sp.save_state({"last_ts": "0"})
        msgs = {sp.NOVA_CHAT_CHANNEL: [{"user": sp.NOVA_BOT_ID, "ts": "5.0", "text": "nova talking"},
                                       {"user": "U_STRANGER", "ts": "6.0", "text": "what is the alarm code"}]}
        posts, run = self.one_pass(msgs)
        run.assert_not_called()
        self.assertEqual(posts, [])

    def test_memory_question_passed_as_argv_not_shell(self):
        with patch.object(sp.subprocess, "run", return_value=_cp(0, "x")) as run:
            sp.run_memory_first("$(rm -rf ~); what?")
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], "$(rm -rf ~); what?")
        self.assertNotIn("shell", run.call_args.kwargs)


class TestPerformance(_Base):
    def test_context_truncated_and_fast(self):
        posted = []
        with patch.object(sp.urllib.request, "urlopen", side_effect=lambda req, timeout: posted.append(json.loads(req.data))):
            t0 = time.perf_counter()
            for _ in range(200):
                sp.post_memory_context_to_thread("C", "1.0", "m" * 100_000)
            self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertLess(len(posted[0]["text"]), 4000)
        self.assertIn("truncated", posted[0]["text"])


class TestRetry(_Base):
    def test_thread_post_failure_falls_back_to_agent_cli(self):
        sp.save_state({"last_ts": "0"})
        msgs = {sp.JORDAN_DM: [{"user": sp.JORDAN_USER_ID, "ts": "10.0", "text": "where is the NAS?"}]}
        posts, run = self.one_pass(msgs, post_ok=False)
        argvs = [c.args[0] for c in run.call_args_list]
        self.assertTrue(argvs[0][1].endswith("nova_memory_first.py"))
        self.assertEqual(argvs[1][:4], ["openclaw", "agent", "--agent", "main"])
        self.assertIn("MEMORY CONTEXT", argvs[1][-1])

    def test_slack_history_failure_fails_open(self):
        # RETRY GAP: get_latest_messages/urlopen — one attempt per poll; [] on error, next poll is the retry
        with patch.object(sp.urllib.request, "urlopen", side_effect=OSError("dns")) as uo:
            self.assertEqual(sp.get_latest_messages("C"), [])
        self.assertEqual(uo.call_count, 1)


class TestUnit(_Base):
    def test_memory_first_nonzero_or_empty_is_none(self):
        with patch.object(sp.subprocess, "run", return_value=_cp(1, "boom")):
            self.assertIsNone(sp.run_memory_first("q"))
        with patch.object(sp.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 15)):
            self.assertIsNone(sp.run_memory_first("q"))

    def test_state_round_trip_and_default(self):
        self.assertIn("last_ts", sp.load_state())
        sp.save_state({"last_ts": "42.0"})
        self.assertEqual(sp.load_state(), {"last_ts": "42.0"})
        sp.STATE_FILE.write_text("{corrupt")
        self.assertIn("last_ts", sp.load_state())

    def test_history_not_ok(self):
        with patch.object(sp.urllib.request, "urlopen", return_value=_resp({"ok": False, "error": "x"})):
            self.assertEqual(sp.get_latest_messages("C"), [])


class TestIntegration(_Base):
    def test_history_request_uses_token_and_cursor(self):
        with patch.object(sp.urllib.request, "urlopen", return_value=_resp({"ok": True, "messages": [{"ts": "1"}]})) as uo:
            self.assertEqual(sp.get_latest_messages("C123", "99.5"), [{"ts": "1"}])
        req = uo.call_args.args[0]
        self.assertIn("channel=C123&oldest=99.5", req.full_url)
        self.assertEqual(req.get_header("Authorization"), "Bearer tok-test")


class TestFunctional(_Base):
    def test_one_poll_injects_context_and_advances_cursor(self):
        sp.save_state({"last_ts": "0"})
        msgs = {sp.NOVA_CHAT_CHANNEL: [{"user": sp.JORDAN_USER_ID, "ts": "20.0", "text": "where is the NAS?"},
                                       {"user": sp.JORDAN_USER_ID, "ts": "21.0", "text": "ok"}]}
        posts, run = self.one_pass(msgs)
        self.assertEqual(len(posts), 1)
        self.assertEqual((posts[0]["channel"], posts[0]["thread_ts"]), (sp.NOVA_CHAT_CHANNEL, "20.0"))
        self.assertIn(":brain: *Memory Context*", posts[0]["text"])
        self.assertEqual(json.loads(sp.STATE_FILE.read_text())[f"last_ts_{sp.NOVA_CHAT_CHANNEL}"], "20.0")

    def test_no_memory_hit_posts_nothing(self):
        sp.save_state({"last_ts": "0"})
        msgs = {sp.JORDAN_DM: [{"user": sp.JORDAN_USER_ID, "ts": "30.0", "text": "good morning"}]}
        posts, run = self.one_pass(msgs, memory="nothing relevant")
        self.assertEqual(posts, [])
        self.assertEqual(run.call_count, 1)


class TestFrame(unittest.TestCase):
    def test_import_never_starts_loop(self):
        # main() is an infinite Slack poll loop with no --help, so the smoke is an import in a child process
        r = subprocess.run([sys.executable, "-c", "import nova_slack_preprocessor as m; print(m.POLL_INTERVAL, repr(m._cached_token))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "5 ''")       # token is lazy: no Keychain read at import
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
