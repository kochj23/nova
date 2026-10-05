#!/usr/bin/env python3
"""Tests for nova_projects.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_projects.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pj = _load("pj_under_test", SCRIPT)


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
    def __init__(self, routes=None, one=None):
        self.routes, self.one = routes or {}, one or {}
        self.sql, self.params, self._last = [], [], ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _route(self, table, default):
        for k, v in table.items():
            if k in self._last:
                return v
        return default

    def fetchall(self):
        return self._route(self.routes, [])

    def fetchone(self):
        return self._route(self.one, None)


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur

    def close(self):
        pass


START_JSON = json.dumps({"title": "Trace every alert in the house", "why": "Grows from my ops preoccupation.",
                         "milestones": ["Inventory alert sources", "Map the bus", "Find the orphans", "Write it up"]})
WORK = ("I traced the first three sources to the notifier bus and found one orphan alert that nobody drains. "
        "That is a real increment, not a plan.\nNEXT: chase the orphan's producer\nMILESTONE_DONE: yes")


def _start_cur():
    return _Cur(routes={"FROM preoccupations": [("ops alerts", "interest", 4, "where alerts are born")],
                        "FROM taste": [("terse dashboards", "ops", "like", 0.8)],
                        "status='completed'": [("the coaxial escapement",)]},
                one={"RETURNING id": (5,)})


def _work_cur(done_count=1):
    return _Cur(routes={"FROM project_milestones": [(1, 0, "m1", "todo"), (2, 1, "m2", "todo")],
                        "FROM project_log": [("note one",)]},
                one={"FROM projects WHERE status='active'": (5, "T", "why", 0),
                     "count(*) FILTER": (done_count, 2)})


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_fully_parameterized(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertNotIn('execute(f\'', SRC)
        self.assertNotIn('""" %', SRC)
        self.assertNotIn('" %', SRC)

    def test_llm_output_is_bounded_before_storage(self):
        self.assertLessEqual(len(pj._one_line("x " * 10_000)), 400)
        self.assertEqual(len(pj._one_line("a" * 1000)), 400)

    def test_owns_only_its_three_tables(self):
        tables = set(re.findall(r"(?:INSERT INTO|UPDATE)\s+(\w+)", SRC))
        self.assertEqual(tables, {"projects", "project_milestones", "project_log"})


class TestPerformance(unittest.TestCase):
    def test_text_helpers_fast_on_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            pj._one_line(f"  a  b {i}  ")
            pj._extract_json(f'junk {{"i": {i}}} trailing')
            pj._parse_mode(["--mode", "work", str(i)])
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_nodes(self):
        calls = []

        def fake(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("down")
            return _Resp({"message": {"content": "ok"}})
        with mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertEqual(pj.llm("x"), "ok")
        self.assertEqual(len(calls), 3)

    def test_recall_fails_open(self):
        # RETRY GAP: recall — one GET, returns [] on any error.
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            self.assertEqual(pj.recall("q"), [])

    def test_remember_no_retry_but_start_survives(self):
        # RETRY GAP: remember — mode_start wraps it; the project row is still saved.
        cur = _start_cur()
        with mock.patch.object(pj, "llm", return_value=START_JSON), \
             mock.patch("urllib.request.urlopen", side_effect=OSError("down")), redirect_stdout(StringIO()):
            self.assertEqual(pj.mode_start(cur), 0)
        self.assertTrue(any("INSERT INTO projects" in s for s in cur.sql))

    def test_accessor_fails_open(self):
        with mock.patch("psycopg2.connect", side_effect=OSError("down")):
            self.assertEqual(pj.current_project(), "")


class TestUnit(unittest.TestCase):
    def test_extract_json_and_one_line(self):
        self.assertEqual(pj._extract_json('pre {"a": 1} post'), '{"a": 1}')
        self.assertEqual(pj._extract_json("no json"), "no json")
        self.assertEqual(pj._one_line("  a \n b  "), "a b")
        self.assertEqual(pj._one_line(None), "")

    def test_parse_mode(self):
        self.assertEqual(pj._parse_mode([]), "status")
        self.assertEqual(pj._parse_mode(["--mode=work"]), "work")
        self.assertEqual(pj._parse_mode(["--mode", "start"]), "start")
        self.assertEqual(pj._parse_mode(["--mode"]), "status")

    def test_work_markers(self):
        self.assertEqual(pj._NEXT_RX.search(WORK).group(1), "chase the orphan's producer")
        self.assertEqual(pj._DONE_RX.search(WORK).group(1).lower(), "yes")
        self.assertIsNone(pj._DONE_RX.search("MILESTONE_DONE: maybe"))

    def test_lookup_runs_with_empty_seed(self):
        with redirect_stdout(StringIO()):
            self.assertEqual(pj.mode_start(_Cur()), 0)  # nothing genuine to choose -> no LLM call


class TestIntegration(unittest.TestCase):
    def test_work_increment_moves_only_completed_milestones(self):
        cur = _work_cur(done_count=1)
        with mock.patch.object(pj, "recall", return_value=[{"text": "material"}]), \
             mock.patch.object(pj, "llm", return_value=WORK), \
             mock.patch.object(pj, "remember", return_value="mem1"), redirect_stdout(StringIO()):
            self.assertEqual(pj.mode_work(cur), 0)
        log_ins = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO project_log" in s][0]
        self.assertEqual(log_ins[0], 5); self.assertEqual(log_ins[2], "mem1")
        self.assertEqual(log_ins[3], "chase the orphan's producer")
        self.assertNotIn("NEXT:", log_ins[1]); self.assertNotIn("MILESTONE_DONE", log_ins[1])
        self.assertTrue(any("SET status='done'" in s and p == (1,) for s, p in zip(cur.sql, cur.params)))
        prog = [p for s, p in zip(cur.sql, cur.params) if "SET progress_pct" in s][0]
        self.assertEqual(prog, (50, 5))
        self.assertFalse(any("status='completed'" in s for s in cur.sql))

    def test_last_milestone_completes_the_project(self):
        cur = _work_cur(done_count=2)
        with mock.patch.object(pj, "recall", return_value=[]), mock.patch.object(pj, "llm", return_value=WORK), \
             mock.patch.object(pj, "remember", return_value="m"), redirect_stdout(StringIO()):
            pj.mode_work(cur)
        self.assertTrue(any("status='completed'" in s for s in cur.sql))

    def test_ensure_tables_and_accessor_shape(self):
        cur = _Cur()
        pj.ensure_tables(cur)
        self.assertEqual(len(cur.sql), 3)
        self.assertEqual(set(pj.MODES), {"start", "work", "status"})
        row = _Cur(one={"LEFT JOIN LATERAL": ("T", 50, "did a thing", "next thing")})
        with mock.patch("psycopg2.connect", return_value=_Conn(row)):
            self.assertEqual(pj.current_project(), "I'm working on T (50%); last: did a thing; next: next thing.")


class TestFunctional(unittest.TestCase):
    def test_main_start_golden_path(self):
        cur = _start_cur()
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), \
             mock.patch.object(pj, "llm", return_value=START_JSON), \
             mock.patch.object(pj, "remember", return_value="m1") as rem, \
             mock.patch.object(pj, "_stamp", return_value=None), \
             mock.patch.object(sys, "argv", ["nova_projects.py", "--mode", "start"]), redirect_stdout(StringIO()) as out:
            self.assertEqual(pj.main(), 0)
        ms = [p for s, p in zip(cur.sql, cur.params) if "INSERT INTO project_milestones" in s]
        self.assertEqual([m[1] for m in ms], [0, 1, 2, 3])
        self.assertEqual(ms[0][0], 5)
        self.assertEqual(rem.call_args[0][1], "projects")
        self.assertIn("STARTED #5", out.getvalue())

    def test_llm_outage_starts_nothing(self):
        cur = _start_cur()
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), mock.patch.object(pj, "llm", return_value=""), \
             mock.patch.object(sys, "argv", ["nova_projects.py", "--mode=start"]), redirect_stdout(StringIO()):
            self.assertEqual(pj.main(), 1)
        self.assertFalse(any("INSERT INTO projects" in s for s in cur.sql))

    def test_unknown_mode_never_connects(self):
        with mock.patch("psycopg2.connect") as c, mock.patch.object(sys, "argv", ["x", "--mode", "bogus"]), \
             redirect_stdout(StringIO()):
            self.assertEqual(pj.main(), 2)
        self.assertEqual(c.call_count, 0)


class TestFrame(unittest.TestCase):
    def test_bogus_mode_exits_2_without_pg(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--mode", "bogus"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("unknown mode", r.stdout)

    def test_import_is_clean_and_guarded(self):
        r = subprocess.run([sys.executable, "-c", "import nova_projects"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
