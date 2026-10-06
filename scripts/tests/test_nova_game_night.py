#!/usr/bin/env python3
"""Tests for nova_game_night.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_game_night.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="game-night-test-"))       # Path.home() for the module: state + log live here
NOVA = "nova@test.local"
HERD = [{"name": n, "email": f"{n.lower()}@test.local"} for n in ("Ada", "Bram", "Cleo", "Dev", "Esme", "Finn")]


def _stub_modules():
    cfg = types.ModuleType("nova_config")
    cfg.NOVA_EMAIL = NOVA
    cfg.slack_bot_token = MagicMock(return_value="xoxb-test")
    cfg.post_both = MagicMock()
    herd = types.ModuleType("herd_config")
    herd.HERD = [dict(m) for m in HERD]
    return {"nova_config": cfg, "herd_config": herd}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    root = logging.getLogger()
    before = list(root.handlers)
    with patch.dict(sys.modules, _stub_modules()), patch.object(Path, "home", staticmethod(lambda: TMP)):
        spec.loader.exec_module(mod)
    for h in root.handlers:                       # basicConfig() may have attached a file+stdout handler: drop them
        if h not in before:
            root.removeHandler(h); h.close()
    return mod


gn = _load("game_night_under_test", SCRIPT)
gn.log.addHandler(logging.NullHandler()); gn.log.propagate = False
assert gn.STATE_FILE == TMP / ".openclaw" / "workspace" / "game_night_state.json"
assert gn.LOG_FILE == TMP / ".openclaw" / "logs" / "nova_game_night.log"


class _Mail:
    """nova_herd_mail.sh stand-in: records sends, serves an inbox and message bodies."""
    def __init__(self, inbox=None, bodies=None, send_rc=0, exc=None):
        self.inbox, self.bodies, self.send_rc, self.exc = list(inbox or []), dict(bodies or {}), send_rc, exc
        self.sent, self.calls = [], []

    def run(self, argv, **kw):
        self.calls.append(argv)
        if self.exc:
            raise self.exc
        verb = argv[1]
        if verb == "send":
            self.sent.append({"to": argv[3], "subject": argv[5], "body": argv[7]})
            return types.SimpleNamespace(returncode=self.send_rc, stdout="", stderr="smtp 550" if self.send_rc else "")
        if verb == "list":
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"messages": self.inbox}), stderr="")
        if verb == "read":
            return types.SimpleNamespace(returncode=0, stdout=json.dumps({"body": self.bodies.get(argv[2], "")}), stderr="")
        raise AssertionError(argv)


class _Net:
    """urllib stand-in: Ollama answers from `ollama` (callable or str); Slack posts are recorded."""
    def __init__(self, ollama="generated text", slack_ok=True, exc=None):
        self.ollama, self.slack_ok, self.exc = ollama, slack_ok, exc
        self.slack, self.prompts = [], []

    def urlopen(self, req, timeout=None):
        if self.exc:
            raise self.exc
        url, payload = req.full_url, json.loads(req.data)
        if "11434" in url:
            self.prompts.append(payload["prompt"])
            text = self.ollama(payload["prompt"]) if callable(self.ollama) else self.ollama
            body = {"response": text}
        else:
            self.slack.append((req.headers.get("Authorization"), payload))
            body = {"ok": self.slack_ok, "error": None if self.slack_ok else "channel_not_found"}
        return _Resp(body)


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


# Module-level stubs: every outbound path (herd mail, Ollama, Slack) is a swapped module attribute of
# the loaded copy — the shared subprocess / urllib modules are never touched.
gn.subprocess = types.SimpleNamespace(run=_Mail(exc=AssertionError("unmocked herd mail")).run)
gn.urllib = types.SimpleNamespace(request=types.SimpleNamespace(
    Request=gn.urllib.request.Request, urlopen=MagicMock(side_effect=AssertionError("unmocked urlopen"))))


def _wire(mail=None, net=None):
    mail, net = mail or _Mail(), net or _Net()
    return (patch.object(gn.subprocess, "run", mail.run), patch.object(gn.urllib.request, "urlopen", net.urlopen),
            mail, net)


def _run(fn, *a, mail=None, net=None, **kw):
    """Call fn with mail + network wired; returns (result, mail, net, stdout)."""
    p1, p2, mail, net = _wire(mail, net)
    buf = io.StringIO()
    with p1, p2, redirect_stdout(buf):
        res = fn(*a, **kw)
    return res, mail, net, buf.getvalue()


def _iso(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()


QA = "\n".join(f"Q: Question {i}?\nA: Answer {i}" for i in range(1, 6))


def _msg(frm, subject, body, uid="1"):
    return {"uid": uid, "from": frm, "subject": subject, "body": body}


class _Base(unittest.TestCase):
    def setUp(self):
        gn.clear_state()

    def tearDown(self):
        gn.clear_state()


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("xoxb-", SRC)
        self.assertIn("nova_config.slack_bot_token()", SRC)        # Slack token from Keychain via nova_config

    def test_mail_goes_through_argv_lists_never_a_shell(self):
        self.assertNotIn("shell=True", SRC)
        evil = 'Re: trivia"; rm -rf / #'
        _, mail, _, _ = _run(gn.send_email, "ada@test.local", evil, "body $(id)")
        argv = mail.calls[0]
        self.assertEqual(argv[:2], [gn.HERD_MAIL, "send"])
        self.assertIn(evil, argv)                                   # one argv element, intact
        self.assertIn("body $(id)", argv)

    def test_slack_post_uses_bearer_token_and_fixed_channel(self):
        _, _, net, _ = _run(gn.slack_post, "hello <script>")
        auth, payload = net.slack[0]
        self.assertEqual(auth, "Bearer xoxb-test")
        self.assertEqual(payload["channel"], gn.SLACK_CHAN)
        self.assertEqual(payload["text"], "hello <script>")

    def test_missing_token_skips_the_post(self):
        with patch.object(gn.nova_config, "slack_bot_token", MagicMock(return_value="")):
            _, _, net, _ = _run(gn.slack_post, "x")
        self.assertEqual(net.slack, [])

    def test_state_and_log_live_under_openclaw_not_cwd(self):
        self.assertEqual(gn.STATE_FILE.parent, gn.WORKSPACE_DIR)
        self.assertTrue(str(gn.LOG_FILE).startswith(str(TMP)))
        self.assertNotEqual(TMP, Path.home())                          # the real ~/.openclaw/logs is never opened


class TestPerformance(unittest.TestCase):
    def test_inbox_filters_fast_on_10k_messages(self):
        gid = "trivia-202610050900"
        subjects = [f"Re: [GAME:{gid}] Game Night" if i % 3 == 0 else f"newsletter {i}" for i in range(10_000)]
        body = "1. Voyager\n2. Hydrogen\n\nOn Mon, Nova wrote:\n> original\n> quoted"
        t0 = time.perf_counter()
        hits = sum(gn.matches_game_subject(s, "trivia", gid) for s in subjects)
        for _ in range(10_000):
            gn.extract_reply_content(body); gn.extract_game_tag(body, gid)
        self.assertLess(time.perf_counter() - t0, 1.5)
        self.assertEqual(hits, 3334)

    def test_question_parse_bounded_to_five_on_a_huge_reply(self):
        big = "\n".join(f"Q: q{i}\nA: a{i}" for i in range(5_000))
        t0 = time.perf_counter()
        _, _, net, _ = _run(gn.trivia_generate_questions, "space", net=_Net(ollama=big))
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(_Base):
    def test_ollama_fails_open_to_placeholder(self):
        # RETRY GAP: ollama_generate — one attempt; any error returns the placeholder string so the game continues
        uo = MagicMock(side_effect=OSError("ollama down"))
        with patch.object(gn.urllib.request, "urlopen", uo):
            out = gn.ollama_generate("p")
        self.assertEqual(uo.call_count, 1)
        self.assertIn("temporarily offline", out)
        qs = None
        with patch.object(gn.urllib.request, "urlopen", uo):
            qs = gn.trivia_generate_questions("space")
        self.assertEqual([q["answer"] for q in qs], ["Voyager 1", "Hydrogen", "1971", "Neuromancer", "Jupiter"])

    def test_send_email_fails_open_false_on_rc_and_exception(self):
        # RETRY GAP: send_email — one herd-mail attempt; rc!=0 or an exception yields False, never a raise
        _, mail, _, _ = _run(gn.send_email, "a@test.local", "s", "b", mail=_Mail(send_rc=1))
        self.assertEqual(len(mail.calls), 1)
        ok, mail, _, _ = _run(gn.send_email, "a@test.local", "s", "b", mail=_Mail(exc=subprocess.TimeoutExpired("m", 60)))
        self.assertFalse(ok)
        self.assertEqual(len(mail.calls), 1)
        # and email_all keeps going past a bad address
        _, mail, _, _ = _run(gn.email_all, "s", "b", mail=_Mail(send_rc=1))
        self.assertEqual(len(mail.sent), 7)

    def test_inbox_and_body_reads_fail_open(self):
        # RETRY GAP: fetch_recent_inbox / read_message_body — single attempt, [] / "" on any failure
        res, mail, _, _ = _run(gn.fetch_recent_inbox, mail=_Mail(exc=OSError("imap")))
        self.assertEqual(res, [])
        self.assertEqual(len(mail.calls), 1)
        bad = _Mail(); bad.run = lambda argv, **kw: types.SimpleNamespace(returncode=0, stdout="not json", stderr="")
        self.assertEqual(_run(gn.fetch_recent_inbox, mail=bad)[0], [])
        self.assertEqual(_run(gn.read_message_body, "9", mail=bad)[0], "")

    def test_slack_failures_are_swallowed(self):
        # RETRY GAP: slack_post — one attempt; transport errors and ok=false are logged, never raised
        _, _, net, _ = _run(gn.slack_post, "x", net=_Net(exc=OSError("slack 502")))
        _, _, net, _ = _run(gn.slack_post, "x", net=_Net(slack_ok=False))
        self.assertEqual(len(net.slack), 1)


class TestUnit(_Base):
    def test_players_and_lookup(self):
        players = gn.all_players()
        self.assertEqual(len(players), 7)
        self.assertEqual(players[-1], {"name": "Nova", "email": NOVA})
        self.assertEqual(gn.player_name("ADA@test.local"), "Ada")
        self.assertEqual(gn.player_name("nobody@x"), "nobody@x")
        self.assertIsNone(gn.player_by_email(""))
        self.assertIn(NOVA, gn.herd_emails())

    def test_state_roundtrip_and_corruption(self):
        self.assertEqual(gn.load_state(), {})
        gn.save_state({"game_type": "trivia", "n": 1})
        self.assertEqual(gn.load_state()["n"], 1)
        gn.STATE_FILE.write_text("{not json")
        self.assertEqual(gn.load_state(), {})
        gn.clear_state(); gn.clear_state()                            # idempotent
        self.assertFalse(gn.STATE_FILE.exists())

    def test_deadlines(self):
        d = datetime.fromisoformat(gn.deadline_str(48))
        self.assertAlmostEqual((d - datetime.now(timezone.utc)).total_seconds(), 48 * 3600, delta=5)
        self.assertTrue(gn.is_past_deadline(_iso(-1)))
        self.assertFalse(gn.is_past_deadline(_iso(1)))
        self.assertTrue(gn.is_past_deadline("2000-01-01T00:00:00"))  # naive → UTC
        self.assertFalse(gn.is_past_deadline("garbage"))
        self.assertFalse(gn.is_past_deadline(""))

    def test_reply_extraction_and_subject_matching(self):
        body = "1. Voyager\n2. Hydrogen\n\nOn Mon, Nova wrote:\n> q\n> q"
        self.assertEqual(gn.extract_reply_content(body), "1. Voyager\n2. Hydrogen")
        self.assertEqual(gn.extract_reply_content("> all quoted"), "")
        self.assertEqual(gn.extract_reply_content(""), "")
        self.assertTrue(gn.extract_game_tag("x [GAME:trivia-1] y", "trivia-1"))
        self.assertTrue(gn.matches_game_subject("Re: [game:TRIVIA-1] hi", "trivia", "TRIVIA-1"))
        self.assertTrue(gn.matches_game_subject("Night phase thoughts", "werewolf", "w-1"))
        self.assertFalse(gn.matches_game_subject("Night phase thoughts", "trivia", "t-1"))
        self.assertFalse(gn.matches_game_subject("anything", "bogus", "x"))

    def test_question_parsing_and_fallback_padding(self):
        raw = "Q: 1) One?\nA: uno\nQ: Two?\nnot an answer\nQ: Three?\nA: tres\n"
        qs, _, _, _ = _run(gn.trivia_generate_questions, "t", net=_Net(ollama=raw))
        self.assertEqual(len(qs), 5)
        self.assertEqual(qs[0], {"question": "One?", "answer": "uno", "points": 1})
        self.assertEqual(qs[1]["answer"], "tres")                     # Q without A is dropped
        self.assertEqual(qs[2]["answer"], "1971")                     # padded from fallback by slot index
        self.assertEqual(qs[4]["answer"], "Jupiter")

    def test_score_parsing(self):
        raw = "Q1: 1 — right\nQ2: 0 - nope\nQ3: 1 – close enough\njunk line\n"
        (score, fb), _, _, _ = _run(gn.trivia_score_response, [{"question": "q", "answer": "a"}] * 5, "ans", net=_Net(ollama=raw))
        self.assertEqual(score, 2)
        self.assertEqual(fb, ["✓ right", "✗ nope", "✓ close enough", "(not scored)", "(not scored)"])

    def test_werewolf_winner(self):
        a = {"w1": "werewolf", "w2": "werewolf", "v1": "villager", "v2": "villager", "s": "seer"}
        self.assertIsNone(gn._werewolf_check_winner({"alive": list(a), "assignments": a}))
        self.assertEqual(gn._werewolf_check_winner({"alive": ["v1", "s"], "assignments": a}), "village")
        self.assertEqual(gn._werewolf_check_winner({"alive": ["w1", "v1"], "assignments": a}), "werewolves")
        self.assertEqual(gn._werewolf_check_winner({"alive": [], "assignments": a}), "village")


class TestIntegration(_Base):
    def test_question_generation_chains_ollama_prompt_to_five_dicts(self):
        qs, _, net, _ = _run(gn.trivia_generate_questions, "deep sea", net=_Net(ollama=QA))
        self.assertIn("deep sea", net.prompts[0])
        self.assertEqual([q["question"] for q in qs], [f"Question {i}?" for i in range(1, 6)])

    def test_cmd_advance_dispatches_on_saved_game_type(self):
        for gt in ("trivia", "werewolf", "relay", "debate"):
            gn.save_state({"game_type": gt, "game_id": "x"})
            with patch.object(gn, f"{gt}_advance", MagicMock()) as adv, redirect_stdout(io.StringIO()):
                gn.cmd_advance()
            adv.assert_called_once_with({"game_type": gt, "game_id": "x"})
        gn.save_state({"game_type": "chess"})
        with redirect_stdout(io.StringIO()) as buf:
            gn.cmd_advance()
        self.assertIn("Unknown game type: chess", buf.getvalue())
        gn.clear_state()
        with redirect_stdout(io.StringIO()) as buf:
            gn.cmd_advance(); gn.cmd_status()
        self.assertEqual(buf.getvalue().count("No game in progress."), 2)

    def test_day_resolution_feeds_night_emails_and_state(self):
        a = {m["email"]: r for m, r in zip(HERD, ["werewolf", "werewolf", "seer", "doctor", "villager", "villager"])}
        a[NOVA] = "villager"
        state = {"game_type": "werewolf", "game_id": "werewolf-1", "phase": "day", "day_number": 1, "deadline": _iso(1),
                 "assignments": a, "alive": list(a), "eliminated": [], "votes": {e: "esme@test.local" for e in list(a)[:4]},
                 "night_actions": {}, "kill_target": None, "protect_target": None, "seer_results": {}}
        _, mail, net, out = _run(gn._werewolf_resolve_day, state)
        saved = gn.load_state()
        self.assertEqual(saved["phase"], "night")
        self.assertNotIn("esme@test.local", saved["alive"])
        self.assertEqual(saved["eliminated"][0]["role"], "villager")
        self.assertEqual({m["to"] for m in mail.sent}, set(saved["alive"]))
        self.assertIn("Night 1: The Village Sleeps", mail.sent[0]["subject"])
        self.assertIn("KILL:", mail.sent[0]["body"])
        self.assertIn("Esme was eliminated (was villager)", net.slack[0][1]["text"])

    def test_night_resolution_doctor_save_and_seer_vision(self):
        a = {m["email"]: r for m, r in zip(HERD, ["werewolf", "werewolf", "seer", "doctor", "villager", "villager"])}
        a[NOVA] = "villager"
        state = {"game_type": "werewolf", "game_id": "werewolf-1", "phase": "night", "day_number": 1, "deadline": _iso(1),
                 "assignments": a, "alive": list(a), "eliminated": [], "votes": {},
                 "night_actions": {"ada@test.local": "Esme", "cleo@test.local": "Bram", "dev@test.local": "Esme"},
                 "kill_target": None, "protect_target": None, "seer_results": {}}
        _, mail, net, out = _run(gn._werewolf_resolve_night, state)
        saved = gn.load_state()
        self.assertEqual((saved["phase"], saved["day_number"]), ("day", 2))
        self.assertEqual(len(saved["alive"]), 7)                      # doctor saved the target
        seer_mail = [m for m in mail.sent if m["to"] == "cleo@test.local" and "Seer Vision" in m["subject"]][0]
        self.assertIn("Bram is a WEREWOLF", seer_mail["body"])
        self.assertEqual(saved["seer_results"]["cleo@test.local"], {"name": "Bram", "role": "werewolf"})
        self.assertIn("was saved", net.slack[0][1]["text"])


class TestFunctional(_Base):
    def test_trivia_golden_path_start_then_finish(self):
        _, mail, net, out = _run(gn.trivia_start, "deep sea", net=_Net(ollama=QA))
        state = gn.load_state()
        self.assertEqual(state["phase"], "collecting_answers")
        self.assertEqual(len(state["questions"]), 5)
        self.assertEqual(len(mail.sent), 7)
        self.assertEqual({m["to"] for m in mail.sent}, gn.herd_emails())
        ada = [m for m in mail.sent if m["to"] == "ada@test.local"][0]
        self.assertTrue(ada["body"].startswith("Hey Ada,"))
        self.assertIn(f"[GAME:{state['game_id']}]", ada["subject"])
        self.assertIn(state["game_id"], net.slack[0][1]["text"])
        self.assertIn("Trivia game started!", out)

        gid = state["game_id"]
        inbox = [_msg(p["email"], f"Re: [GAME:{gid}] Game Night", "1. Answer 1\n2. x\n3. x\n4. x\n5. x", uid=str(i))
                 for i, p in enumerate(gn.all_players())]
        inbox.append(_msg("stranger@else", f"Re: [GAME:{gid}]", "1. a"))
        scorer = lambda prompt: "Q1: 1 — ok\nQ2: 0 — no\nQ3: 0 — no\nQ4: 0 — no\nQ5: 0 — no"
        _, mail, net, out = _run(gn.cmd_advance, mail=_Mail(inbox=inbox), net=_Net(ollama=scorer))
        self.assertFalse(gn.STATE_FILE.exists())                      # game concluded → state cleared
        board = net.slack[0][1]["text"]
        self.assertIn("Trivia Tournament Results — deep sea", board)
        self.assertEqual(board.count("— 1/5"), 7)
        self.assertIn("Answer 1", board)
        self.assertEqual(len(mail.sent), 7)
        self.assertIn("Trivia Results — deep sea", mail.sent[0]["subject"])
        self.assertIn("concluded", out)

    def test_trivia_waits_when_responses_outstanding(self):
        gn.save_state({"game_type": "trivia", "game_id": "trivia-1", "topic": "t", "phase": "collecting_answers",
                       "deadline": _iso(10), "questions": [{"question": "q", "answer": "a", "points": 1}] * 5,
                       "responses": {}, "scores": {}})
        inbox = [_msg("ada@test.local", "Re: [GAME:trivia-1] x", "1. a")]
        _, mail, net, out = _run(gn.cmd_advance, mail=_Mail(inbox=inbox), net=_Net(ollama="Q1: 1 — y"))
        self.assertIn("waiting on 6 more", out)
        self.assertEqual(gn.load_state()["scores"]["ada@test.local"]["score"], 1)
        self.assertEqual(net.slack, [])

    def test_start_refuses_when_a_game_is_running(self):
        gn.save_state({"game_type": "relay"})
        with redirect_stdout(io.StringIO()) as buf, self.assertRaises(SystemExit) as cm:
            gn.trivia_start("x")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("already in progress (relay)", buf.getvalue())

    def test_werewolf_start_assigns_seven_roles_and_opens_day_one(self):
        with patch.object(gn.random, "shuffle", lambda x: None):
            _, mail, net, out = _run(gn.werewolf_start)
        state = gn.load_state()
        self.assertEqual(sorted(state["assignments"].values()), sorted(gn.WEREWOLF_ROLES))
        self.assertEqual(len(mail.sent), 14)                          # 7 secret roles + 7 Day-1 openers
        roles = [m for m in mail.sent if "Your Secret Role" in m["subject"]]
        ada = [m for m in roles if m["to"] == "ada@test.local"][0]
        self.assertIn("YOUR ROLE: WEREWOLF", ada["body"])
        self.assertIn("fellow werewolf is: *Bram*", ada["body"])
        self.assertEqual((state["phase"], state["day_number"], len(state["alive"])), ("day", 1, 7))
        self.assertIn("AI Werewolf has begun", net.slack[0][1]["text"])

    def test_relay_timeout_inserts_placeholder_and_moves_on(self):
        order = [{"name": m["name"], "email": m["email"]} for m in HERD]
        gn.save_state({"game_type": "relay", "game_id": "relay-1", "phase": "collecting", "seed": "s",
                       "segments": [{"name": "Nova", "email": NOVA, "text": "opening"}], "order": order,
                       "current_turn": 0, "deadline": _iso(-1)})
        _, mail, net, out = _run(gn.cmd_advance)
        state = gn.load_state()
        self.assertEqual(state["current_turn"], 1)
        self.assertIn("Ada was silent here", state["segments"][1]["text"])
        self.assertEqual(mail.sent[0]["to"], "bram@test.local")
        self.assertIn("Your Turn, Bram!", mail.sent[0]["subject"])

    def test_debate_deadline_with_no_arguments_clears_quietly(self):
        gn.save_state({"game_type": "debate", "game_id": "debate-1", "topic": "t", "phase": "collecting_arguments",
                       "deadline": _iso(-1), "assignments": {NOVA: "PRO"}, "arguments": {}})
        _, mail, net, _ = _run(gn.cmd_advance)
        self.assertFalse(gn.STATE_FILE.exists())
        self.assertEqual((mail.sent, net.slack), ([], []))

    def test_end_and_status(self):
        gn.save_state({"game_type": "debate", "game_id": "debate-9", "phase": "collecting_arguments", "topic": "t",
                       "arguments": {NOVA: "x"}, "deadline": "d", "started_at": "s"})
        with redirect_stdout(io.StringIO()) as buf:
            gn.cmd_status()
        self.assertIn("Arguments: 1/7", buf.getvalue())
        self.assertIn("Waiting on: Ada, Bram", buf.getvalue())
        _, _, net, out = _run(gn.cmd_end)
        self.assertFalse(gn.STATE_FILE.exists())
        self.assertIn("Game Night canceled. (debate / debate-9)", net.slack[0][1]["text"])
        self.assertIn("Game ended: debate", out)

    def test_main_parses_and_dispatches(self):
        with patch.object(sys, "argv", ["nova_game_night.py", "start", "--game", "debate", "--topic", "T"]), \
             patch.object(gn, "debate_start", MagicMock()) as ds:
            gn.main()
        ds.assert_called_once_with(topic="T")
        with patch.object(sys, "argv", ["nova_game_night.py", "start", "--game", "chess"]), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                gn.main()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_without_touching_real_home(self):
        home = TMP / "frame-home"
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(home)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("{start,status,advance,end}", r.stdout)
        self.assertTrue((home / ".openclaw" / "logs").is_dir())      # the log dir was created under the temp HOME

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_game_night"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP / "frame-home")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
