#!/usr/bin/env python3
"""Tests for nova_queue_aging.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_queue_aging.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="qa-test-"))

import psycopg2  # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


qa = _load("queue_aging_under_test", SCRIPT)


class _Cur:
    def __init__(self, counts=(5, 3), raise_on=None):
        self.counts, self.raise_on, self.sql, self.rowcount = list(counts), raise_on, [], 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if self.raise_on and self.raise_on in sql:
            raise RuntimeError("stub failure")
        self.rowcount = self.counts.pop(0)


class _Conn:
    def __init__(self, cur):
        self.cur, self.closed, self.autocommit = cur, False, False
        self.autocommit_at_execute = None

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _run(cur=None, connect=None):
    cur = cur or _Cur()
    conn = _Conn(cur)
    out = io.StringIO()
    with patch.object(qa.psycopg2, "connect", connect or MagicMock(return_value=conn)), redirect_stdout(out):
        rc = qa.main()
    return rc, conn, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", qa.DSN)

    def test_statements_are_static_and_scoped(self):
        # no runtime interpolation at all: both statements are module constants with no placeholders
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        self.assertNotIn("%s", qa.DELETE_TERMINAL + qa.AGE_OUT_STALE_ALERTS)
        self.assertIn("WHERE status IN ('resolved','completed','aged_out','superseded','cancelled')", qa.DELETE_TERMINAL)
        self.assertIn("WHERE status = 'queued'", qa.AGE_OUT_STALE_ALERTS)
        self.assertNotIn("DROP", SRC.upper().replace("DROPPED", ""))

    def test_only_claude_queue_is_touched(self):
        writes = {m.group(1) for m in re.finditer(r"\b(?:DELETE FROM|UPDATE)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"claude_queue"})


class TestPerformance(unittest.TestCase):
    def test_hygiene_pass_is_two_statements_and_fast(self):
        t0 = time.perf_counter()
        for _ in range(2000):
            rc, conn, _ = _run(_Cur((1, 1)))
            self.assertEqual(len(conn.cur.sql), 2)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_connect_failure_is_one_shot_and_escapes(self):
        # RETRY GAP: main()/psycopg2.connect — single attempt, no guard; scheduler sees the non-zero exit
        pc = MagicMock(side_effect=RuntimeError("pg down"))
        with self.assertRaises(RuntimeError):
            _run(connect=pc)
        self.assertEqual(pc.call_count, 1)

    def test_statement_failure_still_closes_connection(self):
        # RETRY GAP: main()/cur.execute — no retry, but the finally: always releases the connection
        cur = _Cur(raise_on="UPDATE")
        with self.assertRaises(RuntimeError):
            _run(cur)
        self.assertEqual(len(cur.sql), 2)


class TestUnit(unittest.TestCase):
    def test_windows_match_the_docstring(self):
        self.assertIn("interval '7 days'", qa.DELETE_TERMINAL)
        self.assertIn("interval '14 days'", qa.AGE_OUT_STALE_ALERTS)
        self.assertIn("updated_at < now()", qa.DELETE_TERMINAL)
        self.assertIn("created_at < now()", qa.AGE_OUT_STALE_ALERTS)

    def test_every_documented_prefix_is_aged(self):
        for p in ("TASK FAILING:", "OVERNIGHT:", "MAINTENANCE:", "SECURITY:", "STALE DAEMON:", "SYSTEMIC:", "CORE LIVENESS:"):
            self.assertIn(f"description LIKE '{p}%'", qa.AGE_OUT_STALE_ALERTS)
        self.assertIn("SET status = 'aged_out', updated_at = now()", qa.AGE_OUT_STALE_ALERTS)


class TestIntegration(unittest.TestCase):
    def test_targets_ops_db_with_autocommit(self):
        self.assertIn("dbname=nova_ops", qa.DSN)
        rc, conn, _ = _run()
        self.assertTrue(conn.autocommit)
        self.assertEqual(conn.cur.sql, [qa.DELETE_TERMINAL, qa.AGE_OUT_STALE_ALERTS])   # delete first, then age

    def test_rowcounts_flow_into_the_summary(self):
        rc, conn, out = _run(_Cur((12, 7)))
        self.assertIn("deleted_terminal=12 aged_out_stale_alerts=7", out)


class TestFunctional(unittest.TestCase):
    def test_golden_path(self):
        rc, conn, out = _run()
        self.assertEqual(rc, 0)
        self.assertTrue(conn.closed)
        self.assertEqual(out.strip(), "[queue_aging] deleted_terminal=5 aged_out_stale_alerts=3")

    def test_error_path_closes_and_prints_nothing(self):
        cur = _Cur(raise_on="DELETE")
        conn = _Conn(cur)
        out = io.StringIO()
        with patch.object(qa.psycopg2, "connect", return_value=conn), redirect_stdout(out):
            with self.assertRaises(RuntimeError):
                qa.main()
        self.assertTrue(conn.closed)
        self.assertEqual(out.getvalue(), "")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main_and_script_exits_zero_offline(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import sys, runpy; from unittest.mock import MagicMock; import psycopg2\n"
                "cur = MagicMock(); cur.__enter__ = lambda s: s; cur.__exit__ = lambda s, *a: False; cur.rowcount = 0\n"
                "conn = MagicMock(); conn.cursor.return_value = cur\n"
                "psycopg2.connect = MagicMock(return_value=conn)\n"
                "import nova_queue_aging; assert psycopg2.connect.call_count == 0, 'import ran main'\n"
                "try:\n    runpy.run_path(%r, run_name='__main__')\nexcept SystemExit as e:\n    sys.exit(e.code or 0)\n" % str(SCRIPT))
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("deleted_terminal=0 aged_out_stale_alerts=0", r.stdout)


if __name__ == "__main__":
    unittest.main()
