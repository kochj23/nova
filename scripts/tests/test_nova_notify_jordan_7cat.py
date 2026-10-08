#!/usr/bin/env python3
"""7-category gap tests for nova_notify_jordan.py — the proactivity dial (MAX_ITEMS), the Annie
Wilkes rule and the turning-point wiring, and the PG connect retry/backoff. Complements
tests/test_nova_notify_jordan.py. Nothing reaches Slack/Discord/PG: post_both, psycopg2.connect,
nova_annie_rule and nova_turning_point are mocked. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import sys
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_notify_jordan.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_notify_jordan_7cat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.nova_config = types.SimpleNamespace(post_both=mock.MagicMock(), SLACK_CHAN="C_TEST_CHAT")
    return mod


nj = _load()
TS = datetime(2026, 10, 1, 11, 0)
OPERR = nj.psycopg2.OperationalError


class _Cur:
    def __init__(self, rows):
        self.rows = rows; self.sql = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, rows):
        self.cur = _Cur(rows); self.autocommit = False

    def cursor(self):
        return self.cur


def _run(rows, argv=(), connect=None, tp=None, annie=None):
    conn = _Conn(rows)
    nj.nova_config.post_both = mock.MagicMock()
    tp = tp or (lambda oc, r, t: {"allowed": True, "reason": "test"})
    annie = annie or (lambda t: True)
    with mock.patch.object(nj.psycopg2, "connect", **(connect or {"return_value": conn})), \
            mock.patch.object(nj, "turning_point", tp), mock.patch.object(nj, "_annie_ok", annie), \
            mock.patch.object(nj.time, "sleep") as sl, \
            mock.patch.object(sys, "argv", ["nova_notify_jordan.py", *argv]), redirect_stdout(io.StringIO()) as out:
        rc = nj.main()
    return rc, conn, sl, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_guilt_item_never_appears_even_in_dry_run(self):
        rows = [(1, TS, "", "You haven't replied in days."), (2, TS, "", "The NAS scrub finished.")]
        _, conn, _, out = _run(rows, argv=["--dry-run"], annie=lambda t: "replied" not in t)
        self.assertNotIn("haven't replied", out)
        self.assertIn("NAS scrub", out)
        self.assertFalse(any("UPDATE" in s for s, _ in conn.cur.sql))     # dry run writes nothing

    def test_annie_rule_uses_shared_guard(self):
        fake = types.SimpleNamespace(ok=mock.MagicMock(return_value=False))
        with mock.patch.dict(sys.modules, {"nova_annie_rule": fake}):
            self.assertFalse(nj._annie_ok("where have you been"))
        fake.ok.assert_called_once_with("where have you been")

    def test_dropped_ids_are_bound_params(self):
        rows = [(7, TS, "", "x'); DROP TABLE reach_log; --")]
        _, conn, _, _ = _run(rows, annie=lambda t: False)
        sql, params = next(q for q in conn.cur.sql if "dropped" in q[0])
        self.assertNotIn("DROP TABLE", sql)
        self.assertEqual(params, ([7],))


class TestPerformance(unittest.TestCase):
    def test_query_bounded_by_dial_and_no_n_plus_one(self):
        rows = [(i, TS, "", f"item {i}") for i in range(nj.MAX_ITEMS)]
        _, conn, _, _ = _run(rows)
        self.assertEqual(conn.cur.sql[0][1][-1], nj.MAX_ITEMS)        # LIMIT = proactivity dial
        self.assertEqual(len(conn.cur.sql), 2)                          # one SELECT + one batched UPDATE
        self.assertLessEqual(nj.MAX_ITEMS, 10)


class TestRetry(unittest.TestCase):
    def test_connect_recovers_after_two_failures(self):
        conn = _Conn([(1, TS, "", "one")])
        rc, _, sl, _ = _run([], connect={"side_effect": [OPERR("a"), OPERR("b"), conn]})
        self.assertEqual(rc, 0)
        nj.nova_config.post_both.assert_called_once()
        self.assertEqual([c[0][0] for c in sl.call_args_list], [2, 4])  # linear backoff

    def test_connect_exhausted_raises_loudly(self):
        with self.assertRaises(OPERR):
            _run([], connect={"side_effect": OPERR("down")})
        nj.nova_config.post_both.assert_not_called()

    def test_post_failure_does_not_mark_sent(self):
        conn = _Conn([(1, TS, "", "one")])
        nj.nova_config.post_both = mock.MagicMock(side_effect=OSError("slack down"))
        with mock.patch.object(nj.psycopg2, "connect", return_value=conn), \
                mock.patch.object(nj, "turning_point", lambda *a: {"allowed": True, "reason": ""}), \
                mock.patch.object(nj, "_annie_ok", lambda t: True), \
                mock.patch.object(sys, "argv", ["x"]), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                nj.main()
        self.assertFalse(any("status='sent'" in s for s, _ in conn.cur.sql))


class TestUnit(unittest.TestCase):
    def test_turning_point_stakes_and_ceiling(self):
        fake = types.SimpleNamespace(decide=mock.MagicMock(return_value={"allowed": True, "reason": "ok"}))
        with mock.patch.dict(sys.modules, {"nova_turning_point": fake}):
            nj.turning_point("cur", [1, 2, 3], "text")
            nj.turning_point("cur", list(range(20)), "text")
        (a1, k1), (a2, k2) = fake.decide.call_args_list
        self.assertEqual(a1, ("cur", "notify-bundle"))
        self.assertAlmostEqual(k1["stakes"], 0.65)
        self.assertEqual(k2["stakes"], 1.0)                              # capped
        self.assertEqual(k1["ceiling"], "mention")

    def test_turning_point_fails_open_when_module_broken(self):
        with mock.patch.dict(sys.modules, {"nova_turning_point": None}):
            r = nj.turning_point("cur", [1], "t")
        self.assertTrue(r["allowed"])
        self.assertIn("unavailable", r["reason"])

    def test_header_has_no_absence_framing(self):
        self.assertNotIn("since we last talked", nj.bundle([(1, TS, "", "x")]).lower())


class TestIntegration(unittest.TestCase):
    def test_real_annie_rule_blocks_guilt(self):
        self.assertFalse(nj._annie_ok("Where have you been? I've been waiting for you."))
        self.assertTrue(nj._annie_ok("The backup finished cleanly at 03:10."))

    def test_turning_point_receives_bundle_text(self):
        seen = {}

        def tp(oc, rows, text):
            seen["text"] = text; seen["n"] = len(rows)
            return {"allowed": True, "reason": ""}
        _run([(1, TS, "dns", "one"), (2, TS, "", "two")], tp=tp)
        self.assertEqual(seen["n"], 2)
        self.assertEqual(seen["text"], nj.nova_config.post_both.call_args[0][0])


class TestFunctional(unittest.TestCase):
    def test_golden_mixed_drawer(self):
        rows = [(1, TS, "nas", "Scrub done."), (2, TS, "", "Finally back? I missed you."), (3, TS, "", "Cert renewed.")]
        rc, conn, _, _ = _run(rows, annie=lambda t: "missed you" not in t)
        self.assertEqual(rc, 0)
        text = nj.nova_config.post_both.call_args[0][0]
        self.assertTrue(text.startswith("*Things I noticed* (2):"))
        self.assertEqual(conn.cur.sql[1][1], ([2],))                     # dropped
        self.assertEqual(conn.cur.sql[-1][1], ([1, 3],))                 # sent

    def test_all_items_fail_annie_posts_nothing(self):
        rc, conn, _, _ = _run([(1, TS, "", "x")], annie=lambda t: False)
        self.assertEqual(rc, 0)
        nj.nova_config.post_both.assert_not_called()
        self.assertFalse(any("status='sent'" in s for s, _ in conn.cur.sql))


class TestFrame(unittest.TestCase):
    def test_import_has_no_side_effects(self):
        with mock.patch("psycopg2.connect") as c:
            _load()
        c.assert_not_called()

    def test_entrypoints(self):
        self.assertTrue(callable(nj.main))
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertIsInstance(nj.MAX_ITEMS, int)


if __name__ == "__main__":
    unittest.main()
