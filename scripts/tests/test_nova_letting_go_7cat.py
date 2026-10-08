#!/usr/bin/env python3
"""7-category tests for the wish #70 change to nova_letting_go.py: the lockbox proposal hook
(propose_lockboxes -> nova_relationship.propose_boxes). Letting go of a painful memory means
PROPOSING a box — never deleting, never boxing without Jordan's yes. Security, Performance, Retry,
Unit, Integration, Functional, Frame. Offline fakes only; no DB, no Slack, no LLM.
Written by Jordan Koch (via Claude)."""
import inspect
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_letting_go as lg  # noqa: E402
import nova_relationship as rel  # noqa: E402

HOOK_SRC = inspect.getsource(lg.propose_lockboxes)
BOX_SRC = inspect.getsource(rel.propose_boxes)


class _Cur:
    """Records SQL; memory rows answer the SELECT on memories; `fail` raises that many times first."""
    def __init__(self, rows=(), fail=0):
        self.rows, self.fail, self.sql, self.rowcount = list(rows), fail, [], 0
        self.connection = mock.MagicMock()

    def execute(self, sql, params=None):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("could not connect to server")
        self.sql.append((" ".join(sql.split()), params))
        self.rowcount = 1 if "INSERT INTO lockbox" in sql else 0

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return None

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


def _painful(n):
    return [(f"m{i}", "conversation", f"he said someone threatened to kill him, day {i}") for i in range(n)]


def _hook(oc, mc):
    with mock.patch.object(lg.time, "sleep") as sl, mock.patch.object(rel, "log"):
        return lg.propose_lockboxes(oc, mc), sl


class TestSecurity(unittest.TestCase):
    def test_hook_never_deletes_or_boxes(self):
        for src in (HOOK_SRC, BOX_SRC):
            self.assertNotRegex(src, r"(?i)\bDELETE\b")
            self.assertNotIn("boxed=true", src.replace(" ", ""))
        oc, mc = _Cur(), _Cur(_painful(2))
        n, _ = _hook(oc, mc)
        self.assertEqual(n, 2)
        self.assertFalse([s for s, _ in oc.sql + mc.sql if s.lstrip().upper().startswith(("DELETE", "UPDATE"))])
        self.assertTrue(all(s.startswith("SELECT") for s, _ in mc.sql))  # memories are only read

    def test_proposals_are_parameterised_and_marked_letting_go(self):
        oc, mc = _Cur(), _Cur([("m1'; DROP TABLE memories;--", "conversation", "harassment again")])
        _hook(oc, mc)
        (sql, params), = oc.ran("INSERT INTO lockbox")
        self.assertNotIn("DROP", sql)
        self.assertEqual(params[0], "m1'; DROP TABLE memories;--")
        self.assertIn("'letting_go'", sql)
        self.assertIn("ON CONFLICT (memory_id) DO NOTHING", sql)

    def test_reason_never_carries_the_whole_memory(self):
        oc, mc = _Cur(), _Cur([("m1", "imessage", "private detail private detail. he was stalked by X")])
        _hook(oc, mc)
        reason = oc.ran("INSERT INTO lockbox")[0][1][1]
        self.assertNotIn("private detail", reason)
        self.assertIn("stalked", reason)

    def test_only_personal_sources_queried(self):
        mc = _Cur()
        _hook(_Cur(), mc)
        params = mc.ran("FROM memories")[0][1]
        self.assertEqual(tuple(params[0]), rel.PAINFUL_SOURCES)


class TestPerformance(unittest.TestCase):
    def test_bounded_query_and_capped_proposals(self):
        self.assertIn("LIMIT 20", BOX_SRC)
        oc, mc = _Cur(), _Cur(_painful(1000))
        t = time.monotonic()
        n, _ = _hook(oc, mc)
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual(n, rel.MAX_PROPOSALS)
        self.assertEqual(len(oc.ran("INSERT INTO lockbox")), rel.MAX_PROPOSALS)


class TestRetry(unittest.TestCase):
    def test_transient_failure_retries_with_backoff(self):
        oc, mc = _Cur(), _Cur(_painful(1), fail=2)
        n, sl = _hook(oc, mc)
        self.assertEqual(n, 1)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [0.5, 1.0])
        self.assertGreaterEqual(mc.connection.rollback.call_count, 2)

    def test_gives_up_after_three_attempts_and_returns_zero(self):
        mc = _Cur(fail=99)
        with mock.patch.object(rel, "propose_boxes", wraps=rel.propose_boxes) as pb:
            n, sl = _hook(_Cur(), mc)
        self.assertEqual(n, 0)
        self.assertEqual(pb.call_count, 3)
        self.assertEqual(sl.call_count, 2)

    def test_import_failure_is_skipped_not_raised(self):
        with mock.patch.dict(sys.modules, {"nova_relationship": None}):
            n, sl = _hook(_Cur(), _Cur())
        self.assertEqual(n, 0)
        sl.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_returns_propose_boxes_count(self):
        with mock.patch.object(rel, "propose_boxes", return_value=2):
            self.assertEqual(_hook(object(), object())[0], 2)

    def test_painful_filter_is_explicit_and_pushed_to_the_query(self):
        mc = _Cur()
        _hook(_Cur(), mc)
        self.assertEqual(mc.ran("FROM memories")[0][1][2], rel._PAINFUL.pattern)
        self.assertIsNone(rel._PAINFUL.search("we watched a movie and it was lovely"))
        self.assertIsNotNone(rel._PAINFUL.search("a death threat by email"))


class TestIntegration(unittest.TestCase):
    def test_review_calls_hook_before_nominating(self):
        order = []
        with mock.patch.object(lg, "apply_goal_retirements", lambda oc: order.append("apply")), \
             mock.patch.object(lg, "propose_goal_retirements", lambda oc: order.append("goals")), \
             mock.patch.object(lg, "propose_lockboxes", lambda oc, mc: order.append("lockbox")), \
             mock.patch.object(lg, "nominate_preoccupations", lambda oc, mc: order.append("nominate") or []), \
             mock.patch.object(lg, "nominate_projects", lambda oc: []), \
             mock.patch.object(lg, "nominate_taste", lambda oc: []):
            self.assertEqual(lg.run_review(object(), object()), 0)
        self.assertEqual(order, ["apply", "goals", "lockbox", "nominate"])


class TestFunctional(unittest.TestCase):
    def _review(self, oc, mc):
        with mock.patch.object(lg, "apply_goal_retirements", return_value=0), \
             mock.patch.object(lg, "propose_goal_retirements", return_value=0), \
             mock.patch.object(lg, "nominate_preoccupations", return_value=[]), \
             mock.patch.object(lg, "nominate_projects", return_value=[]), \
             mock.patch.object(lg, "nominate_taste", return_value=[]), \
             mock.patch.object(lg.time, "sleep"), mock.patch.object(rel, "log"):
            return lg.run_review(oc, mc)

    def test_golden_review_files_one_proposal_deletes_nothing(self):
        oc, mc = _Cur(), _Cur(_painful(1))
        self.assertEqual(self._review(oc, mc), 0)
        self.assertEqual(len(oc.ran("INSERT INTO lockbox")), 1)
        self.assertFalse([s for s, _ in oc.sql + mc.sql if "DELETE" in s.upper()])

    def test_error_path_memories_down_review_still_completes(self):
        self.assertEqual(self._review(_Cur(), _Cur(fail=99)), 0)


class TestFrame(unittest.TestCase):
    def test_import_side_effect_free_and_main_guarded(self):
        r = subprocess.run([sys.executable, "-c", "import nova_letting_go, nova_relationship"], cwd=SCRIPTS,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        src = (SCRIPTS / "nova_letting_go.py").read_text()
        self.assertIn('if __name__ == "__main__":', src)
        self.assertNotRegex(src, r"/Users/[a-z]")


if __name__ == "__main__":
    unittest.main()
