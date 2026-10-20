#!/usr/bin/env python3
"""7-category tests for the 2026-10-20 belief recap gate: the sleep cycle booked game-column recaps
("Little Mister's presidency lasted less than a lunch break") and article self-reports ("The author took
responsibility...") as beliefs at 0.90-0.95 because it skipped nova_beliefs.reject_reason. Now every
article opinion goes through that gate, which also rejects recaps about Jordan and self-reports.
Reuses the sleep-cycle fakes. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_belief_recap_gate_7cat.py
"""
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nova_beliefs as nb  # noqa: E402
from test_nova_sleep_cycle import Cur, SRC, sc  # noqa: E402

REAL = "Feedback loops in the climate system do not break gradually but cascade past a tipping point"
RECAP = "Little Mister's presidency lasted less than a lunch break"
SELF = "The author took responsibility for the confusion caused by the essay"


def _ans(*stances):
    return "[" + ",".join(f'{{"topic":"t{i}","stance":"{s}","confidence":0.95,"quote":"cascade past a tipping point"}}'
                          for i, s in enumerate(stances)) + "]"


class TestSecurity(unittest.TestCase):
    def test_gate_is_pure_and_never_raises_on_hostile_text(self):
        for s in ["'; DROP TABLE beliefs; --", "\x00\n" * 50, "", None, "Little Mister" * 400]:
            nb.reject_reason(s)


class TestPerformance(unittest.TestCase):
    def test_gate_10k_fast(self):
        t = time.time()
        for _ in range(10000):
            nb.reject_reason(REAL)
        self.assertLess(time.time() - t, 2.0)


class TestRetry(unittest.TestCase):
    def test_rerun_on_same_article_books_nothing_new_for_recaps(self):
        art = [(9, "🎮 Fired by Congress", "x cascade past a tipping point")]
        for _ in range(2):
            oc = Cur([("FROM beliefs", None), ("RETURNING id", (5,))])
            with patch.object(sc, "llm", return_value=_ans(RECAP)):
                sc.phase_beliefs(Cur([("nova_articles", art)]), oc)
            self.assertEqual(oc.writes("INSERT INTO beliefs"), [])


class TestUnit(unittest.TestCase):
    def test_reasons(self):
        self.assertTrue(nb.reject_reason(RECAP).startswith("recap"))
        self.assertTrue(nb.reject_reason(SELF).startswith("self-report"))
        self.assertTrue(nb.reject_reason("This essay argued we gave a billionaire a veto over a war").startswith("self-report"))
        self.assertIsNone(nb.reject_reason(REAL))
        self.assertIsNone(nb.reject_reason("Authors should own their mistakes rather than make excuses for them"))


class TestIntegration(unittest.TestCase):
    def test_sleep_cycle_uses_shared_gate(self):
        self.assertIn("nova_beliefs.reject_reason(stance, title)", SRC)


class TestFunctional(unittest.TestCase):
    def test_only_the_real_belief_is_booked_and_refusals_counted(self):
        oc = Cur([("FROM beliefs", None), ("RETURNING id", (5,))])
        art = [(3, "t", "Loops cascade past a tipping point.")]
        with patch.object(sc, "llm", return_value=_ans(RECAP, SELF, REAL)), \
                patch.dict(sys.modules, {"nova_pg_schema": __import__("unittest.mock").mock.MagicMock()}):
            sc.phase_beliefs(Cur([("nova_articles", art)]), oc)
        ins = oc.writes("INSERT INTO beliefs")
        self.assertEqual([p[1] for _, p in ins], [REAL])
        self.assertEqual(oc.writes("INSERT INTO sleep_cycle_belief_stats")[0][1][1:3], (1, 2))


class TestFrame(unittest.TestCase):
    def test_gate_runs_before_evidence_and_insert(self):
        body = SRC[SRC.index("def phase_beliefs"):]
        i_gate, i_ev = body.index("nova_beliefs.reject_reason"), body.index("belief_evidence(b, mem_id")
        self.assertLess(i_gate, i_ev)


if __name__ == "__main__":
    unittest.main()
