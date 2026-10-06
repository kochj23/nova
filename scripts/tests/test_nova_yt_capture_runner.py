#!/usr/bin/env python3
"""Tests for nova_yt_capture_runner.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
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
SCRIPT = SCRIPTS / "nova_yt_capture_runner.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cr = _load("yt_capture_runner_under_test", SCRIPT)


class _Cur:
    """Cursor stub: `lock` answers pg_try_advisory_lock; `rows` are served one per queue SELECT."""
    def __init__(self, rows=(), lock=True):
        self.rows, self.lock = list(rows), lock
        self.sql, self.params, self._last = [], [], None

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split())); self.params.append(params)
        if "pg_try_advisory_lock" in sql:
            self._last = (self.lock,)
        elif "FROM yt_ingest_seen" in sql:
            self._last = self.rows.pop(0) if self.rows else None
        else:
            self._last = None

    def fetchone(self):
        return self._last

    def ran(self, frag):
        return [(s, p) for s, p in zip(self.sql, self.params) if frag in s]


def _conn(cur):
    c = types.SimpleNamespace(cursor=lambda: cur, autocommit=False, closed=False)
    c.close = lambda: setattr(c, "closed", True)
    return c


def _run(cur, run=None):
    run = run or MagicMock(return_value=types.SimpleNamespace(returncode=0))
    out = io.StringIO()
    with patch.object(cr.psycopg2, "connect", return_value=_conn(cur)) as pg, \
         patch.object(cr.subprocess, "run", run), redirect_stdout(out):
        cr.main()
    return pg, run, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", cr.DSN)

    def test_sql_is_parameterized_and_writes_only_touch_the_queue_table(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"yt_ingest_seen"})

    def test_hostile_video_id_is_one_argv_element_never_a_shell_string(self):
        evil = "abc; rm -rf / #"
        cur = _Cur([(evil, "queued", "t")])
        _, run, _ = _run(cur)
        args, kwargs = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertIn(evil, args[0]); self.assertFalse(kwargs.get("shell"))
        self.assertEqual(cur.ran("SET status=%s")[0][1], ("capturing", evil))


class TestPerformance(unittest.TestCase):
    def test_drains_10k_queued_videos_quickly(self):
        cur = _Cur([(f"v{i}", "queued", f"title {i}") for i in range(10_000)])
        t0 = time.perf_counter()
        _, run, out = _run(cur)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(run.call_count, 10_000)
        self.assertIn("done — 10000 captured", out)


class TestRetry(unittest.TestCase):
    def test_capture_child_failure_marks_the_row_failed_and_the_loop_continues(self):
        # RETRY GAP: subprocess.run(nova_yt_capture.py) — one attempt per video; a crash is recorded, never retried
        cur = _Cur([("bad", "queued", None), ("good", "queued_live", "live one")])
        run = MagicMock(side_effect=[OSError("fork failed"), types.SimpleNamespace(returncode=0)])
        _, run, out = _run(cur, run)
        self.assertEqual(run.call_count, 2)
        self.assertIn("bad failed: fork failed", out)
        self.assertEqual(cur.ran("SET status='failed'")[0][1], ("bad",))
        self.assertIn("done — 2 captured", out)

    def test_pg_connect_failure_escapes_before_any_capture(self):
        # RETRY GAP: main()/psycopg2.connect — no retry; launchd re-fires the job on the next interval
        run = MagicMock()
        with patch.object(cr.psycopg2, "connect", side_effect=OSError("pg down")), \
             patch.object(cr.subprocess, "run", run), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                cr.main()
        run.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_log_prefix(self):
        out = io.StringIO()
        with redirect_stdout(out):
            cr.log("hello")
        self.assertEqual(out.getvalue(), "[capture-runner] hello\n")

    def test_live_rows_get_the_live_flag_and_recording_status(self):
        cur = _Cur([("L1", "queued_live", "x" * 80), ("V1", "queued", None)])
        _, run, out = _run(cur)
        live_args = run.call_args_list[0][0][0]; vod_args = run.call_args_list[1][0][0]
        self.assertEqual(live_args, [cr.PY, cr.CAP, "L1", "fishbowl", "--live"])
        self.assertEqual(vod_args, [cr.PY, cr.CAP, "V1", "fishbowl"])
        self.assertEqual([p for _, p in cur.ran("SET status=%s")], [("recording", "L1"), ("capturing", "V1")])
        self.assertIn("capturing L1 (live) " + "x" * 50 + "\n", out)      # title truncated to 50 chars
        self.assertIn("capturing V1 (vod) \n", out)                         # None title renders empty


class TestIntegration(unittest.TestCase):
    def test_single_instance_lock_is_taken_and_released_with_the_same_id(self):
        cur = _Cur([])
        _run(cur)
        self.assertEqual(cur.ran("pg_try_advisory_lock")[0][1], (cr.LOCK,))
        self.assertEqual(cur.ran("pg_advisory_unlock")[0][1], (cr.LOCK,))
        self.assertEqual(cur.ran("pg_try_advisory_lock")[0][1], cur.ran("pg_advisory_unlock")[0][1])

    def test_capture_script_and_queue_source_are_the_real_ones(self):
        self.assertEqual(Path(cr.CAP), SCRIPTS / "nova_yt_capture.py")
        self.assertTrue(Path(cr.CAP).exists())
        self.assertIn("dbname=nova_ops", cr.DSN)
        cur = _Cur([]); _run(cur)
        q = cur.ran("FROM yt_ingest_seen")[0][0]
        self.assertIn("status IN ('queued','queued_live')", q); self.assertIn("ORDER BY seen_at LIMIT 1", q)

    def test_child_runs_with_a_twelve_hour_ceiling(self):
        cur = _Cur([("v", "queued", "t")])
        _, run, _ = _run(cur)
        self.assertEqual(run.call_args[1]["timeout"], 12 * 3600)


class TestFunctional(unittest.TestCase):
    def test_golden_path_captures_everything_queued_then_unlocks_and_closes(self):
        cur = _Cur([("a", "queued", "A"), ("b", "queued_live", "B")])
        pg, run, out = _run(cur)
        conn = pg.return_value
        self.assertTrue(conn.autocommit); self.assertTrue(conn.closed)
        self.assertEqual(run.call_count, 2)
        self.assertEqual(len(cur.ran("FROM yt_ingest_seen")), 3)            # two rows + the empty poll that ends the loop
        self.assertIn("done — 2 captured", out)
        self.assertEqual(cur.sql[-1], "SELECT pg_advisory_unlock(%s)")

    def test_lock_held_elsewhere_exits_without_touching_the_queue(self):
        cur = _Cur([("a", "queued", "A")], lock=False)
        pg, run, out = _run(cur)
        run.assert_not_called()
        self.assertEqual(cur.ran("FROM yt_ingest_seen"), [])
        self.assertIn("another instance holds the lock", out)

    def test_empty_queue_is_quiet(self):
        _, run, out = _run(_Cur([]))
        run.assert_not_called(); self.assertNotIn("done", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_yt_capture_runner"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
