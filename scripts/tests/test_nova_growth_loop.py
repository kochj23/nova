#!/usr/bin/env python3
"""Tests for nova_growth_loop.py — lessons that recur across works become practices Nova lives by."""

import json
import re
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import nova_growth_loop as g  # noqa: E402

SRC = Path(g.__file__).read_text()


def sig(i, ref="organ:hold"):
    return (f"w{i}", f"Work {i}", i, [{"ref": ref, "meaning": f"so my hold should also cover case {i}"}])


class Cur:
    """Minimal cursor: canned fetch results keyed by SQL fragment; records every execute."""

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
        g.selftest()

    def test_red_line_is_never_a_change(self):
        self.assertEqual(g.classify("redline:kill_switch", "so my kill switch should release on a timer"), "keep")

    def test_one_signal_per_work_and_ref(self):
        rows = [sig(1), ("w1", "Work 1", 2, [{"ref": "organ:hold", "meaning": "so my hold should also do x"}])]
        self.assertEqual(len(g.signals_from(rows)), 1)

    def test_synthesis_needs_three_real_supporters(self):
        ok = json.dumps({"practice": "I give every job I start a recall path.", "change": "add recall to jobs",
                         "supported_by": ["S1", "S2", "S3"]})
        self.assertEqual(g.parse_synthesis(ok, 3)["supported_by"], [1, 2, 3])
        self.assertIsNone(g.parse_synthesis(ok.replace('"S3"', '"S1"'), 3))
        self.assertIsNone(g.parse_synthesis("not json", 3))
        self.assertIsNone(g.parse_synthesis(ok.replace("I give every job I start a recall path.", "Why?" * 8), 3))


class TestIntegration(unittest.TestCase):
    def test_collect_writes_signals_reinforces_and_moves_watermark(self):
        oc = Cur([("FROM service_config", ("2026-10-01",)), ("FROM work_lessons", [sig(1), sig(2)])])
        self.assertEqual(g.collect(oc), 2)
        self.assertEqual(len(oc.writes("INSERT INTO growth_signals")), 2)
        self.assertEqual(len(oc.writes("UPDATE nova_practices SET strength")), 2)
        self.assertEqual(oc.writes("INSERT INTO service_config")[0][1], ("2",))

    def test_dry_run_writes_nothing(self):
        oc = Cur([("FROM service_config", None), ("FROM work_lessons", [sig(1)])])
        self.assertEqual(g.collect(oc, dry=True), 1)
        self.assertEqual(oc.writes("INSERT"), [])

    def test_earned_practice_is_stored_remembered_and_proposed(self):
        sigs = [(f"w{i}", f"T{i}", f"so my hold should cover {i}") for i in (1, 2, 3)]
        oc = Cur([("HAVING count", [("organ:hold", 3)]), ("SELECT work_key, title, meaning", sigs),
                  ("RETURNING id", (7,))])
        llm = MagicMock(return_value=json.dumps({"practice": "I give every job I start a recall path.",
                                                 "change": "add a recall path to scheduled jobs",
                                                 "supported_by": [1, 2, 3], "why": "three works"}))
        with patch.object(g, "_describe", return_value="hold organ"), \
                patch.object(g, "_remember", return_value="m1") as rem, \
                patch.object(g, "file_change", return_value={"proposal_id": 9}) as fc:
            self.assertEqual(g.synthesize(oc, llm=llm), 1)
        self.assertEqual(oc.writes("INSERT INTO nova_practices")[0][1][4], ["w1", "w2", "w3"])
        rem.assert_called_once()
        fc.assert_called_once()

    def test_no_shared_practice_earns_nothing(self):
        sigs = [(f"w{i}", f"T{i}", "m" * 20) for i in (1, 2, 3)]
        oc = Cur([("HAVING count", [("organ:hold", 3)]), ("SELECT work_key, title, meaning", sigs)])
        with patch.object(g, "_describe", return_value="d"):
            self.assertEqual(g.synthesize(oc, llm=lambda *a, **k: '{"practice": null}'), 0)
        self.assertEqual(oc.writes("INSERT INTO nova_practices"), [])

    def test_red_line_change_goes_to_claude_not_coagency(self):
        rl = MagicMock(classify=MagicMock(return_value=["ae35"]), route_to_claude=MagicMock(return_value=42))
        co = MagicMock()
        oc = Cur([("count(*) FROM nova_practices", (0,))])
        with patch.dict(sys.modules, {"nova_red_line": rl, "nova_coagency": co}):
            r = g.file_change(oc, "organ:watchdog", {"change": "silence the watchdog at night", "practice": "p",
                                                     "why": "w", "supported_by": [1, 2, 3]})
        self.assertEqual(r["claude_queue_id"], 42)
        co.file_proposal.assert_not_called()

    def test_daily_proposal_cap(self):
        oc = Cur([("count(*) FROM nova_practices", (g.MAX_PROPOSALS_PER_DAY,))])
        self.assertIsNone(g.file_change(oc, "organ:x", {"change": "a change long enough", "practice": "p",
                                                        "why": "", "supported_by": [1, 2, 3]}))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_hosts(self):
        self.assertIsNone(re.search(r"(password|secret|token)\s*=\s*['\"][^'\"]{6,}", SRC, re.I))
        self.assertIsNone(re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", SRC))

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")

    def test_prompt_forbids_dangerous_changes(self):
        p = g.synth_prompt("organ:x", "d", [{"title": "t", "meaning": "m"}])
        for word in ("credentials", "deletes", "kill switch", "watchers"):
            self.assertIn(word, p)


class TestPerformance(unittest.TestCase):
    def test_10k_signals_fast(self):
        rows = [sig(i, ref=f"organ:o{i % 50}") for i in range(10000)]
        t = time.time()
        self.assertEqual(len(g.signals_from(rows)), 10000)
        self.assertLess(time.time() - t, 2.0)


class TestResilience(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)

    def test_remember_failure_is_non_fatal(self):
        with patch.dict(sys.modules, {"nova_sleep_cycle": MagicMock(remember=MagicMock(side_effect=OSError))}):
            self.assertIsNone(g._remember({"practice": "p"}, "organ:x", ["a"]))


if __name__ == "__main__":
    unittest.main()
