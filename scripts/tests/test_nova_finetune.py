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


def _eval_result(p_wr, p_n, v_wr, v_n, v_diff):
    """An evaluate() result as run() consumes it."""
    st = {"practice": {"wr": p_wr, "n": p_n, "n_pairs": 2 * p_n, "wr_ci": ft.wilson(p_wr, p_n), "diff_ci": (0.5, 0.2, 0.8)},
          "replay": {"wr": v_wr, "n": v_n, "n_pairs": 2 * v_n, "wr_ci": ft.wilson(v_wr, v_n), "diff_ci": v_diff},
          "agreement_with_panel": {}, "order_consistency": {}}
    return {"practice_wr": p_wr, "voice_wr": v_wr, "n": {"practice": p_n, "replay": v_n}, "stats": st,
            "kept": ["j1", "j2"], "runtime_s": 1}


def _fake_judge(style):
    """Mock judge panel. Replies are tagged NEW/BASE (exam) or REAL/BLAND (calibration). Styles:
    good = picks NEW/REAL; bias = always slot A; contrarian = picks REAL in calibration but BASE in the exam."""
    def call(judge, prompt):
        a = prompt.split("Reply A:\n", 1)[1].split("\n\nReply B:", 1)[0]
        good = "A" if ("NEW" in a or "REAL" in a) else "B"
        bad = "B" if good == "A" else "A"
        kind = style[judge]
        w = good if kind == "good" else "A" if kind == "bias" else (good if ("REAL" in prompt) else bad)
        hi, lo = {t: 5 for t in ("warmth", "bite", "directness", "prose", "like_her", "honesty", "lives", "helpful",
                                  "honest")}, {t: 2 for t in ("warmth", "bite", "directness", "prose", "like_her",
                                                             "honesty", "lives", "helpful", "honest")}
        return json.dumps({"A": hi if w == "A" else lo, "B": hi if w == "B" else lo, "winner": w})
    return call


def _exam_cursor(n=5, calib=4):
    rows = [("calibration", 900 + i, f"calib msg {i}", f"REAL reply {i}", "", f"BLAND reply {i}") for i in range(calib)]
    rows += [("practice", 100 + i, f"practice msg {i}", f"ref {i}", f"principle {i}", None) for i in range(n)]
    rows += [("replay", 200 + i, f"voice msg {i}", f"her reply {i}", "", None) for i in range(n)]
    return Cur([("SELECT max(version)", (1,)), ("JOIN finetune_examples", rows)])


def _gen(prompts, adapter, sysmsg, **_):
    return [[("NEW" if adapter else "BASE") + f" reply {i}.{s}" for s in range(ft.EVAL_SAMPLES)]
            for i, _ in enumerate(prompts)]


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

    def test_point_gates_retired(self):
        # 2026-10-19: the CI gate replaced PRACTICE_GATE/VOICE_GATE; nothing may still read them
        self.assertFalse(hasattr(ft, "PRACTICE_GATE") or hasattr(ft, "VOICE_GATE") or hasattr(ft, "EVAL_N"))


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
                patch.object(ft, "evaluate", return_value=_eval_result(0.8, 60, 0.2, 34, (-0.6, -0.8, -0.4))), \
                patch.object(ft, "ROOT", Path("/tmp/nova-ft-test")):
            self.assertEqual(ft.run(["eval", "deploy"], "t"), 0)
        dep.assert_not_called()
        self.assertIs([c for c in oc.calls if "finetune_runs" in c[0]][-1][1][4], False)   # passed=False recorded


class TestStatsAndGate(unittest.TestCase):
    """2026-10-19: Wilson / bootstrap intervals and the gate decisions."""

    def test_wilson_known_values(self):
        lo, hi = ft.wilson(0.5, 60)
        self.assertAlmostEqual(lo, 0.377, places=3)
        self.assertAlmostEqual(hi, 0.623, places=3)
        lo, hi = ft.wilson(0.78, 27)
        self.assertTrue(0.59 < lo < 0.60 and 0.89 < hi < 0.91)
        self.assertEqual(ft.wilson(0.3, 0), (0.0, 1.0))
        self.assertLess(ft.wilson(0.7, 200)[1] - ft.wilson(0.7, 200)[0], ft.wilson(0.7, 20)[1] - ft.wilson(0.7, 20)[0])

    def test_gate_confident_pass(self):
        self.assertTrue(ft.passes_gate(ft.wilson(0.75, 60), ft.wilson(0.5, 34), (0.0, -0.3, 0.3)))

    def test_gate_noisy_fail(self):
        # the 12:38 eval: practice 78% of 27 would pass, but 0.65 of 15 is noise
        why = ft.gate_failures(ft.wilson(0.65, 15), ft.wilson(0.5, 15), (0.0, -0.2, 0.2))
        self.assertEqual(len(why), 1)
        self.assertIn("practice not confidently better", why[0])

    def test_gate_voice_not_worse_is_enough(self):
        # voice may LOSE on points (40%) and still pass when the data cannot say it is worse
        self.assertTrue(ft.passes_gate(ft.wilson(0.8, 60), ft.wilson(0.40, 34), (-0.1, -0.4, 0.2)))
        self.assertFalse(ft.passes_gate(ft.wilson(0.8, 60), ft.wilson(0.25, 60), (-0.1, -0.4, 0.2)))
        self.assertIn("voice rubric clearly worse",
                      " ".join(ft.gate_failures(ft.wilson(0.8, 60), ft.wilson(0.45, 60), (-0.6, -0.9, -0.3))))
        self.assertFalse(ft.passes_gate(ft.wilson(0.8, 60), ft.wilson(0.5, 60), None))

    def test_bootstrap_stable_and_brackets_mean(self):
        xs = [0.5, -0.2, 0.1, 0.0, 0.3, -0.1, 0.4]
        m, lo, hi = ft.bootstrap_ci(xs)
        self.assertEqual(ft.bootstrap_ci(xs), (m, lo, hi))
        self.assertTrue(lo <= m <= hi)
        self.assertIsNone(ft.bootstrap_ci([]))

    def test_rubric_parse(self):
        raw = '<think>x</think>{"A": {"warmth": 4, "bite": 2}, "B": {"warmth": 5, "bite": 9}, "winner": "B"}'
        self.assertEqual(ft.parse_judgement(raw, "replay"), ("B", 3.0, 5.0))      # clamped to 5
        self.assertEqual(ft.parse_judgement('{"winner": "tie"}', "practice"), ("tie", None, None))
        p = ft.judge_prompt("practice", "check first", "m", "a", "b")
        self.assertIn('"lives": n', p)
        self.assertNotIn("Her real reply", ft.judge_prompt("replay", "", "m", "a", "b", "P"))


class TestBenchmark(unittest.TestCase):
    """Frozen set, dedupe-before-split, leakage, calibration, the judge panel."""

    def test_frozen_set_is_read_by_version_not_latest_rows(self):
        oc = _exam_cursor()
        with patch.object(ft, "_generate_all", side_effect=_gen), patch.object(ft, "deployed_system_prompt", return_value="S"), \
                patch.object(ft, "judge_call", side_effect=_fake_judge({"j1": "good", "j2": "good"})):
            r1 = ft.evaluate(oc, Path("/x/adapters"), judges=("j1", "j2"))
            r2 = ft.evaluate(oc, Path("/x/adapters"), judges=("j1", "j2"))
        self.assertEqual(r1["stats"], r2["stats"])
        sqls = [c for c in oc.calls if "finetune_eval_set s" in c[0]]
        self.assertTrue(sqls and all(c[1] == (1,) and "LIMIT" not in c[0] for c in sqls))
        self.assertNotIn("ORDER BY id DESC LIMIT", SRC)

    def test_pick_eval_stable_and_prefix_preserving(self):
        cands = [(i, f"question number {i}", f"answer {i}") for i in range(100)]
        a = ft.pick_eval(cands, set(), 60)
        self.assertEqual(a, ft.pick_eval(cands, set(), 60))
        self.assertEqual(ft.pick_eval(cands + [(999, "new", "row")], set(), 60), a)   # new rows never reshuffle it

    def test_dedupe_before_split(self):
        oc = Cur([("SELECT key, user_text, reply FROM finetune_examples", [("old", "Is the NAS ok?", "It is grumpy today.")])])
        cands = [("replay:gw:1", "is the NAS OK", "A different reply but the same question."),   # dup of old by message
                 ("replay:chat:2", "Totally new question here", "A brand new reply, Little Mister."),
                 ("replay:chat:3", "Totally new question here!", "Yet another reply.")]           # dup of #2
        with patch.object(ft, "principles", return_value=[]), patch.object(ft, "replay_candidates", return_value=cands):
            made = ft.build(oc)
        ins = oc.writes("INSERT INTO finetune_examples")
        self.assertEqual(made["replay"], 1)
        self.assertEqual(ins[0][1][0], "replay:chat:2")
        self.assertEqual(ins[0][1][3], ft.split_for(ft.content_key(cands[1][1], cands[1][2])))
        # copies share a split however they are keyed
        self.assertEqual(ft.split_for(ft.content_key("Hi  Nova!", "Hello.")), ft.split_for(ft.content_key("hi nova", "hello")))

    def test_leakage_check_against_training_text(self):
        banned = ft.text_keys("Is the backup done?", "Trained reply.")
        cands = [(1, "is the backup done", "A held-out reply."), (2, "Fresh question?", "trained reply"),
                 (3, "Clean question", "Clean reply")]
        self.assertEqual(ft.pick_eval(cands, banned, 10), [3])

    def test_export_drops_train_rows_that_leak_into_the_exam(self):
        import tempfile
        oc = Cur([("SELECT max(version)", (1,)),
                  ("JOIN finetune_examples", [("replay", 7, "Is the backup done?", "Exam reply.", "", None)]),
                  ("WHERE split=%s AND excluded", [("k1", "replay", "is the backup done", "Some other reply."),
                                                   ("k2", "replay", "Unrelated message", "Unrelated reply.")])])
        with tempfile.TemporaryDirectory() as d:
            n = ft.export(oc, Path(d))
        self.assertEqual(n["train"], 1)
        self.assertEqual(n["leak_dropped"], 2)      # train + valid copies of the exam text

    def test_calibration_discards_biased_judge(self):
        oc = _exam_cursor()
        with patch.object(ft, "_generate_all", side_effect=_gen), patch.object(ft, "deployed_system_prompt", return_value="S"), \
                patch.object(ft, "judge_call", side_effect=_fake_judge({"j1": "good", "j2": "good", "j3": "bias"})):
            r = ft.evaluate(oc, Path("/x/adapters"), judges=("j1", "j2", "j3"))
        self.assertEqual(r["calibration"]["j3"], 0.5)
        self.assertEqual(r["kept"], ["j1", "j2"])
        self.assertNotIn("void", r)

    def test_calibration_void_when_fewer_than_two_judges(self):
        oc = _exam_cursor()
        gen = MagicMock(side_effect=_gen)
        with patch.object(ft, "_generate_all", gen), \
                patch.object(ft, "judge_call", side_effect=_fake_judge({"j1": "good", "j2": "bias", "j3": "bias"})):
            r = ft.evaluate(oc, Path("/x/adapters"), judges=("j1", "j2", "j3"))
        self.assertIn("void", r)
        gen.assert_not_called()                     # no GPU time spent on a void exam
        with patch.object(ft, "ops", return_value=Cur()), patch.object(ft, "evaluate", return_value=r), \
                patch.object(ft, "deploy") as dep, patch.object(ft, "ROOT", Path("/tmp/nova-ft-test")):
            self.assertEqual(ft.run(["eval", "deploy"], "t"), 0)
        dep.assert_not_called()

    def test_majority_vote_panel(self):
        oc = _exam_cursor(n=6)
        with patch.object(ft, "_generate_all", side_effect=_gen), patch.object(ft, "deployed_system_prompt", return_value="S"), \
                patch.object(ft, "judge_call", side_effect=_fake_judge({"j1": "good", "j2": "good", "j3": "contrarian"})):
            r = ft.evaluate(oc, Path("/x/adapters"), judges=("j1", "j2", "j3"))
        st = r["stats"]
        self.assertEqual(r["kept"], ["j1", "j2", "j3"])
        self.assertEqual((st["practice"]["wr"], st["replay"]["wr"]), (1.0, 1.0))   # 2 of 3 outvote the contrarian
        self.assertEqual((st["practice"]["n"], st["practice"]["n_pairs"]), (6, 6 * ft.EVAL_SAMPLES))
        self.assertEqual(st["agreement_with_panel"]["j3"], 0.0)
        self.assertEqual(st["order_consistency"]["j1"], 1.0)
        self.assertAlmostEqual(st["replay"]["diff_ci"][0], 1.0)   # (5+5+2 - (2+2+5)) / 3 judges

    def test_summarise_split_panel_is_a_tie(self):
        res = {"a": [{"v": "new", "diff": 1.0, "orders": ("B", "A")}],
               "b": [{"v": "base", "diff": -1.0, "orders": ("A", "B")}]}
        st = ft.summarise([("practice", 0)], res, ["a", "b"])
        self.assertEqual(st["practice"]["wr"], 0.5)
        self.assertEqual(st["practice"]["diff_ci"][0], 0.0)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertIsNone(re.search(r"(password|secret|token)\s*=\s*['\"][^'\"]{6,}", SRC, re.I))

    def test_credential_shaped_examples_rejected(self):
        self.assertIsNone(ft.parse_example(json.dumps({"user": "api_key: abc123 is it valid?", "reply": "x" * 50})))

    def test_teacher_forbids_dangerous_content(self):
        p = ft.teacher_prompt("v", "organ:x", "p")
        for w in ("credentials", "deleting", "watchers"):
            self.assertIn(w, p)


    def test_claude_judge_prompt_goes_over_stdin_without_tools(self):
        proc = MagicMock(pid=1, communicate=MagicMock(return_value=('{"winner": "A"}', "")))
        cc = MagicMock(resolve_claude_bin=MagicMock(return_value="/bin/claude"), claude_env=MagicMock(return_value={}))
        with patch.dict(sys.modules, {"nova_claude_code": cc}), patch("subprocess.Popen", return_value=proc) as po:
            self.assertEqual(ft.claude_call("SECRET-PROMPT"), '{"winner": "A"}')
        argv = po.call_args[0][0]
        self.assertNotIn("SECRET-PROMPT", " ".join(argv))
        self.assertEqual(proc.communicate.call_args[0][0], "SECRET-PROMPT")
        self.assertEqual(argv[argv.index("--tools") + 1], "")


class TestPerformance(unittest.TestCase):
    def test_split_and_invented_fast(self):
        t = time.time()
        for i in range(20000):
            ft.split_for(str(i))
            ft.invented("Alert 3 at 72%", "Three at 72% — I'll look next.")
        self.assertLess(time.time() - t, 3.0)


    def test_bootstrap_and_summarise_fast(self):
        t = time.time()
        ft.bootstrap_ci([i / 60 for i in range(60)])
        res = {j: [{"v": "new", "diff": 0.5, "orders": ("B", "A")}] * 2000 for j in ("a", "b", "c")}
        ft.summarise([("practice" if i % 2 else "replay", i // 2) for i in range(2000)], res, ["a", "b", "c"])
        self.assertLess(time.time() - t, 3.0)


class TestFunctional(unittest.TestCase):
    def test_eval_only_scores_and_may_deploy_the_given_adapter(self):
        with patch.object(ft, "run", return_value=0) as run:
            self.assertEqual(ft.main(["--eval-only", "--adapter", "/v/nova-grown/20261018/adapters"]), 0)
        run.assert_called_once_with(["eval", "deploy"], "20261018-rescore", "/v/nova-grown/20261018/adapters")

    def test_stages_with_adapter_never_deploy(self):
        with self.assertRaises(SystemExit):
            ft.main(["--stages", "eval,deploy", "--adapter", "/x/adapters"])

    def test_passing_eval_deploys_and_checks_the_model_answers(self):
        oc = Cur([("SELECT passed", (True,))])
        with patch.object(ft, "ops", return_value=oc), patch.object(ft, "deploy", return_value="nova-grown:t") as dep, \
                patch.object(ft, "evaluate", return_value=_eval_result(0.8, 60, 0.45, 34, (0.0, -0.3, 0.2))), \
                patch.object(ft, "chat", return_value='{"reply": "Hi, Little Mister."}'), \
                patch.object(ft, "ROOT", Path("/tmp/nova-ft-test")):
            self.assertEqual(ft.run(["eval", "deploy"], "t", "/x/adapters"), 0)
        dep.assert_called_once()

    def test_rebuild_eval_set_versions_and_stores_calibration(self):
        oc = Cur([("coalesce(max(version), 0) + 1", (3,)),
                  ("split IN ('test','valid')", [(i, f"msg {i}", f"reply {i}") for i in range(80)]),
                  ("split='train' ORDER BY md5", [(500 + i, f"m{i}", f"r{i}") for i in range(30)])])
        with patch.object(ft, "chat", return_value='{"reply": "Thank you for reaching out. I can help with that."}'), \
                patch.object(ft, "top_up_replay", return_value=0), patch.object(ft, "ROOT", Path("/tmp/nova-ft-none")):
            out = ft.rebuild_eval_set(oc, target=60)
        self.assertEqual(out, {"version": 3, "practice": 60, "replay": 60, "calibration": ft.CALIB_N})
        cal = [c for c in oc.writes("INSERT INTO finetune_eval_set") if "calibration" in c[0]]
        self.assertTrue(all(c[1][0] == 3 and c[1][2] for c in cal))


class TestRetry(unittest.TestCase):
    """Judges and the teacher fail open: '' -> no vote, never a crash; a silent judge fails calibration."""

    def test_teacher_failure_returns_empty(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(ft.chat("x"), "")

    def test_claude_judge_failure_returns_empty(self):
        cc = MagicMock(resolve_claude_bin=MagicMock(side_effect=FileNotFoundError("no claude")))
        with patch.dict(sys.modules, {"nova_claude_code": cc}):
            self.assertEqual(ft.claude_call("x"), "")

    def test_silent_judge_counts_against_calibration(self):
        with patch.object(ft, "judge_call", return_value=""):
            acc = ft.calibrate([("msg", "REAL reply", "BLAND reply")] * 3, "persona", ("j1",))
        self.assertEqual(acc, {"j1": 0.0})
        self.assertIsNone(ft.parse_judgement("", "replay"))



class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        import subprocess
        r = subprocess.run([sys.executable, ft.__file__, "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--eval-only", r.stdout)
        self.assertIn("--rebuild-eval-set", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)

    def test_docstring_describes_the_ci_gate(self):
        for w in ("Wilson", "bootstrap", "CALIB_MIN", "VOID", "--rebuild-eval-set"):
            self.assertIn(w, ft.__doc__)


if __name__ == "__main__":
    unittest.main()
