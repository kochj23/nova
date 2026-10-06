#!/usr/bin/env python3
"""Tests for nova_journal_lint.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import contextlib
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
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_journal_lint.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="journal-lint-test-"))
_MISSING = object()


@contextlib.contextmanager
def _stub_modules(stubs):
    saved = {k: sys.modules.get(k, _MISSING) for k in stubs}
    sys.modules.update(stubs)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is _MISSING:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load():
    spec = importlib.util.spec_from_file_location("journal_lint_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(os.environ, {"HOME": str(TMP)}), patch.object(subprocess, "run", side_effect=AssertionError("git at import")):
        spec.loader.exec_module(mod)
    mod.HUGO_ROOT = TMP / "nova-journal"; mod.CONTENT_DIR = mod.HUGO_ROOT / "content"
    mod.LOG_FILE = TMP / "logs" / "nova_journal_lint.log"
    mod.CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    return mod


jl = _load()

BROKEN = '---\ntitle: "🎨 "Cold Night, Warm Glow""\ndate: 2026-10-05\nimage:\n  alt: "a "quote" inside"\n---\n\nBody with "quotes" untouched.\n'
CLEAN = '---\ntitle: "A fine title: with colon"\ndescription: plain\n---\nbody\n'


def _write(name, text):
    p = jl.CONTENT_DIR / name; p.write_text(text, encoding="utf-8"); return p


def _git(rc=0, stdout="", stderr=""):
    return MagicMock(return_value=types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr))


def _run(mode, run=None):
    with patch.object(sys, "argv", ["nova_journal_lint.py", mode]), patch.object(subprocess, "run", run or _git()) as sp, \
         patch.object(jl, "notify_slack") as ns, redirect_stdout(io.StringIO()) as out:
        try:
            jl.main(); code = 0
        except SystemExit as e:
            code = e.code
    return code, sp, ns, out.getvalue()


def _reset():
    for p in jl.CONTENT_DIR.rglob("*.md"):
        p.unlink()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_argv_only(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC); self.assertNotIn("os.system", SRC)

    def test_frontmatter_content_never_reaches_a_shell(self):
        _reset(); _write("evil.md", '---\ntitle: "x" $(touch /tmp/pwned) "y"\n---\n')
        code, sp, ns, out = _run("auto")
        for c in sp.call_args_list:
            self.assertNotIn("pwned", " ".join(c[0][0]))
        self.assertEqual(sp.call_args_list[0][0][0], ["hugo", "--gc", "--minify", "--buildFuture", "--quiet"])
        self.assertEqual(sp.call_args_list[0][1]["cwd"], jl.HUGO_ROOT)

    def test_error_text_posted_to_slack_is_truncated(self):
        cfg = types.ModuleType("nova_config"); cfg.post_both = MagicMock(); cfg.SLACK_ALERTS = "C_ALERTS"
        with _stub_modules({"nova_config": cfg}):
            jl.notify_slack("e" * 5000)
        msg = cfg.post_both.call_args[0][0]
        self.assertLess(len(msg), 700); self.assertEqual(cfg.post_both.call_args[1]["slack_channel"], "C_ALERTS")


class TestPerformance(unittest.TestCase):
    def test_fix_10k_values_and_lint_a_10k_line_frontmatter_fast(self):
        vals = [f'"🎨 "Title {i}""' if i % 2 else f'"ok {i}"' for i in range(10_000)]
        t0 = time.perf_counter()
        fixed = [jl.fix_yaml_value("title", v) for v in vals]
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(fixed[1], '"Title 1"'); self.assertEqual(fixed[0], '"ok 0"')
        _reset(); p = _write("big.md", "---\n" + "\n".join(f'k{i}: v{i}' for i in range(10_000)) + '\ntitle: "a "b""\n---\nbody\n')
        t0 = time.perf_counter()
        self.assertEqual(len(jl.lint_file(p)), 1)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_hugo_build_check_fails_open(self):
        # RETRY GAP: hugo_build_check — one hugo run; a missing binary or timeout returns (False, reason)
        with patch.object(subprocess, "run", side_effect=FileNotFoundError("hugo")) as sp:
            ok, err = jl.hugo_build_check()
        self.assertFalse(ok); self.assertIn("hugo", err); self.assertEqual(sp.call_count, 1)

    def test_push_is_aborted_when_the_rebase_fails_and_timeouts_are_logged(self):
        # RETRY GAP: git_commit_and_push — one commit/pull/push sequence; a diverged clone aborts, never force-pushes
        seq = [types.SimpleNamespace(returncode=0, stdout="", stderr=""),          # add
               types.SimpleNamespace(returncode=0, stdout="", stderr=""),          # commit
               types.SimpleNamespace(returncode=1, stdout="", stderr="CONFLICT"),  # pull --rebase
               types.SimpleNamespace(returncode=0, stdout="", stderr="")]          # rebase --abort
        with patch.object(subprocess, "run", MagicMock(side_effect=seq)) as sp, redirect_stdout(io.StringIO()) as out:
            jl.git_commit_and_push(1)
        argvs = [c[0][0] for c in sp.call_args_list]
        self.assertEqual(argvs[-1], ["git", "rebase", "--abort"]); self.assertNotIn(["git", "push"], argvs)
        self.assertIn("Push ABORTED", out.getvalue())
        with patch.object(subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 15)), redirect_stdout(io.StringIO()) as out:
            jl.git_commit_and_push(1)
        self.assertIn("git operation timed out", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_fix_yaml_value_cases(self):
        self.assertEqual(jl.fix_yaml_value("title", "plain: value"), "plain: value")          # unquoted untouched
        self.assertEqual(jl.fix_yaml_value("title", '"fine title"'), '"fine title"')
        self.assertEqual(jl.fix_yaml_value("title", '"🎨 "Cold Night""'), '"Cold Night"')      # emoji + nested quotes
        self.assertEqual(jl.fix_yaml_value("alt", '"a "quote" inside"'), '"a quote inside"')
        self.assertEqual(jl.fix_yaml_value("description", '"keep \\"escaped\\" and drop "raw""'), '"keep \\"escaped\\" and drop raw"')

    def test_lint_file_skips_non_frontmatter_and_unterminated(self):
        _reset()
        self.assertEqual(jl.lint_file(_write("plain.md", "no frontmatter\n")), [])
        self.assertEqual(jl.lint_file(_write("open.md", "---\ntitle: \"a \"b\"\"\n")), [])
        self.assertEqual(jl.lint_file(jl.CONTENT_DIR / "missing.md"), [])

    def test_lint_file_rewrites_only_the_frontmatter(self):
        _reset(); p = _write("broken.md", BROKEN)
        fixes = jl.lint_file(p)
        self.assertEqual(len(fixes), 2)
        text = p.read_text()
        self.assertIn('title: "Cold Night, Warm Glow"\n', text)
        self.assertIn('  alt: "a quote inside"\n', text)
        self.assertTrue(text.endswith('Body with "quotes" untouched.\n'))
        self.assertEqual(jl.lint_file(p), [])                                   # idempotent

    def test_log_writes_to_the_redirected_file(self):
        with redirect_stdout(io.StringIO()):
            jl.log("hello")
        self.assertIn("] hello", jl.LOG_FILE.read_text())


class TestIntegration(unittest.TestCase):
    def test_check_mode_reports_without_touching_git(self):
        _reset(); _write("a.md", BROKEN); _write("b.md", CLEAN)
        code, sp, ns, out = _run("check")
        self.assertEqual(code, 0); sp.assert_not_called()
        self.assertIn("Fixed 2 issue(s) in 1 file(s)", out); self.assertIn("Dry run — no commit", out)

    def test_hook_mode_exits_one_when_it_fixed_something(self):
        _reset(); _write("a.md", BROKEN)
        code, sp, ns, out = _run("hook")
        self.assertEqual(code, 1); self.assertIn("LINT: Fixed 2 issue(s)", out)

    def test_notify_slack_uses_the_shared_alert_tier(self):
        cfg = types.ModuleType("nova_config"); cfg.post_both = MagicMock(); cfg.SLACK_ALERTS = "C_ALERTS"
        with _stub_modules({"nova_config": cfg}):
            jl.notify_slack("Error: template failed")
        msg = cfg.post_both.call_args[0][0]
        self.assertTrue(msg.startswith("⚠️ *Journal deploy broken*")); self.assertIn("template failed", msg)
        broken = types.ModuleType("nova_config")                                  # no post_both -> swallowed
        with _stub_modules({"nova_config": broken}):
            jl.notify_slack("x")


class TestFunctional(unittest.TestCase):
    def test_auto_mode_fixes_builds_and_pushes(self):
        _reset(); p = _write("a.md", BROKEN)
        code, sp, ns, out = _run("auto")
        self.assertEqual(code, 0)
        argvs = [c[0][0] for c in sp.call_args_list]
        self.assertEqual(argvs[0][0], "hugo")
        self.assertEqual(argvs[1:], [["git", "add", "-A"], ["git", "commit", "-m", "fix(lint): Auto-fix 1 file(s) with broken YAML frontmatter"],
                                     ["git", "pull", "--rebase", "--autostash", "origin", "main"], ["git", "push"]])
        self.assertIn("Hugo build passes after fixes — deployed", out); ns.assert_not_called()
        self.assertIn('title: "Cold Night, Warm Glow"', p.read_text())

    def test_auto_mode_with_a_still_broken_build_alerts_instead_of_pushing(self):
        _reset(); _write("a.md", BROKEN)
        code, sp, ns, out = _run("auto", run=_git(rc=1, stderr="Error: unmarshal failed"))
        self.assertEqual([c[0][0][0] for c in sp.call_args_list], ["hugo"])
        ns.assert_called_once_with("Error: unmarshal failed"); self.assertIn("Hugo still failing", out)
        _reset(); _write("b.md", CLEAN)
        code, sp, ns, out = _run("auto", run=_git(rc=1, stderr="boom"))
        ns.assert_called_once_with("boom"); self.assertIn("All 1 files OK", out)


class TestFrame(unittest.TestCase):
    def test_check_mode_runs_offline_and_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, str(SCRIPT), "check"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP / "frame-home")})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("All 0 files OK", r.stdout)
        self.assertTrue((TMP / "frame-home" / ".openclaw/logs/nova_journal_lint.log").exists())


if __name__ == "__main__":
    unittest.main()
