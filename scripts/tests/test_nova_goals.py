#!/usr/bin/env python3
"""Tests for nova_goals.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_goals.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("goals_ut", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.log = MagicMock()              # nova_logger writes ~/.openclaw/logs/nova.jsonl
    return mod


g = _load()


class _Psql:
    """subprocess.run stand-in for psql: records SQL, answers -tAc queries from a queue."""
    def __init__(self, answers=None, rc=0):
        self.sql = []; self.answers = list(answers or []); self.rc = rc

    def __call__(self, cmd, **kw):
        if cmd[0] == "git":
            return MagicMock(returncode=0, stdout="fix bug\nadd test\n")
        self.sql.append(cmd[-1])
        out = self.answers.pop(0) if ("-tAc" in cmd and self.answers) else ""
        return MagicMock(returncode=self.rc, stdout=out, stderr="ERROR: boom" if self.rc else "")


def _with(psql):
    return patch.object(g.subprocess, "run", psql)


def _row(i="g1", title="Ship MLXCode", proj="MLXCode", pri="high", dl="", ck="7", last=None):
    last = last or datetime.now().strftime("%Y-%m-%d %H:%M:%S.123+00")
    return f"{i}|{title}|{proj}|{pri}|{dl}|{ck}|{last}|2026-01-01"


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_hostile_goal_id_cannot_widen_the_where(self):
        p = _Psql()
        with _with(p):
            g.complete_goal("x' OR '1'='1")
        self.assertIn("WHERE id = 'x'' OR ''1''=''1'", p.sql[0])
        self.assertNotIn("WHERE id = 'x' OR", p.sql[0])

    def test_text_fields_escaped_on_insert(self):
        p = _Psql()
        with _with(p):
            g.add_goal("Jordan's goal", deadline="2026-01-01'); --x", priority="high")
        self.assertIn("'Jordan''s goal'", p.sql[0])
        self.assertIn("'2026-01-01''); --x'", p.sql[0])

    def test_psql_runs_without_shell(self):
        self.assertNotIn("shell=True", SRC)


class TestPerformance(unittest.TestCase):
    def test_parse_10k_goal_rows(self):
        rows = "\n".join(_row(i=f"g{i}", last=(datetime.now() - timedelta(days=i % 20)).strftime("%Y-%m-%d %H:%M:%S"))
                         for i in range(10_000))
        with _with(_Psql([rows])):
            t0 = time.perf_counter()
            stale = g.get_stale_goals()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(stale), sum(1 for i in range(10_000) if i % 20 >= 7))


class TestRetry(unittest.TestCase):
    def test_query_failure_fails_open(self):
        # RETRY GAP: _query/psql — one attempt, [] on failure
        calls = []

        def boom(*a, **k):
            calls.append(1); raise subprocess.TimeoutExpired("psql", 10)
        with _with(boom):
            self.assertEqual(g.get_active_goals(), [])
        self.assertEqual(len(calls), 1)

    def test_exec_failure_returns_false_and_skips_event(self):
        # RETRY GAP: _exec/psql — non-zero exit is reported once as False, no follow-up writes
        p = _Psql(rc=1)
        with _with(p):
            self.assertIsNone(g.add_goal("t"))
        self.assertEqual(len(p.sql), 1)


class TestUnit(unittest.TestCase):
    def test_update_goal_whitelists_fields(self):
        p = _Psql()
        with _with(p):
            self.assertFalse(g.update_goal("g1", owner="mallory"))
            g.update_goal("g1", title="New", deadline=None, check_in_days="3")
        self.assertEqual(len(p.sql), 1)
        self.assertIn("title = 'New', deadline = NULL, check_in_days = 3, updated_at = NOW()", p.sql[0])

    def test_escape(self):
        self.assertEqual(g._escape(None), "")
        self.assertEqual(g._escape("it's"), "it''s")

    def test_summary_and_history_parsing(self):
        with _with(_Psql(["3", "", "1"])):
            self.assertEqual(g.goal_summary(), {"active": 3, "completed": 0, "paused": 1})
        with _with(_Psql(["2026-01-01|progress|did a thing\nmalformed"])):
            self.assertEqual(g.get_goal_history("g1"), [{"timestamp": "2026-01-01", "type": "progress", "note": "did a thing"}])

    def test_unparseable_last_activity_skipped(self):
        with _with(_Psql([_row(last="garbage")])):
            self.assertEqual(g.get_stale_goals(), [])


class TestIntegration(unittest.TestCase):
    def test_add_goal_writes_goal_then_log(self):
        p = _Psql()
        with _with(p):
            gid = g.add_goal("Run 5k", project="Health")
        self.assertRegex(gid, r"^[0-9a-f-]{8}$")
        self.assertIn("INSERT INTO goals", p.sql[0])
        self.assertIn("INSERT INTO goal_log", p.sql[1])
        self.assertIn(f"'{gid}', 'created'", p.sql[1])

    def test_git_activity_touches_goal(self):
        p = _Psql([_row(proj="proj")])
        tmp = Path(tempfile.mkdtemp(prefix="goals_git_"))
        (tmp / "proj").mkdir()
        with _with(p):
            g.detect_activity_from_git(str(tmp))
        self.assertTrue(any("SET last_activity = NOW() WHERE id = 'g1'" in s for s in p.sql))
        self.assertTrue(any("'git_activity'" in s and "2 commit(s) today: fix bug" in s for s in p.sql))


class TestFunctional(unittest.TestCase):
    def test_brief_includes_gaps(self):
        old = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
        rows = _row(last=old)
        overdue = "g1|Ship MLXCode|MLXCode|high|2020-01-01"
        with _with(_Psql([rows, rows, overdue, rows])):
            out = g.format_goals_brief()
        self.assertIn("🔴 Ship MLXCode [MLXCode]", out)
        self.assertIn("*Overdue:*", out)
        self.assertIn("30d idle", out)

    def test_main_list_and_empty_brief(self):
        buf = io.StringIO()
        with _with(_Psql([_row()])), patch.object(sys, "argv", ["x", "list"]), redirect_stdout(buf):
            g.main()
        self.assertIn("[g1] HIGH   Ship MLXCode [MLXCode]", buf.getvalue())
        buf = io.StringIO()
        with _with(_Psql([""])), patch.object(sys, "argv", ["x", "brief"]), redirect_stdout(buf):
            g.main()
        self.assertEqual(buf.getvalue().strip(), "No active goals.")


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Nova Goals Manager", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)


if __name__ == "__main__":
    unittest.main()
