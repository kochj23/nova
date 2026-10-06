#!/usr/bin/env python3
"""Tests for nova_article_watchdog.py --stranded: the host-agnostic sweep that pushes commits
stranded in whichever clone the watchdog runs against (added after nova-core's clone held 3
unpushed articles on 2026-10-05, invisible to the .6 watchdog). Uses throwaway local git repos;
no network, no PG. Written by Jordan Koch (via Claude)."""
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("article_watchdog_under_test", SCRIPTS / "nova_article_watchdog.py")
wd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wd)


def git(cwd, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                          capture_output=True, text=True, check=True).stdout


class StrandedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="artwd-"))
        self.origin = self.tmp / "origin.git"
        git(self.tmp, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.clone = self.tmp / "nova-journal"
        git(self.tmp, "clone", "-q", str(self.origin), str(self.clone))
        git(self.clone, "checkout", "-q", "-b", "main")
        (self.clone / "a.md").write_text("a\n")
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-q", "-m", "seed")
        git(self.clone, "push", "-q", "origin", "main")
        self.lock_calls = []
        acquire = lambda *a, **k: self.lock_calls.append("acquire") or "conn"
        release = lambda c: self.lock_calls.append(("release", c))
        self.patches = [patch.object(wd, "HUGO", self.clone),
                        patch.object(wd, "_push_lock", lambda: (acquire, release))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def _strand(self, name="b.md"):
        (self.clone / name).write_text("article\n")
        git(self.clone, "add", ".")
        git(self.clone, "commit", "-q", "-m", f"publish {name}")

    def _origin_head(self):
        return git(self.origin, "rev-parse", "main").strip()

    def test_clean_clone_is_ok_and_takes_no_lock(self):
        self.assertEqual(wd.push_stranded(dry=False), 0)
        self.assertEqual(self.lock_calls, [])

    def test_dry_run_reports_but_does_not_push(self):
        self._strand()
        before = self._origin_head()
        self.assertEqual(wd.push_stranded(dry=True), 1)
        self.assertEqual(self._origin_head(), before)
        self.assertEqual(wd.unpushed_count(), 1)
        self.assertEqual(self.lock_calls, [])

    def test_stranded_commit_is_pushed_under_lock(self):
        self._strand()
        self.assertEqual(wd.push_stranded(dry=False), 0)
        self.assertEqual(self._origin_head(), git(self.clone, "rev-parse", "HEAD").strip())
        self.assertEqual(wd.unpushed_count(), 0)
        self.assertEqual(self.lock_calls, ["acquire", ("release", "conn")])

    def test_rebases_over_another_writers_push(self):
        # another host pushed meanwhile -> our clone is behind AND ahead
        other = self.tmp / "other"
        git(self.tmp, "clone", "-q", str(self.origin), str(other))
        (other / "c.md").write_text("other host\n")
        git(other, "add", "."); git(other, "commit", "-q", "-m", "other"); git(other, "push", "-q", "origin", "main")
        self._strand()
        self.assertEqual(wd.push_stranded(dry=False), 0)
        log = git(self.origin, "log", "--oneline", "main")
        self.assertIn("publish b.md", log)
        self.assertIn("other", log)

    def test_cli_stranded_never_touches_schedule_or_regenerates(self):
        self._strand()
        with patch.object(sys, "argv", ["x", "--stranded", "--dry-run"]), \
             patch.object(wd, "expected_today", side_effect=AssertionError("must not run")), \
             patch.object(wd, "regenerate", side_effect=AssertionError("must not run")):
            self.assertEqual(wd.main(), 1)

    def test_source_has_no_hardcoded_home(self):
        self.assertNotIn("/Users/" + "kochj/", (SCRIPTS / "nova_article_watchdog.py").read_text())


if __name__ == "__main__":
    unittest.main()
