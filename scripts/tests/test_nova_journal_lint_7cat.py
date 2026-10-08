#!/usr/bin/env python3
"""7-category gap tests for nova_journal_lint.git_commit_and_push — the push retry added here
(a push rejected because another publisher just pushed now re-pulls and retries, up to 3 rounds,
never force-pushes). git is mocked; nothing touches the real journal clone.
Base suite: test_nova_journal_lint.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_journal_lint_7cat.py
"""
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_journal_lint.py"
TMP = Path(tempfile.mkdtemp(prefix="journal-lint-7cat-"))


def _load():
    spec = importlib.util.spec_from_file_location("journal_lint_7cat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"HOME": str(TMP)}), patch.object(subprocess, "run", side_effect=AssertionError("git at import")):
        spec.loader.exec_module(mod)
    mod.HUGO_ROOT = TMP / "nova-journal"; mod.CONTENT_DIR = mod.HUGO_ROOT / "content"
    mod.LOG_FILE = TMP / "logs" / "lint.log"
    mod.CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    return mod


jl = _load()


def r(rc=0, err=""):
    return types.SimpleNamespace(returncode=rc, stdout="", stderr=err)


def run_push(seq):
    with patch.object(subprocess, "run", MagicMock(side_effect=seq)) as sp, patch.object(jl.time, "sleep") as sl, \
            redirect_stdout(io.StringIO()) as out:
        jl.git_commit_and_push(2)
    return [c.args[0] for c in sp.call_args_list], sl, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_never_force_pushes(self):
        argvs, _, _ = run_push([r(), r(), r(), r(1, "rejected"), r(), r(1, "rejected"), r(), r(1, "rejected")])
        self.assertFalse(any("--force" in a or "-f" in a for a in argvs))


class TestPerformance(unittest.TestCase):
    def test_bounded_rounds_and_backoff(self):
        argvs, sl, _ = run_push([r(), r(), r(), r(1), r(), r(1), r(), r(1)])
        self.assertEqual(argvs.count(["git", "push"]), 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [5, 10])


class TestRetry(unittest.TestCase):
    def test_rejected_push_repulls_and_succeeds(self):
        argvs, _, out = run_push([r(), r(), r(), r(1, "! [rejected] fetch first"), r(), r()])
        self.assertEqual(argvs.count(["git", "push"]), 2)
        self.assertEqual(sum(1 for a in argvs if a[:3] == ["git", "pull", "--rebase"]), 2)
        self.assertIn("Pushed auto-fix commit: 2 file(s)", out)

    def test_final_failure_logged(self):
        _, _, out = run_push([r(), r(), r(), r(1, "denied"), r(), r(1, "denied"), r(), r(1, "denied")])
        self.assertIn("Push FAILED after 3 tries", out)


class TestUnit(unittest.TestCase):
    def test_conflict_aborts_without_push(self):
        argvs, _, out = run_push([r(), r(), r(1, "CONFLICT"), r()])
        self.assertEqual(argvs[-1], ["git", "rebase", "--abort"])
        self.assertNotIn(["git", "push"], argvs)


class TestIntegration(unittest.TestCase):
    def test_commit_message_counts_files(self):
        argvs, _, _ = run_push([r(), r(), r(), r()])
        self.assertIn("Auto-fix 2 file(s)", argvs[1][-1])


class TestFunctional(unittest.TestCase):
    def test_golden_single_push(self):
        argvs, sl, out = run_push([r(), r(), r(), r()])
        self.assertEqual(argvs.count(["git", "push"]), 1)
        sl.assert_not_called()
        self.assertIn("Pushed auto-fix commit", out)


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        p = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == "__main__":
    unittest.main()
