#!/usr/bin/env python3
"""Tests for nova_sleep_cycle.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Both PG cursors are scripted fakes that answer by SQL substring, llm()/remember() and urlopen are mocked,
and the Slack delivery in phase_questions goes to a stub nova_config injected with patch.dict."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_sleep_cycle.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sc = _load("nova_sleep_cycle_t", SCRIPT)


class Cur:
    """Answers fetchone/fetchall from the first rule whose needle is in the last executed SQL."""
    def __init__(self, rules=()):
        self.rules, self.sql, self._last = list(rules), [], None

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))
        self._last = next((ans for needle, ans in self.rules if needle in sql), None)

    def fetchall(self):
        return list(self._last or [])

    def fetchone(self):
        a = self._last
        if isinstance(a, list):
            return a[0] if a else None
        return a

    def writes(self, verb):
        return [(s, p) for s, p in self.sql if s.startswith(verb)]


class _Base(unittest.TestCase):
    def setUp(self):
        boom = MagicMock(side_effect=AssertionError("unmocked outbound"))
        self.remember = MagicMock(return_value="mem-1")
        for p in (patch.object(sc.urllib.request, "urlopen", boom), patch.object(sc, "remember", self.remember),
                  patch.object(sc.psycopg2, "connect", boom), patch.dict(os.environ, {"SLEEP_CYCLE_FORCE_GRAVEL": "0"})):
            p.start()
            self.addCleanup(p.stop)
        self.out = io.StringIO()
        r = redirect_stdout(self.out)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_with_values_is_parameterized(self):
        self.assertNotRegex(SRC, r'\.execute\(\s*f["\']')
        oc = Cur([("FROM beliefs", None), ("RETURNING id", (5,))])
        mc = Cur([("nova_articles", [("T'; --", "body")])])
        with patch.object(sc, "llm", return_value='[{"topic":"NAS speed","stance":"UNAS cut latency","confidence":0.8}]'):
            sc.phase_beliefs(mc, oc)
        sql, params = oc.writes("INSERT INTO beliefs")[0]
        self.assertNotIn("T'; --", sql)
        self.assertEqual(params[3], "T'; --")

    def test_credential_shaped_fragments_never_asked_about(self):
        mc = Cur([("source='curiosity'", (0,)), ("access_count = 0", [(1, "sms", "Your verification code is 482913")])])
        with patch.object(sc, "llm") as llm:
            sc.phase_questions(mc, Cur())
        llm.assert_not_called()


class TestPerformance(_Base):
    def test_tokens_and_credential_regex_10k_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            sc._tokens(f"Home Automation Topic {i}")
            sc._CREDENTIAL_SHAPE.search(f"note {i}: the pin is 1234 maybe")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(_Base):
    def _resp(self, content):
        r = MagicMock()
        r.__enter__.return_value = io.BytesIO(json.dumps({"message": {"content": content}}).encode())
        return r

    def test_llm_walks_nodes_until_one_answers(self):
        uo = MagicMock(side_effect=[OSError("down"), self._resp(""), self._resp("hello")])
        with patch.object(sc.urllib.request, "urlopen", uo):
            self.assertEqual(sc.llm("p"), "hello")
        self.assertEqual(uo.call_count, 3)
        self.assertEqual([c.args[0].full_url for c in uo.call_args_list],
                         [n + "/api/chat" for n in sc.OLLAMA_NODES[:3]])

    def test_llm_all_nodes_down_returns_empty(self):
        with patch.object(sc.urllib.request, "urlopen", side_effect=OSError("x")) as uo:
            self.assertEqual(sc.llm("p"), "")
        self.assertEqual(uo.call_count, len(sc.OLLAMA_NODES))

    def test_one_failing_phase_never_blocks_the_rest(self):
        conns = [MagicMock(), MagicMock()]
        ran = []
        from contextlib import ExitStack
        with ExitStack() as st:
            st.enter_context(patch.object(sc.psycopg2, "connect", side_effect=conns))
            st.enter_context(patch.object(sc, "phase_episode", side_effect=RuntimeError("boom")))
            for n in ("beliefs", "resonance", "questions", "citations", "preoccupations", "taste", "gravel"):
                st.enter_context(patch.object(sc, f"phase_{n}", side_effect=lambda *a, n=n: ran.append(n)))
            self.assertEqual(sc.main(), 1)
        self.assertEqual(len(ran), 7)
        self.assertIn("episode: FAILED — boom", self.out.getvalue())


class TestUnit(_Base):
    def test_episode_skips_empty_day_and_short_output(self):
        sc.phase_episode(Cur())
        self.assertIn("nothing to summarize", self.out.getvalue())
        with patch.object(sc, "llm", return_value="too short"):
            sc.phase_episode(Cur([("source='conversation'", [("hi Nova",)]), ("GROUP BY 1", [("tv", 3)])]))
        self.remember.assert_not_called()

    def test_belief_same_stance_skipped_new_stance_supersedes(self):
        ans = '[{"topic":"NAS speed","stance":"S2","confidence":0.9}]'
        oc = Cur([("FROM beliefs", (7, "S1")), ("RETURNING id", (8,))])
        with patch.object(sc, "llm", return_value=ans):
            sc.phase_beliefs(Cur([("nova_articles", [("t", "b")])]), oc)
        self.assertEqual(oc.writes("UPDATE beliefs")[0][1], (8, 7))
        oc2 = Cur([("FROM beliefs", (7, "S2"))])
        with patch.object(sc, "llm", return_value=ans):
            sc.phase_beliefs(Cur([("nova_articles", [("t", "b")])]), oc2)
        self.assertEqual(oc2.writes("INSERT"), [])

    def test_taste_requires_an_encounter(self):
        mc = Cur([("source='television'", [(11, "Severance", "The office is lit like a dentist")]),
                  ("'fishbowl'", [])])
        oc = Cur([("FROM taste", None)])
        prefs = [{"subject": "Severance", "verdict": "smug and knows it", "valence": 5, "ref": "[T0]"},
                 {"subject": "Unknown Show", "verdict": "meh", "ref": "Z9"}]
        with patch.object(sc, "llm", return_value=json.dumps(prefs)):
            sc.phase_taste(mc, oc)
        (sql, params), = oc.writes("INSERT INTO taste")
        self.assertEqual(params[3], 1.0)                                    # valence clamped
        self.assertIn("[mem 11]", params[4])
        self.assertIn("dropped 'Unknown Show'", self.out.getvalue())


class TestIntegration(_Base):
    def test_questions_write_ledger_and_post_to_slack(self):
        cfg = types.ModuleType("nova_config")
        cfg.post_both, cfg.SLACK_NOTIFY = MagicMock(), "C_N"
        mc = Cur([("created_at > now() - interval '10 minutes'", [("[Curiosity d] Who is Rex?",)]),
                  ("source='curiosity'", (0,)), ("access_count = 0", [(42, "fishbowl", "Rex barked at 3am again")])])
        oc = Cur()
        with patch.object(sc, "llm", return_value='[{"idx":0,"question":"Who is Rex?"},{"idx":9,"question":"bad"}]'), \
                patch.dict(sys.modules, {"nova_config": cfg}):
            sc.phase_questions(mc, oc)
        self.assertEqual(self.remember.call_args.args[1], "curiosity")
        self.assertEqual(oc.writes("INSERT INTO reflection_questions")[0][1][0], "42")
        self.assertIn("• Who is Rex?", cfg.post_both.call_args.args[0])
        self.assertEqual(cfg.post_both.call_args.kwargs["slack_channel"], "C_N")

    def test_citations_link_into_memory_links(self):
        oc = Cur([("article_citations", [("glass-tides", "m9")])])
        mc = Cur([("nova_articles", (101,))])
        sc.phase_citations(mc, oc)
        self.assertEqual(mc.writes("INSERT INTO memory_links")[0][1], (101, "m9"))


class TestFunctional(_Base):
    def test_gravel_marks_and_reinterprets_when_forced(self):
        mc = Cur([("IS DISTINCT FROM 'true'", [(1, "dream", "x"), (2, "fishbowl", "y")]),
                  ("metadata->>'gravel'='true'", [(9, "dream", "a strange old fragment")]),
                  ("gravel_reinterpretation", [("earlier reading",)])])
        with patch.dict(os.environ, {"SLEEP_CYCLE_FORCE_GRAVEL": "1"}), \
                patch.object(sc, "llm", return_value="Now it reads like a smoke alarm in another apartment."):
            sc.phase_gravel(mc)
        self.assertEqual(len(mc.writes("UPDATE memories")), 2)
        text, source, meta = self.remember.call_args.args
        self.assertTrue(text.startswith("[Gravel reinterpretation #2"))
        self.assertEqual((source, meta["about_memory"]), ("gravel_reinterpretation", "9"))
        self.assertEqual(mc.writes("INSERT INTO memory_links")[0][1], ("mem-1", "9"))

    def test_preoccupations_cap_and_deny(self):
        mc = Cur([("source='unclaimed'", [("[Unclaimed — radio trunking] notes",)]),
                  ("GROUP BY 1 HAVING", [("scanner", 900, 50), ("birding", 40, 9), ("vintage_cars", 30, 8),
                                          ("knots", 25, 6)]),
                  ("WHERE source=%s", [("a sample",)])])
        oc = Cur([("FROM preoccupations", [(1, "radio trunking", datetime.now() - timedelta(days=2))]),
                  ("ON CONFLICT (topic)", (77,))])
        with patch.object(sc, "llm", return_value="I keep coming back to it."):
            sc.phase_preoccupations(mc, oc)
        topics = [p[0] for _, p in oc.writes("INSERT INTO preoccupations")]
        self.assertEqual(topics, ["birding", "vintage cars"])
        self.assertEqual(len(oc.writes("UPDATE preoccupations")), 1)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # main() connects to PG and runs the LLM, so the smoke is an import in a child process
        code = ("import importlib.util as u;"
                f"s=u.spec_from_file_location('s', {str(SCRIPT)!r}); m=u.module_from_spec(s);"
                "s.loader.exec_module(m); print(m.MAX_QUESTIONS_PER_NIGHT)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "3")
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
