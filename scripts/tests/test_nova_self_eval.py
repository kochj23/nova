#!/usr/bin/env python3
"""Tests for nova_self_eval.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_self_eval.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


se = _load("self_eval_under_test", SCRIPT)


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Cur:
    def __init__(self, routes=None, one=None):
        self.routes, self.one = routes or {}, one or {}
        self.sql, self.params, self._last = [], [], ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _route(self, table, default):
        for k, v in table.items():
            if k in self._last:
                return v
        return default

    def fetchall(self):
        return self._route(self.routes, [])

    def fetchone(self):
        return self._route(self.one, None)

    def writes(self, needle):
        return [p for s, p in zip(self.sql, self.params) if needle in s]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur

    def close(self):
        pass


FIZZLE = {"kind": "fizzle_rate", "window_days": 14, "direction": "lower"}
DUE_ROW = (1, "Follow-through", "Am I finishing what catches me?", FIZZLE, "< 0.30", "daily", None, None)
PREDS = [(0.9, "correct"), (0.9, "incorrect"), (0.5, "partial"), (0.2, "incorrect")]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_window_is_int_cast_before_sql(self):
        mc = _Cur()
        with mock.patch.object(se, "_mem_cursor", return_value=(None, mc)), redirect_stdout(StringIO()):
            value, detail = se.compute_metric({"kind": "fizzle_rate", "window_days": "14; DROP TABLE memories"}, _Cur())
        self.assertIsNone(value); self.assertIn("measurement error", detail["note"]); self.assertEqual(mc.sql, [])

    def test_topic_is_a_bound_parameter(self):
        mc = _Cur(one={"metadata->>'topic'=%s": (3,)})
        with mock.patch.object(se, "_mem_cursor", return_value=(None, mc)):
            se.measure_topic_engagement({"topic": "x'; DROP", "window_days": 21}, _Cur())
        self.assertEqual(mc.params[-1], ("x'; DROP",)); self.assertNotIn("DROP", mc.sql[-1])
        self.assertNotIn('execute(f"', SRC)

    def test_metric_menu_is_computable_by_construction(self):
        oc = _Cur(routes={"FROM preoccupations": [("horology",)], "FROM predictions": PREDS})
        for c in se._candidate_tests(oc):
            self.assertIn(c["metric_spec"]["kind"], se.MEASURERS)


class TestPerformance(unittest.TestCase):
    def test_verdicts_and_calibration_fast_on_10k(self):
        rows = [((i % 10) / 10 + 0.05, ("correct", "incorrect", "partial")[i % 3]) for i in range(10_000)]
        oc = _Cur(routes={"FROM predictions": rows})
        t0 = time.perf_counter()
        for i in range(10_000):
            se.verdict_for(i / 10_000, 0.5, 0.3, "lower")
        val, detail = se.measure_calibration({}, oc)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(detail["n"], 10_000); self.assertIsNotNone(val)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_nodes(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("down")
            return _Resp({"message": {"content": "ok"}})
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertEqual(se.llm("x"), "ok")
        self.assertEqual(len(calls), 3)

    def test_remember_no_retry_but_run_still_records(self):
        # RETRY GAP: remember — one POST; do_run wraps it so the runs row is still saved.
        oc = _Cur(routes={"LEFT JOIN LATERAL": [DUE_ROW]}, one={"INSERT INTO self_eval_runs": (7,)})
        mc = _Cur(one={"FILTER": (1, 4)})
        with mock.patch.object(se, "_mem_cursor", return_value=(None, mc)), mock.patch.object(se, "llm", return_value=""), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("down")), redirect_stdout(StringIO()):
            ran = se.do_run(oc)
        self.assertEqual(ran, [(1, "improving", 0.2)])
        self.assertEqual(len(oc.writes("INSERT INTO self_eval_runs")), 1)

    def test_measurement_and_accessor_fail_open(self):
        with mock.patch.object(se, "_mem_cursor", side_effect=OSError("mem pg down")), redirect_stdout(StringIO()):
            self.assertIsNone(se.compute_metric(FIZZLE, _Cur())[0])
        with mock.patch("psycopg2.connect", side_effect=OSError("down")):
            self.assertEqual(se.current_self_eval(), "")


class TestUnit(unittest.TestCase):
    def test_verdict_for(self):
        self.assertEqual(se.verdict_for(None, 1, 1, "lower"), "inconclusive")
        self.assertEqual(se.verdict_for(0.20, 0.30, None, "lower"), "improving")
        self.assertEqual(se.verdict_for(0.40, 0.30, None, "lower"), "regressing")
        self.assertEqual(se.verdict_for(0.301, 0.30, None, "lower"), "flat")
        self.assertEqual(se.verdict_for(3.0, 2.0, None, "higher"), "improving")
        self.assertEqual(se.verdict_for(0.5, None, None, "lower"), "flat")
        self.assertEqual(se.verdict_for(0.2, None, 0.3, "lower"), "improving")
        self.assertEqual(se.verdict_for(0.5, None, 0.3, "lower"), "regressing")
        self.assertEqual(se.verdict_for(0.3, None, 0.3, "lower"), "flat")

    def test_small_helpers(self):
        self.assertEqual(se._target_num("< 0.30"), 0.3); self.assertEqual(se._target_num(">= 2"), 2.0)
        self.assertIsNone(se._target_num("none"))
        self.assertEqual(se._extract_json('x {"a": 1} y'), '{"a": 1}'); self.assertEqual(se._one_line(" a  b ", 2), "a")
        self.assertTrue(se._is_dup({"metric_spec": FIZZLE}, [{"kind": "fizzle_rate"}]))
        self.assertFalse(se._is_dup({"metric_spec": {"kind": "topic_engagement", "topic": "a"}}, [{"kind": "topic_engagement", "topic": "b"}]))

    def test_phrase_test_and_note_degrade_to_templates(self):
        cand = {"concern": "my follow-through", "metric_spec": {"how": "h"}, "target": "< 0.3"}
        with mock.patch.object(se, "llm", return_value=""):
            self.assertEqual(se.phrase_test(cand), ("my follow-through", "Am I improving at my follow-through?"))
            self.assertIn("value 0.2", se.phrase_note("n", "Q?", 0.2, "flat", "< 0.3", {}))
        with mock.patch.object(se, "llm", return_value='{"name": "Finishing", "question": "Do I finish?"}'):
            self.assertEqual(se.phrase_test(cand), ("Finishing", "Do I finish?"))

    def test_measure_calibration_and_fizzle(self):
        self.assertEqual(se.measure_calibration({}, _Cur(routes={"FROM predictions": PREDS[:2]}))[0], None)
        val, d = se.measure_calibration({}, _Cur(routes={"FROM predictions": PREDS}))
        self.assertEqual(d["n"], 4); self.assertEqual(d["hit_rate"], 0.375)
        with mock.patch.object(se, "_mem_cursor", return_value=(None, _Cur(one={"FILTER": (0, 0)}))):
            self.assertIsNone(se.measure_fizzle_rate({}, _Cur())[0])


class TestIntegration(unittest.TestCase):
    def test_design_skips_active_kinds_and_inserts_next(self):
        oc = _Cur(routes={"FROM self_eval_tests WHERE status='active'": [({"kind": "fizzle_rate"},)],
                          "FROM preoccupations": [("horology",)], "FROM predictions": []},
                  one={"INSERT INTO self_eval_tests": (3,)})
        with mock.patch.object(se, "llm", return_value=""), mock.patch.object(se, "remember", return_value="m") as rem, \
             mock.patch.object(se, "_stamp", return_value={}), redirect_stdout(StringIO()):
            self.assertEqual(se.do_design(oc), 3)
        ins = oc.writes("INSERT INTO self_eval_tests")[0]
        self.assertEqual(json.loads(ins[2])["kind"], "topic_engagement"); self.assertEqual(ins[4], "weekly")
        self.assertEqual(json.loads(ins[5])["trigger"], "design")
        self.assertEqual(rem.call_args[0][1], "self_eval")

    def test_due_tests_respect_cadence(self):
        rows = [DUE_ROW, DUE_ROW[:6] + (0.2, datetime(2026, 10, 5))]
        oc = _Cur(routes={"LEFT JOIN LATERAL": rows}, one={"::interval * 0.9": (False,)})
        self.assertEqual(len(se._due_tests(oc)), 1)
        self.assertEqual(oc.params[-1], (datetime(2026, 10, 5), "1 day"))

    def test_run_chain_compute_verdict_record(self):
        oc = _Cur(routes={"LEFT JOIN LATERAL": [DUE_ROW[:6] + (0.5, datetime(2026, 10, 4))]},
                  one={"::interval * 0.9": (True,), "INSERT INTO self_eval_runs": (8,)})
        with mock.patch.object(se, "_mem_cursor", return_value=(None, _Cur(one={"FILTER": (2, 2)}))), \
             mock.patch.object(se, "llm", return_value="I am finishing half."), mock.patch.object(se, "remember") as rem, \
             redirect_stdout(StringIO()):
            self.assertEqual(se.do_run(oc), [(1, "flat", 0.5)])
        self.assertEqual(oc.writes("INSERT INTO self_eval_runs")[0], (1, 0.5, "flat", "I am finishing half."))
        self.assertEqual(rem.call_count, 0)  # flat is not notable


class TestFunctional(unittest.TestCase):
    def test_main_run_golden_path(self):
        oc = _Cur(routes={"LEFT JOIN LATERAL": [DUE_ROW]}, one={"INSERT INTO self_eval_runs": (7,)})
        with mock.patch("psycopg2.connect", return_value=_Conn(oc)), mock.patch.object(se, "_mem_cursor", return_value=(None, _Cur(one={"FILTER": (1, 4)}))), \
             mock.patch.object(se, "llm", return_value="I finish four in five."), mock.patch.object(se, "remember", return_value="m") as rem, \
             mock.patch.object(se, "_stamp", return_value={}), mock.patch.object(sys, "argv", ["nova_self_eval.py", "--mode", "run"]), \
             redirect_stdout(StringIO()) as out:
            self.assertEqual(se.main(), 0)
        self.assertEqual(oc.writes("INSERT INTO self_eval_runs")[0], (1, 0.2, "improving", "I finish four in five."))
        self.assertEqual(rem.call_args[0][2]["verdict"], "improving")
        self.assertIn("recorded 1 self-test run(s)", out.getvalue())
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS self_eval_tests" in s for s in oc.sql))

    def test_main_report_and_inconclusive_run(self):
        oc = _Cur(routes={"GROUP BY status": [("active", 1)],
                          "LEFT JOIN LATERAL": [(1, "n", "q", "< 0.3", "daily", FIZZLE, 0.2, "improving", datetime(2026, 10, 5), "note")]})
        with mock.patch("psycopg2.connect", return_value=_Conn(oc)), mock.patch.object(sys, "argv", ["x", "--mode", "report"]), \
             redirect_stdout(StringIO()) as out:
            self.assertEqual(se.main(), 0)
        self.assertIn("latest: improving  value=0.2", out.getvalue())
        oc = _Cur(routes={"LEFT JOIN LATERAL": [DUE_ROW]}, one={"INSERT INTO self_eval_runs": (9,)})
        with mock.patch("psycopg2.connect", return_value=_Conn(oc)), mock.patch.object(se, "_mem_cursor", side_effect=OSError("down")), \
             mock.patch.object(se, "llm", return_value=""), mock.patch.object(se, "remember") as rem, \
             mock.patch.object(sys, "argv", ["x", "--mode", "run"]), redirect_stdout(StringIO()):
            self.assertEqual(se.main(), 0)
        self.assertEqual(oc.writes("INSERT INTO self_eval_runs")[0][:3], (1, None, "inconclusive")); self.assertEqual(rem.call_count, 0)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_without_pg(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("design", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch("psycopg2.connect", side_effect=AssertionError("main ran on import")):
            self.assertTrue(callable(_load("self_eval_import_probe", SCRIPT).main))


if __name__ == "__main__":
    unittest.main()
