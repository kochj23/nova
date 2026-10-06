#!/usr/bin/env python3
"""Tests for nova_weekly_ops_report.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). All DB queries, the LLM, image gen, git and the notify bus are
mocked; HUGO/content/image dirs are redirected to a tempdir. No PG, no git push, no network.
Written by Jordan Koch (via Claude)."""
import importlib.util
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


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wo = _load("nova_weekly_ops_report_t", SCRIPTS / "nova_weekly_ops_report.py")
SRC = (SCRIPTS / "nova_weekly_ops_report.py").read_text()
wo.notify = mock.MagicMock()   # neutralize the only outbound notification


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_brief_is_sanitized_before_llm(self):
        # the report routes every table cell through the daily log's _sanitize before the LLM sees it
        self.assertIn("_sanitize(c)", SRC)
        self.assertIs(wo._sanitize, wo.daily._sanitize)

    def test_sql_uses_interval_constants_not_user_values(self):
        # the only f-string interpolation in queries is the fixed window literal, never user input
        self.assertIn("INTERVAL '{W}'", SRC)
        self.assertNotRegex(SRC, r"INTERVAL '\{[a-z_]*(user|input|arg)")


class TestPerformance(unittest.TestCase):
    def test_fmt_large_tables_fast(self):
        rows = [("host", "sig", str(i)) for i in range(10_000)]
        d = {k: rows for k in ("actions_by_type", "deploys", "work_done", "crashes", "incidents",
                               "snmp_alerts", "obs_notable", "threats", "snmp_health", "capacity",
                               "mem_by_source", "work_counts", "work_open_top")}
        d.update(crash_total=1, syslog_vol=1, mem_week=1, mem_total=1, gh={"prs": 0, "merged": 0, "issues": 0})
        with mock.patch.object(wo, "_sanitize", side_effect=lambda c: str(c)):
            t0 = time.perf_counter()
            out = wo.fmt(d)
            self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertIn("WEEKLY INFRASTRUCTURE BRIEF", out)


class TestRetry(unittest.TestCase):
    def test_main_aborts_cleanly_when_llm_returns_none(self):
        # call_llm returns None on any LLM failure; main() must degrade to the abort path, not crash
        with mock.patch.object(wo, "gather", return_value={}), \
             mock.patch.object(wo, "fmt", return_value="brief"), \
             mock.patch.object(wo, "call_llm", return_value=None), \
             mock.patch.object(wo, "publish") as pub, mock.patch.object(wo, "log"):
            self.assertIsNone(wo.main())
        pub.assert_not_called()

    def test_make_cover_image_failure_returns_empty(self):
        # RETRY GAP: make_cover()/generate_image — one attempt; failure yields "" so publish continues text-only
        fake_iu = types.SimpleNamespace(generate_image=mock.Mock(side_effect=RuntimeError("img down")))
        with mock.patch.dict(sys.modules, {"nova_image_utils": fake_iu}), mock.patch.object(wo, "log"):
            self.assertEqual(wo.make_cover("slug", "2026-10-05"), "")


class TestUnit(unittest.TestCase):
    def test_fmt_empty_sections_show_none(self):
        d = {k: [] for k in ("actions_by_type", "deploys", "work_done", "crashes", "incidents",
                             "snmp_alerts", "obs_notable", "threats", "snmp_health", "capacity",
                             "mem_by_source", "work_counts", "work_open_top")}
        d.update(crash_total=0, syslog_vol=0, mem_week=0, mem_total=0, gh={"prs": 0, "merged": 0, "issues": 0})
        with mock.patch.object(wo, "_sanitize", side_effect=lambda c: str(c)):
            out = wo.fmt(d)
        self.assertIn("(none)", out)

    def test_generate_title_strips_quotes(self):
        with mock.patch.object(wo, "call_llm", return_value='  "Seven Days, Nine Crashes"  '):
            self.assertEqual(wo.generate_title("preview"), "Seven Days, Nine Crashes")


class TestIntegration(unittest.TestCase):
    def test_reuses_daily_log_primitives(self):
        self.assertIs(wo.q, wo.daily.q)
        self.assertIs(wo.call_llm, wo.daily.call_llm)
        self.assertIs(wo._gh_json, wo.daily._gh_json)
        self.assertIs(wo.DB, wo.daily.DB)

    def test_schedule_is_thursday_1600(self):
        self.assertIn("cron 0 16 * * 4", SRC)


class TestFunctional(unittest.TestCase):
    def test_publish_writes_post_and_notifies(self):
        with tempfile.TemporaryDirectory() as td:
            content = Path(td) / "content" / "operations"
            with mock.patch.object(wo, "HUGO_ROOT", Path(td)), \
                 mock.patch.object(wo, "CONTENT_DIR", content), \
                 mock.patch.object(wo, "make_cover", return_value=""), \
                 mock.patch.object(wo.subprocess, "run",
                                   return_value=types.SimpleNamespace(returncode=1, stdout="", stderr="")) as run, \
                 mock.patch.object(wo, "notify") as notify, mock.patch.object(wo, "log"):
                url = wo.publish("A Wry Title", "body " * 100)
            posts = list(content.glob("*.md"))
            self.assertEqual(len(posts), 1)
            fm = posts[0].read_text()
            self.assertIn('title: "A Wry Title"', fm)
            self.assertIn("ops-report", fm)
            self.assertNotIn("git push", str(run.call_args_list))  # commit returned rc=1 -> never pushed
            notify.assert_called_once()
            self.assertIn("digitalnoise.net/operations/", url)

    def test_main_golden_path(self):
        with mock.patch.object(wo, "gather", return_value={}), \
             mock.patch.object(wo, "fmt", return_value="the brief"), \
             mock.patch.object(wo, "call_llm", return_value="generated body " * 50), \
             mock.patch.object(wo, "generate_title", return_value="T"), \
             mock.patch.object(wo, "publish", return_value="http://x") as pub, mock.patch.object(wo, "log"):
            wo.main()
        pub.assert_called_once()
        self.assertEqual(pub.call_args[0][0], "T")


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_weekly_ops_report"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Weekly Infrastructure Report starting", r.stdout)


if __name__ == "__main__":
    unittest.main()
