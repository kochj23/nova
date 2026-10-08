#!/usr/bin/env python3
"""Tests for nova_cardinal.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import nova_cardinal as C  # noqa: E402

SRC = (SCRIPTS / "nova_cardinal.py").read_text()


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []
        self.connection = mock.MagicMock()

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = []
        for k, v in self.routes.items():
            if k in sql:
                self._last = list(v)
                break

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"(?i)(password|token)\s*=\s*['\"][^'\"]{6,}")

    def test_suspect_source_grades_F(self):
        led = {"detector:x": {"reliability": "A", "compromise_suspect": True}}
        g = C.grade({"sources": [{"id": "detector:x"}]}, ledger=led)
        self.assertEqual(g["reliability"], "F")
        self.assertTrue(g["sources"][0]["compromise_suspect"])

    def test_visitor_identities_never_scored(self):
        cur = FakeCur({"face_retention": [], "embodiment_state": [], "FROM memories": [
            (datetime.now(timezone.utc), "Dave Bloom")]})
        rows = C.harvest_face(cur, cur)
        self.assertEqual(rows[0]["n"], 0)   # non-household sighting contributed nothing


class TestPerformance(unittest.TestCase):
    def test_score_10k_outcomes(self):
        t = time.monotonic()
        r = C.score_record("detector:x", "detector", "labelled", [(None, float(i % 3 != 0)) for i in range(10000)])
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertEqual(r["n"], 10000)

    def test_grade_fast(self):
        led = {f"camera:c{i}": {"reliability": "C"} for i in range(500)}
        t = time.monotonic()
        for _ in range(500):
            C.grade({"sources": [{"id": "camera:c1"}, {"id": "scanner:x"}]}, ledger=led)
        self.assertLess(time.monotonic() - t, 3.0)


class TestRetry(unittest.TestCase):
    def test_load_ledger_fails_open_to_empty(self):
        # RETRY GAP: load_ledger — connection retry lives in nova_watch_common.connect (3x backoff);
        # an unreadable ledger must degrade to {} (everything grades F), never raise.
        self.assertEqual(C.load_ledger(FakeCur(boom=True)), {})

    def test_harvester_query_failure_is_contained(self):
        self.assertEqual(C.harvest_predictions(FakeCur(boom=True)), [])

    def test_connect_retries_with_backoff(self):
        import nova_watch_common as W
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("pg failover")
            return mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=flaky):
            W.connect(attempts=3, delay=0, _sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)


class TestUnit(unittest.TestCase):
    def test_reliability_letters(self):
        self.assertEqual(C.reliability(5, 5, "labelled"), "F")
        self.assertEqual(C.reliability(300, 297, "labelled"), "A")
        self.assertEqual(C.reliability(100, 30, "labelled"), "E")
        self.assertEqual(C.reliability(1000, 999, "fusion-agreement"), "B")       # capped
        self.assertEqual(C.reliability(0, 0, "corroboration", corroborated=2), "F")
        self.assertEqual(C.reliability(0, 0, "corroboration", corroborated=9), "F")   # under 10 -> F
        self.assertEqual(C.reliability(0, 0, "corroboration", corroborated=10), "C")

    def test_brier_skill_drags_letter_down(self):
        self.assertEqual(C.reliability(200, 190, "labelled", skill=-0.2), "E")

    def test_credibility_needs_two_types(self):
        led = {"camera:a": {"reliability": "B"}, "scanner:x": {"reliability": "C"}}
        self.assertEqual(C.grade({"sources": [{"id": "camera:a"}, {"id": "scanner:x"}]}, ledger=led)["credibility"], 1)
        self.assertEqual(C.grade({"sources": [{"id": "scanner:x"}, {"id": "scanner:y"}]}, ledger=led)["credibility"], 2)
        self.assertEqual(C.grade({"sources": [{"id": "camera:a"}, {"id": "camera:b"}]}, ledger=led)["credibility"], 3)

    def test_contradiction(self):
        led = {"camera:a": {"reliability": "C"}, "detector:z": {"reliability": "A"}}
        g = C.grade({"sources": [{"id": "camera:a"}], "contradicted_by": [{"id": "detector:z"}]}, ledger=led)
        self.assertEqual(g["credibility"], 5)

    def test_estimative(self):
        self.assertEqual(C.estimative(0.99), "almost certainly")
        self.assertEqual(C.estimative(0.6), "likely")
        self.assertEqual(C.estimative(0.1), "very unlikely")
        self.assertEqual(C.estimative(None), "cannot estimate")

    def test_mmwave_health(self):
        a = {i: i % 5 == 0 for i in range(600)}
        bad = C.mmwave_health({"bed": a, "patio": dict(a), "living": {i: False for i in range(600)}})
        self.assertIn("duplicate", bad["patio"])
        self.assertIn("stuck", bad["living"])
        self.assertNotIn("bed", bad)

    def test_volume_and_drift(self):
        self.assertIn("silent", C.volume_anomaly([20] * 14, 0))
        self.assertIn("jumped", C.volume_anomaly([10, 12, 9, 11, 10, 10, 11, 9], 200))
        self.assertIsNone(C.volume_anomaly([1] * 14, 0))           # too quiet to judge
        self.assertIsNone(C.accuracy_drift(85, 100, 8, 10))        # small dip is not drift

    def test_selftest(self):
        self.assertEqual(C.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_uses_soft_certainty_brier(self):
        self.assertIn("from nova_soft_certainty import brier_stats", SRC)
        r = C.score_record("nova:prediction:x", "prediction", "labelled", [(0.9, 0.0)] * 6 + [(0.9, 1.0)] * 6)
        self.assertIsNotNone(r["skill"])

    def test_uses_spinnaker_for_independence(self):
        g = C.grade({"sources": [{"id": "network:unifi"}, {"id": "network:dhcp"}]}, ledger={})
        self.assertEqual(g["spinnaker"]["independent"], 1)

    def test_prediction_harvest_shape(self):
        cur = FakeCur({"FROM predictions": [("self", 0.8, "correct"), ("self", 0.8, "incorrect")] * 6})
        rows = C.harvest_predictions(cur)
        ids = {r["source_id"] for r in rows}
        self.assertEqual(ids, {"nova:prediction:self", "nova:prediction:all"})

    def test_buick8_on_new_suspect(self):
        self.assertIn('log_unexplained("source_behaviour_change"', SRC)


class TestFunctional(unittest.TestCase):
    def test_write_rows_upserts_and_keeps_history(self):
        cur = FakeCur()
        rows = [C.score_record("detector:a", "detector", "labelled", [(None, 1.0)] * 20)]
        self.assertEqual(C.write_rows(cur, rows), 1)
        sqls = " ".join(s for s, _ in cur.sql)
        self.assertIn("ON CONFLICT (source_id) DO UPDATE", sqls)
        self.assertIn("INSERT INTO source_ledger_history", sqls)

    def test_detector_harvest_marks_drift(self):
        now = datetime.now(timezone.utc)
        old = [("x", "page", "was_real", now - timedelta(days=20))] * 60
        new = [("x", "page", "was_noise", now - timedelta(days=1))] * 12
        rows = {r["source_id"]: r for r in C.harvest_detectors(FakeCur({"alert_triage_log": old + new}))}
        self.assertIn("below its long-run", C.detect_compromise(rows["detector:x"], None))

    def test_record_outcome_validates(self):
        cur = FakeCur()
        C.record_outcome(cur, "camera:a", "camera", 1, ref="t", recorded_by="test")
        self.assertTrue(any("INSERT INTO source_outcomes" in s for s, _ in cur.sql))


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_cardinal.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_cardinal.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
