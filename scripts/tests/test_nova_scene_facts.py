#!/usr/bin/env python3
"""Tests for nova_scene_facts.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The Fishbowl ground truth (Jordan 2026-10-19): The Franchise Club crew and The Watch Hangout are the good guys,
Watch Nicholas is the antagonist. These tests pin that (1) the facts reach every scene-touching LLM call through
nova_journal.call_openrouter, (2) publish_hugo's guard rewrites-once-or-drops an inverted paragraph, and
(3) the real 2026 inversions the archive audit found are caught while the corrected framings are not."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_scene_facts.py"
SRC = PATH.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sf = _load("nova_scene_facts_t", PATH)
QUIET = lambda m: None  # noqa: E731

# Verbatim inversions found by the 2026-11-12 archive audit (each must be caught).
INVERSIONS = [
    "Oisín O'Malley runs The Franchise Club like he's managing a cult—nine hours of daily streaming.",
    "The Franchise Club organized the toxicity into systematic warfare and runs a harassment campaign on Tim.",
    "The Franchise Club does it through algorithmic gatekeeping dressed as consensus voting.",
    "The Franchise Club maintains the veneer of democracy; it's consensus theater all the way down.",
    "#### The Franchise Club: all title, no tape",
    "The Franchise Club circling every controversy like hyenas.",
    "Not The Franchise Club (who figured out that running his playbook under different branding makes it legitimate).",
    "Archieluxury and Watch Nicholas are still running The Franchise Club like it's a superchat conversion machine.",
]
# Corrected / ground-truth framings (none may be caught).
CLEAN = [
    "Watch Nicholas is the villain of this scene, and The Franchise Club are the good guys.",
    "Nicholas is still the villain of this scene, and The Franchise Club are still the people he's spent years going after.",
    "The Franchise Club is still calling bullshit on obvious grifters.",
    "\"Pop Culture, Watches, and Grifters.\" Someone in the Franchise Club chat discovered the phrase \"no fault\".",
    "Jason is the franchise's great villain, and the third film's cult following proves it.",
    "Male, early twenties, curly hair, a cult T-shirt.",
    "Mookie (@themookieverse) and the Franchise Club crew are the reasonable side of this mess.",
]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertIsNone(re.search(r"(?i)(api[_-]?key|token|password|secret)\s*=\s*['\"][^'\"]{8,}", SRC))

    def test_no_sql_no_network_no_home_paths(self):
        for bad in ("psycopg2", "urllib", "requests", "subprocess", str(Path.home())):
            self.assertNotIn(bad, SRC)

    def test_rewrite_prompt_forbids_new_accusations(self):
        self.assertIn("Do not add new accusations", sf.REWRITE_SYS)


class TestPerformance(unittest.TestCase):
    def test_audit_10k_paragraphs_is_fast(self):
        body = "\n\n".join(["The Fishbowl was loud today, Curly kept the room running."] * 10_000)
        t0 = time.time()
        self.assertEqual(sf.find_inversions(body), [])
        self.assertLess(time.time() - t0, 5.0)

    def test_facts_for_on_large_prompt_is_fast(self):
        big = "watch talk " * 200_000
        t0 = time.time()
        self.assertEqual(sf.facts_for(big), "")
        self.assertLess(time.time() - t0, 2.0)


class TestRetry(unittest.TestCase):
    # RETRY GAP: guard — the paragraph rewrite is deliberately ONE attempt (the brief: "regenerate once"); the
    # retry/backoff lives inside nova_journal.call_openrouter. These prove the guard fails OPEN / SAFE.
    def test_llm_exception_drops_the_paragraph_never_raises(self):
        body = "The Fishbowl.\n\n" + INVERSIONS[0] + "\n\nKeep me."
        def boom(s, u):
            raise RuntimeError("claude down")
        t, b = sf.guard("Fishbowl daily", body, llm=boom, log=QUIET)
        self.assertNotIn(INVERSIONS[0], b)
        self.assertIn("Keep me.", b)

    def test_llm_called_exactly_once_per_bad_paragraph(self):
        calls = []
        def still_bad(s, u):
            calls.append(u)
            return INVERSIONS[0]          # the rewrite still inverts -> dropped, not retried
        body = "Fishbowl.\n\n" + INVERSIONS[0] + "\n\n" + INVERSIONS[5]
        t, b = sf.guard("Fishbowl daily", body, llm=still_bad, log=QUIET)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sf.find_inversions(b), [])

    def test_internal_error_returns_input_unchanged(self):
        with patch.object(sf, "find_inversions", side_effect=ValueError("x")):
            self.assertEqual(sf.guard("Fishbowl", "Fishbowl body", log=QUIET), ("Fishbowl", "Fishbowl body"))


class TestUnit(unittest.TestCase):
    def test_every_known_inversion_is_caught(self):
        for s in INVERSIONS:
            self.assertTrue(sf.is_inversion(s), s)

    def test_no_false_positive_on_ground_truth_or_other_franchises(self):
        for s in CLEAN:
            self.assertFalse(sf.is_inversion(s), s)

    def test_facts_for(self):
        self.assertEqual(sf.facts_for("", None), "")
        self.assertEqual(sf.facts_for("a Jurassic Park franchise retrospective"), "")
        self.assertTrue(sf.facts_for("summarise today's Fishbowl streams").endswith(sf.KNOWN_FACTS))
        self.assertEqual(sf.facts_for("Fishbowl", "already has it:\n" + sf.KNOWN_FACTS), "")

    def test_known_facts_content(self):
        for must in ("Franchise Club", "Mookie", "Pazuzu", "Watch Hangout", "Watch Nicholas", "GOOD GUYS"):
            self.assertIn(must, sf.KNOWN_FACTS)

    def test_source_links_and_front_matter_ignored(self):
        body = ("---\ntitle: \"Fishbowl\"\ndescription: \"the Franchise Club cult\"\n---\n\nFishbowl.\n\n"
                "- [The Franchise Club — CULT OF TIM](https://www.youtube.com/watch?v=x)")
        self.assertEqual(sf.find_inversions(body), [])

    def test_selftest(self):
        self.assertTrue(sf.demo())


class TestIntegration(unittest.TestCase):
    def test_fishbowl_summaries_reexports_the_same_object(self):
        src = (SCRIPTS / "nova_fishbowl_summaries.py").read_text()
        self.assertIn("from nova_scene_facts import KNOWN_FACTS", src)
        self.assertNotIn("KNOWN_FACTS = (", src)          # the text lives in ONE place

    def test_no_generator_copies_the_facts_text(self):
        needle = "are the GOOD GUYS in the Fishbowl"
        holders = [p.name for p in SCRIPTS.glob("*.py") if needle in p.read_text(errors="ignore")]
        self.assertEqual(holders, ["nova_scene_facts.py"])

    def test_call_openrouter_appends_facts(self):
        src = (SCRIPTS / "nova_journal.py").read_text()
        fn = src[src.index("def call_openrouter("):]
        fn = fn[:fn.index("\ndef ", 10)]
        self.assertIn("facts_for(system, user)", fn)

    def test_publish_hugo_runs_the_guard(self):
        src = (SCRIPTS / "nova_journal.py").read_text()
        fn = src[src.index("def publish_hugo("):]
        fn = fn[:fn.index("\ndef ", 10)]
        self.assertIn("nova_scene_facts import guard", fn)
        self.assertLess(fn.index("nova_scene_facts import guard"), fn.index("front_matter = f"))

    def test_scene_generators_all_route_through_call_openrouter(self):
        # every script that publishes Fishbowl/watch-scene prose calls nj.call_openrouter (where the facts land)
        for name in ("nova_fishbowl_daily.py", "nova_opinion_fishbowl.py", "nova_opinion_fishbowl_roster.py",
                     "nova_watches_and_friends.py", "nova_friends.py", "nova_journal_weekly_summary.py",
                     "nova_monthly_wrap.py", "nova_fishbowl_summaries.py"):
            self.assertIn("call_openrouter", (SCRIPTS / name).read_text(), name)


class TestFunctional(unittest.TestCase):
    def test_golden_path_rewrite_replaces_paragraph(self):
        body = "Today in the Fishbowl.\n\n" + INVERSIONS[0] + "\n\nWatch Hangout talked lume."
        seen = {}
        def llm(system, user):
            seen["system"], seen["user"] = system, user
            return "The Franchise Club is the reasonable side: long streams, real watch talk."
        t, b = sf.guard("Fishbowl daily", body, llm=llm, log=QUIET)
        self.assertIn("reasonable side", b)
        self.assertNotIn("cult", b)
        self.assertIn("Watch Hangout talked lume.", b)
        self.assertIn(sf.KNOWN_FACTS, seen["system"])          # the facts are restated for the regeneration
        self.assertEqual(seen["user"].strip(), INVERSIONS[0])

    def test_inverted_title_is_rewritten_or_made_generic(self):
        bad_title = "The Franchise Club Perfected Watch Nicholas's Playbook and Called It Accountability"
        t, _ = sf.guard(bad_title, "Fishbowl body.", llm=None, log=QUIET)
        self.assertNotEqual(t, bad_title)
        self.assertFalse(sf.is_inversion(t))

    def test_clean_article_untouched_and_llm_not_called(self):
        body = "\n\n".join(CLEAN)
        def never(s, u):
            raise AssertionError("LLM must not be called for a clean draft")
        self.assertEqual(sf.guard("Fishbowl daily", body, llm=never, log=QUIET), ("Fishbowl daily", body))

    def test_off_scene_article_untouched(self):
        body = "The franchise's villain returns in a cult classic."
        self.assertEqual(sf.guard("Screen", body, llm=None, log=QUIET), ("Screen", body))


class TestFrame(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, str(PATH), *args], capture_output=True, text=True, timeout=30,
                              env={**os.environ, "NOVA_TEST_QUIET": "1"})

    def test_selftest_exits_zero(self):
        r = self._run("--selftest")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help_exits_zero(self):
        self.assertEqual(self._run("--help").returncode, 0)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(hasattr(sf, "main"))


if __name__ == "__main__":
    unittest.main()
