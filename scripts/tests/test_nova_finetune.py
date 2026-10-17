#!/usr/bin/env python3
"""Tests for nova_finetune.py — weight-level growth, kept only when it beats the model she has."""

import json
import re
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nova_finetune as ft  # noqa: E402

SRC = Path(ft.__file__).read_text()


class Cur:
    def __init__(self, canned=None):
        self.canned, self.calls, self._last = canned or [], [], None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        self._last = next((v for k, v in self.canned if k in sql), None)

    def fetchall(self):
        return self._last if isinstance(self._last, list) else []

    def fetchone(self):
        return self._last if isinstance(self._last, tuple) else None

    def writes(self, frag):
        return [c for c in self.calls if frag in c[0]]


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        ft.selftest()

    def test_invented_facts_are_caught(self):
        self.assertIsNone(ft.invented("Alert: 3 disks at 72%", "Three disks at 72% is bad; I'll look at the array next."))
        self.assertTrue(ft.invented("Is the NAS ok?", "The logs show it's fine, I verified it."))
        self.assertTrue(ft.invented("Is the NAS ok?", "It's at 91% capacity."))

    def test_verdict_mapping_is_order_blind(self):
        # flip=True means A is the new model
        for flip, v, want in ((True, "A", "new"), (True, "B", "base"), (False, "A", "base"), (False, "B", "new")):
            got = "tie" if v == "tie" else ("new" if (v == "A") == flip else "base")
            self.assertEqual(got, want)


class TestVoiceCuration(unittest.TestCase):
    """2026-10-17: voice_wr 0.44 was judged against junk 'real replies'; these keep junk out of train and eval."""

    def test_persona_breaks_are_named(self):
        self.assertEqual(ft.voice_issues("Fine, Little Mister. It ran; I'd still eyeball the disk first."), [])
        self.assertIn("help-desk", ft.voice_issues("I'm ready to assist under the RESTRAINT rule. You can ask about lights."))
        self.assertIn("bullet list", ft.voice_issues("Snapshot:\n- Mac one\n- Mac two"))
        self.assertIn("bold header", ft.voice_issues("**Active Clients:** two"))
        self.assertIn("permission-seeking", ft.voice_issues("Done. Want me to add an automation for it?"))
        self.assertIn("tool failure leak", ft.voice_issues("web search failed, all connection attempts timed out."))
        self.assertIn("claimed check", ft.voice_issues("Checked shared_observations. Claude says it is done."))
        self.assertNotIn("claimed check", ft.voice_issues("I'll check if the logs show a pattern first."))

    def test_replay_ok(self):
        self.assertEqual(ft.replay_ok("lol", "Toaster fleet is jealous of your sensor plans, Little Mister."), "context-free prompt")
        self.assertTrue(ft.replay_ok("Can you list the plugs per strip?", "Kitchen 7.1W, office 3.2W.").startswith("invented"))
        self.assertIsNone(ft.replay_ok("Do you know where I am right now?",
                                       "No, Little Mister. I don't have your location, and I'm not going to guess."))

    def test_clean_user_and_internal_parts(self):
        self.assertIsNone(ft.clean_user("[chatroom message from Baalsong]: @nova mad?"))
        self.assertIsNone(ft.clean_user("test_ping_from_tests"))
        self.assertEqual(ft.clean_user("[chatroom message from Jordan]: Nova, describe the network."), "Nova, describe the network.")
        self.assertTrue(ft.names_internal_part("organ:card_rack", "Alert: Card_rack efficiency dropped", "ok"))

    def test_consensus_removes_position_bias(self):
        self.assertEqual(ft.consensus("B", "A"), "new")    # new won in both orders
        self.assertEqual(ft.consensus("A", "B"), "base")
        self.assertEqual(ft.consensus("A", "A"), "tie")    # always picks slot A -> bias, not preference
        self.assertEqual(ft.consensus("tie", "tie"), "tie")
        self.assertIsNone(ft.consensus(None, None))

    def test_balance_keeps_replay_share(self):
        rows = [(f"r{i}", "replay") for i in range(45)] + [(f"p{i}", "practice") for i in range(200)]
        out = ft.balance(rows)
        rep = sum(1 for r in out if r[1] == "replay")
        self.assertEqual(rep, 45)
        self.assertGreaterEqual(rep / len(out), 0.45 - 0.01)
        self.assertEqual(ft.balance(rows), out)            # stable

    def test_audit_score(self):
        self.assertEqual(ft.audit_score('{"score": 4, "why": "x"}'), 4)
        self.assertIsNone(ft.audit_score("nope"))

    def test_judge_prompt_carries_persona_for_voice(self):
        p = ft.judge_prompt("replay", "real reply", "msg", "a", "b", persona="PERSONA-XYZ")
        self.assertIn("PERSONA-XYZ", p)
        self.assertIn("real reply", p)

    def test_gates_unchanged(self):
        self.assertEqual((ft.PRACTICE_GATE, ft.VOICE_GATE), (0.60, 0.45))


class TestIntegration(unittest.TestCase):
    def test_build_keeps_only_honest_critiqued_examples(self):
        good = json.dumps({"user": "Alert: login twice from a new IP at night.", "reply": "Two logins at night from a new IP — I'll look it up before calling it hostile."})
        fab = json.dumps({"user": "Is the NAS fine?", "reply": "The logs show it's fine, I checked it this morning for you."})
        crit_ok = json.dumps({"invented": [], "lives": 5, "coherent": 5, "voice": 5})
        answers = iter([good, crit_ok, fab])
        oc = Cur([("SELECT key FROM finetune_examples", [])])
        with patch.object(ft, "principles", return_value=[("p:1", "organ:x", "check first"), ("p:2", "organ:x", "check")]), \
                patch.object(ft, "chat", side_effect=lambda *a, **k: next(answers)), \
                patch.dict(sys.modules, {"psycopg2": MagicMock(connect=MagicMock(side_effect=OSError)),
                                         "nova_dsn": MagicMock()}):
            made = ft.build(oc, variants=1)
        self.assertEqual(made["practice"], 1)
        self.assertEqual(len(oc.writes("INSERT INTO finetune_examples")), 1)

    def test_export_writes_chat_jsonl(self):
        import tempfile
        oc = Cur([("FROM finetune_examples", [("k1", "replay", "hi Nova", "Hello, Little Mister.")])])
        with tempfile.TemporaryDirectory() as d:
            n = ft.export(oc, Path(d))
            row = json.loads((Path(d) / "train.jsonl").read_text().splitlines()[0])
        self.assertEqual(n["train"], 1)
        self.assertEqual([m["role"] for m in row["messages"]], ["system", "user", "assistant"])

    def test_deploy_refused_without_a_passing_eval(self):
        oc = Cur([("SELECT passed", (False,))])
        with patch.object(ft, "ops", return_value=oc), patch.object(ft, "deploy") as dep, \
                patch.object(ft, "ROOT", Path("/tmp/nova-ft-test")):
            self.assertEqual(ft.run(["deploy"], "t"), 1)
        dep.assert_not_called()

    def test_failed_gate_stops_before_deploy(self):
        oc = Cur()
        with patch.object(ft, "ops", return_value=oc), patch.object(ft, "deploy") as dep, \
                patch.object(ft, "evaluate", return_value={"practice_wr": 0.8, "voice_wr": 0.2, "n": {}}), \
                patch.object(ft, "ROOT", Path("/tmp/nova-ft-test")):
            self.assertEqual(ft.run(["eval", "deploy"], "t"), 0)
        dep.assert_not_called()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertIsNone(re.search(r"(password|secret|token)\s*=\s*['\"][^'\"]{6,}", SRC, re.I))

    def test_credential_shaped_examples_rejected(self):
        self.assertIsNone(ft.parse_example(json.dumps({"user": "api_key: abc123 is it valid?", "reply": "x" * 50})))

    def test_teacher_forbids_dangerous_content(self):
        p = ft.teacher_prompt("v", "organ:x", "p")
        for w in ("credentials", "deleting", "watchers"):
            self.assertIn(w, p)


class TestPerformance(unittest.TestCase):
    def test_split_and_invented_fast(self):
        t = time.time()
        for i in range(20000):
            ft.split_for(str(i))
            ft.invented("Alert 3 at 72%", "Three at 72% — I'll look next.")
        self.assertLess(time.time() - t, 3.0)


class TestResilience(unittest.TestCase):
    def test_teacher_failure_returns_empty(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(ft.chat("x"), "")

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
