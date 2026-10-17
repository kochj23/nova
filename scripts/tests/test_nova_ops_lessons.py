#!/usr/bin/env python3
"""Tests for nova_ops_lessons.py — Nova learns from her own operations (2026-10-17)."""

import json
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nova_ops_lessons as ol  # noqa: E402

SRC = Path(ol.__file__).read_text()
NOW = datetime(2026, 10, 17, 20, 0, tzinfo=timezone.utc)


class Cur:
    """Canned fetchall results keyed by SQL fragment; records every execute."""

    def __init__(self, canned=None, fail=None):
        self.canned, self.calls, self._last, self.fail = canned or [], [], None, fail
        self.connection = type("C", (), {"rollback": lambda self: None})()

    def execute(self, sql, params=None):
        if self.fail and self.fail in sql:
            raise RuntimeError("relation does not exist")
        self.calls.append((" ".join(sql.split()), params))
        self._last = next((v for k, v in self.canned if k in sql), None)

    def fetchall(self):
        return self._last if isinstance(self._last, list) else []

    def fetchone(self):
        return self._last if isinstance(self._last, tuple) else None


class TestSecurity(unittest.TestCase):
    def test_no_fstring_sql_and_local_models_only(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertNotIn("openrouter", SRC.lower())
        for url in ol.OLLAMA_URLS:
            self.assertTrue(url.startswith("http://127.0.0.1"), url)

    def test_writes_only_its_ledger_signals_and_config(self):
        writes = set(re.findall(r"\b(?:INSERT INTO|UPDATE)\s+(\w+)", SRC)) - {"SET"}  # ON CONFLICT DO UPDATE SET
        self.assertEqual(writes, {"ops_lessons", "growth_signals", "service_config"})
        self.assertNotIn("DELETE", SRC)


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        ol.selftest()

    def test_lesson_problem(self):
        good = "A job that times out on every run hides a real dependency failure behind a generic error."
        self.assertIsNone(ol.lesson_problem(good))
        self.assertEqual(ol.lesson_problem("The scheduler failed today because the NAS mount was slow again."),
                         "tied to one day")
        self.assertTrue(ol.lesson_problem("Item #3725 was rejected because there is no such service to reboot."))

    def test_parse_lessons_caps_per_group_and_resolves_keys(self):
        facts = [(f"hotwash:{i}", "x") for i in range(1, 4)]
        lessons = [{"lesson": f"Suppressing an accurate detector number {w} hides real failures until they recur.",
                    "refs": ["R1"], "self_key": "hold", "change": "so my hold should page it"} for w in "abc"]
        out = ol.parse_lessons(json.dumps({"lessons": lessons}), facts, {"organ:hold"})
        self.assertEqual(len(out), ol.PER_GROUP)
        self.assertEqual(out[0]["ref"], "organ:hold")
        out = ol.parse_lessons(json.dumps({"lessons": [dict(lessons[0], self_key="organ:nope")]}), facts, {"organ:hold"})
        self.assertIsNone(out[0]["ref"])
        self.assertEqual(out[0]["change"], "")                                   # no change without a real key

    def test_signal_kind_uses_growth_classifier(self):
        self.assertEqual(ol.signal_kind("organ:hold", "so my hold should page chronic jobs"), "change")
        self.assertEqual(ol.signal_kind("redline:two_man", "so my two_man should cover more"), "keep")
        self.assertIsNone(ol.signal_kind("organ:hold", ""))

    def test_window_tag(self):
        self.assertEqual(ol.window_tag(NOW), "2026-10-17T20")


class TestIntegration(unittest.TestCase):
    def test_gather_groups_each_source_and_fails_open(self):
        cur = Cur([
            ("FROM hotwash", [(1, "missed_event", "source:a", "q2", "q3", "q4", 40),
                              (2, "false_alarm", "source:b", "q2", "q3", "q4", 3)]),
            ("FROM postmortems", [(5, "Probe fails", "narr", "[]", "recurring")]),
            ("FROM scheduler_runs", [("job_x", 3, 10, "Traceback ... boom")]),
            ("FROM coagency_proposals", [(196, "rejected", "growth", "adopt skill", "no such service", "")]),
            ("FROM gateway_traces", [("t1", "slack", "router:x:rescue", "hi", "")]),   # only one -> no group
        ], fail="telemetry.incidents")
        groups = ol.gather(cur, NOW - timedelta(hours=2), NOW)
        keys = [g["key"] for g in groups]
        self.assertEqual(keys, ["hotwash_missed_event", "hotwash_false_alarm", "postmortem", "scheduler", "coagency"])
        self.assertEqual(groups[-1]["facts"][0][0], "coagency:196")
        self.assertTrue(all(isinstance(p, tuple) for _, p in cur.calls if p is not None))

    def test_prompt_lists_only_parts_she_can_change(self):
        g = {"title": "failing scheduled jobs", "facts": [("scheduler:x", "job x failed")]}
        p = ol.lesson_prompt(g, [("organ:hold", "pauses"), ("herd:Sam", "a friend"), ("redline:ae35", "never")])
        self.assertIn("organ:hold", p)
        self.assertNotIn("herd:Sam", p)
        self.assertNotIn("redline:ae35", p)
        self.assertIn("[R1] job x failed", p)

    def test_to_belief_reports_missing_module(self):
        with patch.dict(sys.modules, {"nova_beliefs": None}):
            cid, st = ol.to_belief(Cur(), 1, {"key": "k", "title": "t"},
                                   {"lesson": "x", "evidence": ["hotwash:1"], "ref": None}, NOW)
        self.assertIsNone(cid)
        self.assertTrue(st.startswith("beliefs_unavailable"))

    def test_to_belief_passes_ops_origin_and_observed_credibility(self):
        seen = {}

        class FakeBeliefs:
            @staticmethod
            def add_candidate(oc, origin, ref, title, stance, evidence, **kw):
                seen.update(origin=origin, ref=ref, evidence=evidence, **kw)
                return 11, "pending"
        with patch.dict(sys.modules, {"nova_beliefs": FakeBeliefs}):
            cid, st = ol.to_belief(Cur(), 1, {"key": "scheduler", "title": "failing jobs"},
                                   {"lesson": "Jobs fail.", "evidence": ["scheduler:x"], "ref": "organ:hold",
                                    "topic": "jobs"}, NOW)
        self.assertEqual((cid, st), (11, "pending"))
        self.assertEqual(seen["origin"], "ops")
        self.assertEqual(seen["credibility"], "observed")
        self.assertEqual(seen["ref"], "ops:scheduler:2026-10-17T20")
        self.assertEqual(seen["fit"], 2)


if __name__ == "__main__":
    unittest.main()
