#!/usr/bin/env python3
"""Tests for nova_fix_starthere.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The script is a one-shot fixer with NO __main__ guard: it edits ~/nova-journal and runs git at import.
So it is never imported here; every run goes through runpy with HOME pointed at a tempdir and
subprocess.run mocked (no git add/commit/pull/push ever reaches a real repo)."""
import os
import re
import runpy
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_fix_starthere.py"
SRC = SCRIPT.read_text()


def _r(rc=0, err=""):
    return SimpleNamespace(returncode=rc, stdout="", stderr=err)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.page = self.home / "nova-journal/content/start-here/index.md"
        self.page.parent.mkdir(parents=True)

    def run_script(self, text, results=None):
        self.page.write_text(text)
        calls = []
        seq = list(results or [])
        def fake(args, **kw):
            calls.append((args, kw.get("cwd")))
            return seq.pop(0) if seq else _r()
        out = StringIO()
        with patch.dict(os.environ, {"HOME": str(self.home)}), patch.object(subprocess, "run", side_effect=fake), \
             redirect_stdout(out):
            runpy.run_path(str(SCRIPT), run_name="__main__")
        return calls, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)
        self.assertNotIn("--force", SRC)

    def test_only_touches_the_start_here_page(self):
        self.assertEqual(re.findall(r'"git", "add", "([^"]+)"', SRC), ["content/start-here/index.md"])


class TestPerformance(_Base):
    def test_rewrites_10k_links_fast(self):
        text = "\n".join(f"- [r{i}](/rando/2026-05-22-post-{i}/)" for i in range(10_000))
        t0 = time.perf_counter()
        _, out = self.run_script(text)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertIn("replaced 10000", out)


class TestRetry(_Base):
    def test_failed_rebase_aborts_push(self):
        # RETRY GAP: git pull --rebase — one attempt; on failure it runs `rebase --abort` and never pushes
        calls, out = self.run_script("x /rando/2026-05-22-a", [_r(), _r(), _r(1, "CONFLICT"), _r()])
        cmds = [a[:3] for a, _ in calls]
        self.assertIn(["git", "rebase", "--abort"], cmds)
        self.assertNotIn(["git", "push"], [a for a, _ in calls])
        self.assertIn("push ABORTED", out)


class TestUnit(_Base):
    def test_only_dated_rando_links_rewritten(self):
        self.run_script("[a](/rando/2026-05-22-x/) [b](/rando/2026-06-01-y/)")
        s = self.page.read_text()
        self.assertIn("/operations/2026-05-22-x/", s)
        self.assertIn("/rando/2026-06-01-y/", s)

    def test_nothing_to_replace_still_reports_zero(self):
        _, out = self.run_script("clean page")
        self.assertIn("replaced 0", out)


class TestIntegration(_Base):
    def test_git_runs_in_the_journal_repo_in_order(self):
        calls, _ = self.run_script("/rando/2026-05-22-a")
        self.assertTrue(all(cwd == str(self.home / "nova-journal") for _, cwd in calls))
        self.assertEqual([a[1] for a, _ in calls], ["add", "commit", "pull", "push"])


class TestFunctional(_Base):
    def test_golden_path(self):
        calls, out = self.run_script("see /rando/2026-05-22-one and /rando/2026-05-22-two")
        self.assertEqual(self.page.read_text().count("/operations/2026-05-22-"), 2)
        self.assertIn("replaced 2", out)
        self.assertIn("push rc=0", out)
        self.assertTrue(out.rstrip().endswith("DONE"))

    def test_missing_page_fails_before_git(self):
        calls = []
        with patch.dict(os.environ, {"HOME": str(self.home / "nowhere")}), \
             patch.object(subprocess, "run", side_effect=lambda *a, **k: calls.append(a)):
            with self.assertRaises(FileNotFoundError):
                runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(calls, [])


class TestFrame(unittest.TestCase):
    def test_script_fails_closed_without_a_journal(self):
        # smoke in a real interpreter with an empty HOME: it must die on the missing page before any git call
        with tempfile.TemporaryDirectory() as h:
            r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                               env={**os.environ, "HOME": h, "NOVA_TEST_QUIET": "1", "GIT_CEILING_DIRECTORIES": h})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FileNotFoundError", r.stderr)
        self.assertNotIn("> git", r.stdout)

    def test_has_no_main_guard_so_never_imported(self):
        self.assertNotIn("__main__", SRC)        # documented: importing would run it — tests never do


if __name__ == "__main__":
    unittest.main()
