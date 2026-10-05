#!/usr/bin/env python3
"""Tests for nova_learning.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_learning.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ln = _load("ln", SCRIPT)


class _Cur:
    """Routes each query to a canned answer by SQL substring (first match wins); records every statement."""
    def __init__(self, routes=()):
        self.routes = list(routes); self.sql = []; self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split()); self.sql.append((self._last, params))

    def _hit(self):
        for key, val in self.routes:
            if key in self._last:
                return val
        return None

    def fetchone(self):
        v = self._hit()
        if isinstance(v, list):
            return v[0] if v else None
        return v

    def fetchall(self):
        v = self._hit()
        return list(v) if isinstance(v, list) else ([] if v is None else [v])

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Boom(_Cur):
    def execute(self, sql, params=None):
        raise RuntimeError("relation does not exist")


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


PRED_ROWS = [("weather", 4, 0.8, [1, 2, 3, 4], ["rain by noon", "heat dome lifts"])]
CUR_ROWS = [(10, "Why does the attic sensor lag the porch by an hour?", "home", "excerpt"), (11, "short?", "", "")]
THIN_ROWS = [(5, "horology", 9, 0), (6, "radios", 6, 3)]
PLAN = [{"n": 1, "step": "find the sensor datasheet", "status": "done", "memory_id": 1},
        {"n": 2, "step": "compare lag across a week", "status": "pending", "memory_id": None}]


def _study_cur(plan=PLAN):
    return _Cur([("SELECT id, topic, why, plan, progress FROM learning_agenda", (3, "sensor lag", "it matters", json.dumps(plan), "1/2"))])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        # the one % interpolation only ever injects the integer constant GAP_DEDUP_DAYS, never a value
        self.assertIn('% ("%s", GAP_DEDUP_DAYS)', SRC)
        self.assertIsInstance(ln.GAP_DEDUP_DAYS, int)
        cur = _Cur([("SELECT 1 FROM learning_gaps", None)])
        ln._record_gaps(cur, [{"gap": "x'); DROP TABLE learning_gaps; --", "evidence": {}, "source_kind": "curiosity"}])
        sql, params = cur.ran("INSERT INTO learning_gaps")[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params[0], "x'); DROP TABLE learning_gaps; --")

    def test_writes_only_its_own_tables_and_memories_are_private(self):
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"learning_gaps", "learning_agenda"})
        self.assertNotIn("DELETE FROM", SRC)
        self.assertEqual(SRC.count('"privacy": "private"'), 2)       # every memory write is private


class TestPerformance(unittest.TestCase):
    def test_text_helpers_and_recording_fast_on_10k(self):
        gaps = [{"gap": f"gap {i} " + "x " * 50, "evidence": {"i": i}, "source_kind": "curiosity"} for i in range(10_000)]
        cur = _Cur([("SELECT 1 FROM learning_gaps", None)])
        t0 = time.perf_counter()
        for g in gaps:
            ln._one_line(g["gap"]); ln._extract_json('noise {"a": 1} noise')
        n = ln._record_gaps(cur, gaps)
        self.assertLess(time.perf_counter() - t0, 1.5)
        self.assertEqual(n, 10_000)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_nodes_then_succeeds(self):
        calls = []

        def fake(req, timeout=0):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("node down")
            return _Resp({"message": {"content": "  an answer  "}})
        with patch("urllib.request.urlopen", side_effect=fake):
            self.assertEqual(ln.llm("q"), "an answer")
        self.assertEqual(calls, [n + "/api/chat" for n in ln.OLLAMA_NODES[:3]])

    def test_llm_and_recall_fail_open_when_every_node_is_down(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")) as u:
            self.assertEqual(ln.llm("q"), "")
            self.assertEqual(u.call_count, len(ln.OLLAMA_NODES))
            self.assertEqual(ln.recall("q"), [])

    def test_remember_failure_never_blocks_progress(self):
        # RETRY GAP: remember — a single POST, no retry; do_study catches it and still records the step
        cur = _study_cur()
        with patch.object(ln, "recall", lambda *a, **k: []), patch.object(ln, "_web", lambda *a, **k: []), \
             patch.object(ln, "llm", lambda p, **k: "VERDICT: learning\nstill going" if "VERDICT" in p else "learned a bit"), \
             patch.object(ln, "remember", MagicMock(side_effect=OSError("memory server down"))), redirect_stdout(io.StringIO()):
            ln.do_study(cur)
        sql, params = cur.ran("UPDATE learning_agenda")[0]
        self.assertEqual(params[1:3], ("2/2", "learning"))

    def test_accessor_fails_safe_without_pg(self):
        with patch.object(ln.psycopg2, "connect", side_effect=OSError("no pg")):
            self.assertEqual(ln.current_learning_focus(), "")


class TestUnit(unittest.TestCase):
    def test_one_line_and_extract_json(self):
        self.assertEqual(ln._one_line("  a \n\n b\tc  "), "a b c")
        self.assertEqual(ln._one_line(None), "")
        self.assertEqual(len(ln._one_line("x" * 500, 20)), 20)
        self.assertEqual(ln._extract_json('```json\n{"a": [1, {"b": 2}]}\n```'), '{"a": [1, {"b": 2}]}')
        self.assertEqual(ln._extract_json("no braces"), "no braces")

    def test_verdict_regexes(self):
        self.assertTrue(ln._PASS_RX.search("...\nVERDICT: understand"))
        self.assertTrue(ln._ABANDON_RX.search("verdict:   Abandon"))
        self.assertFalse(ln._PASS_RX.search("VERDICT: learning"))

    def test_gap_detectors_score_and_filter(self):
        preds = ln._gap_from_predictions(_Cur([("FROM predictions", PRED_ROWS)]))
        self.assertEqual(preds[0]["score"], 3.2)                           # n * mean surprise
        self.assertEqual(preds[0]["evidence"]["prediction_ids"], [1, 2, 3, 4])
        cur = ln._gap_from_curiosity(_Cur([("FROM reflection_questions", CUR_ROWS)]))
        self.assertEqual([g["evidence"]["reflection_question_id"] for g in cur], [10])   # 'short?' dropped
        thin = ln._gap_from_thin_coverage(_Cur([("FROM preoccupations p", THIN_ROWS)]))
        self.assertEqual([g["evidence"]["topic"] for g in thin], ["horology"])          # coverage 3 is covered
        self.assertEqual(thin[0]["score"], 1.35)
        for fn in (ln._gap_from_predictions, ln._gap_from_curiosity, ln._gap_from_thin_coverage):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(fn(_Boom()), [])                            # a missing cross-organ table is skipped

    def test_record_gaps_dedups_within_window(self):
        cur = _Cur([("SELECT 1 FROM learning_gaps", (1,))])
        self.assertEqual(ln._record_gaps(cur, [{"gap": "g", "evidence": {}, "source_kind": "k"}]), 0)
        self.assertEqual(cur.ran("INSERT INTO learning_gaps"), [])

    def test_current_learning_focus_formats_the_row(self):
        from datetime import datetime
        conn = MagicMock()
        conn.cursor.return_value.fetchone.return_value = ("sensor lag", "1/3", datetime(2026, 10, 2))
        with patch.object(ln.psycopg2, "connect", return_value=conn):
            s = ln.current_learning_focus()
        self.assertEqual(s, "What I'm teaching myself: sensor lag (1/3) — last learned Oct 02.")
        conn.close.assert_called_once()
        conn.cursor.return_value.fetchone.return_value = None
        with patch.object(ln.psycopg2, "connect", return_value=conn):
            self.assertEqual(ln.current_learning_focus(), "")


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_are_imported_not_reimplemented(self):
        import nova_lineage
        self.assertIn("nova_lineage.lineage_stamp(", SRC)
        self.assertIsInstance(ln._stamp(), dict)
        self.assertIn("lineage_stamp", dir(nova_lineage))
        self.assertTrue(ln.MEMSRV.endswith(":18790"))
        self.assertIn('"/remember"', SRC.replace("f\"{MEMSRV}/remember\"", '"/remember"'))

    def test_detect_then_record_lands_evidence_in_learning_gaps(self):
        cur = _Cur([("FROM predictions", PRED_ROWS), ("FROM reflection_questions", []), ("FROM preoccupations p", []),
                    ("SELECT 1 FROM learning_gaps", None)])
        gaps = ln._gap_from_predictions(cur) + ln._gap_from_curiosity(cur) + ln._gap_from_thin_coverage(cur)
        self.assertEqual(ln._record_gaps(cur, gaps), 1)
        sql, params = cur.ran("INSERT INTO learning_gaps")[0]
        self.assertEqual(json.loads(params[1])["domain"], "weather")
        self.assertEqual(params[2], "prediction")

    def test_plan_skips_a_gap_already_on_the_agenda(self):
        cur = _Cur([("FROM predictions", PRED_ROWS), ("FROM reflection_questions", CUR_ROWS), ("FROM preoccupations p", []),
                    ("SELECT 1 FROM learning_gaps", None),
                    ("SELECT lower(topic) FROM learning_agenda", [("i keep being confidently wrong in the 'weather' domain",)])])
        llm = MagicMock(return_value=json.dumps({"topic": "attic sensor lag", "why": "w", "steps": ["a", "b", "c"]}))
        with patch.object(ln, "llm", llm), redirect_stdout(io.StringIO()):
            cur.routes.append(("INSERT INTO learning_agenda", (9,)))
            ln.do_plan(cur)
        self.assertIn("attic sensor", llm.call_args[0][0])      # the curiosity gap was chosen, not the active weather one
        self.assertEqual(cur.ran("UPDATE learning_gaps SET status='adopted'")[0][1], (CUR_ROWS[0][1],))


class TestFunctional(unittest.TestCase):
    def _plan_cur(self):
        return _Cur([("FROM predictions", PRED_ROWS), ("FROM reflection_questions", []), ("FROM preoccupations p", THIN_ROWS),
                     ("SELECT 1 FROM learning_gaps", None), ("SELECT lower(topic) FROM learning_agenda", []),
                     ("INSERT INTO learning_agenda", (7,))])

    def test_plan_golden_path_adopts_the_strongest_gap(self):
        cur = self._plan_cur()
        llm = MagicMock(return_value='here you go {"topic": "What drives weather outcomes", "why": "I am wrong a lot", '
                                     '"steps": ["read the forecast model docs", "log 10 predictions", "compare", "", "fifth", "sixth"]}')
        out = io.StringIO()
        with patch.object(ln, "llm", llm), redirect_stdout(out):
            ln.do_plan(cur)
        self.assertEqual(len(cur.ran("INSERT INTO learning_gaps")), 2)
        sql, params = cur.ran("INSERT INTO learning_agenda")[0]
        self.assertEqual(params[0], "What drives weather outcomes")
        plan = json.loads(params[2])
        self.assertEqual(len(plan), ln.STEPS_MAX)                              # capped at 5, blank step dropped
        self.assertEqual(plan[0], {"n": 1, "step": "read the forecast model docs", "status": "pending", "memory_id": None})
        self.assertEqual(params[3], "0/5")
        self.assertIn("NEW CURRICULUM ITEM #7", out.getvalue())

    def test_plan_error_path_unusable_llm_writes_no_item(self):
        cur = self._plan_cur()
        with patch.object(ln, "llm", MagicMock(return_value="I cannot help with that")), redirect_stdout(io.StringIO()):
            ln.do_plan(cur)
        self.assertEqual(cur.ran("INSERT INTO learning_agenda"), [])
        self.assertEqual(cur.ran("UPDATE learning_gaps"), [])

    def test_study_golden_path_marks_learned_and_remembers(self):
        cur = _study_cur()
        remember = MagicMock(return_value=42)
        llm = lambda p, **k: "I can predict the lag now.\nVERDICT: understand" if "VERDICT" in p else "the sensor polls hourly"
        with patch.object(ln, "recall", lambda *a, **k: [{"text": "old note"}]), patch.object(ln, "_web", lambda *a, **k: []), \
             patch.object(ln, "llm", llm), patch.object(ln, "remember", remember), redirect_stdout(io.StringIO()):
            ln.do_study(cur)
        sql, params = cur.ran("UPDATE learning_agenda")[0]
        plan = json.loads(params[0])
        self.assertEqual(plan[1]["status"], "done"); self.assertEqual(plan[1]["memory_id"], 42)
        self.assertEqual(params[1:3], ("2/2", "learned"))
        self.assertEqual(remember.call_count, 2)                               # the step + the honest outcome
        self.assertEqual(remember.call_args_list[0][0][1], "learning")
        self.assertEqual(remember.call_args_list[1][0][2]["type"], "learning_learned")

    def test_study_stops_when_the_llm_is_silent(self):
        cur = _study_cur()
        with patch.object(ln, "recall", lambda *a, **k: []), patch.object(ln, "_web", lambda *a, **k: []), \
             patch.object(ln, "llm", lambda *a, **k: ""), redirect_stdout(io.StringIO()):
            ln.do_study(cur)
        self.assertEqual(cur.ran("UPDATE learning_agenda"), [])

    def test_main_report_mode(self):
        from datetime import datetime
        cur = _Cur([("SELECT status, count(*) FROM learning_agenda", [("learned", 2), ("abandoned", 1), ("learning", 1)]),
                    ("SELECT count(*) FROM learning_gaps WHERE status='adopted'", (3,)),
                    ("SELECT count(*) FROM learning_gaps", (9,)),
                    ("FROM learning_agenda ORDER BY ts DESC", [(1, "sensor lag", "learned", "3/3", datetime(2026, 10, 1))])])
        conn = MagicMock(); conn.cursor.return_value = cur
        out = io.StringIO()
        with patch.object(ln.psycopg2, "connect", return_value=conn), patch.object(sys, "argv", ["nova_learning.py", "--mode", "report"]), \
             redirect_stdout(out):
            self.assertEqual(ln.main(), 0)
        self.assertIn("actually LEARNED: 2   (learn-through rate 67% of resolved)", out.getvalue())
        self.assertTrue(cur.ran("CREATE TABLE IF NOT EXISTS learning_agenda"))


class TestFrame(unittest.TestCase):
    def test_help_runs_without_pg(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--mode", r.stdout)
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
