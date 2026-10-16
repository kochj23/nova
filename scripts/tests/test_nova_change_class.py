#!/usr/bin/env python3
"""Tests for nova_change_class.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Little Mister, 2026-10-15 (#55). Written by Jordan Koch (via Claude)."""
import importlib.util
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_change_class.py"
spec = importlib.util.spec_from_file_location("change_class_under_test", SCRIPT)
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)
SRC = SCRIPT.read_text()


class TestSecurity(unittest.TestCase):
    def test_git_is_called_without_a_shell(self):
        self.assertNotIn("shell=True", SRC)
        self.assertIsNone(re.search(r"(password|token|secret)\s*=\s*['\"]\w", SRC, re.I))

    def test_columns_are_additive_only(self):
        self.assertNotRegex(cc.COLUMNS, r"DROP|ALTER COLUMN|TYPE ")
        self.assertEqual(cc.COLUMNS.count("ADD COLUMN IF NOT EXISTS"), 4)


class TestPerformance(unittest.TestCase):
    def test_classifiers_fast_on_10k(self):
        t0 = time.perf_counter()
        for i in range(10000):
            cc.classify_commit(f"feat: thing {i}", ["scripts/x.py"])
            cc.classify_proposal(f"raise threshold {i}")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_git_failure_is_empty(self):
        # RETRY GAP: git_commits — one call; any failure returns [] (callers carry on without commits)
        def boom(*a, **k):
            raise OSError("no git")
        self.assertEqual(cc.git_commits(3, _run=boom), [])
        self.assertEqual(cc.git_commits(3, _run=lambda *a, **k: types.SimpleNamespace(returncode=128, stdout="")), [])


class TestUnit(unittest.TestCase):
    def test_commit_classes(self):
        self.assertEqual(cc.classify_commit("fix: hotfix for outage on gateway"), "emergency")
        self.assertEqual(cc.classify_commit('Revert "feat: x"'), "emergency")
        self.assertEqual(cc.classify_commit("docs: README"), "standard")
        self.assertEqual(cc.classify_commit("whatever", ["scripts/tests/test_x.py", "docs/a.md"]), "standard")
        self.assertEqual(cc.classify_commit("feat: new organ", ["scripts/nova_x.py"]), "normal")

    def test_proposal_classes(self):
        self.assertEqual(cc.classify_proposal("restart gateway", open_critical=True), "emergency")
        self.assertEqual(cc.classify_proposal("raise the threshold for llm_ping to 3"), "standard")
        self.assertEqual(cc.classify_proposal("adopt skill 'x'"), "normal")
        self.assertIn(cc.classify_proposal(None), cc.CLASSES)

    def test_reverted_target(self):
        self.assertEqual(cc.reverted_target('Revert "feat: x"'), ("feat: x", None))
        self.assertEqual(cc.reverted_target("reverts 05f46203f3"), (None, "05f46203f3"))
        self.assertEqual(cc.reverted_target("feat: y"), (None, None))


class TestIntegration(unittest.TestCase):
    def test_ensure_columns_goes_through_safe_ddl(self):
        seen = []
        fake = types.ModuleType("nova_pg_schema")
        fake.safe_ddl = lambda cur, sql: seen.append(sql)
        with patch.dict(sys.modules, {"nova_pg_schema": fake}):
            cc.ensure_columns(object())
        self.assertEqual(seen, [cc.COLUMNS])

    def test_shared_by_problems_and_comet_tail(self):
        for name in ("nova_problems.py", "nova_comet_tail.py"):
            self.assertIn("nova_change_class", (SCRIPTS / name).read_text(), name)


class TestFunctional(unittest.TestCase):
    def test_git_log_is_parsed(self):
        out = ("\x1eabc123\x1f2026-10-15T10:15:04-07:00\x1fcinc: run from .6\nscripts/nova_cinc_daily.py\n"
               "config/scheduler.yaml\n\x1edef456\x1f2026-10-14T09:00:00-07:00\x1fdocs: x\n")
        rows = cc.git_commits(3, _run=lambda *a, **k: types.SimpleNamespace(returncode=0, stdout=out))
        self.assertEqual([r["sha"] for r in rows], ["abc123", "def456"])
        self.assertEqual(rows[0]["files"], ["scripts/nova_cinc_daily.py", "config/scheduler.yaml"])
        self.assertEqual(rows[1]["files"], [])
        self.assertEqual(rows[0]["at"].utcoffset().total_seconds(), -7 * 3600)

    def test_real_repo_log_runs(self):
        rows = cc.git_commits(2)
        self.assertIsInstance(rows, list)


class TestFrame(unittest.TestCase):
    def test_import_has_no_side_effects(self):
        r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); "
                            "import nova_change_class; print('ok')"], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.stdout.strip(), "ok", r.stderr)


if __name__ == "__main__":
    unittest.main()
