#!/usr/bin/env python3
"""Tests for nova_gtnw_host.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

No mail is ever sent: nova_herd_mail.sh (subprocess.run), the GTNW API/Ollama (urlopen) and
notify are mocked file-wide; the herd roster is a fake and the state file lives in a tempdir."""
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
PATH = SCRIPTS / "nova_gtnw_host.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("gtnw_host_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gh = _load()
_TD = tempfile.TemporaryDirectory()
_REAL_RUN = subprocess.run
HERD = [{"name": "Alpha", "email": "alpha@example.org"}, {"name": "Bravo", "email": "bravo@example.org"}]
_PATCHES = []


def setUpModule():
    d = Path(_TD.name)
    for p in (patch.object(gh, "WORKSPACE", d), patch.object(gh, "STATE_FILE", d / "gtnw.json"),
              patch.object(gh, "HERD", HERD),
              patch.object(gh.subprocess, "run", side_effect=AssertionError("unmocked herd mail")),
              patch.object(gh.urllib.request, "urlopen", side_effect=OSError("offline")),
              patch.object(gh, "notify", side_effect=AssertionError("unmocked notify"))):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


def _cp(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


CRISIS = {"id": "c1", "title": "Missiles in Cuba", "severity": "GRAVE", "description": "d",
          "options": [{"index": i, "title": t, "description": "x", "successChance": 0.5,
                       "consequences": {"defconChange": dc, "message": f"m{i}"}}
                      for i, (t, dc) in enumerate([("Negotiate", 0), ("Escalate", -1), ("Stand Down", 1)])]}


def _mail_stub(inbox=(), bodies=None):
    sent = []

    def run(args, **kw):
        if args[1] == "send":
            sent.append(dict(zip(args[2::2], args[3::2])))
            return _cp()
        if args[1] == "list":
            return _cp(out=json.dumps({"messages": list(inbox)}))
        if args[1] == "read":
            return _cp(out=json.dumps({"body": (bodies or {}).get(args[3], "")}))
        return _cp(1)
    return run, sent


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_addresses(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r"[\w.+-]+@[\w-]+\.(com|net|org)", SRC))

    def test_mail_args_are_argv_not_shell(self):
        self.assertNotIn("shell=True", SRC)
        run, sent = _mail_stub()
        with patch.object(gh.subprocess, "run", side_effect=run):
            self.assertTrue(gh.send_mail("a@example.org", "subj; echo pwned", "$(id)"))
        self.assertEqual(sent[0]["--subject"], "subj; echo pwned")
        self.assertEqual(sent[0]["--body"], "$(id)")

    def test_reply_without_sender_never_matches_a_player(self):
        # regression for the empty-From bug ("" is a substring of every address)
        gh.save_state({"turn": 1, "assignments": {}, "decision_log": [], "history": [], "defcon": 5,
                       "pending_decisions": {"c1::USA": {"player": "Alpha", "email": "alpha@example.org",
                                                         "country_id": "USA", "crisis_id": "c1", "crisis": CRISIS}}})
        run, sent = _mail_stub(inbox=[{"id": "m1", "from": "", "snippet": "1"}], bodies={"m1": "1"})
        with patch.object(gh.subprocess, "run", side_effect=run), patch.object(gh, "notify") as n, \
                redirect_stdout(io.StringIO()):
            gh.cmd_check()
        n.assert_not_called()
        self.assertEqual(sent, [])
        self.assertIn("c1::USA", gh.load_state()["pending_decisions"])


class TestPerformance(unittest.TestCase):
    def test_compose_1k_briefings_fast(self):
        t0 = time.perf_counter()
        for i in range(1000):
            gh.compose_crisis_email(CRISIS, "Alpha", "USA", "JFK", 1962, i)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(gh._president_for_year(1962), "John F. Kennedy")


class TestRetry(unittest.TestCase):
    def test_ollama_down_falls_back_to_canned_crisis(self):
        # RETRY GAP: generate_crisis_with_ollama() — one attempt; failure returns a hardcoded 4-option crisis
        with redirect_stdout(io.StringIO()):
            c = gh.generate_crisis_with_ollama(1962, "Cuba", "JFK", ["USA"])
        self.assertEqual(len(c["options"]), 4)
        self.assertIn("1962", c["title"])

    def test_mail_and_api_failures_fail_open(self):
        with patch.object(gh.subprocess, "run", side_effect=OSError("no script")), redirect_stdout(io.StringIO()):
            self.assertFalse(gh.send_mail("a@example.org", "s", "b"))
            self.assertEqual(gh.check_inbox(), [])
            self.assertIsNone(gh.read_message("1"))
            self.assertFalse(gh.gtnw_available())
            self.assertIsNone(gh.gtnw_post("/x", {}))


class TestUnit(unittest.TestCase):
    def test_president_lookup_edges(self):
        self.assertEqual(gh._president_for_year(1945), "Harry Truman")
        self.assertEqual(gh._president_for_year(1800), "Donald Trump")
        self.assertEqual(gh._president_for_year(2021), "Joe Biden")

    def test_assignments_from_herd(self):
        a = gh.default_country_assignments()
        self.assertEqual(list(a), ["USA", "USSR"])
        self.assertEqual(a["USSR"]["email"], "bravo@example.org")

    def test_ollama_json_extracted(self):
        r = MagicMock(); r.__enter__.return_value = r
        r.read.return_value = json.dumps({"response": "<think>x</think> here: " + json.dumps(CRISIS)}).encode()
        with patch.object(gh.urllib.request, "urlopen", return_value=r):
            self.assertEqual(gh.generate_crisis_with_ollama(1962, "s", "p", [])["id"], "c1")


class TestIntegration(unittest.TestCase):
    def test_slack_post_routes_through_notify(self):
        with patch.object(gh, "notify") as n:
            gh.slack_post("Title\nbody")
        self.assertEqual((n.call_args.args[0], n.call_args.kwargs["category"]), ("Title", "gtnw"))

    def test_live_mode_feeds_decision_to_api(self):
        gh.save_state({"turn": 2, "assignments": {}, "decision_log": [], "history": [], "defcon": 5,
                       "pending_decisions": {"c1::USA": {"player": "Alpha", "email": "alpha@example.org",
                                                         "country_id": "USA", "crisis_id": "c1", "crisis": CRISIS}}})
        run, sent = _mail_stub(inbox=[{"id": "m1", "from": "Alpha <alpha@example.org>"}], bodies={"m1": "I pick 1"})
        with patch.object(gh.subprocess, "run", side_effect=run), patch.object(gh, "gtnw_available", return_value=True), \
                patch.object(gh, "gtnw_post", return_value={"consequence": "DEFCON 3", "defcon": 3}) as gp, \
                patch.object(gh, "notify") as n, redirect_stdout(io.StringIO()):
            gh.cmd_check()
        self.assertEqual(gp.call_args.args, ("/api/decision", {"crisis_id": "c1", "choice": 1, "player": "Alpha"}))
        st = gh.load_state()
        self.assertEqual((st["defcon"], st["pending_decisions"]), (3, {}))
        self.assertIn("🔴", n.call_args.args[0])
        self.assertIn("**Outcome:** DEFCON 3", sent[0]["--body"])


class TestFunctional(unittest.TestCase):
    def test_start_then_advance_simulation(self):
        run, sent = _mail_stub()
        with patch.object(gh.subprocess, "run", side_effect=run), patch.object(gh, "notify") as n, \
                patch.object(gh, "generate_crisis_with_ollama", return_value=CRISIS), redirect_stdout(io.StringIO()) as out:
            gh.cmd_start(year=1962, scenario="Cuba")
            gh.cmd_advance()
        st = gh.load_state()
        self.assertEqual((st["api_mode"], st["president"], st["turn"]), ("simulation", "John F. Kennedy", 1))
        self.assertEqual(set(st["pending_decisions"]), {"c1::USA", "c1::USSR"})
        self.assertEqual(len(sent), 4)                                  # 2 assignments + 2 briefings
        self.assertIn("TURN 1", sent[2]["--body"])
        self.assertIn("TURN 1 CRISIS", n.call_args.args[0])

    def test_reset_and_no_session_paths(self):
        gh.save_state({"x": 1})
        with redirect_stdout(io.StringIO()) as out:
            gh.cmd_reset()
            gh.cmd_status()
            gh.cmd_advance()
        self.assertFalse(gh.STATE_FILE.exists())
        self.assertEqual(out.getvalue().count("No active session"), 2)


class TestFrame(unittest.TestCase):
    def test_no_args_prints_usage_exit_zero(self):
        r = _REAL_RUN([sys.executable, str(PATH)], capture_output=True, text=True, timeout=30,
                      env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nova_gtnw_host.py start", r.stdout)

    def test_import_never_runs_main(self):
        r = _REAL_RUN([sys.executable, "-c", "import nova_gtnw_host"], cwd=str(SCRIPTS),
                      capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
