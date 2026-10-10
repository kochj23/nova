#!/usr/bin/env python3
"""Tests for nova_pattern_sense.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Since the M7 merge (2026-10-09) the script is a thin wrapper: miscalibration lives in
nova_soft_certainty --refresh, recurring incidents in nova_alert_learn recurrence."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_pattern_sense.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ps = _load("ps_under_test", SCRIPT)
sc = ps._sc
al = ps._al
T0 = datetime(2026, 10, 8, 4, 5, tzinfo=timezone.utc)


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
    """Routes reads for both halves (tuple rows for soft certainty, dict rows for alert_learn)."""
    def __init__(self, preds=(), incs=(), seen=None, fail_on=None, dict_rows=False):
        self.preds, self.incs, self.seen, self.fail_on, self.dict_rows = list(preds), list(incs), seen, fail_on, dict_rows
        self.sql, self.params, self._last = [], [], ""

    def execute(self, sql, params=None):
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("boom")
        self.sql.append(sql); self.params.append(params); self._last = sql

    def fetchall(self):
        if "FROM predictions" in self._last:
            return self.preds
        if "FROM incidents" in self._last:
            return [{"title": t, "n": n} for t, n in self.incs]
        return []

    def fetchone(self):
        if "FROM service_config" in self._last:
            v = {"seen": self.seen or {}}
            return {"value": v} if self.dict_rows else (v,)
        return None


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False; self.commits = 0

    def cursor(self, *a, **k):
        return self._cur

    def commit(self):
        self.commits += 1

    def close(self):
        pass


# scored_rows() shape: (confidence, outcome, surprise, domain, resolved_at)
PREDS = [(0.8, "correct", 0.04, "self", T0)] * 2 + [(0.8, "incorrect", 0.64, "self", T0)] * 3
INCS = [("Disk full on nas", 5), ("blip", 1)]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_wrapper_writes_no_sql_itself(self):
        self.assertIsNone(re.search(r"INSERT INTO|UPDATE\s+\w+\s+SET|DELETE FROM|execute\(", SRC))

    def test_read_only_over_the_world_never_executes(self):
        self.assertNotIn("subprocess", SRC)
        self.assertNotIn("os.system", SRC)

    def test_same_dedupe_row_and_source_as_before(self):
        self.assertEqual((ps.SOURCE, ps.STATE_SERVICE, ps.STATE_KEY),
                         ("pattern_sense", "nova_pattern_sense", "high_water"))
        self.assertIsInstance(ps.RECUR_WINDOW_DAYS, int)


class TestPerformance(unittest.TestCase):
    def test_pattern_math_fast_on_10k(self):
        rows = [(f"d{i % 100}", 0.5 + (i % 5) / 10, i % 3 == 0) for i in range(10_000)]
        recs = [(f"t{i}", i % 7) for i in range(10_000)]
        t0 = time.perf_counter()
        c = ps.calibration_patterns(rows); r = ps.recurrence_patterns(recs)
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertTrue(c and r)


class TestRetry(unittest.TestCase):
    def test_remember_has_no_retry_and_propagates(self):
        # RETRY GAP: remember — a single POST, no backoff; the error reaches the caller
        # (surface_miscalibration catches it and leaves the pattern unmarked).
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")) as u:
            with self.assertRaises(OSError):
                ps.remember("t", {})
        self.assertEqual(u.call_count, 1)

    def test_main_fails_open_when_pg_is_down(self):
        with mock.patch("psycopg2.connect", side_effect=OSError("no pg")), \
             mock.patch.object(al, "_connect", side_effect=OSError("no pg")), \
             mock.patch.object(sys, "argv", ["nova_pattern_sense.py"]), redirect_stdout(StringIO()):
            self.assertEqual(ps.main(), 0)


class TestUnit(unittest.TestCase):
    def test_selftest_passes(self):
        with redirect_stdout(StringIO()) as out:
            ps.demo()
        self.assertIn("assertions passed", out.getvalue())

    def test_empty_and_unknown_domain(self):
        self.assertEqual(ps.calibration_patterns([]), [])
        out = ps.calibration_patterns([(None, 0.9, False)] * 4)
        self.assertEqual(out[0]["domain"], "unknown")

    def test_worst_gap_first(self):
        rows = [("a", 0.9, False)] * 4 + [("b", 0.6, False)] * 4
        self.assertEqual([p["domain"] for p in ps.calibration_patterns(rows)], ["a", "b"])

    def test_sig_and_fresh(self):
        self.assertEqual(ps._sig("calib", "x"), ps._sig("calib", "x"))
        self.assertRegex(ps._sig("recur", "y"), r"^[0-9a-f]{16}$")
        today = date(2026, 10, 5)
        self.assertTrue(ps._fresh({}, "s", today))
        self.assertFalse(ps._fresh({"s": "2026-10-01"}, "s", today))
        self.assertTrue(ps._fresh({"s": "2026-09-01"}, "s", today))
        self.assertTrue(ps._fresh({"s": "garbage"}, "s", today))

    def test_insight_text(self):
        p = ps.calibration_patterns([("self", 0.8, i < 2) for i in range(5)])[0]
        self.assertIn("OVERconfident", ps.calib_insight(p)); self.assertIn("80%", ps.calib_insight(p))
        self.assertIn("5 times", ps.recur_insight({"title": "Disk full", "count": 5}))


class TestIntegration(unittest.TestCase):
    def test_old_names_are_the_new_homes(self):
        self.assertIs(ps.calibration_patterns, sc.calibration_patterns)
        self.assertIs(ps.calib_insight, sc.calib_insight)
        self.assertIs(ps.recurrence_patterns, al.recurrence_patterns)
        self.assertIs(ps.recur_insight, al.recur_insight)
        self.assertIs(ps.load_seen, sc.load_seen)
        self.assertIs(ps.save_seen, sc.save_seen)
        # the recurrence half signs patterns exactly as the old organ did, so de-dupe carries over
        self.assertEqual(al._pattern_sig("recur", "x"), ps._sig("recur", "x"))

    def test_seen_roundtrip_through_service_config(self):
        cur = _Cur()
        ps.save_seen(cur, {"abc": "2026-10-05"})
        self.assertIn("INSERT INTO service_config", cur.sql[-1])
        self.assertEqual(cur.params[-1][:2], (ps.STATE_SERVICE, ps.STATE_KEY))
        self.assertEqual(ps.load_seen(_Cur(seen={"abc": "2026-10-05"})), {"abc": "2026-10-05"})
        self.assertEqual(ps.load_seen(_Cur()), {})


class TestFunctional(unittest.TestCase):
    def _run(self, argv, cur, acur=None):
        posts = []

        def fake(req, timeout=None):
            posts.append((req.full_url, json.loads(req.data.decode())))
            return _Resp({"id": 1})
        aconn = _Conn(acur or _Cur(incs=INCS, dict_rows=True))
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), \
             mock.patch.object(al, "_connect", lambda: aconn), \
             mock.patch("urllib.request.urlopen", side_effect=fake), \
             mock.patch.object(sc, "_stamp", lambda: {}), mock.patch.object(al, "_pattern_stamp", lambda: {}), \
             mock.patch.object(sys, "argv", ["nova_pattern_sense.py"] + argv), redirect_stdout(StringIO()) as out:
            rc = ps.main()
        return rc, posts, out.getvalue(), aconn._cur

    def test_golden_path_delegates_both_halves(self):
        cur = _Cur(preds=PREDS)
        rc, posts, out, acur = self._run([], cur)
        self.assertEqual(rc, 0)
        self.assertIn("merged on 2026-10-09", out)
        self.assertEqual(len(posts), 2)
        self.assertTrue(all(u.endswith("/remember") and b["source"] == "pattern_sense" for u, b in posts))
        self.assertEqual({b["metadata"]["kind"] for _, b in posts}, {"miscalibration", "recurrence"})
        self.assertEqual(sum("INSERT INTO service_config" in s for s in cur.sql), 1)
        self.assertEqual(sum("INSERT INTO service_config" in s for s in acur.sql), 1)

    def test_dry_run_writes_nothing(self):
        cur = _Cur(preds=PREDS)
        rc, posts, out, acur = self._run(["--dry-run"], cur)
        self.assertEqual(rc, 0); self.assertEqual(posts, [])
        self.assertFalse(any("INSERT" in s for s in cur.sql + acur.sql))
        self.assertEqual(out.count("• Pattern I can finally see"), 2)

    def test_predictions_read_failure_is_fail_open(self):
        cur = _Cur(preds=PREDS, fail_on="FROM predictions")
        rc, posts, _, _ = self._run([], cur)
        self.assertEqual(rc, 0)
        self.assertEqual([b["metadata"]["kind"] for _, b in posts], ["recurrence"])


class TestFrame(unittest.TestCase):
    def test_selftest_and_help_exit_zero(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        for flag in ("--selftest", "--help"):
            r = subprocess.run([sys.executable, str(SCRIPT), flag], capture_output=True, text=True,
                               timeout=30, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('if __name__ == "__main__":', SRC)

    def test_import_never_runs_main(self):
        with mock.patch("psycopg2.connect", side_effect=AssertionError("main ran on import")):
            m = _load("ps_import_probe", SCRIPT)
        self.assertTrue(callable(m.main))


if __name__ == "__main__":
    unittest.main()
