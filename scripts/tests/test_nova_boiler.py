#!/usr/bin/env python3
"""Tests for nova_boiler.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_boiler as bo  # noqa: E402

SRC = (SCRIPTS / "nova_boiler.py").read_text()


def _g(**over):
    g = {s: {"n": 0, "age_d": 0.0, "examples": []} for s in bo.SOURCES}
    for k, v in over.items():
        g[k] = {"n": v[0], "age_d": v[1], "examples": v[2] if len(v) > 2 else []}
    return g


class _Cur:
    def __init__(self, bled_today=0):
        self.sql, self.bled_today = [], bled_today
        self.connection = mock.MagicMock()
        self._last = ""

    def execute(self, sql, params=None):
        self.sql.append((sql, params)); self._last = sql

    def fetchone(self):
        return (self.bled_today,)

    def fetchall(self):
        return [(41,), (42,)] if "RETURNING id" in self._last else []


def _run(cur, g, **kw):
    sent = []
    with mock.patch.object(bo, "gather", return_value=g), \
         mock.patch.object(bo, "in_window", return_value=True), \
         mock.patch("nova_turning_point.decide", return_value={"allowed": True}):
        r = bo.run(cur, post=sent.append, **kw)
    return r, sent


BIG = _g(unanswered_jordan=(3, 1.0, ["dns?"]), chronic_jobs=(6, 0, ["journal_essay (x5)"]),
         pending_proposals=(4, 3.0, ["#121"]), repeated_alerts=(30, 0, ["stale-code x47"]))


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized_and_bleed_passes_annie_rule(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        import nova_annie_rule
        _, comps, top = bo.pressure(BIG)
        self.assertTrue(nova_annie_rule.ok(bo.compose_bleed(150, top, [1])))


class TestPerformance(unittest.TestCase):
    def test_pressure_is_cheap(self):
        t = time.monotonic()
        for _ in range(5000):
            bo.pressure(BIG)
        self.assertLess(time.monotonic() - t, 2.0)


class TestRetry(unittest.TestCase):
    def test_one_bad_source_does_not_stop_the_gauge(self):
        cur = mock.MagicMock()
        cur.execute.side_effect = RuntimeError("relation does not exist")
        g = bo.gather(cur)
        self.assertEqual(set(g), set(bo.SOURCES))
        self.assertTrue(all(v["n"] == 0 for v in g.values()))

    def test_post_failure_is_not_marked_bled(self):
        def boom(t):
            raise OSError("slack down")
        with mock.patch.object(bo, "gather", return_value=BIG), mock.patch.object(bo, "in_window", return_value=True), \
             mock.patch("nova_turning_point.decide", return_value={"allowed": True}):
            r = bo.run(_Cur(), post=boom)
        self.assertFalse(r["bled"])


class TestUnit(unittest.TestCase):
    def test_contribution_weight_creep_cap(self):
        self.assertEqual(bo.contribution("unanswered_jordan", 1, 0), 8.0)
        self.assertEqual(bo.contribution("unanswered_jordan", 1, 1.0), 16.0)   # creep 1/day
        self.assertEqual(bo.contribution("unanswered_jordan", 99, 0), 80.0)    # cap 10
        self.assertAlmostEqual(bo.contribution("claude_queue", 255, 0), 16.0)  # log-scaled

    def test_state_labels(self):
        self.assertEqual(bo.state_label(10, 100), "ok")
        self.assertEqual(bo.state_label(75, 100), "rising")
        self.assertEqual(bo.state_label(100, 100), "bleed")

    def test_compose_is_concise(self):
        _, _, top = bo.pressure(BIG)
        txt = bo.compose_bleed(150, top, [41])
        self.assertLessEqual(len(txt.splitlines()), 8)
        self.assertIn("Dropping: 1", txt)


class TestIntegration(unittest.TestCase):
    def test_organ_board_view_includes_boiler(self):
        ob = (SCRIPTS / "nova_organ_board.py").read_text()
        self.assertIn("'boiler'", ob)
        self.assertIn("boiler_state", ob)


class TestFunctional(unittest.TestCase):
    def test_bleeds_once_with_drops(self):
        r, sent = _run(_Cur(), BIG)
        self.assertTrue(r["bled"])
        self.assertEqual(len(sent), 1)
        self.assertIn("Dropping: 2", sent[0])

    def test_never_twice_a_day(self):
        r, sent = _run(_Cur(bled_today=1), BIG)
        self.assertFalse(r["bled"])
        self.assertEqual(sent, [])

    def test_quiet_below_threshold(self):
        r, sent = _run(_Cur(), _g(failed_jobs=(2, 0)))
        self.assertEqual(r["state"], "ok")
        self.assertEqual(sent, [])

    def test_dry_run_writes_nothing(self):
        cur = _Cur()
        r, sent = _run(cur, BIG, dry=True)
        self.assertEqual(sent, [])
        self.assertFalse(any(s.lstrip().startswith(("INSERT", "UPDATE")) for s, _ in cur.sql))


class TestFrame(unittest.TestCase):
    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_boiler.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertNotRegex(SRC, r"/Users/[a-z]")


if __name__ == "__main__":
    unittest.main()
