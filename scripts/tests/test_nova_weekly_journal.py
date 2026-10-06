#!/usr/bin/env python3
"""Tests for nova_weekly_journal.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_weekly_journal.py"
SRC = SCRIPT.read_text()


def _stubs():
    cfg = types.ModuleType("nova_config"); cfg.SLACK_NOTIFY = "C_TEST"
    nn = types.ModuleType("nova_notify"); nn.notify = mock.MagicMock(return_value=True)
    return {"nova_config": cfg, "nova_notify": nn}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, _stubs()), \
         mock.patch.object(logging, "FileHandler", lambda *a, **k: logging.NullHandler()):   # real log untouched
        spec.loader.exec_module(mod)
    return mod


W = _load("weekly_journal_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="weekly-journal-test-"))
W.STATE_DIR = TMP / "state"; W.STATE_DIR.mkdir()
(TMP / "config").mkdir()


def _psql(table):
    """Fake subprocess.run for psql: first matching SQL substring wins; stdout = joined rows."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        sql = cmd[-1]
        for sub, rows in table:
            if sub in sql:
                return subprocess.CompletedProcess(cmd, 0, "\n".join(rows) + "\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    run.calls = calls
    return run


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_interpolates_only_the_week_boundary(self):
        names = set()
        for m in re.finditer(r'f"([^"]*)"', SRC):
            if "SELECT" in m.group(1) or "FROM memories" in m.group(1) or "WHERE" in m.group(1):
                names |= set(re.findall(r"\{(\w+)\}", m.group(1)))
        self.assertEqual(names, {"WEEK_AGO"})
        self.assertRegex(W.WEEK_AGO, r"^\d{4}-\d{2}-\d{2}$")

    def test_psql_runs_as_argv_against_the_memories_db(self):
        with mock.patch.object(W.subprocess, "run", _psql([])) as run:
            W._query("SELECT 1")
        self.assertEqual(run.calls[0][:5], ["psql", "-U", "kochj", "-d", "nova_memories"])
        self.assertNotIn("shell=True", SRC)


class TestPerformance(unittest.TestCase):
    def test_local_news_scoring_on_10k_posts(self):
        rows = [f"Reddit r/burbank: post {i}\nScore: {i % 500}, 3 comments" for i in range(10_000)]
        with mock.patch.object(W.subprocess, "run", _psql([("count(*)", ["10000"]), ("Score:%", rows)])):
            t0 = time.perf_counter()
            out = W.section_local_news()
            self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertIn("(499 upvotes)", out)


class TestRetry(unittest.TestCase):
    def test_query_is_one_shot_and_fails_open(self):
        # RETRY GAP: _query() — a psql failure/timeout is tried once and reads as "no rows"
        with mock.patch.object(W.subprocess, "run", side_effect=subprocess.TimeoutExpired("psql", 15)) as run:
            self.assertEqual(W._query("SELECT 1"), [])
            self.assertIsNone(W._query_field("SELECT 1"))
        self.assertEqual(run.call_count, 2)

    def test_whole_journal_degrades_to_quiet_week(self):
        with mock.patch.object(W.subprocess, "run", side_effect=OSError("psql missing")):
            text = W.generate_weekly()
        self.assertIn("Nova Weekly Journal", text)
        self.assertIn("*App Health:*\n• No outages this week", text)


class TestUnit(unittest.TestCase):
    def test_memory_volume_and_empty(self):
        with mock.patch.object(W.subprocess, "run", _psql([("count(*)", ["1234"]), ("GROUP BY source", ["email|1000", "security|234"])])):
            s = W.section_memory_volume()
        self.assertEqual(s, "*Memory Volume:*\n• 1,234 new memories this week\n  - email: 1,000\n  - security: 234")
        with mock.patch.object(W.subprocess, "run", _psql([])):
            self.assertIsNone(W.section_memory_volume())

    def test_app_health_groups_outages(self):
        rows = ["Plex went down at 3pm", "Plex recovered", "Plex went down again", "Grafana went down"]
        with mock.patch.object(W.subprocess, "run", _psql([("app_watchdog", rows)])):
            s = W.section_app_health()
        self.assertIn("• 3 outage(s), 1 recovery(ies)", s)
        self.assertIn("  - Plex: 2 outage(s)\n  - Grafana: 1 outage(s)", s)

    def test_security_cameras(self):
        cams = ["Protect event on Front Door: person", "Protect event on Front Door: car", "Protect event on Patio: person"]
        tbl = [("date(created_at", ["2026-09-29|2", "2026-09-30|1"]), ("count(*)", ["3"]), ("source = 'security'", cams)]
        with mock.patch.object(W.subprocess, "run", _psql(tbl)):
            s = W.section_security()
        self.assertIn("• 3 Protect events across 2 cameras", s)
        self.assertIn("• Daily: Tue: 2, Wed: 1", s)
        self.assertIn("  - Front Door: 2\n  - Patio: 1", s)
        with mock.patch.object(W.subprocess, "run", _psql([("count(*)", ["0"])])):
            self.assertIsNone(W.section_security())

    def test_dreams_truncate_and_scheduler_failures(self):
        with mock.patch.object(W.subprocess, "run", _psql([("dream", ["Dream: " + "x" * 200 + " tail"])])):
            s = W.section_dreams()
        self.assertIn("• 1 dream(s) recorded", s); self.assertIn("..._", s); self.assertLess(len(s), 220)
        (TMP / "config" / "scheduler_state.json").write_text(json.dumps(
            {"tasks": {"a": {"run_count": 5, "consecutive_failures": 4}, "b": {"run_count": 7}}}))
        self.assertEqual(W.section_scheduler(),
                         "*Scheduler:*\n• 12 total task runs, 2 tasks configured\n• 1 task(s) with recurring failures:\n  - a: 4 consecutive failures")
        self.assertEqual(W._load_state("missing.json"), {})

    def test_infra_and_email(self):
        (W.STATE_DIR / "nova_synology_state.json").write_text(json.dumps({"model": "RS1221+", "volumes": "2 vols", "problem_count": 0}))
        tbl = [("NAS health%' AND text NOT LIKE", ["NAS health check: volume degraded " + "y" * 120]),
               ("Network health%' AND text NOT LIKE", []), ("Network health%' AND created_at", ["40"])]
        with mock.patch.object(W.subprocess, "run", _psql(tbl)):
            s = W.section_infra_summary()
        self.assertIn("• NAS (RS1221+): 2 vols — healthy", s)
        self.assertIn("• NAS issues detected on 1 check(s):", s); self.assertIn("...", s)
        self.assertIn("• Network: 40 checks, all clear", s)
        with mock.patch.object(W.subprocess, "run", _psql([("date(created_at", ["2026-10-01|5"]), ("count(*)", ["5"])])):
            self.assertEqual(W.section_email_volume(), "*Email Volume:*\n• 5 emails processed\n• Daily: Thu: 5")


class TestIntegration(unittest.TestCase):
    def test_slack_post_rides_the_notify_bus(self):
        W.notify.reset_mock()
        W.slack_post("*Nova Weekly Journal — x*\nbody line")
        kw = W.notify.call_args.kwargs
        self.assertEqual(W.notify.call_args.args[0], "Nova Weekly Journal — x")
        self.assertEqual((kw["body"], kw["level"], kw["category"], kw["dedup_key"], kw["meta"]),
                         ("body line", "info", "journal", "weekly-journal", {"host": "Office-M4-2"}))
        self.assertIn("from nova_notify import notify", SRC)
        self.assertEqual(W.DB, "nova_memories")

    def test_sections_compose_into_the_journal(self):
        with mock.patch.object(W.subprocess, "run", _psql([("count(*)", ["0"])])), \
             mock.patch.object(W, "section_memory_volume", return_value="*Memory Volume:*\n• 9 new"):
            text = W.generate_weekly()
        self.assertEqual(text.split("\n\n")[1], "*Memory Volume:*\n• 9 new")
        self.assertNotIn("Quiet week", text)


class TestFunctional(unittest.TestCase):
    def test_golden_path(self):
        tbl = [("GROUP BY source", ["email|10"]), ("count(*)", ["10"]), ("app_watchdog", []), ("dream", ["Dream: flying"])]
        W.notify.reset_mock()
        with mock.patch.object(W.subprocess, "run", _psql(tbl)):
            journal = W.generate_weekly(); W.slack_post(journal)
        self.assertTrue(journal.startswith("*Nova Weekly Journal — "))
        for sec in ("*Memory Volume:*", "*App Health:*", "*Dream Log:*", "  - _flying_"):
            self.assertIn(sec, journal)
        self.assertEqual(W.notify.call_args.kwargs["level"], "info")

    def test_failure_path_shape(self):
        self.assertIn('slack_post(f"Nova Weekly Journal failed: {e}",', SRC)
        self.assertIn('dedup_key="weekly-journal-failure"', SRC)
        W.notify.reset_mock()
        W.slack_post("Nova Weekly Journal failed: boom", level="warning", dedup_key="weekly-journal-failure")
        kw = W.notify.call_args.kwargs
        self.assertEqual((kw["level"], kw["dedup_key"], kw["body"]), ("warning", "weekly-journal-failure", None))

    def test_quiet_week(self):
        with mock.patch.object(W.subprocess, "run", _psql([])), mock.patch.object(W, "section_app_health", return_value=None):
            self.assertTrue(W.generate_weekly().endswith("_Quiet week — no significant events recorded._"))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        home = TMP / "home"; (home / ".openclaw" / "logs").mkdir(parents=True, exist_ok=True)
        r = subprocess.run([sys.executable, "-c", "import nova_weekly_journal"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(home)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertTrue((home / ".openclaw" / "logs" / "weekly-journal.log").exists())   # the log landed in the fake HOME


if __name__ == "__main__":
    unittest.main()
