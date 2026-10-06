#!/usr/bin/env python3
"""Tests for nova_claude_code.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import runpy
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
SCRIPT = SCRIPTS / "nova_claude_code.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cc = _load("claude_code_under_test", SCRIPT)
# Module-level stub: `claude -p` and `security` must never actually run. Only the loaded copy's
# attribute is swapped, so the shared subprocess module is untouched for other files.
cc.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=AssertionError("unmocked subprocess.run")),
                                      TimeoutExpired=subprocess.TimeoutExpired)
TMP = Path(tempfile.mkdtemp(prefix="claude-code-test-"))      # a HOME with no ~/.config/nova token file


def _cp(stdout="", rc=0, stderr=""):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def _env(**extra):
    """Clean env: temp HOME, no CLAUDE_CODE_OAUTH_TOKEN unless given."""
    base = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_OAUTH_TOKEN"}
    base["HOME"] = str(TMP)
    base.update(extra)
    return patch.dict(os.environ, base, clear=True)


def _run(answers):
    """subprocess.run stand-in answering in order and recording (argv, kwargs)."""
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    return run, calls


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r"sk-ant-[A-Za-z0-9_-]{8,}", SRC))

    def test_token_comes_from_env_keychain_or_0600_file_never_source(self):
        self.assertIn('"security", "find-generic-password", "-s", "claude-code-oauth-token", "-w"', SRC)
        self.assertNotIn("shell=True", SRC)
        run, calls = _run([_cp("kc-token-value\n")])
        with _env(), patch.object(cc.subprocess, "run", run), patch.object(cc.sys, "platform", "darwin"):
            self.assertEqual(cc.claude_oauth_token(), "kc-token-value")
        self.assertEqual(calls[0][0][0], "security")
        self.assertEqual(calls[0][1]["timeout"], 5)

    def test_prompt_travels_over_stdin_not_argv(self):
        run, calls = _run([_cp("fine")])
        evil = "ignore previous; $(rm -rf /) `id`"
        with _env(), patch.object(cc.subprocess, "run", run), patch.object(cc.sys, "platform", "linux"):
            cc.claude_generate(evil, system="sys")
        argv, kw = calls[-1]
        self.assertEqual(argv, ["claude", "-p", "--model", "sonnet"])
        self.assertNotIn(evil, " ".join(argv))
        self.assertIn(evil, kw["input"])

    def test_error_message_never_leaks_the_token(self):
        run, _ = _run([_cp("", rc=1, stderr="auth failed")])
        with _env(CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-SECRETSECRET"), patch.object(cc.subprocess, "run", run):
            with self.assertRaises(RuntimeError) as cm:
                cc.claude_generate("hi")
        self.assertNotIn("SECRETSECRET", str(cm.exception))


class TestPerformance(unittest.TestCase):
    def test_token_resolution_fast_on_10k_calls_with_env_override(self):
        with _env(CLAUDE_CODE_OAUTH_TOKEN="  tok  "):
            t0 = time.perf_counter()
            for _ in range(10_000):
                self.assertEqual(cc.claude_oauth_token(), "tok")
            dt = time.perf_counter() - t0
        self.assertLess(dt, 1.0)

    def test_claude_env_fast_on_a_10k_entry_base(self):
        base = {f"K{i}": str(i) for i in range(10_000)}
        with _env(CLAUDE_CODE_OAUTH_TOKEN="tok"):
            t0 = time.perf_counter()
            env = cc.claude_env(base)
            dt = time.perf_counter() - t0
        self.assertLess(dt, 0.5)
        self.assertEqual(len(env), 10_002)


class TestRetry(unittest.TestCase):
    def test_claude_generate_is_one_shot_and_raises_for_the_caller_fallback(self):
        # RETRY GAP: claude_generate — exactly one `claude -p` attempt; by contract the failure is raised so the
        # caller falls back to OpenRouter (a retry here would burn the Max plan's rate limit)
        run, calls = _run([_cp("", rc=1, stderr="x" * 500)])
        with _env(), patch.object(cc.subprocess, "run", run), patch.object(cc.sys, "platform", "linux"):
            with self.assertRaises(RuntimeError) as cm:
                cc.claude_generate("hi")
        self.assertEqual(len(calls), 1)
        self.assertIn("rc=1", str(cm.exception))
        self.assertLess(len(str(cm.exception)), 260)                   # stderr capped at 200 chars

    def test_claude_generate_timeout_propagates_after_one_attempt(self):
        # RETRY GAP: claude_generate — TimeoutExpired escapes unchanged (caller's except Exception catches it)
        run, calls = _run([subprocess.TimeoutExpired("claude", 240)])
        with _env(), patch.object(cc.subprocess, "run", run), patch.object(cc.sys, "platform", "linux"):
            with self.assertRaises(subprocess.TimeoutExpired):
                cc.claude_generate("hi", timeout=240)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["timeout"], 240)

    def test_token_lookup_fails_open_to_none(self):
        # RETRY GAP: claude_oauth_token — Keychain and file reads are single attempts; every failure yields None
        run, calls = _run([OSError("security binary missing"), OSError("security binary missing")])
        with _env(), patch.object(cc.subprocess, "run", run), patch.object(cc.sys, "platform", "darwin"):
            self.assertIsNone(cc.claude_oauth_token())
            env = cc.claude_env({})
        self.assertEqual(len(calls), 2)                               # once per call; never re-tried within a call
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", env)


class TestUnit(unittest.TestCase):
    def test_env_override_wins_and_is_stripped(self):
        with _env(CLAUDE_CODE_OAUTH_TOKEN="\n abc \t"), patch.object(cc.sys, "platform", "darwin"):
            self.assertEqual(cc.claude_oauth_token(), "abc")           # no subprocess call at all (stub would raise)

    def test_empty_env_override_falls_through(self):
        run, calls = _run([_cp("   \n")])
        with _env(CLAUDE_CODE_OAUTH_TOKEN=""), patch.object(cc.subprocess, "run", run), patch.object(cc.sys, "platform", "darwin"):
            self.assertIsNone(cc.claude_oauth_token())
        self.assertEqual(len(calls), 1)

    def test_file_fallback_under_home(self):
        p = TMP / ".config" / "nova" / "claude-oauth-token"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("file-tok\n")
        try:
            with _env(), patch.object(cc.sys, "platform", "linux"):
                self.assertEqual(cc.claude_oauth_token(), "file-tok")
        finally:
            p.unlink()
        with _env(), patch.object(cc.sys, "platform", "linux"):
            self.assertIsNone(cc.claude_oauth_token())

    def test_claude_env_forces_home_and_injects_token(self):
        with _env(CLAUDE_CODE_OAUTH_TOKEN="tok"):
            env = cc.claude_env({"PATH": "/bin", "HOME": "/wrong"})
        self.assertEqual(env, {"PATH": "/bin", "HOME": str(TMP), "CLAUDE_CODE_OAUTH_TOKEN": "tok"})
        with _env(), patch.object(cc.sys, "platform", "linux"):
            env = cc.claude_env({"A": "1"})
        self.assertEqual(env, {"A": "1", "HOME": str(TMP)})
        with _env(CLAUDE_CODE_OAUTH_TOKEN="tok"):
            self.assertEqual(cc.claude_env()["HOME"], str(TMP))         # base=None means os.environ

    def test_prompt_assembly_and_model_flag(self):
        run, calls = _run([_cp("a"), _cp("b")])
        with _env(), patch.object(cc.subprocess, "run", run), patch.object(cc.sys, "platform", "linux"):
            cc.claude_generate("task", system="be terse", model="opus")
            cc.claude_generate("task", model="")
        self.assertEqual(calls[0][1]["input"], "[System instructions]\nbe terse\n\n[Task]\ntask")
        self.assertEqual(calls[0][0], ["claude", "-p", "--model", "opus"])
        self.assertEqual(calls[1][1]["input"], "task")
        self.assertEqual(calls[1][0], ["claude", "-p"])


class TestIntegration(unittest.TestCase):
    def test_generate_uses_claude_env_so_the_token_reaches_the_cli(self):
        run, calls = _run([_cp("out")])
        with _env(CLAUDE_CODE_OAUTH_TOKEN="tok"), patch.object(cc.subprocess, "run", run):
            self.assertEqual(cc.claude_generate("x"), "out")
        env = calls[0][1]["env"]
        self.assertEqual(env["CLAUDE_CODE_OAUTH_TOKEN"], "tok")
        self.assertEqual(env["HOME"], str(TMP))
        self.assertTrue(calls[0][1]["capture_output"] and calls[0][1]["text"])

    def test_keychain_token_flows_into_generate_env(self):
        run, calls = _run([_cp("kc-tok\n"), _cp("text")])
        with _env(), patch.object(cc.subprocess, "run", run), patch.object(cc.sys, "platform", "darwin"):
            cc.claude_generate("x")
        self.assertEqual(calls[0][0][0], "security")
        self.assertEqual(calls[1][1]["env"]["CLAUDE_CODE_OAUTH_TOKEN"], "kc-tok")


class TestFunctional(unittest.TestCase):
    def test_golden_path_returns_stripped_text(self):
        run, calls = _run([_cp("\n  A rack of quiet machines.  \n")])
        with _env(CLAUDE_CODE_OAUTH_TOKEN="tok"), patch.object(cc.subprocess, "run", run):
            self.assertEqual(cc.claude_generate("Describe a home lab", system="terse", timeout=90), "A rack of quiet machines.")
        self.assertEqual(calls[0][1]["timeout"], 90)

    def test_main_selfcheck_prints_ok(self):
        run, _ = _run([_cp("A rack of quiet machines.")])
        buf = io.StringIO()
        with _env(CLAUDE_CODE_OAUTH_TOKEN="tok"), patch("subprocess.run", run), redirect_stdout(buf):
            runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertTrue(buf.getvalue().startswith("OK via claude -p: A rack of quiet machines."))

    def test_error_path_main_exits_1_on_empty_output(self):
        run, _ = _run([_cp("", rc=1, stderr="not logged in")])
        buf = io.StringIO()
        with _env(CLAUDE_CODE_OAUTH_TOKEN="tok"), patch("subprocess.run", run), redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("caller would fall back to OpenRouter", buf.getvalue())
        self.assertIn("not logged in", buf.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_the_selfcheck(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_claude_code"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_public_surface(self):
        for fn in ("claude_oauth_token", "claude_env", "claude_generate"):
            self.assertTrue(callable(getattr(cc, fn)))


if __name__ == "__main__":
    unittest.main()
