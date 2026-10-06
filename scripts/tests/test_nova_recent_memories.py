#!/usr/bin/env python3
"""Tests for nova_recent_memories.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). psycopg2 is mocked; no live DB. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


rm = _load("nova_recent_memories_t", SCRIPTS / "nova_recent_memories.py")
SRC = (SCRIPTS / "nova_recent_memories.py").read_text()
TS = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class _Cur:
    def __init__(self, script):
        self.script = list(script); self.sql = []; self._out = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params)); self._out = self.script.pop(0)

    def fetchone(self):
        return self._out

    def fetchall(self):
        return self._out

    def close(self): pass


class _Conn:
    def __init__(self, script):
        self.cur = _Cur(script); self.ro = None

    def cursor(self, cursor_factory=None):
        return self.cur

    def set_session(self, readonly=None, autocommit=None):
        self.ro = readonly

    def close(self): pass


def _patch(conn):
    return mock.patch.object(rm.psycopg2, "connect", return_value=conn)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_values_are_parameterized_not_interpolated(self):
        # f-strings only interpolate the TABLE constant; every user value goes through %s
        conn = _Conn([(0,), []])
        with _patch(conn):
            rm.get_recent_summary(hours=24, source="television")
        for sql, params in conn.cur.sql:
            self.assertIn("%s", sql)
            self.assertIsNotNone(params)
            self.assertNotIn("television", sql)

    def test_connection_is_readonly(self):
        conn = _Conn([(0,), []])
        with _patch(conn):
            rm.get_recent_summary()
        self.assertTrue(conn.ro)


class TestPerformance(unittest.TestCase):
    def test_truncate_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            rm._truncate("x " * 200, 80)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_db_down_exits_one(self):
        # RETRY GAP: connect()/psycopg2 — one attempt; a read-only reporting tool just exits 1 on outage
        with mock.patch.object(rm.psycopg2, "connect", side_effect=rm.psycopg2.OperationalError("down")) as c, \
             mock.patch.object(sys, "argv", ["x"]), redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                rm.main()
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(c.call_count, 1)
        self.assertIn("Cannot connect", err.getvalue())


class TestUnit(unittest.TestCase):
    def test_truncate_and_fmt(self):
        self.assertEqual(rm._truncate("short"), "short")
        self.assertTrue(rm._truncate("y" * 200).endswith("..."))
        self.assertEqual(rm._fmt_count(12345), "12,345")
        self.assertEqual(rm._label_tag(""), "")
        self.assertEqual(rm._label_tag("Severance"), "[Severance] ")

    def test_cutoff_is_in_past_and_utc(self):
        c = rm._cutoff(24)
        self.assertEqual(c.tzinfo, timezone.utc)
        self.assertLess(c, datetime.now(timezone.utc))

    def test_format_summary_empty(self):
        out = rm.format_summary({"hours": 1, "total": 0, "by_source": []})
        self.assertIn("(none)", out)
        self.assertIn("last 1 hour:", out)


class TestIntegration(unittest.TestCase):
    def test_summary_shape_and_table(self):
        conn = _Conn([(7,), [("television", 5, ["Severance", "Andor"]), ("email", 2, None)]])
        with _patch(conn):
            data = rm.get_recent_summary(hours=12)
        self.assertEqual(data["total"], 7)
        self.assertEqual(data["by_source"][0]["source"], "television")
        self.assertEqual(data["by_source"][0]["labels"], ["Andor", "Severance"])
        self.assertIn("FROM memories", conn.cur.sql[0][0].replace("\n", " ").replace("  ", " ") or SRC)
        self.assertEqual(rm.TABLE, "memories")
        self.assertEqual(rm.DB_NAME, "nova_memories")


class TestFunctional(unittest.TestCase):
    def test_main_json_summary(self):
        conn = _Conn([(3,), [("email", 3, None)]])
        out = io.StringIO()
        with _patch(conn), mock.patch.object(sys, "argv", ["x", "--json"]), redirect_stdout(out):
            rm.main()
        data = json.loads(out.getvalue())
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["by_source"][0]["source"], "email")

    def test_main_detail_text(self):
        # detail: source list, then per-source samples
        conn = _Conn([
            [{"source": "television", "cnt": 2, "labels": ["Andor"]}],
            [{"text": "a long episode about rebellion\nand hope", "label": "Andor", "created_at": TS}],
        ])
        out = io.StringIO()
        with mock.patch.object(rm.psycopg2, "connect", return_value=conn), \
             mock.patch.object(sys, "argv", ["x", "--detail"]), redirect_stdout(out):
            rm.main()
        text = out.getvalue()
        self.assertIn("television (2 new):", text)
        self.assertIn("[Andor] a long episode about rebellion", text)
        self.assertIn("...showing 1 of 2", text)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_recent_memories.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--hours", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
