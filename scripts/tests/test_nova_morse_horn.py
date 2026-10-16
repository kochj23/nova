#!/usr/bin/env python3
"""Tests for nova_morse_horn.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import re
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_morse_horn.py"
spec = importlib.util.spec_from_file_location("mh_under_test", SCRIPT)
mh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mh)
SRC = SCRIPT.read_text()
IP = ".".join(["10", "1", "2", "3"])       # built at runtime: no literal addresses in the repo


def _rows(src, tmpl, n, every=60, ex="x"):
    return [(src, tmpl, 1_700_000_000 + i * every, ex) for i in range(n)]


class FakeCur:
    """Answers each source query with canned rows; can be told to fail one source."""

    def __init__(self, fail=()):
        self.fail, self.q, self.last = set(fail), [], ""

    def execute(self, sql, args=None):
        self.q.append(sql)
        self.last = sql
        for name in self.fail:
            if name in sql:
                raise RuntimeError(f"{name} is gone")

    def fetchall(self):
        now = datetime(2026, 10, 15, tzinfo=timezone.utc)
        if "telemetry.events" in self.last:
            return [(now, "disk", f"Disk full on host {IP} (97%)")] * 6
        if "scheduler_runs" in self.last:
            return [(1_700_000_000_000 + i * 60_000, "t", "failure", 1,
                     "urllib.error.HTTPError: HTTP Error 401: Unauthorized") for i in range(6)]
        if "health_checks" in self.last:
            return [(now, "hue", "down", "")] * 6
        if "telemetry.incidents" in self.last:
            return []
        if "syslog_events" in self.last:
            return [(now, "kernel", "Out of memory: Killed process 4412 (python)")] * 6
        return []

    def fetchone(self):
        return (8,)


class Security(unittest.TestCase):
    def test_no_hardcoded_credentials_or_addresses(self):
        self.assertNotRegex(SRC, r"(?i)(password|api_key|token)\s*=\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(SRC, r"\b(?:192\.168|10\.\d+)\.\d+\.\d+|/Users/|/home/")

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")

    def test_model_is_local_only(self):
        with mock.patch.object(mh, "MODEL_URL", "https://api.example.com/v1/generate"), \
             mock.patch.object(mh.urllib.request, "urlopen") as u:
            self.assertIsNone(mh.model_demand("weird pattern"))
            u.assert_not_called()
        self.assertNotRegex(SRC, r"(?i)openrouter|anthropic|openai")

    def test_digest_and_templates_carry_no_addresses(self):
        t = mh.template(f"conn from {IP}:5432 mac {":".join(["aa", "bb", "cc", "dd", "ee", "ff"])} at /Users/x/y.py by a@b.org")
        self.assertNotRegex(t, r"\d{1,3}\.\d{1,3}\.|aa:bb|/Users/|@b\.org")
        self.assertNotRegex(mh.scrub(f"Disk full on {IP} /home/x/y"), r"\d+\.\d+\.\d+\.\d+|/home/")


class Performance(unittest.TestCase):
    def test_grouping_200k_rows_is_fast(self):
        rows = _rows("syslog", "a: b", 100_000) + _rows("alerts", "c | d", 100_000, every=7)
        t = time.monotonic()
        pats = mh.rank(mh.group(rows))
        self.assertLess(time.monotonic() - t, 5.0)
        self.assertEqual(len(pats), 2)

    def test_template_is_fast(self):
        t = time.monotonic()
        for i in range(20_000):
            mh.template(f"Scheduled task 'job_{i}' is FAILING at 2026-10-14 21:41:{i % 60:02d}")
        self.assertLess(time.monotonic() - t, 3.0)


class Retry(unittest.TestCase):
    def test_model_failure_fails_open_to_fallback(self):
        with mock.patch.object(mh.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.assertIsNone(mh.model_demand("zxqv"))
        pats = mh.assign_demands([{"template": "zxqv blorp"}], use_model=True, model_fn=lambda t: None)
        self.assertEqual((pats[0]["demand"], pats[0]["demand_by"]), (mh.FALLBACK_DEMAND, "fallback"))

    def test_model_calls_are_capped(self):
        calls = []
        pats = [{"template": f"zz{i}"} for i in range(20)]
        mh.assign_demands(pats, model_fn=lambda t: calls.append(t) or "wants x")
        self.assertEqual(len(calls), mh.MODEL_CALLS)

    def test_one_failing_source_does_not_stop_the_rest(self):
        cur = FakeCur(fail=("syslog_events",))
        rows, status = mh.gather(cur)
        self.assertTrue(status["syslog"].startswith("error"))
        self.assertEqual(status["alerts"], "ok")
        self.assertTrue(any(r[0] == "health" for r in rows))
        self.assertTrue(any("ROLLBACK TO SAVEPOINT" in q for q in cur.q))


class Unit(unittest.TestCase):
    def test_template_normalises(self):
        a = mh.template("Scheduled task 'nas_x' is FAILING (pid 12) at 2026-10-14 21:41:14")
        b = mh.template("Scheduled task 'other' is FAILING (pid 99) at 2026-10-15 01:02:03")
        self.assertEqual(a, b)
        self.assertIn("kex_exchange_identification", mh.template("sshd: kex_exchange_identification: closed"))

    def test_rule_table(self):
        cases = {"no space left on device": "wants more disk",
                 "http error #: unauthorized": "wants a renewed credential",
                 "token has expired": "wants a renewed credential",
                 "connection refused": "wants its dependency back",
                 "timed out waiting": "wants its dependency back",
                 "out of memory: killed process #": "wants memory",
                 "http error #: too many requests (429)": "wants you to slow down",
                 "certificate has expired": "wants a cert"}
        for t, d in cases.items():
            self.assertEqual(mh.rule_demand(t), d, t)

    def test_periodicity(self):
        self.assertEqual(mh.periodicity([0, 600, 1200, 1800, 2400]), (600, 1.0))
        p, r = mh.periodicity([0, 10, 900, 905, 5000, 5001])
        self.assertLess(r, 0.6)
        self.assertEqual(mh.periodicity([5, 5, 5, 5]), (None, 0.0))

    def test_clean_model_demand(self):
        self.assertEqual(mh.clean_model_demand("<think>x</think>Wants a new fan."), "wants a new fan")
        self.assertIsNone(mh.clean_model_demand("Sure! The machine wants a new fan"))
        self.assertIsNone(mh.clean_model_demand("wants " + "very " * 20))

    def test_error_line(self):
        self.assertEqual(mh.error_line("x\npsycopg2.errors.QueryCanceled: canceling statement"),
                         "QueryCanceled: canceling statement")
        self.assertEqual(mh.error_line("", None, "timeout"), "timed out (no error text)")


class Integration(unittest.TestCase):
    def test_gather_reads_every_source_with_fake_cursor(self):
        rows, status = mh.gather(FakeCur())
        self.assertEqual(set(status), set(mh.SOURCES))
        pats = mh.assign_demands(mh.rank(mh.group(rows)), use_model=False)
        demands = {p["source"]: p["demand"] for p in pats}
        self.assertEqual(demands["alerts"], "wants more disk")
        self.assertEqual(demands["scheduler"], "wants a renewed credential")
        self.assertEqual(demands["health"], "wants its dependency back")
        self.assertEqual(demands["syslog"], "wants memory")

    def test_store_answers_only_silent_patterns_from_clean_sources(self):
        cur = mock.MagicMock()
        cur.fetchall.return_value = [(1, "alerts", "gone"), (2, "alerts", "still here"), (3, "syslog", "gone")]
        out = mh.store(cur, [], {"alerts": "ok", "syslog": "capped"}, {("alerts", "still here")})
        self.assertEqual(out["answered_by_silence"], 1)
        upd = [c for c in cur.execute.call_args_list if "answered_by = 'silence'" in c[0][0]]
        self.assertEqual([c[0][1] for c in upd], [(1,)])


class Functional(unittest.TestCase):
    def test_explained_share_and_digest(self):
        rows = _rows("alerts", "disk | disk full", 30) + _rows("alerts", "x | zxqv", 10) + \
            _rows("syslog", "kernel: out of memory", 50)
        pats = mh.assign_demands(mh.rank(mh.group(rows)), use_model=False)
        pct, per = mh.explained(pats, 100)
        self.assertEqual(pct, 40.0)
        self.assertEqual(per["wants more disk"], 30)
        body = mh.render_digest("2026-W42", pats, 100, pct, per, 2, 1, ["syslog (error: x)"])
        self.assertIn("It wants more disk: 30 signals (alerts, 30.0% of alerts, every ~1m)", body)
        self.assertIn("explain 40.0% of last week's 100 alerts", body)
        self.assertIn("2 this month, 1 last month", body)
        self.assertIn("Could not read: syslog", body)

    def test_per_source_quota_keeps_alerts_in_the_top(self):
        rows = []
        for i in range(30):
            rows += _rows("syslog", f"noise {chr(97 + i % 26)}{i}", 1000)
        rows += _rows("alerts", "disk | disk full", 10)
        pats = mh.rank(mh.group(rows), top_n=10)
        self.assertIn("alerts", {p["source"] for p in pats})

    def test_rarer_but_rhythmic_beats_bursty_at_equal_volume(self):
        g = mh.group(_rows("a", "steady", 20, every=300) +
                     [("a", "bursty", t, "x") for t in [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 5000, 5001, 9000, 9001,
                                                       20000, 20001, 30000, 40000, 40001, 50000]])
        pats = mh.rank(g)
        self.assertEqual(pats[0]["template"], "steady")


class Frame(unittest.TestCase):
    def test_help_exits(self):
        with self.assertRaises(SystemExit):
            mh.main(["--help"])

    def test_selftest_passes(self):
        self.assertEqual(mh.selftest(), 0)

    def test_cli_flags_present(self):
        for f in ("--scheduled", "--dry-run", "--report", "--selftest", "--no-model"):
            self.assertIn(f'"{f}"', SRC)

    def test_docstring_convention(self):
        self.assertIn("Little Mister, 2026-10-15", SRC)
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)

    def test_schema_has_the_spec_columns(self):
        for col in ("ts", "source", "template", "count_7d", "period_seconds", "regularity", "demand",
                    "evidence", "status"):
            self.assertRegex(mh.SCHEMA, rf"\n\s+{col}\s")
        self.assertIn("CHECK (status IN ('open','answered','ignored'))", mh.SCHEMA)

    def test_dry_run_writes_nothing(self):
        self.assertTrue(re.search(r"if dry:.*?conn\.rollback\(\)\s+return res", SRC, re.S))



class Patterns(unittest.TestCase):
    """Consolidation #14 (2026-10-15): alert_patterns is the render/Slack mode of the Morse Horn."""

    def test_patterns_flag_present(self):
        self.assertIn('"--patterns"', SRC)

    def test_open_demands_fails_open(self):
        with mock.patch("psycopg2.connect", side_effect=RuntimeError("pg down")):
            self.assertEqual(mh.open_demands(), [])

    def test_open_demands_reads_open_rows(self):
        cur = mock.MagicMock()
        cur.fetchone.return_value = (True,)
        cur.fetchall.return_value = [(3, "alerts", 12, "wants more disk", "rule")]
        conn = mock.MagicMock(); conn.cursor.return_value = cur
        with mock.patch("psycopg2.connect", return_value=conn):
            d = mh.open_demands(5)
        self.assertEqual(d, [{"id": 3, "source": "alerts", "count_7d": 12, "demand": "wants more disk", "demand_by": "rule"}])
        self.assertIn("status = 'open'", cur.execute.call_args_list[-1][0][0])
        conn.close.assert_called_once()

    def test_patterns_mode_hands_core_demands_to_renderer(self):
        import types
        rend = types.ModuleType("nova_alert_patterns"); rend.publish = mock.MagicMock()
        dm = [{"id": 1, "demand": "wants memory"}]
        with mock.patch.dict(sys.modules, {"nova_alert_patterns": rend}), \
                mock.patch.object(mh, "open_demands", return_value=dm):
            self.assertEqual(mh.main(["--patterns", "--dry-run"]), 0)
        rend.publish.assert_called_once_with(dm, dry_run=True)

if __name__ == "__main__":
    unittest.main()
