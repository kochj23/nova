#!/usr/bin/env python3
"""Tests for nova_watch_bill.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_watch_common as W  # noqa: E402
import nova_watch_bill as WB  # noqa: E402

SRC = (SCRIPTS / "nova_watch_bill.py").read_text()
END = datetime(2026, 10, 8, 14, 0, tzinfo=timezone.utc)
START = END - timedelta(hours=9)
LED = {"detector:nova_security_organ": {"reliability": "D", "hit_rate": 0.54, "n": 35},
       "camera:front_door": {"reliability": "F", "truth_kind": "none"},
       "nova:prediction:all": {"reliability": "E"},
       "detector:nova_daemon_staleness": {"reliability": "A"}}


def night(**kw):
    base = {"by_class": {"car": 2}, "person_zones": {}, "person_n": 0, "person_p95": 3.0, "deep_person": 0,
            "scanner": [], "loiters": [], "newdev": [], "buick": [], "bodach_max": 0.0, "bodach_fired": False,
            "bodach_types": 0, "bed_phone": (None, None, 0), "bed_mmwave": (None, None, 0)}
    base.update(kw)
    return base


class Cur:
    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._r = routes or {}, boom, [], []
        self.connection = mock.MagicMock()

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._r = []
        for k, v in self.routes.items():
            if k in sql:
                self._r = list(v)
                break

    def fetchall(self):
        return self._r

    def fetchone(self):
        return self._r[0] if self._r else None


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')

    def test_pdb_never_carries_an_address(self):
        g = night(scanner=[(START, 0.3, "ambulance to 1200 Olive Ave (~0.3 mi NE)", "Burbank PD")])
        msg = WB.compose_pdb(Cur(), g, START, END, t=None, ledger=LED)
        self.assertNotIn("Olive", msg)
        self.assertNotRegex(msg, r"\d+\.\d+ mi (N|S|E|W)")

    def test_no_flattery_no_greeting(self):
        msg = WB.compose_pdb(Cur(), night(), START, END, t=None, ledger=LED)
        self.assertNotRegex(msg.lower(), r"good morning|great job|you're doing|proud of you")
        self.assertTrue(msg.splitlines()[1].startswith("BLUF:"))


class TestPerformance(unittest.TestCase):
    def test_compose_1k_items_bounded(self):
        t = {"degraded": [f"svc{i}@n down" for i in range(1000)], "open_loops": [], "expected": []}
        t0 = time.monotonic()
        msg = WB.compose_pdb(Cur(), night(newdev=[1] * 50), START, END, t=t, ledger=LED)
        self.assertLess(time.monotonic() - t0, 3.0)
        numbered = [l for l in msg.splitlines() if l[:2].rstrip(".").isdigit()]
        self.assertLessEqual(len(numbered), 6)          # 3-6 items, never a wall


class TestRetry(unittest.TestCase):
    def test_queries_fail_soft(self):
        # RETRY GAP: _q — single attempt per query (connect retries 3x in nova_watch_common);
        # a missing table or dead PG yields an empty section, never a crash.
        cur = Cur(boom=True)
        self.assertEqual(WB.open_loops(cur, END), [])
        self.assertEqual(WB.degraded(cur, {}, {"reasons": []}), [])

    def test_turnover_survives_nova_state_failure(self):
        with mock.patch("nova_escalation.nova_state", side_effect=RuntimeError("probe died")), \
             mock.patch("nova_cardinal.load_ledger", return_value={}):
            t = WB.turnover(Cur(), "0630")
        self.assertIn("error", t["nova"])


class TestUnit(unittest.TestCase):
    def test_night_items_grade_inputs(self):
        it = WB.night_items(night(newdev=[1]), LED)[0]
        self.assertEqual(it["kind"], "network")
        self.assertEqual(it["p"], 0.54)

    def test_importance_puts_unpleasant_first(self):
        t = {"degraded": ["a@x down", "b@x down", "c@x down"], "open_loops": [], "expected": ["#1 x"]}
        msg = WB.compose_pdb(Cur(), night(), START, END, t=t, ledger=LED)
        self.assertIn("BLUF: 3 system(s) degraded", msg)

    def test_red_cell(self):
        self.assertIn("quiet sensor", WB.red_cell(None, LED))
        lead = {"kind": "cameras", "grade": {"spinnaker": {"shared_upstream": [1], "verdict": "SINGLE_SOURCE"}}}
        self.assertIn("one upstream", WB.red_cell(lead, LED))

    def test_change_my_mind(self):
        self.assertIn("ARP", WB.change_my_mind({"kind": "network", "grade": {}}))
        self.assertIn("corroboration from", WB.change_my_mind({"kind": "scanner",
                                                               "grade": {"spinnaker": {"independent_types": ["radio"]}}}))

    def test_current_watch(self):
        self.assertEqual(WB.current_watch(datetime(2026, 1, 1, 18, 0, tzinfo=W.TZ)), "1800")
        self.assertEqual(WB.current_watch(datetime(2026, 1, 1, 23, 0, tzinfo=W.TZ)), "2300")

    def test_selftest(self):
        self.assertEqual(WB.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_uses_cardinal_grade_and_escalation_deferred(self):
        self.assertIn("C.grade(it[\"item\"], ledger=ledger)", SRC)
        self.assertIn("from nova_escalation import deferred_since", SRC)
        self.assertIn("CI.standing_orders(cur)", SRC)

    def test_scorecard_from_predictions(self):
        cur = Cur({"FROM predictions": [("correct", 0.7, "a"), ("incorrect", 0.9, "it will rain")]})
        line = WB.scorecard(cur)
        self.assertIn("1/2 right", line)
        self.assertIn("it will rain", line)

    def test_gaps_lists_blind_spots(self):
        t = {"degraded": ["source suspect: presence:mmwave:patio — duplicate of master_bedroom: x"]}
        gp = WB.gaps(Cur(), t, LED, night(by_class={}))
        self.assertTrue(any("ground truth" in x for x in gp))
        self.assertTrue(any("blind: mmwave:patio" in x for x in gp))
        self.assertTrue(any("silent pipeline" in x for x in gp))


class TestFunctional(unittest.TestCase):
    def test_turnover_saved_not_posted(self):
        cur = Cur({"FROM boiler_state": [(82.0, 100.0, [{"source": "doc_drift"}])]})
        with mock.patch("nova_escalation.nova_state", return_value={"degraded": False, "reasons": []}), \
             mock.patch("nova_cardinal.load_ledger", return_value=LED), \
             mock.patch("nova_config.post_both") as pb:
            t = WB.turnover(cur, "2300")
            text = WB.render_turnover(t)
            WB.save_turnover(cur, t, text)
        pb.assert_not_called()
        self.assertIn("Would surprise me", text)
        self.assertTrue(any("INSERT INTO watch_turnover" in s for s, _ in cur.sql))
        self.assertIn("Boiler 82/100", text)

    def test_full_pdb_golden(self):
        t = {"degraded": ["source suspect: detector:x — recent hit-rate 3% (n=9) is below its long-run floor 57%"],
             "open_loops": ["Boiler 82/100: doc_drift"], "expected": []}
        msg = WB.compose_pdb(Cur({"FROM predictions": [("correct", 0.6, "a")]}),
                             night(newdev=[1], scanner=[(START, 0.4, "medical aid", "Burbank PD")]),
                             START, END, t=t, ledger=LED)
        for part in ("*PDB for Little Mister*", "BLUF:", "[D4]", "Would change my mind:", "Gaps:", "Red cell:",
                     "Scorecard", "Chance it matters"):
            self.assertIn(part, msg)


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_watch_bill.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_watch_bill.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
