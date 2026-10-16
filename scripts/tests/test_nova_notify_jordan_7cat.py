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
    mod.quiet_active = lambda cur=None: False   # quiet mode is covered in its own tests
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


# ══ 2026-10-08: quiet mode — the drawer shrinks to QUIET_MAX_ITEMS, terse, the rest stay held ═══
def _run_quiet(rows, active):
    with mock.patch.object(nj, "quiet_active", lambda cur=None: active):
        return _run(rows)


class TestQuietSecurity(unittest.TestCase):
    def test_annie_rule_still_applies_while_quiet(self):
        rows = [(1, TS, "", "You haven't replied in days.")]
        with mock.patch.object(nj, "quiet_active", lambda cur=None: True):
            _, conn, _, _ = _run(rows, annie=lambda t: False)
        nj.nova_config.post_both.assert_not_called()


class TestQuietPerformance(unittest.TestCase):
    def test_quiet_sends_at_most_quiet_max(self):
        rows = [(i, TS, "", f"item {i}") for i in range(5)]
        _run_quiet(rows, True)
        txt = nj.nova_config.post_both.call_args[0][0]
        self.assertEqual(txt.count("•"), nj.QUIET_MAX_ITEMS)


class TestQuietRetry(unittest.TestCase):
    def test_failed_post_leaves_drawer_held(self):
        rows = [(1, TS, "", "The NAS scrub finished.")]
        conn = _Conn(rows)
        with mock.patch.object(nj.psycopg2, "connect", return_value=conn), \
                mock.patch.object(nj, "turning_point", lambda *a: {"allowed": True, "reason": "t"}), \
                mock.patch.object(nj, "_annie_ok", lambda t: True), \
                mock.patch.object(sys, "argv", ["nova_notify_jordan.py"]), redirect_stdout(io.StringIO()):
            nj.nova_config.post_both = mock.MagicMock(return_value=False)
            self.assertEqual(nj.main(), 1)
        self.assertFalse(any("status='sent'" in s for s, _ in conn.cur.sql))


class TestQuietUnit(unittest.TestCase):
    def test_terse_bundle(self):
        t = nj.bundle([(1, TS, "nas", "scrub done")], quiet=True)
        self.assertEqual(t, "• scrub done")
        self.assertIn("Things I noticed", nj.bundle([(1, TS, "nas", "scrub done")]))


class TestQuietIntegration(unittest.TestCase):
    def test_only_delivered_ids_marked_sent(self):
        rows = [(i, TS, "", f"item {i}") for i in (11, 12, 13)]
        _, conn, _, _ = _run_quiet(rows, True)
        sql, params = next(q for q in conn.cur.sql if "status='sent'" in q[0])
        self.assertEqual(params, ([11],))


class TestQuietFunctional(unittest.TestCase):
    def test_not_quiet_full_bundle(self):
        rows = [(i, TS, "", f"item {i}") for i in range(3)]
        _run_quiet(rows, False)
        self.assertIn("Things I noticed* (3)", nj.nova_config.post_both.call_args[0][0])


class TestQuietFrame(unittest.TestCase):
    def test_quiet_constants(self):
        self.assertGreaterEqual(nj.QUIET_MAX_ITEMS, 1)
        self.assertLessEqual(nj.QUIET_MAX_ITEMS, nj.MAX_ITEMS)



# ═════════════════════════════════════════════════════════════════════════════
# 2026-10-15 — ONE daily 18:00 summary + immediate messages only for real impact
# ═════════════════════════════════════════════════════════════════════════════
import time  # noqa: E402
from datetime import date  # noqa: E402


class _SumCur:
    """Answers by SQL substring; records statements. Missing sources raise like a missing table."""
    def __init__(self, answers=(), missing=()):
        self.answers, self.missing, self.sql, self._rows = list(answers), tuple(missing), [], []
        self.connection = mock.MagicMock()

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        for m in self.missing:
            if m in sql:
                raise RuntimeError(f'relation "{m}" does not exist')
        self._rows = next((v for k, v in self.answers if k in sql), [])

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def stmts(self, frag):
        return [(q, p) for q, p in self.sql if frag in q]


DAY = date(2026, 10, 15)
FULL = [("FROM coagency_proposals", [("dropped", 2), ("approved", 1)]),
        ("FROM directive_conflicts", [(3, "ask first")]),
        ("FROM claude_reviewer_log", [("APPROVE", 4)]),
        ("FROM autonomy_ledger", [("restart:x", 2)]),
        ("FROM outbound_ledger", [("email", 1)]),
        ("FROM claude_queue\n                     WHERE completed_at", [(5, "Build wish #9")]),
        ("FROM problems", [("DNS flaps",)]),
        ("FROM postmortems", [("PM: NAS outage",)]),
        ("FROM letting_go_log", [("organ", "fishbowl daily")]),
        ("FROM herd_correspondents", []),
        ("FROM flop_slate", []),
        ("FROM claude_queue WHERE description LIKE", [(1, 2)]),
        ("FROM reach_log", [(7, TS, "t", "the moon is close tonight")]),
        ("FROM nova_daily_summaries", [])]


def _daily(cur, post=None, dry=False, question=None):
    ask = types.SimpleNamespace(question_for_summary=lambda c: question)
    with mock.patch.dict(sys.modules, {"nova_ask_one": ask, "nova_pg_schema": mock.MagicMock()}), \
         mock.patch.object(nj, "_annie_ok", return_value=True), redirect_stdout(io.StringIO()) as out:
        rc = nj.daily(cur, dry=dry, day=DAY, post=post)
    return rc, out.getvalue()


class TestDailySecurity(unittest.TestCase):
    def test_impact_bar_lets_only_real_impact_through(self):
        for t in ("Smoke detected in the garage", "water leak sensor under the sink", "front door unlocked at 2am",
                  "tier-0 gateway is down", "card was charged $400", "backups were deleted"):
            self.assertTrue(nj.is_real_impact(t)[0], t)
        for t in ("I read a lovely essay about tides", "a disk is 81% full", "new episode of a show you like"):
            self.assertFalse(nj.is_real_impact(t)[0], t)
        self.assertTrue(nj.is_real_impact("x", tier=0)[0])
        self.assertTrue(nj.is_real_impact("x", level="critical", category="security")[0])
        self.assertFalse(nj.is_real_impact("x", level="info", category="security")[0])

    def test_summary_sql_is_parameterised(self):
        cur = _SumCur(FULL)
        _daily(cur, post=lambda t: (True, "9.9"))
        for q, p in cur.sql:
            if "nova_daily_summaries" in q and "CREATE" in q:
                continue
            self.assertIsNotNone(p, q)


class TestDailyPerformance(unittest.TestCase):
    def test_compose_is_bounded_and_fast(self):
        big = {k: [f"item {i}" for i in range(500)] for k in ("decided", "did", "fixed", "retired", "claude")}
        t = time.perf_counter()
        text = nj.compose_daily(DAY, big)
        self.assertLess(time.perf_counter() - t, 0.5)
        self.assertLess(len(text), 1200)          # terse: SUMMARY_LINES per section


class TestDailyRetry(unittest.TestCase):
    def test_failed_post_records_error_and_returns_1_for_a_retry(self):
        cur = _SumCur(FULL)
        rc, _ = _daily(cur, post=lambda t: (False, None))
        self.assertEqual(rc, 1)
        (_, p), = cur.stmts("INSERT INTO nova_daily_summaries")
        self.assertEqual(p[-1], "post failed")
        self.assertFalse(cur.stmts("UPDATE reach_log"))

    def test_already_posted_today_is_a_no_op(self):
        cur = _SumCur([("FROM nova_daily_summaries", [(TS,)])])
        post = mock.MagicMock()
        rc, _ = _daily(cur, post=post)
        self.assertEqual(rc, 0)
        post.assert_not_called()


class TestDailyUnit(unittest.TestCase):
    def test_compose_omits_empty_sections_and_nothing_means_none(self):
        self.assertIsNone(nj.compose_daily(DAY, {}))
        t = nj.compose_daily(DAY, {"fixed": ["problem closed: DNS"]})
        self.assertIn("*Fixed:* problem closed: DNS", t)
        self.assertNotIn("Decided", t)
        self.assertIn("FYI", t)

    def test_question_is_optional_and_about_him(self):
        t = nj.compose_daily(DAY, {}, question=(12, "Do you prefer warmer light in the evening?"))
        self.assertIn("Q#12", t)
        self.assertIn("only if you feel like it", t)


class TestDailyIntegration(unittest.TestCase):
    def test_missing_sources_drop_lines_not_the_summary(self):
        cur = _SumCur(FULL, missing=("postmortems", "outbound_ledger", "letting_go_log"))
        posted = []
        rc, _ = _daily(cur, post=lambda t: posted.append(t) or (True, "1.2"))
        self.assertEqual(rc, 0)
        self.assertIn("*Decided:*", posted[0])
        self.assertNotIn("postmortem", posted[0])


class TestDailyFunctional(unittest.TestCase):
    def test_golden_path_posts_once_marks_drawer_and_records_question(self):
        cur = _SumCur(FULL)
        posted = []
        rc, _ = _daily(cur, post=lambda t: posted.append(t) or (True, "1700.1"),
                       question=(12, "Do you prefer warmer light in the evening?"))
        self.assertEqual(rc, 0)
        self.assertEqual(len(posted), 1)
        t = posted[0]
        for frag in ("*Decided:* proposals: 2 dropped, 1 approved", "3 directive conflict(s) resolved myself",
                     "*Did:*", "*Fixed:* problem closed: DNS flaps", "*Retired:* organ: fishbowl daily",
                     "*Red lines → Claude:* 1 new", "*Noticed* (1)", "Q#12"):
            self.assertIn(frag, t)
        self.assertEqual(cur.stmts("UPDATE reach_log SET status='sent'")[0][1], ([7],))
        self.assertEqual(cur.stmts("INSERT INTO slack_prompts")[0][1], ("12", "C_TEST_CHAT", "1700.1"))

    def test_dry_run_prints_and_writes_nothing(self):
        cur = _SumCur(FULL)
        post = mock.MagicMock()
        rc, out = _daily(cur, post=post, dry=True)
        post.assert_not_called()
        self.assertIn("Nova — Thu Oct 15", out)
        self.assertFalse(cur.stmts("INSERT"))
        self.assertFalse(cur.stmts("UPDATE"))


class TestDailyFrame(unittest.TestCase):
    def test_cli_has_daily(self):
        import subprocess
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--daily", r.stdout)


if __name__ == "__main__":
    unittest.main()
