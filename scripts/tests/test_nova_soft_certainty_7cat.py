#!/usr/bin/env python3
"""7-category tests for the 2026-10-08 Brier calibration in nova_soft_certainty.py:
domain_brier, brier_calibrate, calibrate(domain=...) routing, and the connect retry (_connect).

Categories: Security, Performance, Retry, Unit, Integration, Functional, Frame.
No real DB: cursors are scripted. Written by Jordan Koch (via Claude).
"""
import importlib.util
import inspect
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_soft_certainty.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sc = _load("sc_7cat", SCRIPT)
SRC = SCRIPT.read_text()


class _Cur:
    """Routes fetchone/fetchall by SQL substring; records every execute."""
    def __init__(self, routes=()):
        self.routes = list(routes); self.sql = []; self.params = []; self._last = ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql
        for needle, val in self.routes:
            if needle in sql and isinstance(val, Exception):
                raise val

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


class _Conn:
    def __init__(self, cur): self._cur = cur; self.autocommit = False; self.closed = False

    def cursor(self): return self._cur

    def close(self): self.closed = True


BRIER_Q = "SELECT outcome, confidence FROM predictions"
SELF_ROWS = [("incorrect", 0.56)] * 6 + [("correct", 0.55)] * 4          # confidence carries no information


def _quiet():
    return redirect_stdout(io.StringIO())


# ── Security ──────────────────────────────────────────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_domain_and_window_are_parameters(self):
        evil = "self'; DROP TABLE predictions; --"
        cur = _Cur([(BRIER_Q, [])])
        sc.domain_brier(cur, evil, window=50)
        self.assertNotIn("DROP", cur.sql[0])
        self.assertEqual(cur.params[0], (evil, 50))

    def test_domain_brier_is_read_only(self):
        cur = _Cur([(BRIER_Q, SELF_ROWS)])
        sc.domain_brier(cur, "self")
        self.assertTrue(all(s.lstrip().upper().startswith("SELECT") for s in cur.sql))

    def test_garbage_rows_are_ignored(self):
        rows = [("correct", None), ("bogus", 0.9), ("correct", 0.9)] * 3
        db = sc.domain_brier(_Cur([(BRIER_Q, rows)]), "ops", min_n=3)
        self.assertEqual(db["n"], 3)
        self.assertEqual(db["base"], 1.0)

    def test_output_is_always_a_bounded_probability(self):
        for db in ({"n": 1000, "base": 0.0, "skill": -5.0}, {"n": 1000, "base": 1.0, "skill": 9.0}):
            for stated in (-3.0, 0.0, 0.5, 1.0, 7.0):
                v = sc.brier_calibrate(stated, db)
                self.assertGreaterEqual(v, 0.03); self.assertLessEqual(v, 0.97)

    def test_no_credentials_in_dsn(self):
        self.assertNotIn("password", sc.OPS_DSN)
        self.assertIsNone(re.search(r"(password|secret|token)\s*=\s*['\"][^'\"]{8,}", SRC, re.I))


# ── Performance ───────────────────────────────────────────────────────────────
class TestPerformance(unittest.TestCase):
    def test_domain_brier_query_is_windowed(self):
        cur = _Cur([(BRIER_Q, [])])
        sc.domain_brier(cur, "self")
        self.assertIn("LIMIT %s", cur.sql[0])
        self.assertEqual(cur.params[0][1], 200)

    def test_domain_brier_10k_rows_fast(self):
        rows = [(("correct", "incorrect", "partial")[i % 3], 0.3 + (i % 60) / 100) for i in range(10_000)]
        t0 = time.perf_counter()
        db = sc.domain_brier(_Cur([(BRIER_Q, rows)]), "ops", window=10_000)
        self.assertLess(time.perf_counter() - t0, 0.2)
        self.assertEqual(db["n"], 10_000)

    def test_brier_calibrate_hot_path(self):
        db = {"n": 104, "base": 0.45, "skill": -0.12}
        t0 = time.perf_counter()
        for i in range(100_000):
            sc.brier_calibrate((i % 100) / 100, db)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_calibrate_with_domain_is_one_query_when_brier_hits(self):
        cur = _Cur([(BRIER_Q, SELF_ROWS)])
        sc.calibrate(0.8, cur, domain="self")
        self.assertEqual(sum(BRIER_Q in s for s in cur.sql), 1)
        self.assertFalse(any("avg((outcome" in s for s in cur.sql))      # no redundant domain_stats


# ── Retry ─────────────────────────────────────────────────────────────────────
class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff_then_raises_and_logs(self):
        calls, sleeps = [], []

        def boom(*a, **k):
            calls.append(k); raise OSError("pg down")
        with mock.patch.object(sc.psycopg2, "connect", boom), redirect_stdout(io.StringIO()) as buf:
            with self.assertRaises(OSError):
                sc._connect(backoff=(0.1, 0.2), _sleep=sleeps.append)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [0.1, 0.2])
        self.assertIn("failed after 3 attempts", buf.getvalue())

    def test_default_hot_path_makes_two_attempts(self):
        calls = []

        def boom(*a, **k):
            calls.append(1); raise OSError("down")
        with mock.patch.object(sc.psycopg2, "connect", boom), mock.patch.object(sc.time, "sleep", lambda s: None), _quiet():
            self.assertEqual(sc.calibrate(0.8, domain="self"), 0.8)          # fails open
        self.assertEqual(len(calls), 1 + len(sc.CONNECT_BACKOFF))

    def test_connect_recovers_on_second_attempt(self):
        cur = _Cur([("FROM soft_certainty_state", (20, 0.63, 0.45, 0.18, 0.36))])
        seq = [OSError("blip"), _Conn(cur)]

        def flaky(*a, **k):
            v = seq.pop(0)
            if isinstance(v, Exception):
                raise v
            return v
        with mock.patch.object(sc.psycopg2, "connect", flaky), mock.patch.object(sc.time, "sleep", lambda s: None):
            self.assertLess(sc.calibrate(0.9), 0.9)                        # got the real state

    def test_main_uses_longer_backoff(self):
        sleeps = []

        def boom(*a, **k):
            raise OSError("down")
        with mock.patch.object(sc.psycopg2, "connect", boom), mock.patch.object(sc.time, "sleep", sleeps.append), \
                mock.patch.object(sys, "argv", ["x", "--show"]), _quiet():
            with self.assertRaises(OSError):
                sc.main()
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_domain_brier_error_falls_back_to_domain_stats(self):
        cur = _Cur([(BRIER_Q, RuntimeError("no resolved_at")), ("avg((outcome", (0.2, 10))])
        self.assertEqual(sc.calibrate(0.8, cur, domain="relationship"), round(0.8 + (0.2 - 0.8) * 0.5, 4))


# ── Unit ──────────────────────────────────────────────────────────────────────
class TestUnit(unittest.TestCase):
    def test_brier_math(self):
        db = sc.domain_brier(_Cur([(BRIER_Q, SELF_ROWS)]), "self")
        self.assertEqual(db["n"], 10); self.assertEqual(db["base"], 0.4)
        brier = (6 * 0.56 ** 2 + 4 * 0.45 ** 2) / 10
        ref = (6 * 0.4 ** 2 + 4 * 0.6 ** 2) / 10
        self.assertAlmostEqual(db["brier"], round(brier, 4))
        self.assertAlmostEqual(db["brier_ref"], round(ref, 4))
        self.assertAlmostEqual(db["skill"], round(1 - brier / ref, 4), places=3)
        self.assertLess(db["skill"], 0)

    def test_partial_counts_half(self):
        db = sc.domain_brier(_Cur([(BRIER_Q, [("partial", 0.5)] * 5)]), "ops")
        self.assertEqual(db["base"], 0.5); self.assertEqual(db["brier"], 0.0)

    def test_degenerate_reference(self):
        perfect = sc.domain_brier(_Cur([(BRIER_Q, [("correct", 1.0)] * 5)]), "x")
        self.assertEqual(perfect["skill"], 1.0)
        wrong = sc.domain_brier(_Cur([(BRIER_Q, [("correct", 0.5)] * 5)]), "x")
        self.assertEqual(wrong["skill"], 0.0)

    def test_min_n(self):
        self.assertIsNone(sc.domain_brier(_Cur([(BRIER_Q, SELF_ROWS[:4])]), "self"))
        self.assertIsNotNone(sc.domain_brier(_Cur([(BRIER_Q, SELF_ROWS[:4])]), "self", min_n=4))

    def test_brier_calibrate_shapes(self):
        no_skill = {"n": 90, "base": 0.45, "skill": -0.12}
        self.assertEqual(sc.brier_calibrate(0.8, no_skill), round(0.8 + (0.45 - 0.8) * 0.9, 4))
        self.assertEqual(sc.brier_calibrate(0.2, no_skill), round(0.2 + (0.45 - 0.2) * 0.9, 4))   # moves UP too
        full = {"n": 90, "base": 0.45, "skill": 1.0}
        self.assertEqual(sc.brier_calibrate(0.8, full), 0.8)
        half = {"n": 10, "base": 0.5, "skill": 0.5}
        self.assertEqual(sc.brier_calibrate(0.9, half), round(0.9 + (0.7 - 0.9) * 0.5, 4))

    def test_documented_live_example(self):
        # agent_docs: self n=104, base 0.45, skill -0.12 -> 0.80 becomes ~0.48
        self.assertAlmostEqual(sc.brier_calibrate(0.80, {"n": 104, "base": 0.45, "skill": -0.12}), 0.48, places=2)


# ── Integration ───────────────────────────────────────────────────────────────
class TestIntegration(unittest.TestCase):
    def test_calibrate_routes_brier_then_domain_stats_then_global(self):
        state = ("FROM soft_certainty_state", (20, 0.63, 0.45, 0.18, 0.36))
        brier = _Cur([state, (BRIER_Q, SELF_ROWS)])
        self.assertEqual(sc.calibrate(0.8, brier, domain="self"),
                         sc.brier_calibrate(0.8, sc.domain_brier(brier, "self")))
        stats = _Cur([state, (BRIER_Q, [("correct", None)] * 9), ("avg((outcome", (0.3, 9))])
        self.assertEqual(sc.calibrate(0.8, stats, domain="ops"), round(0.8 + (0.3 - 0.8) * 9 / 19, 4))
        glob = _Cur([state])
        self.assertEqual(sc.calibrate(0.8, glob, domain="world"), round(0.8 + (0.45 - 0.8) * 0.36, 4))

    def test_predictions_own_mistake_uses_the_same_number(self):
        pr = _load("pr_for_sc_7cat", SCRIPTS / "nova_predictions.py")
        pr._stamp = lambda: {}
        cur = _Cur([(BRIER_Q, SELF_ROWS)])
        expected = sc.brier_calibrate(0.8, sc.domain_brier(cur, "self"))
        with mock.patch.object(pr, "remember", return_value=1), _quiet():
            text = pr.own_mistake(cur, 1, "x", "self", 0.8, "")
        self.assertIn(f"now comes out as {expected:.0%}", text)


# ── Functional ────────────────────────────────────────────────────────────────
class TestFunctional(unittest.TestCase):
    def test_calibrate_golden_and_error_paths(self):
        cur = _Cur([(BRIER_Q, SELF_ROWS)])
        out = sc.calibrate(0.9, cur, domain="self")
        self.assertLess(out, 0.9); self.assertGreater(out, 0.03)
        self.assertEqual(sc.calibrate("nope", cur, domain="self"), "nope")
        self.assertEqual(sc.calibrate(0.7, _Cur(), domain="empty"), 0.7)             # no evidence anywhere

    def test_show_runs_end_to_end(self):
        cur = _Cur([("FROM soft_certainty_state", (20, 0.63, 0.45, 0.18, 0.36))])
        buf = io.StringIO()
        with mock.patch.object(sc.psycopg2, "connect", lambda *a, **k: _Conn(cur)), \
                mock.patch.object(sys, "argv", ["x", "--show"]), redirect_stdout(buf):
            self.assertEqual(sc.main(), 0)
        self.assertIn("calibrate(0.95) ->", buf.getvalue())


# ── Frame ─────────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_soft_certainty as s; print(s.brier_calibrate.__name__)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "brier_calibrate")

    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--refresh", r.stdout)

    def test_signatures(self):
        self.assertEqual(list(inspect.signature(sc.calibrate).parameters), ["stated", "oc", "domain"])
        self.assertEqual(list(inspect.signature(sc.domain_brier).parameters), ["oc", "domain", "min_n", "window"])


if __name__ == "__main__":
    unittest.main()
