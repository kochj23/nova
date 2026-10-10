#!/usr/bin/env python3
"""Tests for nova_growth.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gr = _load("gr", SCRIPTS / "nova_growth.py")
SRC = (SCRIPTS / "nova_growth.py").read_text()
import nova_soft_certainty as sc   # noqa: E402  (M7: measure_calibration reads soft certainty's pass)

_REAL_SCORED, _REAL_READ = sc.scored_rows, sc.read_calibration
_PATCHES = []


def _padded_scored(oc, since=None):
    # the fixtures below hand (confidence, outcome) pairs; scored_rows yields 5-tuples
    return [tuple(r) + (None,) * (5 - len(r)) for r in _REAL_SCORED(oc, since=since)]


def setUpModule():
    # Most fixtures model "no fresh soft_certainty_state row" (the live path), which is
    # what the queue-ordered _Cur answers; TestIntegration covers the stored-row path.
    for name, fn in (("scored_rows", _padded_scored),
                     ("read_calibration", lambda oc, high_surprise=sc.HIGH_SURPRISE:
                      (sc.calibration_detail(sc.scored_rows(oc), high_surprise), "live"))):
        p = patch.object(sc, name, fn); p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


class _Cur:
    """fetchone()/fetchall() answer from one queue in call order; every execute is recorded."""
    def __init__(self, answers=(), fail=None):
        self.answers = list(answers); self.sql = []; self.params = []; self.fail = fail

    def execute(self, sql, params=None):
        if self.fail and (self.fail is True or self.fail in sql):
            raise RuntimeError("db down")
        self.sql.append(" ".join(sql.split())); self.params.append(params)

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        return self.answers.pop(0) if self.answers else []

    def executed(self, frag):
        return [s for s in self.sql if frag in s]


class _Conn:
    def __init__(self, cur): self.cur = cur; self.autocommit = False; self.closed = False

    def cursor(self): return self.cur

    def close(self): self.closed = True


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()

    def read(self): return self._d

    def __enter__(self): return self

    def __exit__(self, *a): return False


def _urlopen(fn):
    import urllib.request
    return patch.object(urllib.request, "urlopen", fn)


def _overconfident_rows(n=12):
    # confidence 0.9 everywhere, right only a third of the time -> calibration error ~0.57
    return [(0.9, "correct" if i % 3 == 0 else "incorrect") for i in range(n)]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_values_are_parameterized(self):
        self.assertNotIn('execute(f"', SRC)
        # the only %-formatting into SQL is the REVIEW_DAYS constant (an int literal in this module), never data
        for args in re.findall(r"\)\s*%\s*\(([^)]*)\)", SRC):
            self.assertTrue(set(a.strip() for a in args.split(",") if a.strip()) <= {'"%s"', "REVIEW_DAYS"}, args)
        self.assertRegex(SRC, r"% REVIEW_DAYS, \(cid,\)")
        self.assertIsInstance(gr.REVIEW_DAYS, int)

    def test_writes_only_its_own_growth_tables(self):
        writes = set(re.findall(r"\b(?:INSERT INTO|UPDATE)\s+([\w.]+)", SRC))
        self.assertEqual(writes, {"growth_commitments", "growth_reviews"})
        self.assertNotIn("DELETE FROM", SRC)
        # every UPDATE is scoped to one row (the statement may span concatenated literals)
        for m in re.finditer(r"UPDATE growth_commitments", SRC):
            self.assertIn("WHERE id", SRC[m.start():m.start() + 200])

    def test_growth_memories_are_private(self):
        self.assertEqual(SRC.count('"privacy": "private"'), 2)        # commitment + review memories


class TestPerformance(unittest.TestCase):
    def test_measure_and_judge_fast_on_10k(self):
        rows = [((i % 10) / 10 + 0.05, ("correct", "incorrect", "partial")[i % 3]) for i in range(10_000)]
        t0 = time.perf_counter()
        m = gr.measure_calibration(_Cur([rows]))
        for i in range(10_000):
            gr.judge(i / 10_000, 0.5, 0.3, bool(i % 2))
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(m["n"], 10_000)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_nodes_until_one_answers(self):
        calls = []

        def flaky(req, timeout=0):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("node down")
            return _Resp({"message": {"content": "  I will hedge.  "}})
        with _urlopen(flaky):
            self.assertEqual(gr.llm("p"), "I will hedge.")
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls, [n + "/api/chat" for n in gr.OLLAMA_NODES[:3]])

    def test_llm_all_nodes_down_fails_open_to_a_template(self):
        with _urlopen(lambda *a, **k: (_ for _ in ()).throw(OSError("down"))):
            self.assertEqual(gr.llm("p"), "")
            txt = gr.phrase_commitment({"weakness": "w", "metric": "m", "baseline": {}, "target": "< 0.1"})
        self.assertIn("I commit to a concrete change targeting < 0.1", txt)

    def test_remember_has_no_retry_but_assess_still_saves_the_row(self):
        # RETRY GAP: remember() — one POST; its failure is logged and the commitment row is kept
        attempts = []

        def boom(*a, **k):
            attempts.append(1); raise OSError("memory server down")
        cur = _Cur([_overconfident_rows(), None, None, [], (None,), (0,), (7, NOW)])
        buf = io.StringIO()
        with _urlopen(boom), redirect_stdout(buf):
            cid = gr.do_assess(cur)
        self.assertEqual(cid, 7)
        self.assertEqual(len(cur.executed("INSERT INTO growth_commitments")), 1)
        self.assertIn("commitment memory write failed (row still saved)", buf.getvalue())
        self.assertEqual(len(attempts), 1 + len(gr.OLLAMA_NODES))      # llm failover + the single remember

    def test_accessor_fails_open_when_pg_is_down(self):
        # RETRY GAP: current_growth_focus()/psycopg2.connect — one attempt, '' on failure
        real = gr.psycopg2
        gr.psycopg2 = types.SimpleNamespace(connect=lambda *a, **k: (_ for _ in ()).throw(OSError("pg down")))
        try:
            self.assertEqual(gr.current_growth_focus(), "")
        finally:
            gr.psycopg2 = real


class TestUnit(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(gr._one_line("  a \n b  ", 3), "a b")
        self.assertEqual(gr._one_line(None), "")
        self.assertEqual(gr._num_from("< 0.30"), 0.3)
        self.assertEqual(gr._num_from("> -2.5"), -2.5)
        self.assertIsNone(gr._num_from(""))
        self.assertEqual(gr._num_from(None, default=1.0), 1.0)

    def test_measure_calibration(self):
        self.assertIsNone(gr.measure_calibration(_Cur([[]])))
        m = gr.measure_calibration(_Cur([[(0.8, "correct"), (0.8, "incorrect"), (0.8, "partial")]]))
        self.assertEqual(m["n"], 3)
        self.assertEqual(m["hit_rate"], 0.5)
        self.assertEqual(m["gap"], -0.3)
        self.assertEqual(m["calib_error"], 0.3)
        cur = _Cur([[(0.5, "correct")]])
        gr.measure_calibration(cur, since="2026-10-01")
        self.assertIn("resolved_at > %s", cur.sql[0])
        self.assertEqual(cur.params[0], ["2026-10-01"])

    def test_judge(self):
        self.assertEqual(gr.judge(None, 0.5, 0.3, True), "inconclusive")
        self.assertEqual(gr.judge(0.2, 0.5, 0.3, True), "succeeded")      # hit target
        self.assertEqual(gr.judge(0.4, 0.5, 0.3, True), "succeeded")      # better than baseline by > delta
        self.assertEqual(gr.judge(0.49, 0.5, 0.3, True), "failed")        # inside the noise band
        self.assertEqual(gr.judge(0.9, 0.5, 0.8, False), "succeeded")
        self.assertEqual(gr.judge(0.4, 0.5, None, False), "failed")

    def test_detectors_edges(self):
        self.assertIsNone(gr.detect_calibration(_Cur([[(0.9, "correct")] * 2])))              # below MIN_SAMPLE
        self.assertIsNone(gr.detect_calibration(_Cur([[(0.5, "correct"), (0.5, "incorrect")] * 3])))   # well calibrated
        c = gr.detect_calibration(_Cur([_overconfident_rows(), (3, 0.57, NOW)]))
        self.assertIn("overconfident", c["weakness"])
        self.assertEqual(c["evidence"]["scoreboard"]["scoreboard_id"], 3)
        self.assertEqual(c["metric_spec"]["kind"], "prediction_calibration")
        self.assertIsNone(gr.detect_incident_recurrence(_Cur([None])))
        self.assertIsNone(gr.detect_incident_recurrence(_Cur(fail=True)))
        r = gr.detect_incident_recurrence(_Cur([("gpu wedge #", 6)]))
        self.assertEqual(r["baseline"], {"count_30d": 6, "per_day": 0.2})
        self.assertEqual(r["target_num"], 0.1)
        s = gr.detect_scoreboard_regression(_Cur([[("surprise_rate",), ("recall_hit",)],
                                                  [(2, 0.4, NOW), (1, 0.2, NOW)],      # lower-better got worse
                                                  [(4, 0.9, NOW), (3, 0.8, NOW)]]))    # higher-better improved
        self.assertEqual(s["evidence"]["metric"], "surprise_rate")
        self.assertTrue(s["metric_spec"]["lower_better"])
        self.assertIsNone(gr.detect_scoreboard_regression(_Cur([[("x",)], [(1, 0.5, NOW)]])))   # one reading
        self.assertIsNone(gr.detect_autonomy_progress(_Cur([(None,)])))                  # table absent
        self.assertIsNone(gr.detect_autonomy_progress(_Cur([("autonomy_trust",), []])))   # nothing waiting
        self.assertIsNone(gr.detect_autonomy_progress(_Cur([("autonomy_trust",), [("restart", 4)],
                                                            [(0.5, "correct"), (0.5, "incorrect")] * 3])))   # under the gate
        a = gr.detect_autonomy_progress(_Cur([("autonomy_trust",), [("restart", 4)], _overconfident_rows()]))
        self.assertEqual(a["target_num"], gr.GATE_CALIB)
        self.assertGreater(a["priority"], 0.5)

    def test_remeasure_kinds(self):
        self.assertEqual(gr.remeasure(_Cur(), {"kind": "nope"}, {}, NOW)[1], None)
        self.assertEqual(gr.remeasure(_Cur(), None, {}, NOW)[0]["note"], "unknown metric_spec kind 'None'")
        r, cur_v, lb = gr.remeasure(_Cur([[(0.9, "correct")]]), {"kind": "prediction_calibration", "since": "created_at"}, {}, NOW)
        self.assertIsNone(cur_v); self.assertEqual(r["n"], 1)
        r, cur_v, lb = gr.remeasure(_Cur([(4, 8.0)]), {"kind": "incident_rate", "pattern": "p", "since": "created_at"}, {}, NOW)
        self.assertEqual((cur_v, lb), (0.5, True))
        r, cur_v, lb = gr.remeasure(_Cur([None]), {"kind": "scoreboard_metric", "metric": "m", "lower_better": False}, {}, NOW)
        self.assertEqual((cur_v, lb), (None, False))
        r, cur_v, lb = gr.remeasure(_Cur([(9, 0.25, NOW)]), {"kind": "scoreboard_metric", "metric": "m"}, {}, NOW)
        self.assertEqual((r["scoreboard_id"], cur_v), (9, 0.25))


class TestIntegration(unittest.TestCase):
    def test_whole_table_calibration_comes_from_soft_certainty_state(self):
        # M7: a fresh stored row is used as-is (no predictions scan); a stale one is not.
        rows = [(0.8, "correct", 0.04, "self", NOW), (0.8, "incorrect", 0.64, "self", NOW),
                (0.8, "partial", 0.09, "ops", NOW)]
        det = {"fingerprint": sc.fingerprint_of(rows), "report": sc.calibration_detail(rows)}
        with patch.object(sc, "read_calibration", _REAL_READ):
            cur = _Cur([(json.dumps(det),), (3, NOW)])
            m = gr.measure_calibration(cur)
            self.assertEqual(m, {"n": 3, "mean_conf": 0.8, "hit_rate": 0.5, "gap": -0.3,
                                 "abs_gap": 0.3, "calib_error": 0.3})
            self.assertTrue(cur.executed("FROM soft_certainty_state"))
            self.assertFalse(cur.executed("SELECT confidence, outcome"))
            stale = _Cur([(json.dumps(det),), (4, NOW), rows[:2]])        # a 4th resolution landed since
            m2 = gr.measure_calibration(stale)
            self.assertTrue(stale.executed("SELECT confidence, outcome"))
            self.assertEqual(m2["n"], 2)

    def test_measure_matches_the_shared_soft_certainty_math(self):
        rows = _overconfident_rows()
        d = sc.calibration_detail([r + (None, None, None) for r in rows])
        m = gr.measure_calibration(_Cur([rows]))
        self.assertEqual(m["calib_error"], round(d["calib_error"], 3))
        self.assertNotIn("buckets.setdefault", SRC)        # the decile math lives only in soft certainty

    def test_baseline_and_remeasure_are_commensurable(self):
        cand = gr.detect_calibration(_Cur([_overconfident_rows(), None]))
        re_m, cur_v, lb = gr.remeasure(_Cur([_overconfident_rows()]), cand["metric_spec"], cand["baseline"], NOW)
        self.assertEqual(set(re_m), set(cand["baseline"]))
        self.assertEqual(cur_v, cand["baseline"]["calib_error"])
        self.assertEqual(gr.judge(cur_v, cand["baseline"]["calib_error"], cand["target_num"], lb), "failed")   # unchanged = no growth

    def test_review_settles_a_due_commitment_and_writes_the_review_row(self):
        lineage = {"metric_spec": {"kind": "incident_rate", "pattern": "gpu wedge #", "since": "created_at"}, "target_num": 0.1}
        row = (5, "w", "I commit", "m", {"count_30d": 6, "per_day": 0.2}, "< 0.1", NOW, NOW, lineage)
        cur = _Cur([[row], (1, 14.0), (21,)])
        posted = []
        with patch.object(gr, "remember", lambda t, s, m: posted.append((t, s, m))), redirect_stdout(io.StringIO()):
            out = gr.do_review(cur)
        self.assertEqual(out, [(5, "succeeded", 0.071)])
        self.assertEqual(len(cur.executed("INSERT INTO growth_reviews")), 1)
        self.assertIn("SET status=%s, resolved_at=now()", cur.sql[-1])
        self.assertIn("WHERE id=%s", cur.sql[-1])
        self.assertEqual(posted[0][1], "growth")
        self.assertEqual(posted[0][2]["review_id"], 21)

    def test_inconclusive_review_reschedules_instead_of_fabricating(self):
        lineage = {"metric_spec": {"kind": "prediction_calibration", "measure": "calib_error", "since": "created_at"}}
        row = (6, "w", "c", "m", {"calib_error": 0.5}, "< 0.3", NOW, NOW, lineage)
        cur = _Cur([[row], [(0.9, "correct")], (22,)])
        with patch.object(gr, "remember", lambda *a: self.fail("no memory for an inconclusive review")), \
                redirect_stdout(io.StringIO()):
            out = gr.do_review(cur)
        self.assertEqual(out, [(6, "inconclusive", None)])
        self.assertTrue(any("SET review_due = now()" in s for s in cur.sql))
        self.assertFalse(cur.executed("resolved_at=now()"))

    def test_already_committed_keys_on_lineage_metric_spec_kind(self):
        cur = _Cur([(1,)])
        self.assertTrue(gr.already_committed(cur, "incident_rate"))
        self.assertIn("lineage->'metric_spec'->>'kind' = %s", cur.sql[0])
        self.assertEqual(cur.params[0], ("incident_rate",))

    def test_accessor_reads_active_commitments(self):
        cur = _Cur([[("I will hedge.",), ("  I will cite rows. ",)]])
        real = gr.psycopg2
        gr.psycopg2 = types.SimpleNamespace(connect=lambda *a, **k: _Conn(cur))
        try:
            self.assertEqual(gr.current_growth_focus(2), "What I'm working to improve: I will hedge.; I will cite rows.")
        finally:
            gr.psycopg2 = real
        self.assertEqual(cur.params[0], (2,))

    def test_ensure_tables_is_idempotent_ddl(self):
        cur = _Cur()
        gr.ensure_tables(cur)
        self.assertEqual(len(cur.executed("CREATE TABLE IF NOT EXISTS growth_commitments")), 1)
        self.assertEqual(len(cur.executed("CREATE TABLE IF NOT EXISTS growth_reviews")), 1)
        self.assertTrue(all("IF NOT EXISTS" in s for s in cur.sql))


class TestFunctional(unittest.TestCase):
    def _main(self, argv, cur, llm_text="I will state confidence only after checking two sources."):
        posted = []
        real_pg, real_argv = gr.psycopg2, sys.argv
        gr.psycopg2 = types.SimpleNamespace(connect=lambda *a, **k: _Conn(cur)); sys.argv = ["nova_growth.py", *argv]
        buf = io.StringIO()
        try:
            with patch.object(gr, "llm", lambda *a, **k: llm_text), \
                    patch.object(gr, "remember", lambda t, s, m: posted.append((t, s, m)) or 1), redirect_stdout(buf):
                rc = gr.main()
        finally:
            gr.psycopg2, sys.argv = real_pg, real_argv
        return rc, posted, buf.getvalue()

    def test_assess_golden_path_commits_and_remembers(self):
        cur = _Cur([_overconfident_rows(), None, None, [], (None,), (0,), (7, NOW)])
        rc, posted, out = self._main(["--mode", "assess"], cur)
        self.assertEqual(rc, 0)
        ins = cur.executed("INSERT INTO growth_commitments")
        self.assertEqual(len(ins), 1)
        params = cur.params[cur.sql.index(ins[0])]
        self.assertEqual(params[2], "I will state confidence only after checking two sources.")
        self.assertEqual(json.loads(params[6])["metric_spec"]["kind"], "prediction_calibration")
        self.assertEqual(posted[0][1], "growth")
        self.assertEqual(posted[0][2]["type"], "commitment")
        self.assertIn("commitment #7 [prediction_calibration]", out)

    def test_assess_with_no_weakness_is_an_honest_no_op(self):
        cur = _Cur(fail="FROM")           # every detector read fails (DDL still runs) -> each detector is skipped
        rc, posted, out = self._main(["--mode", "assess"], cur)
        self.assertEqual(rc, 0)
        self.assertEqual(posted, [])
        self.assertIn("no weakness worth committing to this cycle", out)

    def test_report_prints_counts_and_rate(self):
        cur = _Cur([[("active", 2), ("succeeded", 3), ("failed", 1)], [], []])
        rc, posted, out = self._main(["--mode", "report"], cur)
        self.assertEqual(rc, 0)
        self.assertIn("growth rate = 75%", out)
        self.assertIn("active=2  succeeded=3  failed=1", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_growth.py"), "--help"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--mode {assess,review,report}", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_growth"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("[growth", r.stdout)


if __name__ == "__main__":
    unittest.main()
