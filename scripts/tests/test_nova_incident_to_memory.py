#!/usr/bin/env python3
"""Tests for nova_incident_to_memory.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
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
SCRIPT = SCRIPTS / "nova_incident_to_memory.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="incident-mem-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("incident_to_memory_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = _load()


class _Cur:
    def __init__(self, rows):
        self.rows = rows; self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.autocommit = False

    def cursor(self):
        return self.cur


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.body).encode()


ROW = (42, "Plex down", "warning", "launchd job unloaded; reloaded it", ["plex"], "2026-10-01", "2026-10-01 10:00")


def _run(rows, argv=None, remember=None):
    cur = _Cur(rows)
    with patch.object(m.psycopg2, "connect", return_value=_Conn(cur)), \
         patch.object(sys, "argv", argv or ["nova_incident_to_memory.py"]), \
         patch.object(m, "remember", remember if remember is not None else MagicMock(return_value=1)) as rem, \
         redirect_stdout(io.StringIO()) as out:
        rc = m.main()
    return rc, cur, rem, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", m.OPS_DSN)

    def test_sql_is_parameterized_and_read_only(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", SRC))   # PG is only read
        rc, cur, _, _ = _run([], argv=["x", "2026-01-01'; DROP TABLE incidents; --"])
        sql, params = cur.sql[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params, ("2026-01-01'; DROP TABLE incidents; --",))

    def test_memories_are_marked_private(self):
        rc, cur, rem, _ = _run([ROW])
        self.assertEqual(rem.call_args[0][1]["privacy"], "private")


class TestPerformance(unittest.TestCase):
    def test_10k_incidents_format_and_dispatch_fast(self):
        rows = [(i, f"t{i}", "warning", "cause", ["svc"], "2026-10-01", "r") for i in range(10_000)]
        t0 = time.perf_counter()
        rc, cur, rem, out = _run(rows)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(rem.call_count, 10_000)
        self.assertIn("ingested 10000/10000", out)


class TestRetry(unittest.TestCase):
    def test_remember_failure_is_isolated_per_row(self):
        # RETRY GAP: remember()/urlopen — one POST per incident; a failure is logged and the loop continues
        calls = []

        def rem(text, meta):
            calls.append(meta["incident_id"])
            if meta["incident_id"] == "1":
                raise OSError("memory server down")
            return 9
        rows = [(1, "a", "w", "c", None, "d", "r"), (2, "b", "w", "c", None, "d", "r")]
        rc, cur, _, out = _run(rows, remember=rem)
        self.assertEqual(rc, 0)
        self.assertEqual(calls, ["1", "2"])
        self.assertIn("failed 1: memory server down", out)
        self.assertIn("ingested 1/2", out)

    def test_remember_is_a_single_urlopen(self):
        with patch("urllib.request.urlopen", side_effect=OSError("down")) as u:
            with self.assertRaises(OSError):
                m.remember("t", {})
        self.assertEqual(u.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_remember_builds_a_json_post_and_returns_the_id(self):
        with patch("urllib.request.urlopen", return_value=_Resp({"id": 77})) as u:
            self.assertEqual(m.remember("hello", {"k": "v"}), 77)
        req = u.call_args[0][0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, f"{m.MEMSRV}/remember")
        self.assertEqual(json.loads(req.data), {"text": "hello", "source": "incident", "metadata": {"k": "v"}})

    def test_empty_result_set_and_default_since(self):
        rc, cur, rem, out = _run([])
        self.assertEqual(rc, 0)
        rem.assert_not_called()
        self.assertEqual(cur.sql[0][1], ("2000-01-01",))
        self.assertIn("ingested 0/0 resolved incidents (since 2000-01-01)", out)

    def test_null_services_render_as_dash_and_empty_list(self):
        rc, cur, rem, _ = _run([(1, "t", "critical", "c", None, "2026-10-01", None)])
        text, meta = rem.call_args[0]
        self.assertIn("Affected: —.", text)
        self.assertEqual(meta["affected"], [])
        self.assertEqual(meta["resolved"], "None")


class TestIntegration(unittest.TestCase):
    def test_query_targets_resolved_incidents_with_root_cause(self):
        rc, cur, _, _ = _run([], argv=["x", "2026-09-01"])
        sql, params = cur.sql[0]
        self.assertIn("FROM incidents", sql)
        self.assertIn("status='resolved' AND root_cause IS NOT NULL", sql)
        self.assertEqual(params, ("2026-09-01",))

    def test_memory_shape_lets_the_triage_brain_find_it_again(self):
        rc, cur, rem, _ = _run([ROW])
        text, meta = rem.call_args[0]
        self.assertTrue(text.startswith("[Incident 2026-10-01] Plex down\n"))
        self.assertIn("Severity: warning. Affected: plex.", text)
        self.assertIn("Root cause / resolution: launchd job unloaded", text)
        self.assertEqual(meta["type"], "incident")
        self.assertEqual(meta["incident_id"], "42")           # stable string id for the feedback loop


class TestFunctional(unittest.TestCase):
    def test_golden_path_ingests_every_row(self):
        rows = [ROW, (43, "DNS flap", "critical", "bind restart", ["bind", "dns"], "2026-10-02", "2026-10-02 11:00")]
        rc, cur, rem, out = _run(rows)
        self.assertEqual(rc, 0)
        self.assertEqual(rem.call_count, 2)
        self.assertEqual(rem.call_args_list[1][0][1]["affected"], ["bind", "dns"])
        self.assertIn("ingested 2/2", out)

    def test_pg_failure_surfaces_instead_of_silently_ingesting_nothing(self):
        with patch.object(m.psycopg2, "connect", side_effect=RuntimeError("pg down")), \
             patch.object(sys, "argv", ["x"]), patch.object(m, "remember") as rem:
            with self.assertRaises(RuntimeError):
                m.main()
        rem.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        boot = ("import sys, unittest.mock as um, psycopg2, urllib.request, runpy; "
                "psycopg2.connect = um.MagicMock(side_effect=AssertionError('pg at import')); "
                "urllib.request.urlopen = um.MagicMock(side_effect=AssertionError('net at import')); "
                "runpy.run_path(sys.argv[1], run_name='imported'); print('IMPORT_OK')")
        r = subprocess.run([sys.executable, "-c", boot, str(SCRIPT)], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "IMPORT_OK")


if __name__ == "__main__":
    unittest.main()
