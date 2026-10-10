#!/usr/bin/env python3
"""Tests for nova_soft_certainty.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from datetime import date, datetime, timezone
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
INJ = "2026-10-01'; SELECT pg_sleep(9); --"
T0 = datetime(2026, 10, 8, 4, 5, tzinfo=timezone.utc)
T1 = datetime(2026, 10, 9, 4, 5, tzinfo=timezone.utc)
# scored_rows() shape: (confidence, outcome, surprise, domain, resolved_at)
M7_ROWS = [(0.9, "incorrect", 0.81, "relationship", T0)] * 4 + \
          [(0.6, "correct", 0.16, "self", T0), (0.6, "incorrect", 0.36, "self", T1),
           (0.5, "partial", 0.0, "partial_only", T0)]


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


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
        # M7: plus the Pattern Sense de-dupe row it inherited (service_config, merged never replaced)
        writes = set(re.findall(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)) - {"SET"}
        self.assertEqual(writes, {"soft_certainty_state", "service_config"})
        self.assertIn("coalesce(service_config.value->'seen', '{}'::jsonb) || (EXCLUDED.value->'seen')", SRC)

    def test_scored_rows_since_is_parameterized(self):
        cur = _Cur()
        sc.scored_rows(cur, since=INJ)
        self.assertNotIn("pg_sleep", cur.sql[0])
        self.assertEqual(cur.params[0], [INJ])

    def test_calibrate_rejects_garbage_input_unchanged(self):
        self.assertEqual(sc.calibrate("not a number", oc=_Cur()), "not a number")
        self.assertIsNone(sc.calibrate(None, oc=_Cur()))


class TestPerformance(unittest.TestCase):
    def test_calibration_detail_and_patterns_fast_on_10k(self):
        rows = [((i % 10) / 10 + 0.05, ("correct", "incorrect", "partial")[i % 3], (i % 7) / 10,
                 f"d{i % 50}", None) for i in range(10_000)]
        t0 = time.perf_counter()
        d = sc.calibration_detail(rows); dd = sc.domain_detail(rows)
        p = sc.calibration_patterns([(r[3], r[0], r[1] == "correct") for r in rows])
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual((d["n"], len(dd)), (10_000, 50)); self.assertIsInstance(p, list)

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
    # calibrate() / current_stance() — 2 connect attempts with backoff (nova_soft_certainty._connect); both fail open.
    def test_calibrate_fails_open_to_stated_when_pg_is_down(self):
        calls = []

        def boom(*a, **k):
            calls.append(1); raise OSError("pg down")
        with mock.patch.object(sc.psycopg2, "connect", boom), mock.patch.object(sc.time, "sleep", lambda s: None), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(sc.calibrate(0.9), 0.9)
            self.assertEqual(sc.current_stance(), "")
        self.assertEqual(len(calls), 4)       # two attempts each, never raises

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


class TestRetryM7(unittest.TestCase):
    def test_miscalibration_memory_failure_fails_open_and_stays_unmarked(self):
        # RETRY GAP: remember() — one POST, no backoff; refresh logs it, does not mark it seen,
        # and still writes the calibration row (tried again next night).
        cur = _Cur([("SELECT confidence, outcome FROM predictions", RESOLVED),
                    ("SELECT confidence, outcome, surprise", M7_ROWS)])
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")) as u, \
             mock.patch.object(sc, "_stamp", lambda: {}), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sc.refresh(cur), 0)
        self.assertEqual(u.call_count, 1)
        self.assertIn("miscalibration memory failed", out.getvalue())
        self.assertFalse(any("INSERT INTO service_config" in q for q in cur.sql))
        self.assertEqual(sum("INSERT INTO soft_certainty_state" in q for q in cur.sql), 1)

    def test_read_calibration_falls_back_when_state_unreadable(self):
        cur = _Cur([("FROM soft_certainty_state", RuntimeError("no table")),
                    ("SELECT confidence, outcome, surprise", M7_ROWS)])
        with redirect_stdout(io.StringIO()):
            d, how = sc.read_calibration(cur)
        self.assertEqual((how, d["n"]), ("live", len(M7_ROWS)))


class TestUnit(unittest.TestCase):
    def test_calibration_detail_matches_the_report_arithmetic(self):
        self.assertIsNone(sc.calibration_detail([]))
        rows = [(0.8, "correct", 0.04, "a", None), (0.8, "incorrect", 0.64, "a", None),
                (0.85, "partial", None, "b", None), (0.3, "correct", 0.49, "b", None)]
        d = sc.calibration_detail(rows)
        self.assertEqual(d["n"], 4)
        self.assertAlmostEqual(d["hit_rate"], 2.5 / 4)
        self.assertAlmostEqual(d["mean_surprise"], (0.04 + 0.64 + 0 + 0.49) / 4)
        self.assertAlmostEqual(d["high_surprise_rate"], 2 / 4)            # 0.64, 0.49 > 0.4
        self.assertEqual([b[:2] for b in d["deciles"]], [[3, 1], [8, 3]])
        # decile-weighted |hit - conf|: bucket 3 -> |1-0.3|*1, bucket 8 -> |0.5-0.8167|*3
        self.assertAlmostEqual(d["calib_error"], (0.7 + abs(0.5 - (0.8 + 0.8 + 0.85) / 3) * 3) / 4)

    def test_domain_detail_is_strict_correct_incorrect(self):
        dd = sc.domain_detail(M7_ROWS)
        self.assertEqual(dd["relationship"], {"n": 4, "mean_conf": 0.9, "hit_rate": 0.0, "gap": 0.9})
        self.assertNotIn("partial_only", dd)

    def test_fingerprint(self):
        self.assertEqual(sc.fingerprint_of([]), {"n": 0, "max_resolved_at": None})
        fp = sc.fingerprint_of([(0.5, "correct", None, "x", T1), (0.5, "correct", None, "x", T0)])
        self.assertEqual(fp, {"n": 2, "max_resolved_at": T1.isoformat()})

    def test_moved_pattern_helpers(self):
        p = sc.calibration_patterns([("self", 0.8, i < 2) for i in range(5)])[0]
        self.assertIn("OVERconfident", sc.calib_insight(p)); self.assertIn("80%", sc.calib_insight(p))
        self.assertEqual(sc.calibration_patterns([("cal", 0.5, i < 2) for i in range(4)]), [])
        self.assertRegex(sc._sig("calib", "x"), r"^[0-9a-f]{16}$")
        today = date(2026, 10, 9)
        self.assertTrue(sc._fresh({}, "s", today)); self.assertFalse(sc._fresh({"s": "2026-10-05"}, "s", today))

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


class TestIntegrationM7(unittest.TestCase):
    def test_refresh_stores_the_report_figures_readers_use(self):
        cur = _Cur([("SELECT confidence, outcome FROM predictions", RESOLVED),
                    ("SELECT confidence, outcome, surprise", M7_ROWS)])
        with mock.patch.object(sc, "surface_miscalibration", lambda *a, **k: 0), redirect_stdout(io.StringIO()):
            sc.refresh(cur)
        ins = [p for q, p in zip(cur.sql, cur.params) if "INSERT INTO soft_certainty_state" in q][0]
        det = json.loads(ins[5])
        self.assertEqual(det["report"], json.loads(json.dumps(sc.calibration_detail(M7_ROWS))))
        self.assertEqual(det["fingerprint"], sc.fingerprint_of(M7_ROWS))
        self.assertIn("relationship", det["domains"])
        # the reader uses exactly that row while the live fingerprint still matches it
        live = _Cur([("FROM soft_certainty_state", (ins[5],)),
                     ("SELECT count(*), max(resolved_at)", (len(M7_ROWS), T1))])
        d, how = sc.read_calibration(live)
        self.assertEqual((how, d), ("state", det["report"]))
        self.assertFalse(any("SELECT confidence, outcome, surprise" in q for q in live.sql))
        # ...and recomputes once a new resolution has landed
        moved = _Cur([("FROM soft_certainty_state", (ins[5],)),
                      ("SELECT count(*), max(resolved_at)", (len(M7_ROWS) + 1, T1)),
                      ("SELECT confidence, outcome, surprise", M7_ROWS[:2])])
        self.assertEqual(sc.read_calibration(moved)[1], "live")
        # a different surprise threshold never reuses the stored row
        other = _Cur([("FROM soft_certainty_state", (ins[5],)),
                      ("SELECT count(*), max(resolved_at)", (len(M7_ROWS), T1)),
                      ("SELECT confidence, outcome, surprise", M7_ROWS)])
        self.assertEqual(sc.read_calibration(other, high_surprise=0.5)[1], "live")

    def test_miscalibration_memory_keeps_pattern_sense_source_and_dedupe_row(self):
        self.assertEqual((sc.PATTERN_SOURCE, sc.PATTERN_SERVICE, sc.PATTERN_KEY),
                         ("pattern_sense", "nova_pattern_sense", "high_water"))
        posts = []

        def fake(req, timeout=None):
            posts.append(json.loads(req.data.decode())); return _Resp({"id": 1})
        cur = _Cur([("FROM service_config", None)])
        with mock.patch("urllib.request.urlopen", side_effect=fake), mock.patch.object(sc, "_stamp", lambda: {}), \
             redirect_stdout(io.StringIO()):
            n = sc.surface_miscalibration(cur, M7_ROWS)
        self.assertEqual(n, 1)
        self.assertEqual(posts[0]["source"], "pattern_sense")
        self.assertEqual(posts[0]["metadata"]["kind"], "miscalibration")
        self.assertEqual(posts[0]["metadata"]["organ"], "nova_pattern_sense")
        saved = [p for q, p in zip(cur.sql, cur.params) if "INSERT INTO service_config" in q]
        self.assertEqual(saved[0][:2], ("nova_pattern_sense", "high_water"))
        # already surfaced within RESURFACE_DAYS -> silent
        sig = sc._sig("calib", "relationship" + "overconfident")
        today = sc.datetime.now(sc.timezone.utc).date().isoformat()
        cur2 = _Cur([("FROM service_config", ({"seen": {sig: today}},))])
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("posted")), redirect_stdout(io.StringIO()):
            self.assertEqual(sc.surface_miscalibration(cur2, M7_ROWS), 0)


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


class TestFunctionalM7(unittest.TestCase):
    def test_refresh_dry_run_writes_nothing_and_posts_nothing(self):
        cur = _Cur([("SELECT confidence, outcome FROM predictions", RESOLVED),
                    ("SELECT confidence, outcome, surprise", M7_ROWS)])
        with mock.patch.object(sc.psycopg2, "connect", return_value=_Conn(cur)), \
             mock.patch.object(sc.sys, "argv", ["nova_soft_certainty.py", "--refresh", "--dry-run"]), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("posted")), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sc.main(), 0)
        self.assertFalse(any(re.search(r"INSERT|UPDATE|CREATE", q) for q in cur.sql))
        self.assertIn("Pattern I can finally see: on 'relationship'", out.getvalue())
        self.assertIn('"calib_error"', out.getvalue())


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
