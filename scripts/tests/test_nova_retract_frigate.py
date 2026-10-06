#!/usr/bin/env python3
"""Tests for nova_retract_frigate.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_retract_frigate.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="retract-frigate-test-"))
_REAL_RUN = subprocess.run
GIT_REMOVE = ["git", "rm", "-f"]


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


def _exec(results=None, home=None):
    """The script is a flat one-shot: every statement runs at import. Execute it with subprocess.run
    stubbed (never a real git call) and HOME pointed at a tempdir; return (module, calls, stdout).
    `results` answers the git calls in order: remove, remove, commit, pull --rebase, [push | rebase --abort]."""
    results = list(results or [_cp()] * 5)
    calls = []

    def fake_run(args, **kw):
        calls.append((list(args), kw))
        return results.pop(0) if results else _cp()

    spec = importlib.util.spec_from_file_location("retract_frigate_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    env = {"HOME": str(home or TMP)}
    with patch.dict(os.environ, env), patch.object(subprocess, "run", fake_run), redirect_stdout(io.StringIO()) as out:
        spec.loader.exec_module(mod)
    return mod, calls, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_tokens(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("https://", SRC)                 # push goes over the clone's own (SSH) remote, no token URL

    def test_git_runs_as_argv_never_a_shell(self):
        self.assertNotIn("shell=True", SRC)
        self.assertNotIn("os.system", SRC)
        _, calls, _ = _exec()
        for args, kw in calls:
            self.assertIsInstance(args, list)
            self.assertEqual(args[0], "git")
            self.assertFalse(kw.get("shell", False))

    def test_slug_is_a_safe_path_component(self):
        _, calls, _ = _exec()
        mod_slug = re.search(r'SLUG = "([^"]+)"', SRC).group(1)
        self.assertRegex(mod_slug, r"^[a-z0-9-]+$")      # no '..', '/', or shell metacharacters
        self.assertEqual(calls[0][0][-1], f"content/operations/{mod_slug}.md")
        self.assertEqual(calls[1][0][-1], f"static/images/operations/{mod_slug}.webp")

    def test_output_is_truncated_so_a_huge_git_error_cannot_flood_the_log(self):
        mod, _, _ = _exec()
        with patch.object(subprocess, "run", lambda *a, **k: _cp(1, "o" * 5000, "e" * 5000)), \
             redirect_stdout(io.StringIO()) as out:
            mod.run(["git", "status"])
        self.assertLess(len(out.getvalue()), 800)


class TestPerformance(unittest.TestCase):
    def test_run_wrapper_is_cheap_over_10k_calls(self):
        mod, _, _ = _exec()
        t0 = time.perf_counter()
        with patch.object(subprocess, "run", lambda *a, **k: _cp(0, "ok", "")), redirect_stdout(io.StringIO()):
            for _ in range(10_000):
                mod.run(["git", "status"])
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_script_is_a_bounded_straight_line(self):
        self.assertNotIn("while ", SRC)
        self.assertLessEqual(len([a for a, _ in _exec()[1]]), 5)


class TestRetry(unittest.TestCase):
    def test_pull_rebase_failure_aborts_and_never_pushes(self):
        # RETRY GAP: pull --rebase / push — one attempt each; a diverged clone aborts the rebase and skips the push
        _, calls, out = _exec([_cp(), _cp(), _cp(), _cp(1, "", "CONFLICT in content/x.md"), _cp()])
        argv = [a for a, _ in calls]
        self.assertIn(["git", "rebase", "--abort"], argv)
        self.assertNotIn(["git", "push"], argv)
        self.assertIn("push ABORTED", out)
        self.assertIn("CONFLICT", out)
        self.assertTrue(out.rstrip().endswith("RETRACT DONE"))   # the script still finishes cleanly

    def test_git_remove_failure_does_not_stop_the_sequence(self):
        # RETRY GAP: run() — a failed git remove (file already gone) is logged and the commit/pull/push still run
        _, calls, out = _exec([_cp(128, "", "fatal: pathspec did not match"), _cp(), _cp(), _cp(), _cp()])
        self.assertEqual(len(calls), 5)
        self.assertEqual(calls[-1][0], ["git", "push"])
        self.assertIn("-> 128", out)

    def test_push_failure_is_reported_not_raised(self):
        _, _, out = _exec([_cp(), _cp(), _cp(), _cp(), _cp(1, "", "rejected: non-fast-forward")])
        self.assertIn("push rc=1", out)
        self.assertIn("non-fast-forward", out)


class TestUnit(unittest.TestCase):
    def test_run_prints_rc_and_tails(self):
        mod, _, _ = _exec()
        with patch.object(subprocess, "run", lambda *a, **k: _cp(0, "   \n", "warning: x")), \
             redirect_stdout(io.StringIO()) as out:
            r = mod.run(["git", "status"])
        self.assertIsNone(r)
        self.assertEqual(out.getvalue(), "> git status -> 0\n  err: warning: x\n")   # blank stdout is skipped

    def test_run_uses_the_journal_checkout_as_cwd(self):
        mod, _, _ = _exec()
        with patch.object(subprocess, "run", MagicMock(return_value=_cp())) as sp, redirect_stdout(io.StringIO()):
            mod.run(["git", "status"])
        kw = sp.call_args[1]
        self.assertEqual(kw["cwd"], mod.ROOT)
        self.assertTrue(kw["capture_output"] and kw["text"])

    def test_root_follows_home(self):
        mod, _, _ = _exec(home=TMP / "elsewhere")
        self.assertEqual(mod.ROOT, str(TMP / "elsewhere" / "nova-journal"))


class TestIntegration(unittest.TestCase):
    def test_git_sequence_in_order_with_autostash_rebase_before_push(self):
        _, calls, _ = _exec()
        argv = [a for a, _ in calls]
        self.assertEqual(argv[0][:3], GIT_REMOVE)
        self.assertEqual(argv[1][:3], GIT_REMOVE)
        self.assertEqual(argv[2][:3], ["git", "commit", "-m"])
        self.assertEqual(argv[3], ["git", "pull", "--rebase", "--autostash", "origin", "main"])
        self.assertEqual(argv[4], ["git", "push"])
        self.assertTrue(all(kw["cwd"].endswith("nova-journal") for _, kw in calls))

    def test_commit_message_cites_the_production_issue(self):
        _, calls, _ = _exec()
        msg = calls[2][0][-1]
        self.assertIn("#635", msg)
        self.assertIn("Retract Frigate", msg)


class TestFunctional(unittest.TestCase):
    def test_golden_path_removes_article_and_image_then_pushes(self):
        _, calls, out = _exec([_cp(0, "removed content/x.md"), _cp(0, "removed static/x.webp"), _cp(0, "[main abc] Retract"),
                               _cp(0, "Already up to date."), _cp(0, "", "To github.com:kochj23/nova-journal.git")])
        self.assertEqual(len(calls), 5)
        self.assertIn("> " + " ".join(GIT_REMOVE) + " content/operations/", out)
        self.assertIn("out: [main abc] Retract", out)
        self.assertIn("push rc=0 To github.com:kochj23/nova-journal.git", out)
        self.assertTrue(out.rstrip().endswith("RETRACT DONE"))

    def test_error_path_never_leaves_a_half_done_rebase(self):
        _, calls, out = _exec([_cp(), _cp(), _cp(), _cp(1, "", "error: could not apply"), _cp(0)])
        abort = [kw for a, kw in calls if a == ["git", "rebase", "--abort"]]
        self.assertEqual(len(abort), 1)
        self.assertTrue(abort[0]["capture_output"])
        self.assertNotIn("push rc=", out)


class TestFrame(unittest.TestCase):
    def test_script_has_no_main_guard_by_design_and_tests_never_import_it_bare(self):
        # A flat one-shot (no main(), no argparse): every statement is a side effect, so the only safe
        # smoke is a compile check plus the stubbed exec above — never `python3 nova_retract_frigate.py`.
        self.assertNotIn('if __name__ == "__main__"', SRC)
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_stubbed_exec_makes_no_real_git_call(self):
        self.assertIs(subprocess.run, _REAL_RUN)          # the stub is scoped to _exec and restored
        _, calls, _ = _exec()
        self.assertEqual(len(calls), 5)
        self.assertIs(subprocess.run, _REAL_RUN)


if __name__ == "__main__":
    unittest.main()
