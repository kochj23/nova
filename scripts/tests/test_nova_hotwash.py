#!/usr/bin/env python3
"""Tests for nova_hotwash.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_hotwash.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hw = _load("hotwash_under_test", SCRIPT)
hw.log = lambda m: None
SRC = SCRIPT.read_text()
T0 = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)


class _Cur:
    """Answers by SQL substring; records everything executed."""

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.sql = []
        self._rows = []
        self.connection = self

    def rollback(self):
        pass

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._rows = []
        for k, v in self.answers.items():
            if k in sql:
                self._rows = list(v(sql, params) if callable(v) else v)
                break

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def executed(self, word):
        return [s for s, _ in self.sql if s.lstrip().upper().startswith(word)]


def _world():
    return _Cur({
        "to_regclass": [("x",)],
        "information_schema.columns": [],
        "decision='page'": [("nova_x.py", date(2026, 10, 8), 12, ["Down", "Down"], ["k1"], T0)],
        "decision IN ('suppress','downgrade')": [("nova_y.py", date(2026, 10, 8), 2, ["Real"], ["suppress"], T0)],
        "channel='guard'": [(384, T0, "physical guard @ scene-runner", "PHYSICAL GUARD: Good Night", "physical",
                             "scene-runner", True)],
        "FROM autonomy_ledger": [(9, T0, "restart nova-foo", "nova-foo", True, False, "no", "actor")],
        "FROM predictions p": [(5, "it rains", "world", 0.8, "it did not.", T0)],
        "SELECT outcome, confidence FROM predictions": [("incorrect", 0.8)] * 6 + [("correct", 0.6)] * 4,
        "INSERT INTO hotwash": [(1,)],
        "FROM source_ledger": [],
    })


class TestSecurity(unittest.TestCase):
    def test_no_credentials_or_home_paths(self):
        self.assertIsNone(re.search(r"(password|secret|token)\s*=\s*['\"][^'\"]+['\"]", SRC, re.I))
        self.assertNotIn("/Users/", SRC)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))
        cur = _Cur({"INSERT INTO hotwash": [(1,)]})
        evil = "x'); DROP TABLE hotwash; --"
        hw.upsert(cur, hw.aar_prediction(1, evil, "self", 0.5, evil))
        sql, params = cur.sql[-1]
        self.assertNotIn("DROP", sql)
        self.assertIn(evil, params[4])

    def test_rollup_never_executes_anything_only_files_gated_proposals(self):
        self.assertNotIn("mode_execute", SRC)
        self.assertIn("file_proposal", SRC)


class TestPerformance(unittest.TestCase):
    def test_plan_rollup_10k_rows_fast(self):
        rows = [dict(hw.aar_false_alarm(f"s{i % 50}", date(2026, 10, i % 7 + 1), 3, ["t"], ["k"]), id=i)
                for i in range(10000)]
        t = time.time()
        plan = hw.plan_rollup(rows)
        self.assertLess(time.time() - t, 1.0)
        self.assertEqual(len(plan), hw.MAX_PROPOSALS)


class TestRetry(unittest.TestCase):
    def test_retry_backoff_fail_twice_then_succeed(self):
        calls, sleeps = [], []

        def flaky():
            calls.append(1)
            if len(calls) < 3:
                raise OSError("down")
            return "ok"
        self.assertEqual(hw._retry(flaky, "x", _sleep=sleeps.append), "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_retry_reraises_after_bound(self):
        with self.assertRaises(OSError):
            hw._retry(lambda: (_ for _ in ()).throw(OSError("x")), "x", _sleep=lambda s: None)

    def test_from_prediction_fails_open(self):
        class Boom(_Cur):
            def execute(self, sql, params=None):
                raise RuntimeError("pg gone")
        self.assertIsNone(hw.from_prediction(Boom(), 1, "s", "self", 0.5, "r"))


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(hw.selftest(), 0)

    def test_four_questions_always_answered(self):
        for r in (hw.aar_false_alarm("s", "d", 1, [], []), hw.aar_missed("s", "d", 1, [], ["suppress"]),
                  hw.aar_guard(1, None, "c", "r", "g", "s"), hw.aar_ledger(1, None, "a", "", True, False, "", "s"),
                  hw.aar_prediction(1, "s", "self", 0.5, ""), hw.aar_escalation(1, None, "s", "alert", "r", "unneeded")):
            for q in ("q1_expected", "q2_actual", "q3_why", "q4_sustain", "q4_fix"):
                self.assertTrue(r[q].strip(), (r["kind"], q))
            self.assertIn(r["kind"], hw.KINDS)

    def test_blameless_no_flattery_or_self_blame(self):
        bad = re.compile(r"\b(stupid|my fault|I failed|great job|brilliant|blame)\b", re.I)
        self.assertIsNone(bad.search(SRC.split("def plan_rollup")[0].split("# ── pure AAR builders")[1]))

    def test_prediction_without_skill_proposes_no_threshold(self):
        r = hw.aar_prediction(1, "s", "ops", 0.6, "", {"n": 30, "base": 0.4, "skill": 0.2})
        self.assertEqual(r["fix_kind"], "none")
        self.assertEqual(hw.plan_rollup([dict(r, id=1), dict(r, id=2)]), [])

    def test_big_single_false_alarm_day_qualifies(self):
        r = dict(hw.aar_false_alarm("s", "d", 10, [], []), id=1)
        self.assertEqual(len(hw.plan_rollup([r])), 1)
        self.assertEqual(hw.plan_rollup([dict(r, weight=3)]), [])

    def test_week_key(self):
        self.assertEqual(hw.week_key(date(2026, 10, 8)), "2026-W41")

    def test_grade_note_cites_cardinal(self):
        n = hw.grade_note({"detector:a": {"reliability": "E", "n": 20, "hit_rate": 0.1,
                                          "compromise_suspect": True, "compromise_reason": "went silent"}}, "detector:a")
        self.assertIn("grades detector:a E", n)
        self.assertIn("went silent", n)
        self.assertIn("grade F", hw.grade_note({}, "detector:z"))


class TestIntegration(unittest.TestCase):
    def test_gather_reads_the_right_tables(self):
        cur = _world()
        rows = hw.gather(cur, 72)
        kinds = sorted(r["kind"] for r in rows)
        self.assertEqual(kinds, ["false_alarm", "missed_event", "overreach", "overreach", "wrong_prediction"])
        joined = " ".join(s for s, _ in cur.sql)
        for t in ("alert_triage_log", "restraint_ledger", "autonomy_ledger", "predictions"):
            self.assertIn(t, joined)
        self.assertFalse(cur.executed("INSERT"))

    def test_uses_soft_certainty_and_cardinal_not_reimplemented(self):
        self.assertIn("sc.domain_brier", SRC)
        self.assertIn("from nova_cardinal import load_ledger", SRC)
        self.assertNotIn("record_outcome(", SRC.split('"""', 2)[2])

    def test_action_audit_adapter_files_unlogged_overreach(self):
        cur = _Cur({"INSERT INTO hotwash": [(42,)]})
        rid = hw.file_hotwash(cur, kind="overreach", ref="action_audit:2026-10-08:big-brother",
                              summary="1847 of 1847 'big-brother' actions had no ledger row")
        self.assertEqual(rid, 42)
        ins = [p for s, p in cur.sql if "INSERT INTO hotwash" in s][0]
        self.assertEqual(ins[0], "overreach")
        self.assertIn("unlogged:big-brother", ins)
        self.assertIsNone(hw.file_hotwash(cur, kind="false_alarm", ref="x", summary="y"))
        audit = (SCRIPTS / "nova_action_audit.py").read_text()
        self.assertIn('"file_hotwash"', audit)

    def test_predictions_do_resolve_calls_hotwash(self):
        p = (SCRIPTS / "nova_predictions.py").read_text()
        body = p[p.index("def do_resolve"):]
        self.assertIn("nova_hotwash.from_prediction(oc, _id, statement, domain, conf, reasoning)", body)


class TestFunctional(unittest.TestCase):
    def test_sweep_writes_idempotent_upserts(self):
        cur = _world()
        rows = hw.sweep(cur, dry=False, hours=72)
        ins = [s for s, _ in cur.sql if "INSERT INTO hotwash" in s]
        self.assertEqual(len(ins), len(rows))
        self.assertTrue(all("ON CONFLICT (ref)" in s for s in ins))

    def test_rollup_files_through_coagency_and_marks_rows(self):
        cur = _Cur({"to_regclass": [("x",)], "service='hotwash'": [("jordan",)],
                    "FROM hotwash WHERE status='open'": [
                        (1, "false_alarm", "source:a", "threshold", 12, {"type": "route_to_digest"}, "fix"),
                        (2, "missed_event", "source:b", "rule", 1, {}, "fix")]})
        filed = []
        with patch.dict(hw.REVIEWERS, {"jordan": lambda c, p: filed.append(p) or {"filed": True, "pid": 77,
                                                                                    "status": "pending_human"}}):
            plan = hw.rollup(cur)
        self.assertEqual(len(plan), 1)
        self.assertEqual(filed[0]["ids"], [1])
        upd = [(s, p) for s, p in cur.sql if s.startswith("UPDATE hotwash SET status=%s")]
        self.assertEqual(upd[0][1][:2], ("proposed", 77))

    def test_rollup_proposal_error_path_keeps_rows_open(self):
        cur = _Cur({"FROM hotwash WHERE status='open'": [
            (1, "false_alarm", "source:a", "threshold", 12, {}, "fix")]})

        def boom(c, p):
            raise RuntimeError("coagency down")
        with patch.dict(hw.REVIEWERS, {"jordan": boom}):
            hw.rollup(cur)
        self.assertFalse([s for s in cur.sql if s[0].startswith("UPDATE hotwash SET status=%s")])

    def test_unknown_reviewer_falls_back_to_jordan(self):
        cur = _Cur({"service='hotwash'": [("claude",)], "FROM hotwash WHERE status='open'": [
            (1, "false_alarm", "source:a", "threshold", 12, {}, "fix")]})
        seen = []
        with patch.dict(hw.REVIEWERS, {"jordan": lambda c, p: seen.append(1) or {"filed": False}}, clear=True):
            hw.rollup(cur)
        self.assertEqual(seen, [1])


class TestFrame(unittest.TestCase):
    def test_help_and_selftest_exit_zero(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        for arg in ("--help", "--selftest"):
            r = subprocess.run([sys.executable, str(SCRIPT), arg], capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_does_not_run_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_hotwash; print('ok')"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
