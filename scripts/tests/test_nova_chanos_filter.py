#!/usr/bin/env python3
"""Tests for nova_chanos_filter.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
SCRIPT = SCRIPTS / "nova_chanos_filter.py"
spec = importlib.util.spec_from_file_location("chanos_under_test", SCRIPT)
cf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cf)
SRC = SCRIPT.read_text()

T = datetime(2026, 10, 1, tzinfo=timezone.utc)
RAW = [  # series, source, readings, bad, flips, last_bad, ever_bad, test_rows
    ("health:core_liveness@mac-studio/core_liveness", "health_checks", 21000, 0, 0, None, 0, 0),
    ("health:redis@mac-studio/mac-studio", "health_checks", 145000, 0, 0, T, 1, 0),
    ("health:plex@nova-core/mac-studio", "health_checks", 145000, 0, 0, None, 0, 0),
    ("probe:postgres", "telemetry.probe_results", 55000, 138, 270, T, 138, 0),
    ("slo:chat", "slo_status", 547, 0, 0, None, 0, 0),
    ("task:backup_restore_test", "scheduler_runs", 12, 12, 0, T, 40, 0),
    ("alerts:journal", "telemetry.events", 90, 0, 0, None, 0, 0),
    ("alerts:probe", "telemetry.events", 90, 0, 0, None, 0, 3),
    ("task:gone", "scheduler_runs", 0, 0, 0, None, 5, 0),
]


class _Cur:
    def __init__(self, routes=None):
        self.routes = routes or []
        self.sql, self.params, self._last = [], [], ""

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                if isinstance(val, Exception):
                    raise val
                return val(self) if callable(val) else val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_ips_emails_or_home_paths(self):
        self.assertNotRegex(SRC, r"(?i)(password|api_key|token|secret)\s*=\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotRegex(SRC, r"/" + r"Users/|/home/")
        self.assertNotRegex(SRC, r"(?i)\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b")

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")
        self.assertNotRegex(SRC, r"execute\([^)]*%\s*\(")
        for name, sql in cf.SOURCES.items():
            self.assertIn("%(since)s", sql, name)

    def test_never_injects_only_reads_records_notifies(self):
        for verb in (r"DELETE\s+FROM", r"\bDROP\s", r"TRUNCATE", r"launchctl", r"systemctl", r"subprocess",
                     r"os\.system", r"kill\("):
            self.assertNotRegex(SRC, verb)
        writes = set(t for _, t in re.findall(r"(INSERT INTO|UPDATE)\s+([\w.]+)", SRC))
        self.assertEqual(writes - {"chanos_filter", "SET"}, set(), writes)

    def test_ddl_goes_through_safe_ddl(self):
        self.assertIn("nova_pg_schema.safe_ddl(cur, SCHEMA)", SRC)
        self.assertNotIn("cur.execute(SCHEMA)", SRC)


class TestPerformance(unittest.TestCase):
    def test_build_rows_on_10k_series(self):
        raw = [(f"health:svc{i}@n/mac-studio", "health_checks", 1000, i % 3, i % 5, None, i % 7, 0) for i in range(10000)]
        t = time.monotonic()
        rows = cf.build_rows(raw, [])
        cf.measure(rows)
        cf.top_unproven(rows)
        self.assertLess(time.monotonic() - t, 2.0)
        self.assertEqual(len(rows), 10000)

    def test_statement_timeout_is_bounded(self):
        self.assertLessEqual(cf.STATEMENT_TIMEOUT_MS, 300000)


class TestRetry(unittest.TestCase):
    def test_a_failing_source_is_skipped_not_fatal(self):
        # RETRY GAP: gather — a failed source is skipped for this run; the next daily run retries it
        cur = _Cur([("FROM health_checks", RuntimeError("statement timeout")),
                    ("FROM telemetry.probe_results", [RAW[3]]),
                    ("FROM organ_board", [("affect", "calm")])])
        with redirect_stdout(io.StringIO()):
            rows = cf.gather(cur, T)
        self.assertEqual({r["series"] for r in rows}, {"probe:postgres", "organ:affect"})

    def test_failed_notify_does_not_lose_rows(self):
        cur = _Cur([("SELECT max(month)", (date(2026, 10, 1),)), ("critical AND verdict", [])])
        conn = mock.Mock(cursor=mock.Mock(return_value=cur))
        with mock.patch.object(cf, "connect", return_value=conn), \
             mock.patch.object(cf, "gather", return_value=cf.build_rows(RAW, [])), \
             mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=mock.Mock(side_effect=RuntimeError("bus"))),
                                           "nova_pg_schema": mock.Mock()}):
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cf.main(["--scheduled"]), 0)
        self.assertEqual(sum(1 for q in cur.sql if q.startswith("INSERT INTO chanos_filter")), 8)


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cf.selftest(), 0)

    def test_verdicts(self):
        rows = {r["series"]: r for r in cf.build_rows(RAW, [("boiler", "rising"), ("core_liveness", "down")])}
        self.assertEqual(rows["health:core_liveness@mac-studio/core_liveness"]["verdict"], "never_red_unproven")
        self.assertEqual(rows["health:redis@mac-studio/mac-studio"]["verdict"], "never_red_proven")
        self.assertEqual(rows["probe:postgres"]["verdict"], "live")
        self.assertEqual(rows["task:backup_restore_test"]["verdict"], "always_red")
        self.assertEqual(rows["alerts:probe"]["verdict"], "never_red_proven")        # a TEST row proves it
        self.assertEqual(rows["organ:boiler"]["verdict"], "black_box")
        self.assertEqual(rows["organ:core_liveness"]["bad"], 1)
        self.assertNotIn("task:gone", rows)                                            # no readings in window
        young = cf.build_rows([("task:new", "scheduler_runs", 4, 0, 0, None, 0, 0)], [])[0]
        self.assertEqual(young["verdict"], "too_young")
        self.assertIsNone(young["idea"])

    def test_bad_share_and_flips(self):
        r = [x for x in cf.build_rows(RAW, []) if x["series"] == "probe:postgres"][0]
        self.assertAlmostEqual(r["bad_share"], 138 / 55000, places=6)
        self.assertEqual(r["flips"], 270)

    def test_critical_classification(self):
        self.assertTrue(cf.is_critical("slo:morning_bluf"))
        self.assertTrue(cf.is_critical("alerts:core-liveness"))
        self.assertFalse(cf.is_critical("alerts:journal"))
        self.assertFalse(cf.is_critical("health:plex@nova-core/mac-studio"))
        self.assertTrue(cf.is_critical("task:pg_backup"))
        self.assertFalse(cf.is_critical("task:garden_report"))
        self.assertFalse(cf.is_critical("weird"))

    def test_ideas_are_specific(self):
        self.assertIn("decoy", cf.injection_idea("health:postgresql@mac-studio/mac-studio"))
        self.assertIn("literal", cf.injection_idea("health:core_liveness@mac-studio/core_liveness"))
        self.assertIn("CTE", cf.injection_idea("slo:chat"))
        self.assertIn("invalid.invalid", cf.injection_idea("task:prober"))
        self.assertIn("history", cf.injection_idea("organ:boiler"))

    def test_top_unproven_orders_by_trust(self):
        top = cf.top_unproven(cf.build_rows(RAW, []))
        self.assertEqual([r["series"] for r in top],
                         ["health:core_liveness@mac-studio/core_liveness", "slo:chat"])
        self.assertTrue(all(r["idea"] for r in top))

    def test_month_of(self):
        self.assertEqual(cf.month_of(datetime(2026, 10, 15, 3, tzinfo=timezone.utc)), date(2026, 10, 1))


class TestIntegration(unittest.TestCase):
    def test_shared_helpers(self):
        self.assertIn("nova_dsn.pg_dsn", SRC)
        self.assertIn("from nova_notify import notify", SRC)

    def test_gather_feeds_sources_with_window_and_test_regex(self):
        cur = _Cur([("FROM organ_board", [])])
        with redirect_stdout(io.StringIO()):
            cf.gather(cur, T)
        p = [x for x in cur.params if x]
        self.assertEqual(len(p), len(cf.SOURCES))
        self.assertTrue(all(x["test_re"] == cf.TEST_RE for x in p))
        self.assertEqual((T - p[0]["since"]).days, cf.WINDOW_DAYS)

    def test_write_upserts_by_month_and_series(self):
        cur = _Cur()
        cf.write(cur, cf.build_rows(RAW[:1], []), date(2026, 10, 1))
        self.assertIn("ON CONFLICT (month, series)", cur.sql[0])
        self.assertEqual(cur.params[0][:2], (date(2026, 10, 1), "health:core_liveness@mac-studio/core_liveness"))

    def test_previous_unproven_first_run_is_none(self):
        self.assertIsNone(cf.previous_unproven(_Cur([("SELECT max(month)", (None,))]), date(2026, 10, 1)))


class TestFunctional(unittest.TestCase):
    def _main(self, args, cur, notify):
        conn = mock.Mock(cursor=mock.Mock(return_value=cur))
        with mock.patch.object(cf, "connect", return_value=conn), \
             mock.patch.object(cf, "gather", return_value=cf.build_rows(RAW, [])), \
             mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=notify), "nova_pg_schema": mock.Mock()}):
            out = io.StringIO()
            with redirect_stdout(out):
                rc = cf.main(args)
        return rc, out.getvalue()

    def test_new_unproven_entrant_notifies_once_with_ideas(self):
        notify = mock.Mock(return_value=True)
        cur = _Cur([("SELECT max(month)", (date(2026, 10, 1),)),
                    ("critical AND verdict", [("slo:chat",)])])
        rc, _ = self._main(["--scheduled"], cur, notify)
        self.assertEqual(rc, 0)
        notify.assert_called_once()
        kw = notify.call_args.kwargs
        self.assertEqual(kw["category"], "chanos")
        self.assertEqual(kw["meta"]["new"], ["health:core_liveness@mac-studio/core_liveness"])
        self.assertIn("literal", kw["body"])

    def test_first_run_seeds_silently(self):
        notify = mock.Mock()
        rc, _ = self._main(["--scheduled"], _Cur([("SELECT max(month)", (None,))]), notify)
        self.assertEqual(rc, 0)
        notify.assert_not_called()

    def test_dry_run_prints_measure_and_writes_nothing(self):
        notify = mock.Mock()
        cur = _Cur()
        rc, out = self._main(["--dry-run"], cur, notify)
        self.assertEqual(rc, 0)
        self.assertIn("MEASURE: critical never-red unproven = 2", out)
        self.assertIn("nothing written", out)
        notify.assert_not_called()
        self.assertFalse(any(re.match(r"\s*(INSERT|UPDATE|CREATE|ALTER)", q) for q in cur.sql))


class TestFrame(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=30,
                              env={**os.environ, "NOVA_TEST_QUIET": "1"})

    def test_help_and_selftest(self):
        self.assertEqual(self._run("--help").returncode, 0)
        r = self._run("--selftest")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_house_conventions(self):
        self.assertIn("Little Mister, 2026-10-15", SRC)
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)
        for col in ("month date", "series text", "readings bigint", "bad bigint", "flips integer",
                    "last_bad timestamptz", "verdict text"):
            self.assertIn(col, SRC)
        for flag in ("--scheduled", "--dry-run", "--report", "--selftest"):
            self.assertIn(flag, SRC)
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
