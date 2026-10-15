#!/usr/bin/env python3
"""Tests for nova_take_four.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Pure case logic on fixtures; DB through fake cursors.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_take_four.py"
spec = importlib.util.spec_from_file_location("take_four_under_test", SCRIPT)
tf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tf)
SRC = SCRIPT.read_text()

# Every outbound side effect is stubbed for the whole module (a test that forgets its own patch must
# never reach the real event bus). Individual tests patch again inside this.
_GUARD = mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=mock.Mock(return_value=True)),
                                       "nova_buick8_log": mock.Mock(log_unexplained=mock.Mock(return_value=None))})


def setUpModule():
    _GUARD.start()


def tearDownModule():
    _GUARD.stop()


NOW = datetime(2026, 10, 19, 14, 20, tzinfo=timezone.utc)      # a Monday
WEEK = date(2026, 10, 19)
# (task, runs, recovered, retry_failed, prior_runs, prior_recovered) — the 10-15 shape, in small
STATS = [("cluster_render", 1766, 201, 120, 979, 140),
         ("prober", 3884, 51, 27, 4563, 16),
         ("wifi_presence", 3995, 26, 1, 4811, 31),
         ("pg_backup", 8, 1, 0, 8, 0),
         ("hue_history", 9000, 3, 1, 9000, 2)]
TIMEOUT_TAIL = ("Traceback (most recent call last):\n  File \"x.py\", line 12, in f\n    raise TimeoutExpired(\n"
                "subprocess.TimeoutExpired: Command '['chromium', '--headless']' timed out after 30 seconds")
FIRST_TAKES = [("cluster_render", TIMEOUT_TAIL, None, 1, "failure"),
               ("cluster_render", TIMEOUT_TAIL.replace("30", "31"), None, 1, "failure"),
               ("cluster_render", "", "[cluster-render] render failed or too small (18683 B)", 1, "failure"),
               ("prober", None, None, 1, "failure"),
               ("wifi_presence", "urllib.error.HTTPError: HTTP Error 503: Service Unavailable", None, 1, "failure")]


class _Cur:
    """Answers fetchone/fetchall by substring of the last SQL; records every execute."""
    def __init__(self, routes=None):
        self.routes = routes or []
        self.sql, self.params, self._last = [], [], ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val(self) if callable(val) else val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


def _db(existing=(), digest_rows=None):
    return _Cur([("GROUP BY task_id HAVING", STATS), ("LEFT JOIN LATERAL", FIRST_TAKES),
                 ("SELECT task_id, status FROM take_four", list(existing)),
                 ("SELECT DISTINCT ON (task_id)", digest_rows or [])])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_ips_emails_or_home_paths(self):
        self.assertNotRegex(SRC, r"(?i)(password|api_key|token)\s*=\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotRegex(SRC, r"/" + r"Users/|/" + r"home/")
        self.assertNotRegex(SRC, r"[\w.+-]+@[\w-]+\.[a-z]{2,}")
        self.assertNotRegex(SRC, r"(?i)\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b")

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(\s*f[\"']")
        self.assertNotRegex(SRC, r"execute\([^)]*%\s*\(")

    def test_read_only_toward_the_fleet(self):
        for verb in (r"DELETE\s+FROM", r"\bDROP\s", r"TRUNCATE", r"launchctl", r"systemctl", r"subprocess",
                     r"os\.system", r"kill\("):
            self.assertNotRegex(SRC, verb)
        writes = re.findall(r"(INSERT INTO|\"UPDATE)\s+([\w.]+)", SRC)
        self.assertTrue(writes)
        self.assertTrue(all(t == "take_four" for _, t in writes), writes)

    def test_ddl_goes_through_safe_ddl(self):
        self.assertIn("nova_pg_schema.safe_ddl(cur, SCHEMA)", SRC)
        self.assertNotIn("cur.execute(SCHEMA)", SRC)


class TestPerformance(unittest.TestCase):
    def test_build_cases_and_fingerprints_scale(self):
        stats = [(f"t{i}", 1000 + i, i % 60, 1, 1000, (i * 7) % 60) for i in range(10000)]
        fps = {f"t{i}": [TIMEOUT_TAIL] * 5 for i in range(0, 10000, 3)}
        t = time.monotonic()
        cases = tf.build_cases(stats, fps)
        for i in range(10000):
            tf.fingerprint(TIMEOUT_TAIL + str(i))
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertTrue(cases)

    def test_queries_are_bounded(self):
        self.assertLessEqual(tf.STATEMENT_TIMEOUT_MS, 60000)
        self.assertIn("started_at >= %(p0)s AND started_at < %(now)s", SRC)
        self.assertIn("LIMIT 1) f ON true", SRC)


class TestRetry(unittest.TestCase):
    # RETRY GAP: connect — a PG failure raises; the scheduler records the failure and the next daily run
    # recomputes the same rolling window, so nothing is lost.
    def test_failed_digest_leaves_rows_unnotified_for_next_monday(self):
        row = ("prober", 3884, 51, 0.0131, 27, 4563, 16, 0.0035, "new", [])
        cur = _db(digest_rows=[row])
        with mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=mock.Mock(return_value=False))}):
            self.assertFalse(tf.digest(cur, {"recovered": 315, "prior_recovered": 218, "tasks_over_threshold": 3},
                                       WEEK))
        self.assertFalse(any("SET notified = true" in q for q in cur.sql))

    def test_buick8_failure_fails_open(self):
        bad = mock.Mock(log_unexplained=mock.Mock(side_effect=RuntimeError("logbook down")))
        case = tf.build_cases(STATS, {})[0]
        with mock.patch.dict(sys.modules, {"nova_buick8_log": bad}), redirect_stdout(io.StringIO()):
            self.assertIsNone(tf.buick8_log(_Cur(), case, WEEK))


class TestUnit(unittest.TestCase):
    def test_fingerprint_template_normalises(self):
        a = tf.fingerprint(TIMEOUT_TAIL)
        self.assertEqual(a, tf.fingerprint(TIMEOUT_TAIL.replace("30 seconds", "45 seconds")))
        self.assertEqual(a, "subprocess.TimeoutExpired: Command '…' timed out after # seconds")
        self.assertEqual(tf.fingerprint("", "[cluster-render] render failed (18683 B)\n"),
                         "[cluster-render] render failed (# B)")
        self.assertEqual(tf.fingerprint(None, None, 1, "timeout"), "timeout: exit 1, no output")
        self.assertIn("<uuid>", tf.normalise("run 0f8fad5b-d9cb-469f-a165-70867728950e failed"))
        self.assertIn("<path>", tf.normalise("No such file: /var/lib/nova/x.db"))

    def test_thresholds(self):
        self.assertTrue(tf.over_threshold(1766, 201))      # 11% and > 20
        self.assertTrue(tf.over_threshold(100000, 21))     # > 20 whatever the share
        self.assertTrue(tf.over_threshold(50, 3))          # 6%, 3 recoveries
        self.assertFalse(tf.over_threshold(8, 1))          # 1 of 8 is not a case
        self.assertFalse(tf.over_threshold(9000, 3))
        self.assertFalse(tf.over_threshold(0, 0))

    def test_classify_and_sticky(self):
        self.assertEqual(tf.classify(51, 3884, 16, 4563), "new")
        self.assertEqual(tf.classify(40, 300, 20, 300), "worsening")
        self.assertEqual(tf.classify(201, 1766, 140, 979), "open")     # more runs, lower share
        self.assertEqual(tf.classify(10, 100, 30, 100), "improving")
        self.assertEqual(tf.sticky_status("new", "open"), "new")
        self.assertEqual(tf.sticky_status("worsening", "improving"), "worsening")
        self.assertEqual(tf.sticky_status("open", "worsening"), "worsening")
        self.assertEqual(tf.sticky_status(None, "open"), "open")

    def test_week_start_and_totals(self):
        self.assertEqual(tf.week_start(date(2026, 10, 15)), date(2026, 10, 12))
        self.assertEqual(tf.week_start(date(2026, 10, 12)), date(2026, 10, 12))
        t = tf.totals(STATS)
        self.assertEqual((t["recovered"], t["prior_recovered"], t["tasks_over_threshold"]), (282, 189, 3))
        self.assertEqual(tf.totals([]), {"recovered": 0, "prior_recovered": 0, "tasks_with_recoveries": 0,
                                         "tasks_over_threshold": 0})

    def test_selftest(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(tf.selftest(), 0)
        self.assertIn("selftest ok", out.getvalue())


class TestIntegration(unittest.TestCase):
    def test_evaluate_chains_stats_and_first_takes(self):
        cur = _db()
        cases, tot = tf.evaluate(cur, NOW)
        self.assertEqual([c["task_id"] for c in cases], ["cluster_render", "prober", "wifi_presence"])
        cr = cases[0]
        self.assertEqual(cr["top_first_take_errors"][0],
                         {"fingerprint": "subprocess.TimeoutExpired: Command '…' timed out after # seconds", "count": 2})
        lateral = cur.params[1]
        self.assertEqual(sorted(lateral[3]), ["cluster_render", "prober", "wifi_presence"])
        self.assertEqual(tot["recovered"], 282)

    def test_shared_helpers_are_imported_not_reimplemented(self):
        self.assertIn("nova_dsn.pg_dsn", SRC)
        self.assertIn("from nova_notify import notify", SRC)
        self.assertIn("nova_buick8_log.log_unexplained(", SRC)
        self.assertIn("occurrence_key=f\"{case['task_id']}:{week}\"", SRC)
        self.assertNotIn("INSERT INTO unexplained_events", SRC)

    def test_buick8_called_with_our_cursor_and_weekly_occurrence(self):
        b8 = mock.Mock(log_unexplained=mock.Mock(return_value=42))
        cur = _Cur()
        case = tf.build_cases(STATS, {})[0]
        with mock.patch.dict(sys.modules, {"nova_buick8_log": b8}):
            self.assertEqual(tf.buick8_log(cur, case, WEEK), 42)
        args, kw = b8.log_unexplained.call_args
        self.assertEqual(args[:2], ("retry_flakiness", "cluster_render"))
        self.assertIs(kw["cur"], cur)
        self.assertEqual(kw["occurrence_key"], "cluster_render:2026-10-19")


class TestFunctional(unittest.TestCase):
    def _run(self, argv, cur, weekday_now=NOW):
        conn = mock.Mock(cursor=mock.Mock(return_value=cur))
        notify = mock.Mock(return_value=True)
        b8 = mock.Mock(log_unexplained=mock.Mock(return_value=7))
        with mock.patch.object(tf, "connect", return_value=conn), \
             mock.patch.object(tf, "ensure_schema"), \
             mock.patch.object(tf, "datetime", wraps=datetime) as dt, \
             mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=notify), "nova_buick8_log": b8}):
            dt.now.return_value = weekday_now
            out = io.StringIO()
            with redirect_stdout(out):
                rc = tf.main(argv)
        return rc, out.getvalue(), notify, b8

    def test_scheduled_monday_writes_cases_and_sends_one_digest(self):
        digest_rows = [("prober", 3884, 51, 0.0131, 27, 4563, 16, 0.0035, "new",
                        [{"fingerprint": "failure: exit 1, no output", "count": 49}])]
        cur = _db(existing=[("prober", "new"), ("old_task", "open")], digest_rows=digest_rows)
        rc, out, notify, b8 = self._run(["--scheduled"], cur)
        self.assertEqual(rc, 0)
        inserts = [p for q, p in zip(cur.sql, cur.params) if q.startswith("INSERT INTO take_four")]
        self.assertEqual([p[0] for p in inserts], ["cluster_render", "prober", "wifi_presence"])
        self.assertEqual(inserts[1][6], "new")                               # status kept sticky
        self.assertTrue(any("SET status = 'cleared'" in q for q in cur.sql))  # old_task dropped under
        self.assertEqual(b8.log_unexplained.call_count, 3)
        notify.assert_called_once()
        kw = notify.call_args.kwargs
        self.assertEqual((kw["level"], kw["category"]), ("info", "take_four"))
        self.assertIn("prober [new] 51/3884", kw["body"])
        self.assertIn("49× failure: exit 1, no output", kw["body"])
        self.assertTrue(any("SET notified = true" in q for q in cur.sql))

    def test_no_digest_midweek_and_none_when_nothing_new(self):
        rc, _, notify, _ = self._run(["--scheduled"], _db(), weekday_now=NOW.replace(day=21))   # Wednesday
        self.assertEqual(rc, 0)
        notify.assert_not_called()
        rc, out, notify, _ = self._run(["--scheduled", "--digest"], _db(digest_rows=[]))
        notify.assert_not_called()
        self.assertIn("nothing sent", out)


class TestFrame(unittest.TestCase):
    def test_help_and_selftest_subprocess(self):
        env = dict(os.environ, NOVA_TEST_QUIET="1")
        for flag in ("--help", "--selftest"):
            r = subprocess.run([sys.executable, str(SCRIPT), flag], capture_output=True, text=True, timeout=30,
                               env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_bad_report_arg(self):
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            tf.main(["--report", "0"])

    def test_dry_run_writes_nothing_and_notifies_nothing(self):
        cur = _db()
        conn = mock.Mock(cursor=mock.Mock(return_value=cur))
        notify = mock.Mock()
        with mock.patch.object(tf, "connect", return_value=conn), \
             mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=notify)}):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(tf.main(["--dry-run"]), 0)
        self.assertIn("nothing written", out.getvalue())
        self.assertIn("cluster_render", out.getvalue())
        notify.assert_not_called()
        self.assertFalse(any(re.match(r"\s*(INSERT|UPDATE|CREATE|ALTER)", q) for q in cur.sql))

    def test_house_conventions(self):
        self.assertIn("Little Mister, 2026-10-15", SRC)
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)
        for col in ("task_id text", "week date", "runs integer", "recovered integer", "share real",
                    "top_first_take_errors jsonb", "status text"):
            self.assertIn(col, SRC)
        for flag in ("--scheduled", "--dry-run", "--report", "--selftest"):
            self.assertIn(flag, SRC)
        self.assertNotIn("__main__", SRC.split("def main")[0])   # importing never runs main()


if __name__ == "__main__":
    unittest.main()
