#!/usr/bin/env python3
"""Tests for nova_soft_certainty.py — the 7 house categories (Security, Performance, Retry, Unit,
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


sc = _load("sc", SCRIPT)
SRC = SCRIPT.read_text()


class _Cur:
    """Cursor stub: answers fetchone/fetchall by substring of the last SQL, records every execute."""
    def __init__(self, routes=None):
        self.routes = routes or []; self.sql = []; self.params = []; self._last = ""

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
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False; self.closed = False

    def cursor(self):
        return self._cur

    def close(self):
        self.closed = True


STATE = ("SELECT n, mean_conf, hit_rate, gap, shrink", (20, 0.63, 0.45, 0.18, 0.36))
RESOLVED = [(0.8, "correct"), (0.7, "incorrect"), (0.9, "partial"), (0.6, "correct"),
            (0.5, "incorrect"), (0.8, "correct"), (0.7, "incorrect"), (0.6, "partial")]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        cur = _Cur([("avg((outcome='correct')::int)", (0.5, 10))])
        sc.domain_stats(cur, "x'; DROP TABLE predictions; --")
        self.assertNotIn("DROP TABLE", cur.sql[0])
        self.assertEqual(cur.params[0], ("x'; DROP TABLE predictions; --",))

    def test_only_write_is_its_own_state_table(self):
        writes = set(re.findall(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC))
        self.assertEqual(writes, {"soft_certainty_state"})

    def test_calibrate_rejects_garbage_input_unchanged(self):
        self.assertEqual(sc.calibrate("not a number", oc=_Cur()), "not a number")
        self.assertIsNone(sc.calibrate(None, oc=_Cur()))


class TestPerformance(unittest.TestCase):
    def test_compute_calibration_fast_on_10k_rows(self):
        rows = [(0.5 + (i % 50) / 100, ("correct", "incorrect", "partial")[i % 3]) for i in range(10_000)]
        cur = _Cur([("SELECT confidence, outcome FROM predictions", rows)])
        t0 = time.perf_counter()
        cal = sc.compute_calibration(cur)
        self.assertLess(time.perf_counter() - t0, 0.2)
        self.assertEqual(cal["n"], 10_000)

    def test_calibrate_hot_path_fast(self):
        cur = _Cur([STATE])
        t0 = time.perf_counter()
        for i in range(10_000):
            sc.calibrate(0.5 + (i % 50) / 100, cur)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    # RETRY GAP: calibrate() / current_stance() — one psycopg2.connect, no backoff; both fail open.
    def test_calibrate_fails_open_to_stated_when_pg_is_down(self):
        calls = []

        def boom(*a, **k):
            calls.append(1); raise OSError("pg down")
        with mock.patch.object(sc.psycopg2, "connect", boom):
            self.assertEqual(sc.calibrate(0.9), 0.9)
            self.assertEqual(sc.current_stance(), "")
        self.assertEqual(len(calls), 2)       # one attempt each, never raises

    def test_latest_and_domain_stats_swallow_cursor_errors(self):
        cur = _Cur([("FROM soft_certainty_state", RuntimeError("no table")),
                    ("avg((outcome", RuntimeError("no column"))])
        self.assertIsNone(sc._latest(cur))
        self.assertIsNone(sc.domain_stats(cur, "ops"))
        self.assertEqual(sc.calibrate(0.9, cur, domain="ops"), 0.9)

    def test_connection_closed_even_when_query_fails(self):
        conn = _Conn(_Cur([("FROM soft_certainty_state", RuntimeError("boom"))]))
        with mock.patch.object(sc.psycopg2, "connect", return_value=conn):
            sc.current_stance()
        self.assertTrue(conn.closed)


class TestUnit(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(sc._clamp(5, 0, 1), 1)
        self.assertEqual(sc._clamp(-5, 0, 1), 0)
        self.assertEqual(sc._clamp(0.5, 0, 1), 0.5)

    def test_compute_calibration_needs_min_n(self):
        cur = _Cur([("SELECT confidence, outcome FROM predictions", RESOLVED[:sc.MIN_N - 1])])
        self.assertIsNone(sc.compute_calibration(cur))
        self.assertIsNone(sc.compute_calibration(_Cur([("SELECT confidence, outcome", [])])))

    def test_compute_calibration_math(self):
        cal = sc.compute_calibration(_Cur([("SELECT confidence, outcome FROM predictions", RESOLVED)]))
        self.assertEqual(cal["n"], 8)
        self.assertAlmostEqual(cal["mean_conf"], 0.7, places=4)
        self.assertAlmostEqual(cal["hit_rate"], 0.5, places=4)       # 3 correct + 2 partial halves
        self.assertAlmostEqual(cal["gap"], 0.2, places=4)
        self.assertAlmostEqual(cal["shrink"], 0.4, places=4)         # gap*2, inside [0.15, 0.6]

    def test_shrink_is_zero_when_not_overconfident(self):
        rows = [(0.4, "correct")] * 8
        self.assertEqual(sc.compute_calibration(_Cur([("SELECT confidence, outcome", rows)]))["shrink"], 0.0)

    def test_shrink_is_clamped(self):
        rows = [(0.99, "incorrect")] * 8
        self.assertEqual(sc.compute_calibration(_Cur([("SELECT confidence, outcome", rows)]))["shrink"], 0.6)
        rows = [(0.53, "incorrect")] * 4 + [(0.53, "correct")] * 4      # gap 0.03 -> raw 0.06 -> floor 0.15
        self.assertEqual(sc.compute_calibration(_Cur([("SELECT confidence, outcome", rows)]))["shrink"], 0.15)

    def test_calibrate_only_pulls_down_and_never_below_hit_rate_side(self):
        cur = _Cur([STATE])
        self.assertEqual(sc.calibrate(0.9, cur), round(0.9 + (0.45 - 0.9) * 0.36, 4))
        self.assertEqual(sc.calibrate(0.4, cur), 0.4)                 # already below realized accuracy
        self.assertEqual(sc.calibrate(0.45, cur), 0.45)

    def test_calibrate_no_state_or_zero_shrink_is_identity(self):
        self.assertEqual(sc.calibrate(0.9, _Cur()), 0.9)
        cur = _Cur([("SELECT n, mean_conf, hit_rate, gap, shrink", (20, 0.5, 0.5, 0.0, 0.0))])
        self.assertEqual(sc.calibrate(0.9, cur), 0.9)

    def test_domain_calibration_outranks_global(self):
        cur = _Cur([STATE, ("avg((outcome='correct')::int)", (0.0, 7))])
        self.assertEqual(sc.calibrate(0.64, cur, domain="relationship"), round(0.64 - 0.64 * 7 / 17, 4))
        cur = _Cur([STATE, ("avg((outcome='correct')::int)", (0.9, 7))])
        self.assertEqual(sc.calibrate(0.64, cur, domain="good"), 0.64)   # below the domain's hit-rate: untouched
        cur = _Cur([STATE, ("avg((outcome='correct')::int)", (0.0, 3))])
        self.assertEqual(sc.calibrate(0.9, cur, domain="thin"), sc.calibrate(0.9, _Cur([STATE])))  # too few -> global

    def test_domain_brier_shrinks_a_skill_free_domain_to_its_base_rate(self):
        # 2026-10-08: same confidence on hits and misses -> no skill -> collapse toward base rate
        rows = [("incorrect", 0.56)] * 57 + [("correct", 0.55)] * 47
        cur = _Cur([STATE, ("SELECT outcome, confidence", rows)])
        db = sc.domain_brier(cur, "self")
        self.assertEqual(db["n"], 104); self.assertAlmostEqual(db["base"], 47 / 104, 3)
        self.assertLess(db["skill"], 0)
        out = sc.calibrate(0.8, cur, domain="self")
        self.assertAlmostEqual(out, round(0.8 + (db["base"] - 0.8) * 104 / 114, 4), 4)
        self.assertGreater(sc.calibrate(0.2, cur, domain="self"), 0.2)    # underconfident pulls UP too
        # a skilled domain keeps most of its spread
        good = [("correct", 0.9)] * 8 + [("incorrect", 0.1)] * 8
        cur = _Cur([("SELECT outcome, confidence", good)])
        self.assertGreater(sc.domain_brier(cur, "ops")["skill"], 0.9)
        self.assertGreater(sc.calibrate(0.9, cur, domain="ops"), 0.85)

    def test_current_stance_text(self):
        self.assertIn("I lean overconfident (recently ~63% sure, ~45% right — off by ~18 points)",
                      sc.current_stance(_Cur([STATE])))
        even = _Cur([("SELECT n, mean_conf, hit_rate, gap, shrink", (20, 0.5, 0.49, 0.01, 0.0))])
        self.assertTrue(sc.current_stance(even).startswith("I'm currently about as sure as I am right."))
        self.assertEqual(sc.current_stance(_Cur()), "")


class TestIntegration(unittest.TestCase):
    def test_refresh_chains_compute_into_insert(self):
        cur = _Cur([("SELECT confidence, outcome FROM predictions", RESOLVED)])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(sc.refresh(cur), 0)
        ins = [(s, p) for s, p in zip(cur.sql, cur.params) if "INSERT INTO soft_certainty_state" in s]
        self.assertEqual(len(ins), 1)
        self.assertEqual(ins[0][1][:2], (8, 0.7))
        self.assertIn(sc.TODAY, ins[0][1][5])
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS public.soft_certainty_state" in s for s in cur.sql))

    def test_refresh_without_evidence_writes_nothing(self):
        cur = _Cur([("SELECT confidence, outcome FROM predictions", RESOLVED[:3])])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(sc.refresh(cur), 1)
        self.assertFalse(any("INSERT" in s for s in cur.sql))

    def test_state_table_and_predictions_source(self):
        self.assertIn("FROM predictions", SRC)
        self.assertIn("ORDER BY computed_at DESC LIMIT 1", SRC)
        self.assertEqual(sc.OPS_DSN.split()[1], "dbname=nova_ops")


class TestFunctional(unittest.TestCase):
    def _main(self, cur, *argv):
        with mock.patch.object(sc.psycopg2, "connect", return_value=_Conn(cur)), \
             mock.patch.object(sc.sys, "argv", ["nova_soft_certainty.py", *argv]), \
             redirect_stdout(io.StringIO()) as out:
            rc = sc.main()
        return rc, out.getvalue()

    def test_show_prints_state_stance_and_samples(self):
        rc, out = self._main(_Cur([STATE]))
        self.assertEqual(rc, 0)
        self.assertIn('"hit_rate": 0.45', out)
        self.assertIn("I lean overconfident", out)
        self.assertIn(f"calibrate(0.95) -> {sc.calibrate(0.95, _Cur([STATE]))}", out)

    def test_refresh_golden_path_inserts_one_row(self):
        cur = _Cur([("SELECT confidence, outcome FROM predictions", RESOLVED)])
        rc, out = self._main(cur, "--refresh")
        self.assertEqual(rc, 0)
        self.assertIn("calibration refreshed: n=8", out)
        self.assertEqual(sum("INSERT INTO soft_certainty_state" in s for s in cur.sql), 1)

    def test_refresh_error_path_too_little_data(self):
        cur = _Cur([("SELECT confidence, outcome FROM predictions", RESOLVED[:2])])
        rc, out = self._main(cur, "--refresh")
        self.assertEqual(rc, 1)
        self.assertIn("not enough resolved predictions", out)
        self.assertFalse(any("INSERT" in s for s in cur.sql))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_without_touching_pg(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--refresh", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        with mock.patch.object(sc.psycopg2, "connect", side_effect=AssertionError("main ran")):
            _load("sc_again", SCRIPT)


if __name__ == "__main__":
    unittest.main()
