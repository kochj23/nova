#!/usr/bin/env python3
"""7-category supplement for nova_boiler.py (the Boiler: unresolved-load gauge + daily bleed).
Fills what tests/test_nova_boiler.py leaves thin: real retry/backoff on the bleed post and the PG
connect, the once-a-day and Annie-rule gates, the forced turning-point spend, and main() end to end.
Offline: fake cursors, the post is a list, no Slack. Written by Jordan Koch (via Claude)."""
import contextlib
import io
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_boiler as bo  # noqa: E402

SRC = (SCRIPTS / "nova_boiler.py").read_text()


def _g(**over):
    g = {s: {"n": 0, "age_d": 0.0, "examples": []} for s in bo.SOURCES}
    for k, v in over.items():
        g[k] = {"n": v[0], "age_d": v[1], "examples": v[2] if len(v) > 2 else []}
    return g


BIG = _g(unanswered_jordan=(3, 1.0, ["dns?"]), chronic_jobs=(6, 0, ["journal_essay (x5)"]),
         pending_proposals=(4, 3.0, ["#121"]), repeated_alerts=(30, 0, ["stale-code x47"]),
         doc_drift=(5, 21, ["hue_bridge"]), claude_queue=(245, 0))


class _Cur:
    """Stateful: an INSERT with bled=True makes later bled_today() checks see it (same 'day')."""
    def __init__(self):
        self.sql, self._last, self.bled_rows = [], "", 0
        self.connection = mock.MagicMock()

    def execute(self, sql, params=None):
        self.sql.append((sql, params)); self._last = sql
        if sql.startswith("INSERT INTO boiler_state") and params and params[4]:
            self.bled_rows += 1

    def fetchone(self):
        return (self.bled_rows,)

    def fetchall(self):
        return [(41,)] if "RETURNING id" in self._last else []


def _run(cur, g=BIG, post=None, annie=True, window=True, **kw):
    sent = []
    post = post or sent.append
    with mock.patch.object(bo, "gather", return_value=g), \
         mock.patch.object(bo, "in_window", return_value=window), \
         mock.patch("nova_annie_rule.ok", return_value=annie), \
         mock.patch("nova_turning_point.decide", return_value={"allowed": True}) as tp, \
         mock.patch.object(bo.time, "sleep") as sl, \
         contextlib.redirect_stdout(io.StringIO()):
        r = bo.run(cur, post=post, **kw)
    return r, sent, tp, sl


class TestSecurity(unittest.TestCase):
    def test_annie_rule_failure_blocks_the_post(self):
        r, sent, tp, _ = _run(_Cur(), annie=False)
        self.assertEqual(sent, [])
        self.assertFalse(r["bled"])
        tp.assert_not_called()

    def test_drop_touches_only_his_stale_held_reaches(self):
        cur = _Cur()
        _run(cur)
        (sql, params), = [(s, p) for s, p in cur.sql if s.startswith("UPDATE")]
        self.assertIn("lower(audience)='jordan' AND status='held'", sql)
        self.assertIn("make_interval(days => %s)", sql)
        self.assertEqual(params, (bo.STALE_DRAFT_DAYS,))
        self.assertFalse([s for s, _ in cur.sql if "DELETE" in s.upper()])

    def test_no_secrets_and_gather_is_read_only(self):
        self.assertNotRegex(SRC, r"(?i)(password|xox[bp]-|api_key\s*=)")
        cur = mock.MagicMock()
        cur.fetchall.return_value = []
        bo.gather(cur)
        for c in cur.execute.call_args_list:
            self.assertTrue(c.args[0].lstrip().upper().startswith("SELECT"), c.args[0][:40])


class TestPerformance(unittest.TestCase):
    def test_gather_is_a_fixed_number_of_queries(self):
        cur = mock.MagicMock()
        cur.fetchall.return_value = []
        bo.gather(cur)
        self.assertEqual(cur.execute.call_count, 8)  # 9 sources, failed/chronic share one query

    def test_contributions_are_capped_so_pressure_is_bounded(self):
        huge = _g(**{s: (10 ** 9, 0) for s in bo.SOURCES})
        total, _, _ = bo.pressure(huge)
        cap = sum(bo.contribution(s, bo.SOURCES[s][3], 0) for s in bo.SOURCES)
        self.assertEqual(total, round(cap, 1))
        t = time.monotonic()
        bo.compose_bleed(total, bo.pressure(huge)[2], list(range(10000)))
        self.assertLess(time.monotonic() - t, 0.5)


class TestRetry(unittest.TestCase):
    def test_post_retries_with_backoff_then_bleeds(self):
        calls = []

        def flaky(t):
            calls.append(t)
            if len(calls) < 3:
                raise OSError("slack 503")
        r, _, _, sl = _run(_Cur(), post=flaky)
        self.assertTrue(r["bled"])
        self.assertEqual(len(calls), 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [1.0, 2.0])

    def test_post_fails_three_times_not_marked_bled(self):
        calls = []

        def down(t):
            calls.append(t)
            raise OSError("slack down")
        cur = _Cur()
        r, _, _, _ = _run(cur, post=down)
        self.assertFalse(r["bled"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(cur.bled_rows, 0)  # tomorrow's gate isn't consumed by a failed post

    def test_connect_retries_operational_errors(self):
        good = mock.MagicMock()
        with mock.patch.object(bo.psycopg2, "connect",
                               side_effect=[psycopg2.OperationalError("x"), psycopg2.OperationalError("y"), good]) as c, \
             mock.patch.object(bo.time, "sleep") as sl, contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(bo.connect(), good)
        self.assertEqual(c.call_count, 3)
        self.assertEqual([x.args[0] for x in sl.call_args_list], [2.0, 4.0])

    def test_connect_raises_after_three(self):
        with mock.patch.object(bo.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")), \
             mock.patch.object(bo.time, "sleep"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(psycopg2.OperationalError):
                bo.connect()


class TestUnit(unittest.TestCase):
    def test_in_window_bounds(self):
        with mock.patch.object(bo, "WINDOW", "10-20"):
            self.assertFalse(bo.in_window(datetime(2026, 10, 8, 9, 59)))
            self.assertTrue(bo.in_window(datetime(2026, 10, 8, 10, 0)))
            self.assertFalse(bo.in_window(datetime(2026, 10, 8, 20, 0)))

    def test_compose_defers_beyond_top_n(self):
        _, _, top = bo.pressure(BIG)
        self.assertGreater(len(top), bo.TOP_N)
        txt = bo.compose_bleed(150, top, [])
        self.assertIn("Deferring", txt)
        self.assertNotIn("Dropping", txt)

    def test_bled_today_uses_current_date(self):
        cur = _Cur()
        self.assertFalse(bo.bled_today(cur))
        self.assertIn("ts::date = current_date", cur.sql[-1][0])


class TestIntegration(unittest.TestCase):
    def test_bleed_is_a_forced_turning_point_spend(self):
        r, _, tp, _ = _run(_Cur())
        kw = tp.call_args.kwargs
        self.assertEqual(tp.call_args.args[1], "bleed")
        self.assertTrue(kw["force"])
        self.assertEqual(kw["ceiling"], "recommend")
        self.assertLessEqual(kw["stakes"], 1.0)

    def test_real_turning_point_and_annie_rule_accept_the_note(self):
        import nova_turning_point as tp
        sent = []
        tcur = _Cur()
        with mock.patch.object(bo, "gather", return_value=BIG), mock.patch.object(bo, "in_window", return_value=True), \
             mock.patch.object(tp, "spent_this_week", return_value=999), \
             mock.patch.object(tp, "calibrated", return_value=0.9), \
             mock.patch("nova_restraint.record_restraint") as rr, contextlib.redirect_stdout(io.StringIO()):
            r = bo.run(tcur, post=sent.append)
        self.assertTrue(r["bled"])  # forced past an exhausted budget
        self.assertTrue(rr.call_args.kwargs["detail"]["turning_point"]["spent"])


class TestFunctional(unittest.TestCase):
    def test_at_most_once_a_day_across_runs(self):
        cur = _Cur()
        r1, sent1, _, _ = _run(cur)
        r2, sent2, _, _ = _run(cur)
        self.assertTrue(r1["bled"])
        self.assertFalse(r2["bled"])
        self.assertEqual(len(sent1) + len(sent2), 1)
        self.assertEqual(cur.bled_rows, 1)

    def test_outside_window_measures_but_does_not_bleed(self):
        cur = _Cur()
        r, sent, _, _ = _run(cur, window=False)
        self.assertEqual(r["state"], "bleed")
        self.assertEqual(sent, [])
        self.assertTrue(any(s.startswith("INSERT INTO boiler_state") for s, _ in cur.sql))

    def test_main_dry_run_end_to_end(self):
        conn = mock.MagicMock()
        cur = _Cur()
        conn.cursor.return_value = cur
        out = io.StringIO()
        with mock.patch.object(bo, "connect", return_value=conn), mock.patch.object(bo, "gather", return_value=BIG), \
             mock.patch.object(bo, "in_window", return_value=True), mock.patch.object(sys, "argv", ["nova_boiler.py", "--dry-run"]), \
             contextlib.redirect_stdout(out):
            self.assertEqual(bo.main(), 0)
        self.assertIn("Bleeding the boiler", out.getvalue())
        self.assertFalse([s for s, _ in cur.sql if s.lstrip().startswith(("INSERT", "UPDATE"))])


class TestFrame(unittest.TestCase):
    def test_import_has_no_side_effects(self):
        env = {k: v for k, v in __import__("os").environ.items() if k != "NOVA_BOILER_THRESHOLD"}
        r = subprocess.run([sys.executable, "-c", "import nova_boiler; print(nova_boiler.THRESHOLD)"], cwd=SCRIPTS, env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertEqual(r.stdout.strip(), "100.0")


if __name__ == "__main__":
    unittest.main()
