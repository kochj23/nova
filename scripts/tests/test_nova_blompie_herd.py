#!/usr/bin/env python3
"""Tests for nova_blompie_herd.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
No mail is sent, no Slack post, no Blompie/Ollama/memory-server call: all mocked at module load."""
import importlib.util
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
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_blompie_herd.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="blompie-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("blompie_herd_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bh = _load()
# module-level stubs: state in a tempdir, no mail / Slack / HTTP / subprocess anywhere
bh.STATE_FILE = TMP / "game.json"
bh.nova_config = types.SimpleNamespace(post_both=MagicMock(), SLACK_EMAIL="C_TEST")
bh.send_mail = MagicMock(return_value=True)
bh.urllib = types.SimpleNamespace(request=types.SimpleNamespace(
    Request=MagicMock(), urlopen=MagicMock(side_effect=OSError("offline"))))
bh.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=OSError("offline")))
bh.log = MagicMock()

P = [{"name": "Nova", "email": "nova@example.test", "agent": "Nova", "style": "poetic"},
     {"name": "Sam", "email": "sam@example.test", "agent": "Sam", "style": "warm"},
     {"name": "Marey", "email": "marey@example.test", "agent": "Marey", "style": "precise"}]


def _state(idx=1):
    return {"session_id": "s1", "turn": 3, "player_index": idx, "players": [dict(p) for p in P],
            "last_scene": "A dark hall.", "inventory": [], "suggested": [], "history": []}


def _cm(obj):
    r = MagicMock(); r.__enter__.return_value.read.return_value = json.dumps(obj).encode(); return r


def _reset():
    bh.send_mail.reset_mock(); bh.nova_config.post_both.reset_mock()
    if bh.STATE_FILE.exists():
        bh.STATE_FILE.unlink()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_addresses(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"[\w.]+@(gmail|digitalnoise)\.")   # roster comes from gitignored herd_config

    def test_mail_is_argv_not_shell(self):
        self.assertNotIn("shell=True", SRC)
        rec = MagicMock(return_value=types.SimpleNamespace(returncode=0))
        fresh = _load()
        with patch.object(fresh.subprocess, "run", rec):
            self.assertTrue(fresh.send_mail("a@example.test", "s", "$(touch pwned)"))
        argv = rec.call_args[0][0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[-1], "$(touch pwned)")              # passed verbatim as one argv element


class TestPerformance(unittest.TestCase):
    def test_format_scene_email_bulk(self):
        players = [{"name": f"p{i}", "email": f"p{i}@x", "style": "s"} for i in range(50)]
        t0 = time.perf_counter()
        for i in range(10_000):
            bh.format_scene_email("scene", players[i % 50], i, ["key"], players[:6])
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_auto_play_llm_failure_defaults(self):
        # RETRY GAP: nova_auto_play — one Ollama attempt; failure returns "look around"
        bh.urllib.request.urlopen.reset_mock()
        self.assertEqual(bh.nova_auto_play("scene", [], 1, []), "look around")
        self.assertEqual(bh.urllib.request.urlopen.call_count, 1)

    def test_inbox_check_failure_fails_open(self):
        # RETRY GAP: check_inbox_for_moves — subprocess failure is swallowed, no turn processed
        _reset(); bh.save_state(_state())
        with patch.object(bh, "cmd_turn") as ct:
            bh.check_inbox_for_moves()
        ct.assert_not_called()

    def test_blompie_api_failure_exits_without_advancing(self):
        # RETRY GAP: cmd_turn/blompie_action — no retry; exits 1 and the state is untouched
        _reset(); bh.save_state(_state())
        with self.assertRaises(SystemExit):
            bh.cmd_turn("sam@example.test", "go north")
        self.assertEqual(bh.load_state()["turn"], 3)


class TestUnit(unittest.TestCase):
    def test_auto_play_strips_think_and_takes_first_line(self):
        with patch.object(bh.urllib.request, "urlopen",
                          return_value=_cm({"response": "<think>x</think>`open the door`\nmore"})):
            self.assertEqual(bh.nova_auto_play("s", [], 1, ["a"]), "open the door")
        with patch.object(bh.urllib.request, "urlopen", return_value=_cm({"response": ""})):
            self.assertEqual(bh.nova_auto_play("s", [], 1, []), "look around")

    def test_format_scene_email(self):
        first = bh.format_scene_email("Fog.", P[1], 1, [], P, is_first=True)
        self.assertIn("The Herd Plays Blompie", first)
        self.assertIn("nothing yet", first)
        later = bh.format_scene_email("Fog.", P[1], 2, ["lamp"], P)
        self.assertNotIn("Players (in order)", later)
        self.assertIn("lamp", later)
        self.assertIn("Nova, Marey", later)

    def test_load_state_absent(self):
        _reset()
        self.assertIsNone(bh.load_state())


class TestIntegration(unittest.TestCase):
    def test_slack_uses_shared_post_both_on_email_channel(self):
        bh.slack_post("hi")
        bh.nova_config.post_both.assert_called_with("hi", slack_channel="C_TEST")

    def test_inbox_reply_routes_to_cmd_turn(self):
        _reset(); bh.save_state(_state())
        lst = types.SimpleNamespace(returncode=0, stdout=json.dumps({"messages": [
            {"from_addr": "Sam <sam@example.test>", "subject": "Re: Blompie turn", "uid": 7}]}))
        rd = types.SimpleNamespace(returncode=0, stdout=json.dumps({"body_plain": "\n> quoted\n`go west`\n"}))
        with patch.object(bh.subprocess, "run", MagicMock(side_effect=[lst, rd])), \
             patch.object(bh, "cmd_turn") as ct:
            bh.check_inbox_for_moves()
        ct.assert_called_once_with("sam@example.test", "go west")


class TestFunctional(unittest.TestCase):
    def test_turn_advances_emails_and_posts(self):
        _reset(); bh.save_state(_state(idx=1))
        res = {"response": [{"text": "You go north."}], "suggestedActions": ["x"], "inventory": ["key"]}
        with patch.object(bh, "blompie_action", return_value=res):
            bh.cmd_turn("sam@example.test", "go north")
        st = bh.load_state()
        self.assertEqual((st["turn"], st["player_index"], st["inventory"]), (4, 2, ["key"]))
        self.assertEqual(st["history"][-1]["command"], "go north")
        recipients = [c.args[0] for c in bh.send_mail.call_args_list]
        self.assertIn("marey@example.test", recipients)          # next player gets the action email
        self.assertIn("Blompie — Turn 3", bh.nova_config.post_both.call_args[0][0])

    def test_nova_is_next_autoplays_once(self):
        _reset(); bh.save_state(_state(idx=2))                  # Marey plays, Nova is next
        res = {"response": [{"text": "Echoes."}], "inventory": []}
        with patch.object(bh, "blompie_action", return_value=res) as ba, \
             patch.object(bh, "nova_auto_play", return_value="listen"):
            bh.cmd_turn("marey@example.test", "shout")
        self.assertEqual([c.args[1] for c in ba.call_args_list], ["shout", "listen"])
        self.assertEqual(bh.load_state()["player_index"], 1)

    def test_nova_only_roster_does_not_recurse(self):
        # regression for the fix: a Nova-only roster used to auto-play forever
        _reset(); st = _state(idx=0); st["players"] = [dict(P[0])]; bh.save_state(st)
        with patch.object(bh, "blompie_action", return_value={"response": [{"text": "x"}]}) as ba, \
             patch.object(bh, "nova_auto_play", return_value="look"):
            bh.cmd_turn("nova@example.test", "look")
        self.assertEqual(ba.call_count, 1)

    def test_turn_without_game_exits(self):
        _reset()
        with self.assertRaises(SystemExit):
            bh.cmd_turn("x@example.test", "look")


class TestFrame(unittest.TestCase):
    def test_no_args_prints_usage_and_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Multiplayer Blompie", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(bh.cmd_start))


if __name__ == "__main__":
    unittest.main()
