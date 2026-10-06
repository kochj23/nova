#!/usr/bin/env python3
"""Tests for nova_article_watchdog.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). All git/subprocess, psycopg2 and the scheduler YAML are mocked, and
HUGO is redirected to a tempdir; NO real repo is pushed and NO generator is run.
Written by Jordan Koch (via Claude)."""
import datetime as dt
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


aw = _load("nova_article_watchdog_t", SCRIPTS / "nova_article_watchdog.py")
SRC = (SCRIPTS / "nova_article_watchdog.py").read_text()
NOW = dt.datetime(2026, 10, 5, 12, 0)
LONG = " ".join(["word"] * 200)


def _article(d, name, *, tags=None, date=None, body=LONG, slug_date="2026-10-05"):
    fm = ["---"]
    if date:
        fm.append(f"date: {date}")
    if tags:
        fm.append(f"tags: [{', '.join(tags)}]")
    fm.append("---")
    p = d / name
    p.write_text("\n".join(fm) + "\n" + body)
    return p


def _clean_git(cmd, cwd=None, timeout=900):
    # a git stub where nothing is uncommitted/unpushed
    return subprocess.CompletedProcess(cmd, 0, "", "")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_scheduler_query_is_parameterized(self):
        self.assertIn("WHERE task_script = %s", SRC)
        self.assertNotRegex(SRC, r'execute\(\s*f"""?SELECT')


class TestPerformance(unittest.TestCase):
    def test_journal_section_parse_cached(self):
        aw._SECTION_CACHE.clear()
        t0 = time.perf_counter()
        for _ in range(10_000):
            aw._journal_section("opinion")
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_run_timeout_returns_124(self):
        # RETRY GAP: run()/subprocess — single attempt; a timeout becomes rc=124, never an exception
        with mock.patch.object(aw.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 1)):
            cp = aw.run(["git", "status"])
        self.assertEqual(cp.returncode, 124)

    def test_diagnose_survives_pg_outage(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(aw, "HUGO", Path(td)), \
             mock.patch("psycopg2.connect", side_effect=RuntimeError("pg down")), \
             mock.patch.object(aw, "run", side_effect=_clean_git):
            causes = aw.diagnose({"script": "x.py", "section": "local"})
        kinds = {c for c, _ in causes}
        self.assertIn("unknown", kinds)   # pg failure captured, not raised


class TestUnit(unittest.TestCase):
    def test_published_stub_is_a_miss(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "content" / "local"; d.mkdir(parents=True)
            _article(d, "2026-10-05-x.md", tags=["airwaves"], body="only three words")
            with mock.patch.object(aw, "HUGO", Path(td)):
                ok, detail = aw.published("local", NOW, ("tag", "airwaves"))
        self.assertFalse(ok)
        self.assertIn("STUB", detail)

    def test_published_future_dated_is_a_miss(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "content" / "operations"; d.mkdir(parents=True)
            future = (dt.datetime.now() + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S")
            _article(d, "2026-10-05-ops.md", date=future)
            with mock.patch.object(aw, "HUGO", Path(td)), mock.patch.object(aw, "run", side_effect=_clean_git):
                ok, detail = aw.published("operations", NOW, ("slug", ""))
        self.assertFalse(ok)
        self.assertIn("FUTURE-DATED", detail)

    def test_published_tag_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "content" / "local"; d.mkdir(parents=True)
            _article(d, "2026-10-05-x.md", tags=["something-else"])
            with mock.patch.object(aw, "HUGO", Path(td)):
                ok, detail = aw.published("local", NOW, ("tag", "airwaves"))
        self.assertFalse(ok)
        self.assertIn("tagged", detail)

    def test_unpushed_count_parses(self):
        with mock.patch.object(aw, "run", return_value=subprocess.CompletedProcess([], 0, "3\n", "")):
            self.assertEqual(aw.unpushed_count(), 3)
        with mock.patch.object(aw, "run", return_value=subprocess.CompletedProcess([], 1, "", "err")):
            self.assertEqual(aw.unpushed_count(), 0)


class TestIntegration(unittest.TestCase):
    def test_published_golden_path_requires_pushed(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td) / "content" / "local"; d.mkdir(parents=True)
            _article(d, "2026-10-05-x.md", tags=["airwaves"])
            with mock.patch.object(aw, "HUGO", Path(td)), mock.patch.object(aw, "run", side_effect=_clean_git):
                ok, detail = aw.published("local", NOW, ("tag", "airwaves"))
        self.assertTrue(ok)
        self.assertEqual(detail, "2026-10-05-x.md")

    def test_expected_today_filters_grace_and_future(self):
        sched = {"tasks": [
            {"name": "airwaves", "script": "nova_local_airwaves.py", "schedule": "cron 0 8 * * *"},   # 08:00, past
            {"name": "late", "script": "nova_local_airwaves.py", "schedule": "cron 55 11 * * *"},       # 11:55, within grace
            {"name": "future", "script": "nova_local_airwaves.py", "schedule": "cron 0 23 * * *"},      # 23:00, not due
            {"name": "interval", "script": "nova_local_airwaves.py", "schedule": "every 10m"},          # no cron
        ]}
        with mock.patch.object(aw, "SCHED", Path("/x.yaml")), \
             mock.patch("yaml.safe_load", return_value=sched), \
             mock.patch.object(aw.Path, "read_text", lambda self, *a, **k: ""):
            out = aw.expected_today(NOW)
        names = {o["task"] for o in out}
        self.assertEqual(names, {"airwaves"})


class TestFunctional(unittest.TestCase):
    def test_check_reports_missing_returns_1(self):
        item = {"task": "airwaves", "script": "nova_local_airwaves.py", "section": "local",
                "matcher": ("tag", "airwaves"), "due": NOW.replace(hour=8), "args": []}
        with tempfile.TemporaryDirectory() as td, mock.patch.object(aw, "HUGO", Path(td)), \
             mock.patch.object(aw, "expected_today", return_value=[item]), \
             mock.patch.object(aw, "run", side_effect=_clean_git), \
             mock.patch.object(sys, "argv", ["x", "--check"]), redirect_stdout(io.StringIO()) as out:
            rc = aw.main()
        self.assertEqual(rc, 1)
        self.assertIn("MISSING", out.getvalue())

    def test_stranded_dry_run_never_pushes(self):
        with mock.patch.object(aw, "unpushed_count", return_value=2), \
             mock.patch.object(aw, "rebase_and_push") as push, \
             mock.patch.object(aw, "run", side_effect=_clean_git), \
             mock.patch.object(sys, "argv", ["x", "--stranded", "--dry-run"]), \
             redirect_stdout(io.StringIO()) as out:
            rc = aw.main()
        self.assertEqual(rc, 1)
        push.assert_not_called()
        self.assertIn("DRY-RUN", out.getvalue())

    def test_autofix_dry_run_changes_nothing(self):
        with mock.patch.object(aw, "run") as run, mock.patch.object(aw, "rebase_and_push") as push, \
             redirect_stdout(io.StringIO()):
            fixed = aw.autofix([("unpushed", "2 commits")], dry=True)
        self.assertTrue(fixed)        # it reports it would fix
        run.assert_not_called()       # but runs no git
        push.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_article_watchdog.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--stranded", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
