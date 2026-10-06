#!/usr/bin/env python3
"""Tests for nova_discord_mirror.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_discord_mirror.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_discord_mirror_t", SCRIPTS / "nova_discord_mirror.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dm = _load()
CHAT, NOTIFY = dm.nova_config.SLACK_CHAN, dm.nova_config.SLACK_NOTIFY


def _resp(payload):
    r = mock.MagicMock()
    r.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return r


class _Env:
    """Tempdir state file, fake Slack token, captured Discord posts — nothing leaves the box."""
    def __init__(self, history=None):
        self.history = history or {}

    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.td.name, "cache", "state.json")
        self.ps = [mock.patch.object(dm, "STATE_FILE", self.state),
                   mock.patch.object(dm.nova_config, "slack_bot_token", return_value="xoxb-test"),
                   mock.patch.object(dm.nova_config, "post_discord", return_value=True),
                   mock.patch.object(dm.urllib.request, "urlopen", side_effect=self._urlopen)]
        self.m = [p.start() for p in self.ps]
        self.post = self.m[2]
        self.urls = []
        return self

    def _urlopen(self, req, timeout=None):
        self.urls.append(req.full_url)
        ch = re.search(r"channel=(\w+)", req.full_url).group(1)
        return _resp({"ok": True, "messages": self.history.get(ch, [])})

    def __exit__(self, *a):
        for p in reversed(self.ps):
            p.stop()
        self.td.cleanup()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/\-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("xoxb-", SRC)

    def test_token_from_config_sent_as_bearer_header(self):
        with _Env(), mock.patch.object(dm.urllib.request, "urlopen", return_value=_resp({"ok": True})) as uo:
            dm.get_slack_history("C1")
        req = uo.call_args[0][0]
        self.assertEqual(req.headers["Authorization"], "Bearer xoxb-test")
        self.assertNotIn("xoxb", req.full_url)

    def test_human_messages_never_mirrored(self):
        h = {CHAT: [{"ts": "1.0", "text": "private human words", "user": "U1"}]}
        with _Env(h) as e:
            self.assertEqual(dm.mirror_once(), 0)
            e.post.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_mirror_10k_messages_fast(self):
        msgs = [{"ts": f"{i}.0", "text": f"m{i}", "bot_id": "B"} for i in range(10_000, 0, -1)]
        with _Env({CHAT: msgs}):
            t0 = time.perf_counter()
            self.assertEqual(dm.mirror_once(), 10_000)
            self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_slack_failure_fails_open(self):
        # RETRY GAP: get_slack_history — one urlopen attempt; failure logs and returns []
        with _Env(), mock.patch.object(dm.urllib.request, "urlopen", side_effect=OSError("down")) as uo, \
                mock.patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(dm.get_slack_history("C1"), [])
            self.assertEqual(uo.call_count, 1)
            self.assertEqual(dm.mirror_once(), 0)

    def test_no_token_skips_network(self):
        with _Env() as e, mock.patch.object(dm.nova_config, "slack_bot_token", return_value=""):
            self.assertEqual(dm.get_slack_history("C1"), [])
            self.assertEqual(e.urls, [])


class TestUnit(unittest.TestCase):
    def test_post_truncates_to_discord_limit(self):
        with mock.patch.object(dm.nova_config, "post_discord", return_value=True) as pd:
            dm.post_to_discord("D", "x" * 5000)
        text = pd.call_args[0][0]
        self.assertEqual(len(text), 2000)
        self.assertTrue(text.endswith("..."))

    def test_state_roundtrip_and_corrupt(self):
        with _Env() as e:
            self.assertEqual(dm.load_state(), {})
            dm.save_state({"C": "5.0"})
            self.assertEqual(dm.load_state(), {"C": "5.0"})
            Path(e.state).write_text("{bad")
            self.assertEqual(dm.load_state(), {})


class TestIntegration(unittest.TestCase):
    def test_channel_map_comes_from_nova_config(self):
        self.assertEqual(dm.CHANNEL_MAP[CHAT], dm.nova_config.DISCORD_CHAT)
        self.assertEqual(dm.CHANNEL_MAP[NOTIFY], dm.nova_config.DISCORD_NOTIFY)
        self.assertNotIn("discord.com/api", SRC)   # posting goes through nova_config.post_discord


class TestFunctional(unittest.TestCase):
    def test_mirror_once_posts_bot_messages_in_order_and_advances_state(self):
        h = {CHAT: [{"ts": "3.0", "text": "third", "bot_id": "B"},
                    {"ts": "1.0", "text": "first", "subtype": "bot_message"},
                    {"ts": "2.0", "text": "human", "user": "U"}]}
        with _Env(h) as e:
            self.assertEqual(dm.mirror_once(), 2)
            self.assertEqual([c[0] for c in e.post.call_args_list],
                             [("first", dm.nova_config.DISCORD_CHAT), ("third", dm.nova_config.DISCORD_CHAT)])
            self.assertEqual(dm.load_state()[CHAT], "3.0")
            self.assertTrue(any("oldest=0" in u for u in e.urls))

    def test_resume_skips_already_mirrored_ts(self):
        h = {CHAT: [{"ts": "3.0", "text": "seen", "bot_id": "B"}]}
        with _Env(h) as e:
            dm.save_state({CHAT: "3.0"})
            self.assertEqual(dm.mirror_once(), 0)
            self.assertTrue(any("oldest=3.0" in u for u in e.urls))

    def test_main_one_shot_prints_count(self):
        with mock.patch.object(dm, "mirror_once", return_value=4), mock.patch.object(sys, "argv", ["x"]), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            dm.main()
        self.assertIn("Mirrored 4 message(s)", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_discord_mirror"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
