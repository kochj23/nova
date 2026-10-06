#!/usr/bin/env python3
"""Tests for nova_scene_daily_report.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_scene_daily_report.py"
SRC = SCRIPT.read_text()
import psycopg2.extras  # noqa: E402  real module locked in; its Json adapter is used by the script
TMP = Path(tempfile.mkdtemp(prefix="scene-report-test-"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock()
    with patch.dict(sys.modules, {"nova_notify": nn}):     # the script binds `notify` at import; restored after
        spec.loader.exec_module(mod)
    return mod


sr = _load("scene_report_under_test", SCRIPT)
sr.LOG_FILE = TMP / "scene_daily_report.log"              # never touch ~/.openclaw/logs


class _Cur:
    def __init__(self, rows, raise_on=()):
        self.rows = rows; self.raise_on = raise_on; self.sql = []; self.closed = False

    def execute(self, sql, params=None):
        s = " ".join(sql.split()); self.sql.append((s, params))
        for frag in self.raise_on:
            if frag in s:
                raise RuntimeError(f"stub failure on {frag}")

    def fetchall(self):
        return self.rows

    def close(self):
        self.closed = True

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.commits = 0; self.closed = False

    def cursor(self, cursor_factory=None):
        self.factory = cursor_factory; return self.cur

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _run(rows, notify=None, raise_on=()):
    cur = _Cur(rows, raise_on); conn = _Conn(cur)
    pg = types.SimpleNamespace(connect=MagicMock(return_value=conn), extras=psycopg2.extras)
    sr.notify = notify or MagicMock()
    with patch.object(sr, "psycopg2", pg), redirect_stdout(io.StringIO()) as out:
        sr.run()
    return conn, sr.notify, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", sr.DSN)

    def test_sql_is_parameterized_and_only_writes_shared_observations(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"shared_observations"})
        evil = "Movie Night'); DROP TABLE shared_observations; --"
        conn, _, _ = _run([{"scene_name": evil, "n": 2}])
        sql, params = conn.cur.ran("INSERT INTO shared_observations")[0]
        self.assertNotIn("DROP", sql)
        self.assertIn(evil, params[0])
        self.assertEqual(params[1].adapted, {"scenes": {evil: 2}})       # psycopg2.extras.Json, not string-built JSON


class TestPerformance(unittest.TestCase):
    def test_10k_scenes_summarize_under_1s(self):
        rows = [{"scene_name": f"Scene {i}", "n": i} for i in range(10_000)]
        t0 = time.perf_counter()
        conn, notify, _ = _run(rows)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertTrue(notify.call_args[1]["body"].startswith("49995000 activation(s) across 10000 scene(s):"))


class TestRetry(unittest.TestCase):
    def test_notify_failure_is_swallowed_after_the_row_is_committed(self):
        # RETRY GAP: run()/nova_notify.notify — one attempt; a failure is logged and the report still completes
        conn, notify, out = _run([{"scene_name": "Bedtime", "n": 1}], notify=MagicMock(side_effect=RuntimeError("bus down")))
        self.assertEqual(notify.call_count, 1)
        self.assertEqual(conn.commits, 1)
        self.assertIn("Notify failed: bus down", out)
        self.assertIn("Report complete", out)
        self.assertTrue(conn.closed)

    def test_notify_failure_on_the_quiet_day_path_is_swallowed_too(self):
        conn, notify, out = _run([], notify=MagicMock(side_effect=OSError("x")))
        self.assertEqual(conn.commits, 1)
        self.assertIn("Notify failed: x", out)
        self.assertTrue(conn.closed)

    def test_pg_read_failure_is_one_shot_and_escapes(self):
        # RETRY GAP: run()/psycopg2 — no retry; a failed read escapes to the scheduler and nothing is posted
        notify = MagicMock()
        with self.assertRaises(RuntimeError):
            _run([], notify=notify, raise_on=("home_scene_activations",))
        notify.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_log_writes_to_the_redirected_file_and_stdout(self):
        with redirect_stdout(io.StringIO()) as out:
            sr.log("hello")
        self.assertIn("[scene_report ", out.getvalue()); self.assertIn("] hello", out.getvalue())
        self.assertTrue(sr.LOG_FILE.read_text().strip().endswith("] hello"))
        self.assertTrue(str(sr.LOG_FILE).startswith(str(TMP)))

    def test_log_swallows_an_unwritable_path(self):
        real = sr.LOG_FILE
        sr.LOG_FILE = TMP / "no" / "such" / "dir" / "x.log"
        try:
            with redirect_stdout(io.StringIO()):
                sr.log("still fine")
        finally:
            sr.LOG_FILE = real


class TestIntegration(unittest.TestCase):
    def test_uses_realdictcursor_and_the_shared_observations_shape(self):
        conn, notify, _ = _run([{"scene_name": "Good Morning", "n": 3}, {"scene_name": "Bedtime", "n": 1}])
        self.assertIs(conn.factory, psycopg2.extras.RealDictCursor)
        self.assertIn("FROM home_scene_activations", conn.cur.sql[0][0])
        self.assertIn("interval '1 day'", conn.cur.sql[0][0])
        sql, params = conn.cur.ran("INSERT INTO shared_observations")[0]
        self.assertIn("VALUES ('nova_scene_daily_report', 'home', 'scene-activity', %s, 'info', %s)", sql)
        self.assertEqual(params[0], "4 scene activation(s) in the last 24h: Good Morning x3, Bedtime x1")
        self.assertEqual(params[1].adapted, {"scenes": {"Good Morning": 3, "Bedtime": 1}})
        self.assertEqual(notify.call_args[1]["dedup_key"], "scene-daily-report")
        self.assertEqual(notify.call_args[1]["category"], "home")


class TestFunctional(unittest.TestCase):
    def test_golden_path_posts_the_digest(self):
        conn, notify, out = _run([{"scene_name": "Good Morning", "n": 3}])
        title, kw = notify.call_args[0][0], notify.call_args[1]
        self.assertTrue(title.startswith("Home Scenes Report ("))
        self.assertEqual(kw["body"], "3 activation(s) across 1 scene(s):\n  Good Morning: 3")
        self.assertEqual(kw["level"], "info")
        self.assertIn("total=3 scenes=1", out); self.assertIn("Report complete", out)
        self.assertEqual(conn.commits, 1); self.assertTrue(conn.closed); self.assertTrue(conn.cur.closed)

    def test_quiet_day_still_records_and_notifies(self):
        conn, notify, out = _run([])
        sql, params = conn.cur.ran("INSERT INTO shared_observations")[0]
        self.assertIn("No HomeKit scene activations logged in the last 24h.", sql)
        self.assertIsNone(params)
        self.assertEqual(notify.call_args[0][0], "Home Scenes Report")
        self.assertEqual(notify.call_args[1]["body"], "No scene activations logged in the last 24h.")
        self.assertIn("No scene activations in the last 24h", out)
        self.assertTrue(conn.closed)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no argparse: --help would run the report against PG, so the smoke is an import
        self.assertIn('if __name__ == "__main__":\n    run()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_scene_daily_report"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
