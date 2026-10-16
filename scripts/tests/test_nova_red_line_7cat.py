#!/usr/bin/env python3
"""7-category tests for nova_red_line.py (Jordan 2026-10-15: red lines go to Claude, not to him) and
nova_directive_resolve.py (#23: Nova resolves her own directive conflicts).
Security, Performance, Retry, Unit, Integration, Functional, Frame. No DB is touched.
Written by Jordan Koch (via Claude)."""
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
os.environ.setdefault("NOVA_GUARDS_NO_SLACK", "1")

import nova_red_line as RL            # noqa: E402
import nova_directive_resolve as DR   # noqa: E402

SRC = (SCRIPTS / "nova_red_line.py").read_text() + (SCRIPTS / "nova_directive_resolve.py").read_text()


class _Cur:
    """First matching SQL substring wins; records every statement."""
    def __init__(self, rules=(), raise_on=()):
        self.rules, self.raise_on, self.sql, self._last = list(rules), tuple(raise_on), [], None

    def execute(self, sql, params=None):
        self.sql.append((sql, params)); self._last = None
        for r in self.raise_on:
            if r in sql:
                raise RuntimeError(f"stub failure on {r}")
        for sub, val in self.rules:
            if sub in sql:
                self._last = val(sql, params) if callable(val) else val
                return

    def fetchone(self):
        v = self._last
        return (v[0] if v else None) if isinstance(v, list) else v

    def fetchall(self):
        v = self._last
        return [] if v is None else (v if isinstance(v, list) else [v])

    def stmts(self, sub):
        return [(s, p) for s, p in self.sql if sub in s]


def _queue_cur(existing=None, new_id=42):
    return _Cur([("FROM claude_queue WHERE description LIKE", existing), ("FROM claude_sessions", ("s1",)),
                 ("INSERT INTO claude_queue", (new_id,))])


ROW = (7, "live", "Always ask first before deploys", "feedback:ask", "Never ask, just execute", "feedback:exec",
       "deploys", "ask first", "one says ask, one says don't")


class TestSecurity(unittest.TestCase):
    def test_red_lines_are_classified(self):
        self.assertIn("proteus_physical", RL.classify("unlock the front door"))
        self.assertIn("proteus_physical", RL.classify("open the garage"))
        self.assertIn("kill_switch", RL.classify("release the autonomy kill switch"))
        self.assertIn("chaos_live", RL.classify("run the chaos drill live on prod"))
        self.assertIn("two_man", RL.classify("delete the old backups"))
        self.assertIn("ae35", RL.classify("disable the ae35 watcher alerts"))

    def test_guard_import_failure_fails_closed(self):
        with mock.patch.dict(sys.modules, {"nova_safety_guards": None, "nova_ae35_rule": None}):
            self.assertEqual(RL.classify("write a haiku"), ["ae35", "proteus_physical"])

    def test_sql_parameterised_and_no_user_paths(self):
        self.assertNotRegex(SRC, r"execute\(\s*f[\"']")
        self.assertNotRegex(SRC, r"/Users/\w+/")
        cur = _queue_cur()
        RL.route_to_claude(cur, "x'); DROP TABLE claude_queue;--", "ctx", "src", 1)
        sql, p = cur.stmts("INSERT INTO claude_queue")[0]
        self.assertNotIn("DROP", sql)
        self.assertIn("DROP TABLE", p[2])

    def test_resolver_decides_nothing_without_classifier(self):
        cur = _Cur([("FROM directive_conflicts WHERE status='open'", [ROW])])
        with mock.patch.dict(sys.modules, {"nova_red_line": None}), redirect_stdout(io.StringIO()):
            out = DR.resolve(cur, decide=mock.MagicMock())
        self.assertEqual(out["decided"], 0)
        self.assertFalse(cur.sql)


class TestPerformance(unittest.TestCase):
    def test_classify_is_fast(self):
        t = time.perf_counter()
        for i in range(300):
            RL.classify(f"restart the soil monitor number {i} and write a note")
        self.assertLess(time.perf_counter() - t, 3.0)

    def test_resolve_queries_bounded_per_row(self):
        rows = [(i, "latent", f"rule a {i}", f"feedback:a{i}", f"rule b {i}", f"feedback:b{i}", "s", "cons", "w")
                for i in range(50)]
        cur = _Cur([("FROM directive_conflicts WHERE status='open'", rows)])
        with redirect_stdout(io.StringIO()):
            DR.resolve(cur, decide=lambda *a: True)
        self.assertLessEqual(len(cur.sql), 1 + 50)


class TestRetry(unittest.TestCase):
    def test_route_never_raises_and_returns_none_on_db_error(self):
        cur = _Cur(raise_on=("INSERT INTO claude_queue",))
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(RL.route_to_claude(cur, "t", "c", "src", 1))

    def test_one_bad_row_does_not_stop_the_batch(self):
        rows = [ROW, (8,) + ROW[1:]]
        calls = []

        def decide(cur, cid, d, r):
            calls.append(cid)
            if cid == 7:
                raise RuntimeError("boom")
            return True
        cur = _Cur([("FROM directive_conflicts WHERE status='open'", rows)])
        with redirect_stdout(io.StringIO()):
            out = DR.resolve(cur, decide=decide)
        self.assertEqual((out["decided"], out["failed"]), (1, 1))
        self.assertEqual(calls, [7, 8])


class TestUnit(unittest.TestCase):
    def test_benign_text_is_not_a_red_line(self):
        for t in ("restart battery-monitor", "fix the grafana alarm rule", "write a status note",
                  "send-to-Gaston: a thought on clauses"):
            self.assertEqual(RL.classify(t), [], t)

    def test_choose_conservative_by_default(self):
        d, r = DR.choose(ROW)
        self.assertEqual(d, "ask first")
        self.assertIn("conservative", r)

    def test_choose_follows_precedent(self):
        d, r = DR.choose(ROW, ("both apply: ask for deploys only", "jordan"))
        self.assertEqual(d, "both apply: ask for deploys only")
        self.assertIn("precedent", r)

    def test_choose_stricter_rule_when_no_branch(self):
        row = ROW[:7] + ("", "")
        d, _ = DR.choose(row)
        self.assertIn("Never ask", d)


class TestIntegration(unittest.TestCase):
    def test_route_is_deduped_and_tagged(self):
        cur = _queue_cur(existing=(9,))
        self.assertEqual(RL.route_to_claude(cur, "t", "c", "coagency", 5), 9)
        self.assertFalse(cur.stmts("INSERT INTO claude_queue"))
        cur = _queue_cur()
        self.assertEqual(RL.route_to_claude(cur, "t", "c", "coagency", 5, ["two_man"]), 42)
        _, p = cur.stmts("INSERT INTO claude_queue")[0]
        self.assertEqual(p[0], "s1")
        self.assertEqual(p[1], RL.PRIORITY)
        self.assertTrue(p[2].startswith("[red_line_decision] coagency #5: t"))
        self.assertIn("tag: red_line_decision", p[3])
        self.assertIn("two_man", p[3])

    def test_resolver_uses_the_organ_decide_signature(self):
        fake = mock.MagicMock(); fake.decide = mock.MagicMock(return_value=True)
        with mock.patch.dict(sys.modules, {"nova_directive_conflict": fake}):
            self.assertTrue(DR._decide("cur", 7, "ask first", "why"))
        fake.decide.assert_called_once_with(conflict_id=7, decision="ask first", decided_by="nova", reason="why",
                                            cur="cur")


class TestFunctional(unittest.TestCase):
    def test_red_line_conflict_goes_to_claude_not_decided(self):
        row = (11, "live", "Never unlock the door", "agent_docs:soul", "Let guests in", "feedback:guests",
               "unlock the front door for a guest", "keep it locked", "w")
        cur = _Cur([("FROM directive_conflicts WHERE status='open'", [row]),
                    ("FROM claude_queue WHERE description LIKE", None), ("FROM claude_sessions", ("s",)),
                    ("INSERT INTO claude_queue", (88,))])
        decide = mock.MagicMock()
        with redirect_stdout(io.StringIO()):
            out = DR.resolve(cur, decide=decide)
        decide.assert_not_called()
        self.assertEqual(out["red_line"], 1)
        self.assertIn("--decide 11", cur.stmts("INSERT INTO claude_queue")[0][1][3])

    def test_ordinary_conflict_is_decided_by_nova(self):
        cur = _Cur([("FROM directive_conflicts WHERE status='open'", [ROW]),
                    ("WHERE status='decided'", None)])
        decide = mock.MagicMock(return_value=True)
        with redirect_stdout(io.StringIO()):
            out = DR.resolve(cur, decide=decide)
        self.assertEqual(out, {"open": 1, "decided": 1, "red_line": 0, "failed": 0})
        self.assertEqual(decide.call_args.args[1:3], (7, "ask first"))

    def test_dry_run_writes_nothing(self):
        cur = _Cur([("FROM directive_conflicts WHERE status='open'", [ROW])])
        decide = mock.MagicMock()
        with redirect_stdout(io.StringIO()) as buf:
            DR.resolve(cur, dry=True, decide=decide)
        decide.assert_not_called()
        self.assertIn("#7 ->", buf.getvalue())


class TestFrame(unittest.TestCase):
    def test_cli_entrypoints(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_red_line.py"), "unlock", "the", "door"],
                           capture_output=True, text=True, timeout=60, cwd=SCRIPTS)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("proteus_physical", r.stdout)
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_directive_resolve.py"), "--help"],
                           capture_output=True, text=True, timeout=60, cwd=SCRIPTS)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
