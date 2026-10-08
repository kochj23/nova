#!/usr/bin/env python3
"""7-category supplement for nova_turning_point.py (the intervention ladder + weekly budget).
Covers what tests/test_nova_turning_point.py leaves thin: retry/backoff on the budget read and the
restraint-ledger write, fail-OPEN when the ledger stays unreadable, input clamping, the real
nova_restraint write path through a fake connection, and status(). Offline; no PG, no Slack.
Written by Jordan Koch (via Claude)."""
import contextlib
import io
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_turning_point as tp  # noqa: E402

SRC = (SCRIPTS / "nova_turning_point.py").read_text()


class _Cur:
    """spent_this_week fake; `fail` = transient errors before the budget read succeeds."""
    def __init__(self, spent=0, fail=0, rows=()):
        self.spent, self.fail, self.rows, self.sql = spent, fail, list(rows), []
        self.connection = mock.MagicMock()
        self._last = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if "restraint_ledger" in sql and "sum(" in sql and self.fail:
            self.fail -= 1
            raise RuntimeError("SSL connection has been closed unexpectedly")
        self._last = sql

    def fetchone(self):
        return (self.spent,) if "sum(" in (self._last or "") else (1,)

    def fetchall(self):
        return self.rows


def _decide(cur, rr=None, **kw):
    rr = rr or mock.MagicMock(return_value=1)
    with mock.patch.object(tp, "calibrated", side_effect=lambda oc, s, d: s), \
         mock.patch.object(tp, "weekly_budget", return_value=40), \
         mock.patch("nova_restraint.record_restraint", rr), \
         mock.patch.object(tp.time, "sleep") as sl, \
         contextlib.redirect_stderr(io.StringIO()):
        r = tp.decide(cur, kw.pop("kind", "reach"), text=kw.pop("text", "hello"), **kw)
    return r, rr, sl


class TestSecurity(unittest.TestCase):
    def test_all_sql_parameterised(self):
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')
        self.assertNotRegex(SRC, r"execute\([^)]*%\s*\(")
        cur = _Cur()
        tp.spent_this_week(cur)
        self.assertEqual(cur.sql[-1][1], (tp.CHANNEL,))

    def test_hostile_kind_and_text_only_travel_as_data(self):
        cur = _Cur()
        r, rr, _ = _decide(cur, kind="x'; DROP TABLE restraint_ledger;--", text="t" * 50000, stakes=0.5)
        self.assertFalse([s for s, _ in cur.sql if "DROP" in s])
        kw = rr.call_args.kwargs
        self.assertLessEqual(len(kw["would_have_said"]), 4000)       # bounded ledger payload
        self.assertEqual(kw["detail"]["turning_point"]["kind"], "x'; DROP TABLE restraint_ledger;--")

    def test_journal_rung_never_allows_an_intervention(self):
        r, _, _ = _decide(_Cur(), stakes=0.1, ceiling="act", force=True)
        self.assertFalse(r["allowed"])
        self.assertEqual(r["cost"], 0)


class TestPerformance(unittest.TestCase):
    def test_one_budget_query_per_decision(self):
        cur = _Cur(spent=3)
        _decide(cur, stakes=0.7, ceiling="recommend")
        # one budget query, plus (since 2026-10-08) at most one fatigue read (quiet_mode)
        self.assertEqual(len([s for s, _ in cur.sql if "restraint_ledger" in s]), 1)
        self.assertLessEqual(len(cur.sql), 2)

    def test_1k_decisions_fast(self):
        t = time.monotonic()
        for i in range(1000):
            _decide(_Cur(spent=i % 40), stakes=(i % 100) / 100, ceiling="act")
        self.assertLess(time.monotonic() - t, 5.0)


class TestRetry(unittest.TestCase):
    def test_budget_read_retries_then_succeeds(self):
        cur = _Cur(spent=5, fail=2)
        r, _, sl = _decide(cur, stakes=0.6, ceiling="mention")
        self.assertTrue(r["allowed"])
        self.assertEqual(r["spent"], 5)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [0.25, 0.5])
        self.assertEqual(cur.connection.rollback.call_count, 2)

    def test_budget_unreadable_after_three_fails_open(self):
        cur = _Cur(fail=99)
        r, rr, sl = _decide(cur, stakes=0.6, ceiling="mention")
        self.assertTrue(r["allowed"])
        self.assertIsNone(r["spent"])
        self.assertIn("failing open", r["reason"])
        self.assertEqual(len([1 for s, _ in cur.sql if "sum(" in s]), 3)
        rr.assert_not_called()

    def test_ledger_write_retries_with_backoff(self):
        rr = mock.MagicMock(side_effect=[RuntimeError("pg"), RuntimeError("pg"), 7])
        r, rr, sl = _decide(_Cur(), rr=rr, stakes=0.6, ceiling="mention")
        self.assertEqual(rr.call_count, 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [0.25, 0.5])
        self.assertTrue(r["allowed"])

    def test_ledger_write_final_failure_is_logged_not_raised(self):
        err = io.StringIO()
        rr = mock.MagicMock(side_effect=RuntimeError("pg gone"))
        with mock.patch.object(tp, "calibrated", side_effect=lambda oc, s, d: s), \
             mock.patch.object(tp, "weekly_budget", return_value=40), \
             mock.patch("nova_restraint.record_restraint", rr), mock.patch.object(tp.time, "sleep"), \
             contextlib.redirect_stderr(err):
            r = tp.decide(_Cur(), "reach", 0.6, "hi")
        self.assertEqual(rr.call_count, 3)
        self.assertIn("ledger write failed", err.getvalue())
        self.assertTrue(r["allowed"])

    def test_main_block_retries_connect(self):
        self.assertRegex(SRC, r"for _a in range\(3\):\s+try:\s+c = psycopg2\.connect")


class TestUnit(unittest.TestCase):
    def test_spinnaker_caps_uncorroborated_to_journal(self):
        r, _, _ = _decide(_Cur(), stakes=0.95, ceiling="act", item={"sources": [{"id": "nova:reasoning"}]})
        self.assertFalse(r["allowed"])
        self.assertIn("SPINNAKER", r["reason"])
        r2, _, _ = _decide(_Cur(), stakes=0.95, ceiling="act",
                           item={"sources": [{"id": "camera:a"}, {"id": "camera:b"}]})
        self.assertEqual(r2["wanted"], "ask")          # one NVR = one source = ask at most

    def test_depleted_jordan_defers_non_urgent(self):
        with mock.patch.object(tp, "jordan_depleted", return_value="hard-stretch quiet mode"):
            r, _, _ = _decide(_Cur(), stakes=0.5, ceiling="mention")
            self.assertFalse(r["allowed"])
            self.assertIn("depleted", r["reason"])
            r2, _, _ = _decide(_Cur(), stakes=0.95, ceiling="mention")
            self.assertTrue(r2["allowed"])

    def test_stakes_are_clamped(self):
        self.assertEqual(_decide(_Cur(), stakes=-3)[0]["stakes"], 0.0)
        self.assertEqual(_decide(_Cur(), stakes=7, ceiling="act")[0]["stakes"], 1.0)
        self.assertEqual(_decide(_Cur(), stakes=None)[0]["stakes"], 0.0)

    def test_held_costs_nothing_and_reports_wanted(self):
        r, _, _ = _decide(_Cur(spent=40), stakes=0.7, ceiling="recommend")
        self.assertFalse(r["allowed"])
        self.assertEqual((r["rung"], r["wanted"], r["cost"]), ("journal", "recommend", 0))

    def test_weekly_budget_defaults_to_40_without_voice(self):
        with mock.patch.dict(sys.modules, {"nova_voice": None}):
            self.assertEqual(tp.weekly_budget(), 40)

    def test_calibrated_none_without_soft_certainty(self):
        with mock.patch.dict(sys.modules, {"nova_soft_certainty": None}):
            self.assertIsNone(tp.calibrated(object(), 0.5, "x"))


class TestIntegration(unittest.TestCase):
    def test_real_record_restraint_writes_through_callers_connection(self):
        import nova_restraint
        ins = mock.MagicMock()
        ins.fetchone.return_value = (11,)
        cur = _Cur(spent=2)
        cur.connection.cursor.return_value = ins
        with mock.patch.object(tp, "calibrated", side_effect=lambda oc, s, d: s), \
             mock.patch.object(tp, "weekly_budget", return_value=40), \
             mock.patch.object(nova_restraint, "_conn", side_effect=AssertionError("must reuse caller conn")):
            r = tp.decide(cur, "reach", 0.6, "a note to him", ceiling="mention")
        self.assertTrue(r["allowed"])
        sql, params = ins.execute.call_args.args
        self.assertIn("INSERT INTO restraint_ledger", sql)
        self.assertEqual(params[3], "turning-point")
        self.assertIn('"spent": true', params[4])


class TestFunctional(unittest.TestCase):
    def test_golden_week_spends_until_late_budget_then_holds(self):
        allowed = [_decide(_Cur(spent=s), stakes=0.5, ceiling="mention")[0]["allowed"] for s in (0, 20, 30, 31, 39, 40)]
        self.assertEqual(allowed, [True, True, True, False, False, False])

    def test_error_path_ledger_and_calibration_both_down(self):
        cur = _Cur(fail=99)
        with mock.patch.dict(sys.modules, {"nova_soft_certainty": None, "nova_restraint": None}), \
             mock.patch.object(tp.time, "sleep"), contextlib.redirect_stderr(io.StringIO()):
            r = tp.decide(cur, "notify", 0.5, "bundle", ceiling="mention")
        self.assertTrue(r["allowed"])  # never silences a real alert because the ledger is down

    def test_status_reports_by_kind(self):
        cur = _Cur(spent=9, rows=[("reach", 3, 1), ("bleed", 1, 0)])
        with mock.patch.object(tp, "weekly_budget", return_value=40):
            s = tp.status(cur)
        self.assertEqual(s, {"budget": 40, "spent": 9,
                             "by_kind": {"reach": {"spent": 3, "held": 1}, "bleed": {"spent": 1, "held": 0}}})


class TestFrame(unittest.TestCase):
    def test_import_smoke(self):
        r = subprocess.run([sys.executable, "-c", "import nova_turning_point as t; print(t.ladder())"], cwd=SCRIPTS,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertIn("journal", r.stdout)


if __name__ == "__main__":
    unittest.main()
