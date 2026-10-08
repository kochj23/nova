#!/usr/bin/env python3
"""7-category tests for the "I was wrong" loop in nova_predictions.py (2026-10-08):
own_mistake() and its call from do_resolve(), which writes a source='self_correction'
memory carrying the domain's Brier feedback, plus the retry/backoff on remember().

Categories: Security, Performance, Retry, Unit, Integration, Functional, Frame.
No network, no real DB, no memory-server writes. Written by Jordan Koch (via Claude).
"""
import importlib.util
import inspect
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
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_predictions.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pr = _load("pr_selfcorr_7cat", SCRIPT)
pr._stamp = lambda: {}
SRC = SCRIPT.read_text()
OWN_SRC = SRC[SRC.index("def own_mistake"):SRC.index("def do_resolve")]
BRIER_Q = "SELECT outcome, confidence FROM predictions"
SELF_ROWS = [("incorrect", 0.56)] * 6 + [("correct", 0.55)] * 4


class _Cur:
    def __init__(self, answer=lambda s, p: []):
        self.answer = answer; self.sql = []; self._rows = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        r = self.answer(sql, params)
        if isinstance(r, Exception):
            raise r
        self._rows = list(r or [])

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()

    def read(self): return self._d

    def __enter__(self): return self

    def __exit__(self, *a): return False


def _quiet():
    return redirect_stdout(io.StringIO())


def _brier_cur():
    return _Cur(lambda s, p: SELF_ROWS if BRIER_Q in s else [])


# ── Security ──────────────────────────────────────────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_note_is_private_and_sourced(self):
        with patch.object(pr, "remember", return_value=1) as rem, _quiet():
            pr.own_mistake(_brier_cur(), 7, "x", "self", 0.8, "y")
        text, source, meta = rem.call_args[0]
        self.assertEqual(source, "self_correction")
        self.assertEqual(meta["privacy"], "private")
        self.assertEqual(meta["type"], "self_correction")

    def test_untrusted_text_is_bounded_and_flattened(self):
        stmt = "S" * 5000
        reason = "line1\n\n\tline2 " + "R" * 5000
        with patch.object(pr, "remember", return_value=1), _quiet():
            text = pr.own_mistake(_Cur(), 1, stmt, "ops", 0.6, reason)
        self.assertNotIn("S" * 241, text)
        self.assertNotIn("R" * 241, text)
        self.assertIn("line1 line2", text)
        self.assertNotIn("\n", text)

    def test_domain_only_reaches_sql_as_a_parameter(self):
        evil = "self'; DROP TABLE predictions; --"
        cur = _Cur()
        with patch.object(pr, "remember", return_value=1), _quiet():
            pr.own_mistake(cur, 1, "x", evil, 0.6, "")
        for sql, params in cur.sql:
            self.assertNotIn("DROP", sql)
        self.assertEqual(cur.sql[0][1][0], evil)

    def test_own_mistake_writes_no_tables(self):
        self.assertIsNone(re.search(r"\b(INSERT|UPDATE|DELETE)\b", OWN_SRC))

    def test_memory_goes_to_the_house_memory_server_only(self):
        urls = []

        def fake(req, timeout=60):
            urls.append(req.full_url); return _Resp({"id": 1})
        with patch.object(pr.urllib.request, "urlopen", fake), _quiet():
            pr.own_mistake(_Cur(), 1, "x", "ops", 0.6, "")
        self.assertEqual(urls, [pr.MEMSRV + "/remember"])
        self.assertIn("digitalnoise.net", pr.MEMSRV)


# ── Performance ───────────────────────────────────────────────────────────────
class TestPerformance(unittest.TestCase):
    def test_one_bounded_query_per_mistake(self):
        cur = _brier_cur()
        with patch.object(pr, "remember", return_value=1), _quiet():
            pr.own_mistake(cur, 1, "x", "self", 0.8, "")
        self.assertEqual(len(cur.sql), 1)
        self.assertIn("LIMIT", cur.sql[0][0])

    def test_resolving_200_misses_is_fast(self):
        due = [(i, f"s{i}", "self", 0.8, 'c\n```check\n{"type": "mem_activity", "source": "b", "expect": "active"}\n```',
                "t0") for i in range(200)]
        oc = _Cur(lambda s, p: due if "status='open'" in s else (SELF_ROWS if BRIER_Q in s else []))
        mc = _Cur(lambda s, p: [(0,)])
        t0 = time.perf_counter()
        with patch.object(pr, "remember", return_value=1) as rem, _quiet():
            res = pr.do_resolve(oc, mc)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(res), 200)
        self.assertEqual(sum(1 for c in rem.call_args_list if c[0][1] == "self_correction"), 200)


# ── Retry ─────────────────────────────────────────────────────────────────────
class TestRetry(unittest.TestCase):
    def test_remember_retries_three_times_with_backoff_then_raises(self):
        calls, sleeps = [], []

        def boom(*a, **k):
            calls.append(1); raise OSError("memory server down")
        with patch.object(pr.urllib.request, "urlopen", boom), _quiet():
            with self.assertRaises(OSError):
                pr.remember("t", "self_correction", {}, _sleep=sleeps.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, list(pr.REMEMBER_BACKOFF))
        self.assertEqual(sleeps, sorted(sleeps))

    def test_remember_recovers_from_a_transient_failure(self):
        calls = []

        def flaky(*a, **k):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("blip")
            return _Resp({"id": "m-3"})
        with patch.object(pr.urllib.request, "urlopen", flaky), _quiet():
            self.assertEqual(pr.remember("t", "s", {}, _sleep=lambda s: None), "m-3")
        self.assertEqual(len(calls), 3)

    def test_own_mistake_logs_final_failure_and_still_returns(self):
        def boom(*a, **k):
            raise OSError("down")
        with patch.object(pr.urllib.request, "urlopen", boom), patch.object(pr.time, "sleep", lambda s: None), \
                redirect_stdout(io.StringIO()) as buf:
            text = pr.own_mistake(_Cur(), 1, "x", "ops", 0.6, "")
        self.assertIn("I was wrong.", text)
        self.assertIn("self_correction write failed", buf.getvalue())     # never silent
        self.assertIn("remember attempt 1 failed", buf.getvalue())

    def test_calibration_lookup_failure_still_writes_the_note(self):
        with patch.dict(sys.modules, {"nova_soft_certainty": types.SimpleNamespace(
                domain_brier=MagicMock(side_effect=RuntimeError("pg down")))}), \
                patch.object(pr, "remember", return_value=1) as rem, _quiet():
            text = pr.own_mistake(_Cur(), 1, "x", "ops", 0.6, "")
        self.assertEqual(rem.call_count, 1)
        self.assertNotIn("Brier", text)


# ── Unit ──────────────────────────────────────────────────────────────────────
class TestUnit(unittest.TestCase):
    def test_text_shape_with_calibration(self):
        with patch.object(pr, "remember", return_value=1), _quiet():
            text = pr.own_mistake(_brier_cur(), 3, "the printer finishes", "self", 0.8, "it jammed")
        self.assertRegex(text, r"^\[Self-correction \d{4}-\d{2}-\d{2}\] I was wrong\. ")
        self.assertIn('I said "the printer finishes" at 80%', text)
        self.assertIn("My self forecasts: 10 resolved, base rate 40%", text)
        self.assertIn("(skill -", text)

    def test_text_without_reason_or_calibration(self):
        with patch.object(pr, "remember", return_value=1), _quiet():
            text = pr.own_mistake(_Cur(), 3, "x", "world", 0.5, "   ")
        self.assertNotIn("What happened", text)
        self.assertNotIn("forecasts:", text)
        self.assertTrue(text.endswith("didn't happen."))

    def test_metadata(self):
        with patch.object(pr, "remember", return_value=1) as rem, _quiet():
            pr.own_mistake(_Cur(), 11, "x", "ops", 0.65, "")
        meta = rem.call_args[0][2]
        self.assertEqual({k: meta[k] for k in ("prediction_id", "domain", "confidence")},
                         {"prediction_id": 11, "domain": "ops", "confidence": 0.65})
        self.assertRegex(meta["date"], r"^\d{4}-\d{2}-\d{2}$")


# ── Integration ───────────────────────────────────────────────────────────────
class TestIntegration(unittest.TestCase):
    def _resolve(self, outcome):
        due = [(5, "a thing", "self", 0.7, "prose", "t0")]
        oc = _Cur(lambda s, p: due if "status='open'" in s else (SELF_ROWS if BRIER_Q in s else []))
        hit = {"correct": 1.0, "partial": 0.5, "incorrect": 0.0, "unresolvable": None}[outcome]
        with patch.object(pr, "eval_llm", return_value=(outcome, hit, "because")), \
                patch.object(pr, "remember", return_value=1) as rem, _quiet():
            pr.do_resolve(oc, _Cur())
        return [c for c in rem.call_args_list if c[0][1] == "self_correction"], oc

    def test_only_incorrect_outcomes_own_the_mistake(self):
        for outcome in ("correct", "partial", "unresolvable"):
            notes, _ = self._resolve(outcome)
            self.assertEqual(notes, [], outcome)
        notes, oc = self._resolve("incorrect")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][0][2]["prediction_id"], 5)
        self.assertIn("What happened: because", notes[0][0][0])
        self.assertTrue(any(BRIER_Q in s for s, _ in oc.sql))         # real soft_certainty read via same cursor

    def test_resolution_is_written_before_the_note(self):
        _, oc = self._resolve("incorrect")
        order = [i for i, (s, _) in enumerate(oc.sql) if "SET status='resolved'" in s or BRIER_Q in s]
        self.assertIn("SET status='resolved'", oc.sql[order[0]][0])


# ── Functional ────────────────────────────────────────────────────────────────
class TestFunctional(unittest.TestCase):
    def test_cli_resolve_golden_path(self):
        due = [(9, "backup lands", "ops", 0.9, "prose", "t0")]
        oc = _Cur(lambda s, p: due if "status='open'" in s else [])
        conn = MagicMock(); conn.cursor.return_value = oc
        buf = io.StringIO()
        with patch.object(pr.psycopg2, "connect", return_value=conn), patch.object(pr, "ensure_table", lambda oc: None), \
                patch.object(pr, "eval_llm", return_value=("incorrect", 0.0, "it never landed")), \
                patch.object(pr, "remember", return_value=1) as rem, \
                patch.object(sys, "argv", ["nova_predictions.py", "--mode", "resolve"]), redirect_stdout(buf):
            self.assertEqual(pr.main(), 0)
        self.assertIn("settled 1 predictions", buf.getvalue())
        self.assertIn("self_correction", [c[0][1] for c in rem.call_args_list])

    def test_memory_server_down_does_not_block_resolution(self):
        due = [(9, "x", "ops", 0.9, "prose", "t0")]
        oc = _Cur(lambda s, p: due if "status='open'" in s else [])
        with patch.object(pr, "eval_llm", return_value=("incorrect", 0.0, "r")), \
                patch.object(pr.urllib.request, "urlopen", side_effect=OSError("down")), \
                patch.object(pr.time, "sleep", lambda s: None), _quiet():
            res = pr.do_resolve(oc, _Cur())
        self.assertEqual(res[0][:2], (9, "incorrect"))
        self.assertTrue(any("SET status='resolved'" in s for s, _ in oc.sql))


# ── Frame ─────────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_predictions as p; print(callable(p.own_mistake))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")

    def test_signature_and_wiring(self):
        self.assertEqual(list(inspect.signature(pr.own_mistake).parameters),
                         ["oc", "pred_id", "statement", "domain", "conf", "reasoning"])
        self.assertIn("own_mistake(oc, _id, statement, domain, conf, reasoning)",
                      SRC[SRC.index("def do_resolve"):])


if __name__ == "__main__":
    unittest.main()
