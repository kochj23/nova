#!/usr/bin/env python3
"""Tests for nova_beliefs.py — lessons become ranked candidates; only the best become (calibrated) beliefs."""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nova_beliefs as nb  # noqa: E402


class Cur:
    """Canned cursor: (sql fragment, result) pairs; records every execute."""

    def __init__(self, canned=None):
        self.canned, self.sql, self.params, self._last = canned or [], [], [], None
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.sql.append(sql)
        self.params.append(params)
        self._last = next((v for k, v in self.canned if k in sql), None)

    def fetchone(self):
        v = self._last
        return v[0] if isinstance(v, list) and v else (None if isinstance(v, list) else v)

    def fetchall(self):
        return self._last if isinstance(self._last, list) else []


class TestCleaning(unittest.TestCase):
    def test_when_so_template_is_repaired(self):
        self.assertEqual(
            nb.clean_stance("When FESA failed to mention Yarloop in alerts despite the fire , so communities faced "
                            "delayed evacuation ."),
            "When FESA failed to mention Yarloop in alerts despite the fire, communities faced delayed evacuation.")

    def test_space_before_punctuation_and_cites(self):
        self.assertEqual(nb.clean_stance("pilots must trust instruments [2][3] ; not their ears ."),
                         "Pilots must trust instruments; not their ears.")

    def test_dash_so_becomes_comma_so(self):
        self.assertIn(", so ", nb.clean_stance("A recall path matters — so every automation needs one"))

    def test_a_proper_when_clause_with_main_clause_keeps_its_so(self):
        s = "When the pyrostat detects flame failure, it closes the valves, so automatic shutoff is essential."
        self.assertEqual(nb.clean_stance(s), s)


class TestRejection(unittest.TestCase):
    def test_scripture_is_never_a_belief(self):
        for s in ("When Israel 'forsakes the LORD', they become sick with wounds.",
                  "When Israel remembers their slavery in Egypt, they observe God's statutes, ensuring justice."):
            self.assertEqual(nb.reject_reason(nb.clean_stance(s)), "scripture")
        self.assertEqual(nb.reject_reason("Ritual purity laws separate the clean from the unclean in daily life.",
                                          "Leviticus", "book"), "scripture")

    def test_fragments_questions_and_dangling_clauses(self):
        self.assertEqual(nb.reject_reason("Too short."), "fragment")
        self.assertEqual(nb.reject_reason("Should every alert name the community it threatens first?"), "question")
        self.assertTrue(nb.reject_reason("When the alert never named the town in time.").startswith("malformed"))

    def test_a_general_lesson_passes(self):
        self.assertIsNone(nb.reject_reason("Alerts that do not name the threatened community delay its evacuation."))

    def test_generality_ranks_rules_above_plot(self):
        rule = "Pilots must trust instruments over bodily sensations during vestibular illusions."
        plot = "When Norm turns the top with a face plate, the raised lip matches the antique's edge detail."
        self.assertGreater(nb.generality(rule), nb.generality(plot) + 0.2)


class TestCalibration(unittest.TestCase):
    def test_confidence_is_not_a_constant(self):
        vals = {nb.confidence_for(c, n) for c in ("observed", "established", "opinion", "fiction") for n in (1, 3)}
        self.assertGreater(len(vals), 5)

    def test_more_independent_sources_more_confidence_with_diminishing_returns(self):
        c1, c2, c5 = (nb.confidence_for("established", n) for n in (1, 2, 5))
        self.assertLess(c1, c2)
        self.assertLess(c2, c5)
        self.assertLess(c5 - c2, (c2 - c1) * 3)
        self.assertLessEqual(c5, 0.9)

    def test_credibility_orders_confidence(self):
        self.assertGreater(nb.confidence_for("observed"), nb.confidence_for("established"))
        self.assertGreater(nb.confidence_for("established"), nb.confidence_for("opinion"))
        self.assertGreater(nb.confidence_for("opinion"), nb.confidence_for("fiction"))

    def test_reinforce_and_contradict_move_the_right_way(self):
        self.assertGreater(nb.reinforced_conf(0.6, "established"), 0.6)
        self.assertLessEqual(nb.reinforced_conf(0.949, "observed"), nb.MAX_CONF)
        self.assertLess(nb.contradicted_conf(0.7, "established"), 0.7)


class TestScoring(unittest.TestCase):
    def test_relevant_general_novel_beats_irrelevant_trivia(self):
        good = nb.value_score(0.95, 0.4, 0.68, "established", 1, 2)
        craft = nb.value_score(0.55, 0.4, 0.40, "established", 1, 1)
        self.assertGreater(good, nb.MIN_SCORE)
        self.assertLess(craft, nb.MIN_SCORE)

    def test_a_restatement_of_what_she_believes_scores_lower(self):
        self.assertGreater(nb.value_score(0.9, 0.35, 0.6, "established"), nb.value_score(0.9, 0.02, 0.6, "established"))

    def test_independent_sources_raise_the_score(self):
        self.assertGreater(nb.value_score(0.8, 0.3, 0.6, "unknown", 3), nb.value_score(0.8, 0.3, 0.6, "unknown", 1))

    def test_fringe_never_scores(self):
        self.assertEqual(nb.value_score(1, 1, 1, "fringe", 5, 3), 0)

    def test_budget_is_released_through_the_day(self):
        self.assertEqual(nb.allowance(0, 0), 5)
        self.assertEqual(nb.allowance(50, 12), 0)
        self.assertEqual(nb.allowance(0, 23), 50)
        self.assertLess(nb.allowance(0, 6), nb.allowance(0, 18))


class TestTaste(unittest.TestCase):
    def test_numbers_and_filler_are_not_verdicts(self):
        for v in ("-1.0", "1.0", "0.6", "n/a", "null", "Woodworking techniques don't translate to system design",
                  "A practical guide, but not applicable to Nova's operational system.",
                  "A woodworking tutorial's precision mirrors Nova's operational rigor.",
                  "A political analysis with no bearing on Nova's operational integrity."):
            self.assertFalse(nb.verdict_ok(v), v)

    def test_a_real_opinion_is_kept(self):
        for v in ("Bleak and exact.", "Smug about its own cleverness, but the second half earns it."):
            self.assertTrue(nb.verdict_ok(v), v)


class TestDistill(unittest.TestCase):
    def test_parse(self):
        d = nb.parse_distill('<think>x</think>{"belief": "alerts that skip the town delay evacuation", '
                             '"topic": "alerts", "relation": "refines", "why": "same"}')
        self.assertEqual(d["belief"], "Alerts that skip the town delay evacuation.")
        self.assertEqual(d["relation"], "refines")
        self.assertIsNone(nb.parse_distill('{"belief": "null"}')["belief"])
        self.assertEqual(nb.parse_distill('{"belief": "x y z", "relation": "weird"}')["relation"], "new")
        self.assertIsNone(nb.parse_distill("not json"))

    def test_prompt_asks_for_relation_only_when_a_neighbour_exists(self):
        self.assertIn("closest EXISTING belief", nb.distill_prompt("s", "t", "work_lessons", None, "old belief"))
        self.assertNotIn("closest EXISTING belief", nb.distill_prompt("s", "t", "work_lessons", None, None))


class TestAddCandidate(unittest.TestCase):
    def _cur(self, beliefs=None, pending=None):
        return Cur([("FROM beliefs b LEFT JOIN belief_vectors", beliefs or []),
                    ("FROM belief_candidates WHERE status='pending'", pending or []),
                    ("INSERT INTO belief_candidates", [(42,)]), ("SELECT confidence FROM beliefs", (0.6,))])

    def test_rejects_without_evidence_and_from_fringe(self):
        self.assertEqual(nb.add_candidate(Cur(), "ops", "r", "t", "Some general lesson about systems here.", [])[1],
                         "rejected: no evidence")
        self.assertEqual(nb.add_candidate(Cur(), "ops", "r", "t", "Some general lesson about systems here.", ["x"],
                                          credibility="fringe")[1], "rejected: fringe source")

    def test_scripture_is_stored_as_rejected_not_pending(self):
        oc = self._cur()
        _, st = nb.add_candidate(oc, "work_lessons", "w", "Deuteronomy",
                                 "When Israel remembers their slavery, they observe God's statutes.", ["memory:m"])
        self.assertEqual(st, "rejected: scripture")
        self.assertTrue(any("'rejected'" in s for s in oc.sql))

    def test_restatement_of_an_active_belief_reinforces_it_without_spending_the_cap(self):
        oc = self._cur(beliefs=[(7, "Alerts must name the town.", "h", [1.0, 0.0])])
        with mock.patch.object(nb, "embed", return_value=[[1.0, 0.001]]), \
                mock.patch("hashlib.md5", return_value=mock.Mock(hexdigest=lambda: "h")):
            bid, st = nb.add_candidate(oc, "work_lessons", "w", "t", "Alerts that skip the town name delay evacuation.",
                                       ["memory:m"], credibility="established")
        self.assertEqual((bid, st), (7, "reinforced:7"))
        self.assertTrue(any(s.startswith("UPDATE beliefs SET evidence") for s in oc.sql))
        self.assertFalse(any("work_lessons_daily" in s for s in oc.sql))

    def test_same_claim_from_another_source_merges_into_the_pending_candidate(self):
        oc = self._cur(pending=[(9, [0.0, 1.0], ["w1"])])
        with mock.patch.object(nb, "embed", return_value=[[0.0, 1.0]]):
            cid, st = nb.add_candidate(oc, "work_lessons", "w2", "t", "Backups nobody restores are not backups at all.",
                                       ["memory:m"])
        self.assertEqual((cid, st), (9, "merged"))
        self.assertTrue(any("n_sources = n_sources + 1" in s for s in oc.sql))

    def test_a_new_claim_waits_as_pending(self):
        oc = self._cur()
        with mock.patch.object(nb, "embed", return_value=None):
            cid, st = nb.add_candidate(oc, "ops", "hotwash:1", "t", "Restarting a healthy poller fakes presence events.",
                                       ["hotwash:1"], credibility="observed")
        self.assertEqual((cid, st), (42, "pending"))


class TestPromote(unittest.TestCase):
    def test_relation_same_reinforces_and_new_books_with_calibrated_confidence(self):
        cands = [
            {"id": 1, "origin": "ops", "ref": "h:1", "title": "", "kind": None, "topic": "t",
             "stance": "Restarting a healthy poller fakes presence events.", "quote": None, "evidence": ["h:1"],
             "credibility": "observed", "fit": 2, "n_sources": 2, "embedding": [1, 0], "near_id": 5,
             "near_cos": 0.85, "near_stance": "Restarts of healthy pollers create fake events.", "domain": "sre",
             "relevance": 0.7, "generality": 0.9, "score": 0.9},
            {"id": 2, "origin": "work_lessons", "ref": "w", "title": "X", "kind": "film", "topic": "t",
             "stance": "Alerts that skip the town name delay evacuation.", "quote": None, "evidence": ["memory:m"],
             "credibility": "established", "fit": 2, "n_sources": 1, "embedding": [0, 1], "near_id": None,
             "near_cos": 0.3, "near_stance": None, "domain": "sre", "relevance": 0.6, "generality": 0.9,
             "score": 0.8}]
        answers = iter(['{"belief": "Restarting a healthy poller fakes presence events.", "relation": "same"}',
                        '{"belief": "Alerts that do not name the threatened town delay its evacuation.", '
                        '"relation": "new", "topic": "alert naming"}'])
        oc = Cur([("SELECT confidence FROM beliefs", (0.6,)), ("work_lessons_daily SET", (1,)),
                  ("INSERT INTO beliefs", (77,)), ("FROM work_lessons_daily WHERE", (0,))])
        with mock.patch.object(nb, "belief_pool", return_value=[]), \
                mock.patch.object(nb, "score_pending", return_value=cands), \
                mock.patch.object(nb, "llm_json", side_effect=lambda p: next(answers)), \
                mock.patch.object(nb, "embed", return_value=None):
            st = nb.promote(oc, budget=5)
        self.assertEqual((st["reinforced"], st["new"]), (1, 1))
        ins = next(p for s, p in zip(oc.sql, oc.params) if s.startswith("INSERT INTO beliefs"))
        self.assertEqual(ins[1], "Alerts that do not name the threatened town delay its evacuation.")
        self.assertEqual(ins[2], nb.confidence_for("established", 1, 2))

    def test_model_null_rejects_and_budget_zero_books_nothing(self):
        c = {"id": 3, "origin": "work_lessons", "ref": "w", "title": "Poker Table", "kind": "tv_episode",
             "topic": "t", "stance": "Minimal brads keep a table's lines clean.", "quote": None, "evidence": ["m"],
             "credibility": "established", "fit": 2, "n_sources": 1, "embedding": [1], "near_id": None,
             "near_cos": 0.1, "near_stance": None, "domain": "home", "relevance": 0.5, "generality": 0.8,
             "score": 0.9}
        oc = Cur()
        with mock.patch.object(nb, "belief_pool", return_value=[]), \
                mock.patch.object(nb, "score_pending", return_value=[c, dict(c, id=4)]), \
                mock.patch.object(nb, "llm_json", side_effect=['{"belief": null, "why": "craft step"}',
                                                               '{"belief": "Using fewer visible fasteners keeps finished furniture looking clean and simple."}']):
            st = nb.promote(oc, budget=0)
        self.assertEqual((st["rejected"], st["new"]), (1, 0))
        self.assertFalse(any(s.startswith("INSERT INTO beliefs") for s in oc.sql))

    def test_below_the_bar_never_reaches_the_model(self):
        c = {"id": 5, "score": nb.MIN_SCORE - 0.01}
        with mock.patch.object(nb, "belief_pool", return_value=[]), \
                mock.patch.object(nb, "score_pending", return_value=[c]), \
                mock.patch.object(nb, "llm_json") as llm:
            st = nb.promote(Cur(), budget=5)
        llm.assert_not_called()
        self.assertEqual(st["below_bar"], 1)


class TestSource(unittest.TestCase):
    def test_sql_is_parameterized_and_local_only(self):
        src = Path(nb.__file__).read_text()
        self.assertNotRegex(src, r"execute\(\s*f[\"']")
        self.assertNotIn("openrouter", src.lower())
        self.assertNotIn("anthropic", src.lower())

    def test_selftest(self):
        nb.selftest()


if __name__ == "__main__":
    unittest.main()
