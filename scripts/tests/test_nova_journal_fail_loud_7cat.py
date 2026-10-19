#!/usr/bin/env python3
"""Tests for nova_journal.py's fail-loud LLM path (claude CLI resolution, outage tracking, the exit-75
guard, the publish ledger) — the 7 house categories (Security, Performance, Retry, Unit, Integration,
Functional, Frame). Incident 2026-10-18: `claude` missing -> generators logged "generation failed" and
exited 0. Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
import urllib.error    # noqa: F401  (stdlib imported before the scoped load, see test_nova_journal.py)
import urllib.parse    # noqa: F401
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2          # noqa: F401
import psycopg2.extras   # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_journal.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_journal_failloud_"))


def _stub_modules():
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    iu = types.ModuleType("nova_image_utils"); iu.generate_image = MagicMock(return_value=None)
    oc = types.ModuleType("nova_ops_context")
    oc.get_full_context = lambda hours=24: {}; oc.format_security_brief = lambda c: ""; oc.format_infra_brief = lambda c: ""
    rs = types.ModuleType("nova_resolve"); rs.resolve_url = lambda svc, path="": f"http://127.0.0.1:0{path}"
    return {"nova_notify": nn, "nova_image_utils": iu, "nova_ops_context": oc, "nova_resolve": rs}


def _load():
    spec = importlib.util.spec_from_file_location("nj_failloud", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stub_modules()), patch("psycopg2.connect", side_effect=RuntimeError("offline")), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")), \
         patch("subprocess.run", side_effect=RuntimeError("offline")), patch("subprocess.Popen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


nj = _load()
nj.LOG_FILE = TMP / "nova_journal.log"
nj.STATE_FILE = TMP / "journal_state.json"
nj.HUGO_ROOT = TMP / "nova-journal"


def _cc(bin_path="/opt/x/claude", missing=False):
    """nova_claude_code stand-in: resolve_claude_bin returns a path or raises ClaudeCLIMissing."""
    cc = types.ModuleType("nova_claude_code")
    cc.claude_env = MagicMock(return_value={"HOME": str(TMP)})

    class ClaudeCLIMissing(FileNotFoundError):
        pass
    cc.ClaudeCLIMissing = ClaudeCLIMissing
    if missing:
        cc.resolve_claude_bin = MagicMock(side_effect=ClaudeCLIMissing("Claude Code CLI not found on testhost — set NOVA_CLAUDE_BIN"))
    else:
        cc.resolve_claude_bin = MagicMock(return_value=bin_path)
    return cc


class _Proc:
    def __init__(self, rc=0, out="generated", err=""):
        self.returncode, self._out, self._err, self.pid = rc, out, err, 4242

    def communicate(self, input=None, timeout=None):
        return self._out, self._err

    def kill(self):
        pass


def _reset():
    nj._RUN_STATE.update(llm_ok=0, llm_fail=[], published=0)


def _quiet():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_ledger_sql_is_parameterized_and_cli_argv_is_a_list(self):
        m = re.search(r"INSERT INTO journal_publish_log.*?VALUES \(([^)]*)\)", SRC, re.S)
        self.assertIsNotNone(m)
        self.assertEqual(set(x.strip() for x in m.group(1).split(",")), {"%s"})
        self.assertIn("[claude_bin, \"-p\", \"--model\", cli_model, \"--system-prompt\", system]", SRC)
        cc_src = (SCRIPTS / "nova_claude_code.py").read_text()
        self.assertNotIn("shell=True", cc_src)

    def test_missing_cli_error_never_leaks_the_oauth_token(self):
        spec = importlib.util.spec_from_file_location("cc_failloud", SCRIPTS / "nova_claude_code.py")
        cc = importlib.util.module_from_spec(spec); spec.loader.exec_module(cc)
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-SECRETSECRET", "PATH": ""}), \
             patch.object(cc, "CLAUDE_KNOWN_PATHS", (str(TMP / "none"),)):
            with self.assertRaises(cc.ClaudeCLIMissing) as ctx:
                cc.resolve_claude_bin()
        self.assertNotIn("SECRETSECRET", str(ctx.exception))
        self.assertIn("NOVA_CLAUDE_BIN", str(ctx.exception))


class TestPerformance(unittest.TestCase):
    def test_outage_bookkeeping_is_cheap(self):
        _reset()
        t0 = time.monotonic()
        for i in range(10_000):
            nj._llm_failed(f"exit 1: boom {i} " + "x" * 500)
            nj.llm_unavailable()
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(all(len(r) <= 300 for r in nj._RUN_STATE["llm_fail"]))     # bounded per entry
        _reset()


class TestRetry(unittest.TestCase):
    def test_transient_failures_retry_and_recovery_counts_as_success(self):
        _reset()
        with patch.dict(sys.modules, {"nova_claude_code": _cc()}), \
             patch("subprocess.Popen", side_effect=[_Proc(rc=1, err="ENOENT"), _Proc(rc=0, out="ok text")]) as p, \
             patch("time.sleep") as sl, _quiet():
            self.assertEqual(nj.call_openrouter("s", "u"), "ok text")
        self.assertEqual(p.call_count, 2)
        self.assertEqual(sl.call_args_list[0][0][0], 2)
        self.assertFalse(nj.llm_unavailable())
        self.assertEqual(nj._RUN_STATE["llm_ok"], 1)

    def test_errno2_three_times_is_recorded_as_one_outage(self):
        _reset()
        err = FileNotFoundError(2, "No such file or directory", "claude")
        with patch.dict(sys.modules, {"nova_claude_code": _cc()}), patch("subprocess.Popen", side_effect=[err] * 3) as p, \
             patch("time.sleep"), _quiet():
            self.assertIsNone(nj.call_openrouter("s", "u"))
        self.assertEqual(p.call_count, 3)
        self.assertEqual(len(nj._RUN_STATE["llm_fail"]), 1)
        self.assertIn("No such file or directory", nj._RUN_STATE["llm_fail"][0])
        self.assertTrue(nj.llm_unavailable())

    def test_missing_cli_does_not_retry_or_spawn(self):
        _reset()
        with patch.dict(sys.modules, {"nova_claude_code": _cc(missing=True)}), patch("subprocess.Popen") as p, _quiet() as out:
            self.assertIsNone(nj.call_openrouter("s", "u"))
        p.assert_not_called()
        self.assertIn("Claude Code CLI not found", out.getvalue())
        self.assertTrue(nj.llm_unavailable())


class TestUnit(unittest.TestCase):
    def test_llm_unavailable_truth_table(self):
        _reset()
        self.assertFalse(nj.llm_unavailable())                 # never called the model: intentional skip
        nj._llm_failed("x")
        self.assertTrue(nj.llm_unavailable())
        nj._RUN_STATE["llm_ok"] = 1
        self.assertFalse(nj.llm_unavailable())                 # model answered at least once
        _reset()

    def test_exit_if_llm_unavailable(self):
        _reset()
        nj.exit_if_llm_unavailable("noop")                     # no outage -> returns
        nj._llm_failed("[Errno 2] No such file or directory: 'claude'")
        with _quiet(), self.assertRaises(SystemExit) as ctx:
            nj.exit_if_llm_unavailable("journal_security")
        self.assertEqual(ctx.exception.code, nj.EXIT_LLM_UNAVAILABLE)
        self.assertEqual(nj.EXIT_LLM_UNAVAILABLE, 75)
        nj.note_published()
        nj.exit_if_llm_unavailable("published anyway")        # something published -> returns
        _reset()

    def test_guard_is_inert_under_test_runners(self):
        self.assertFalse(nj._is_generator_process())
        _reset()
        nj._llm_failed("x")
        with patch("os._exit") as ex:
            nj._fail_loud_atexit()
        ex.assert_not_called()
        _reset()


class TestIntegration(unittest.TestCase):
    def test_resolved_absolute_path_is_what_runs(self):
        _reset()
        with patch.dict(sys.modules, {"nova_claude_code": _cc("/opt/homebrew/bin/claude")}), \
             patch("subprocess.Popen", return_value=_Proc(out="fine")) as p, _quiet():
            nj.call_openrouter("s", "u")
        self.assertEqual(p.call_args[0][0][0], "/opt/homebrew/bin/claude")
        _reset()

    def test_git_push_wrapper_counts_commits_and_writes_ledger(self):
        _reset()
        for status, counted in ((nj.PUSH_PUSHED, 1), (nj.PUSH_NOT_PUSHED, 1), (nj.PUSH_NOTHING, 0), (nj.PUSH_FAILED, 0)):
            _reset()
            wrapped = nj._records_push(lambda section, title, s=status: s)
            with patch.object(nj, "_record_publish") as rec:
                self.assertEqual(wrapped("operations", "T"), status)
            self.assertEqual(nj._RUN_STATE["published"], counted)
            rec.assert_called_once_with(f"push:{status}", nj._canon_section("operations"), None, "T")
        self.assertTrue(hasattr(nj.git_push, "__wrapped__"))       # the real git_push is wrapped
        _reset()

    def test_ledger_write_is_skipped_outside_generator_processes(self):
        with patch("psycopg2.connect") as c:
            nj._record_publish("written", "research", "content/research/x.md", "T")
        c.assert_not_called()


GEN = textwrap.dedent("""
    import sys
    sys.argv[0] = {argv0!r}
    sys.path.insert(0, {scripts!r})
    import nova_claude_code
    nova_claude_code.CLAUDE_KNOWN_PATHS = ()
    import nova_journal as nj
    nj.LOG_FILE = __import__("pathlib").Path({log!r})
    nj._record_publish = lambda *a, **k: None
    out = nj.call_openrouter("sys", "user")
    print("RESULT", out)
    {tail}
""")


class TestFunctional(unittest.TestCase):
    def _gen(self, tail="", env_extra=None):
        code = GEN.format(argv0=str(SCRIPTS / "nova_fake_generator.py"), scripts=str(SCRIPTS),
                          log=str(TMP / "gen.log"), tail=tail)
        env = {**os.environ, "PATH": "/usr/bin:/bin", "NOVA_CLAUDE_BIN": "", "NOVA_TEST_QUIET": "1"}
        env.update(env_extra or {})
        return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                              env=env, cwd=str(TMP))

    def test_generator_that_returns_0_after_cli_outage_exits_75(self):
        r = self._gen("print('generation failed'); sys.exit(0)")      # the 2026-10-18 journal_security shape
        self.assertIn("RESULT None", r.stdout)
        self.assertEqual(r.returncode, 75, r.stdout + r.stderr)
        self.assertIn("LLM UNAVAILABLE", r.stdout)

    def test_published_or_opted_out_runs_keep_their_exit_code(self):
        self.assertEqual(self._gen("nj.note_published()").returncode, 0)
        self.assertEqual(self._gen("", {"NOVA_LLM_FAIL_LOUD": "0"}).returncode, 0)
        self.assertEqual(self._gen("sys.exit(1)").returncode, 75)      # still non-zero, tagged as an outage


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_registers_the_guard(self):
        r = subprocess.run([sys.executable, "-c", "import nova_journal as nj; print(nj.EXIT_LLM_UNAVAILABLE)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=60,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "75")
        self.assertIn("_atexit.register(_fail_loud_atexit)", SRC)


if __name__ == "__main__":
    unittest.main()
