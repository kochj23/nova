#!/usr/bin/env python3
"""Tests for the seven six-month builds (2026-09-28): house facts, output drift, pursuit
threads, answerable questions, fleet executor, live docs, letting go with teeth.
One per house category where it applies: functional, security, privacy, performance,
regression, integration, docs. Written by Jordan Koch (via Claude)."""
import importlib.util
import re
import time
import unittest
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


def _src(name):
    return (SCRIPTS / name).read_text()


class TestFunctional(unittest.TestCase):
    def test_every_build_selftests(self):
        for m in ("nova_house_facts", "nova_output_drift", "nova_ask_one", "nova_slack_answers",
                  "nova_fleet_exec", "nova_live_docs"):
            _load(m).demo()

    def test_thread_next_step_parsing(self):
        u = _load("nova_unclaimed_time")
        body, nxt = u._split_next("Thought.\nNEXT: listen to the Burbank yard channel at 2am")
        self.assertEqual((body, nxt), ("Thought.", "listen to the Burbank yard channel at 2am"))
        self.assertEqual(u._split_next("Thought.\nNEXT: nothing")[1], None)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        for f in ("nova_house_facts.py", "nova_output_drift.py", "nova_ask_one.py", "nova_slack_answers.py",
                  "nova_fleet_exec.py", "nova_live_docs.py", "nova_letting_go.py"):
            self.assertIsNone(pat.search(_src(f)), f)

    def test_executor_refuses_malformed_service_and_pins_key(self):
        fe = _load("nova_fleet_exec")
        ok, why = fe.restart_service("mac-studio", "x; rm -rf /")
        self.assertFalse(ok); self.assertIn("malformed", why)
        argv = fe.plan("mac-studio", "nova-battery-monitor", local=False)
        self.assertIn("-i", argv); self.assertEqual(argv[-1], "restart nova-battery-monitor")  # forced-command verb only

    def test_restart_gate_denies_load_bearing(self):
        gate = (Path.home() / "bin" / "nova-restart-gate.sh").read_text()
        for svc in ("nova-gateway-v2", "nova-scheduler", "big-brother", "nova-notifier"):
            self.assertIn(svc, gate)
        self.assertIn("no-pty", (Path.home() / ".ssh" / "authorized_keys").read_text())


class TestPrivacy(unittest.TestCase):
    def test_question_picker_skips_self_directed_and_reads_no_bodies(self):
        a = _load("nova_ask_one")
        rows = [(1, "I predicted with 66% ... What did I misjudge?", "prediction_surprise", None)]
        self.assertIsNone(a.pick(rows))
        self.assertNotIn("email", _src("nova_ask_one.py").lower().split("skip_sources")[0][-400:])

    def test_blanket_and_thread_approvals_parse(self):
        sa = _load("nova_slack_answers")
        self.assertEqual(sa.verdict("All approved"), "yes"); self.assertIsNotNone(sa.BLANKET_RE.search("all good"))
        self.assertIsNone(sa.verdict("All of these are wrong"))
        self.assertIn("SELECT 1 FROM slack_prompts", _src("nova_gateway/channels/slack.py"))
        self.assertIn("hand_to_claude", _src("nova_coagency.py"))

    def test_answers_ignore_bot_and_system_messages(self):
        sa = _load("nova_slack_answers")
        msgs = [{"user": "U1", "text": "root"}, {"user": sa.BOT_USER, "text": "me"},
                {"subtype": "channel_join", "user": "U2", "text": "joined"}]
        self.assertIsNone(sa.first_human_reply(msgs))


class TestPerformance(unittest.TestCase):
    def test_house_facts_scoring_fast_on_5k_entities(self):
        h = _load("nova_house_facts")
        toks = h.tokens("what firmware is the master bedroom plug running")
        ents = [f"room_{i}_plug_{i % 7}" for i in range(5000)] + ["master_bedroom_plug"]
        t0 = time.perf_counter()
        best = max(ents, key=lambda e: h.score(e, toks))
        self.assertLess(time.perf_counter() - t0, 0.5); self.assertEqual(best, "master_bedroom_plug")

    def test_drift_classify_linear(self):
        d = _load("nova_output_drift")
        tails = ["**Nothing**"] * 10_000
        t0 = time.perf_counter(); d.classify(tails); self.assertLess(time.perf_counter() - t0, 0.2)


class TestRegression(unittest.TestCase):
    def test_drift_catches_the_three_historic_shapes(self):
        d = _load("nova_output_drift")
        self.assertEqual(d.classify(["0.0% success rate, 0 runs, 0 failures, 0 healthy tasks"] * 5), "unchanged")
        self.assertEqual(d.classify(["**Nothing**", "**NOTHING**", "**Nothing.**", "**Nothing**", "**NOTHING**"]), "unchanged")
        self.assertTrue(d.chronic(["down"] * 12))
        # a repeating LOG line is 'unchanged' by shape but not a non-answer: the probe's ZEROISH gate skips it
        self.assertEqual(d.classify(["[x 08:00:01] Checking state...", "[x 09:00:01] Checking state..."] * 3), "unchanged")
        self.assertIsNone(d.ZEROISH_RE.search("[x 08:00:01] Checking state..."))

    def test_predictions_drop_unresolvable_relationship_bets(self):
        s = _src("nova_predictions.py")
        self.assertIn('domain == "relationship" and not', s)
        self.assertNotIn("'Jordan asks about the printer again within", s)

    def test_gateway_loads_persona_docs_first(self):
        s = _src("nova_gateway/agent.py")
        self.assertIn("WHEN 'identity' THEN 0 WHEN 'soul' THEN 1", s)
        self.assertIn("_HOUSE_INTENT", s); self.assertIn("house_facts", _src("nova_gateway/tools.py"))


class TestIntegration(unittest.TestCase):
    def test_live_docs_render_against_placeholders_in_pg_docs(self):
        ld = _load("nova_live_docs")
        docs = ld.load_docs(("identity", "soul"))
        self.assertIn("{{memory_count}}", docs.get("identity", "") + docs.get("soul", ""))
        out = ld.render(docs["identity"], {"memory_count": "1", "script_count": "2", "node_count": "3", "as_of": "d"})
        self.assertNotIn("{{", out)

    def test_letting_go_files_goal_proposals_through_coagency(self):
        s = _src("nova_letting_go.py")
        self.assertIn("propose_goal_retirements(oc)", s); self.assertIn('file_proposal(oc, "letting_go"', s)


class TestDocs(unittest.TestCase):
    def test_each_build_names_its_number_and_modes(self):
        for f, n in (("nova_house_facts.py", "#1"), ("nova_output_drift.py", "#2"), ("nova_ask_one.py", "#4"),
                     ("nova_fleet_exec.py", "#5"), ("nova_live_docs.py", "#6")):
            self.assertIn(f"six-month build {n}", _src(f)); self.assertIn("--selftest", _src(f))


if __name__ == "__main__":
    unittest.main()
