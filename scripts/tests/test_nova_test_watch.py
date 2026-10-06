#!/usr/bin/env python3
"""Tests for nova_test_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
SCRIPT = SCRIPTS / "nova_test_watch.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_test_watch_test_"))


def _load():
    spec = importlib.util.spec_from_file_location("ntestwatch", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tw = _load()


def _notify_stub():
    m = types.ModuleType("nova_notify"); m.notify = MagicMock(return_value=True)
    return m


def _proc(out, rc=0):
    return MagicMock(stdout=out, stderr="", returncode=rc)


def _tree():
    d = Path(tempfile.mkdtemp(dir=TMP))
    (d / "sub").mkdir()
    for n in ("test_a.py", "test_b.py", "sub/test_c.py", "helper.py", "conftest.py"):
        (d / n).write_text("")
    return d


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)

    def test_pytest_invoked_as_argv_with_timeout(self):
        weird = TMP / "test_x; touch pwned.py"
        with patch.object(tw.subprocess, "run", return_value=_proc("1 passed in 0.1s")) as run:
            tw.run_one(weird)
        cmd, kw = run.call_args[0][0], run.call_args.kwargs
        self.assertEqual(cmd[:3], ["python3", "-m", "pytest"])
        self.assertIn(str(weird), cmd)
        self.assertEqual(kw["timeout"], 600)

    def test_no_slack_never_imports_notify(self):
        stub = _notify_stub()
        with patch.dict(sys.modules, {"nova_notify": stub}):
            tw._emit("t", "b", "info", no_slack=True)
        stub.notify.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_discover_dedups_large_list(self):
        d = _tree()
        files = [str(d / "test_a.py")] * 10_000
        t0 = time.perf_counter()
        out = tw.discover(files)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(out, [d / "test_a.py"])


class TestRetry(unittest.TestCase):
    def test_emit_fails_open(self):
        # RETRY GAP: _emit/notify — one attempt per event; a dead bus never breaks the test run
        stub = _notify_stub(); stub.notify.side_effect = RuntimeError("bus down")
        with patch.dict(sys.modules, {"nova_notify": stub}):
            self.assertIsNone(tw._emit("t", None, "info", no_slack=False))
        self.assertEqual(stub.notify.call_count, 1)

    def test_run_one_is_single_shot(self):
        # RETRY GAP: run_one — a failing file is run once and reported, never re-run
        with patch.object(tw.subprocess, "run", return_value=_proc("FAILED test_a.py::x - assert 1 == 2\n1 failed in 0.2s", 1)) as run:
            p, f, s, _, tail = tw.run_one(Path("test_a.py"))
        self.assertEqual(run.call_count, 1)
        self.assertEqual((p, f), (0, 1))
        self.assertIn("assert 1 == 2", tail)


class TestUnit(unittest.TestCase):
    def test_summary_parsing(self):
        cases = {"3 passed, 1 failed, 2 skipped in 0.4s": (3, 1, 2), "5 passed, 2 errors in 1s": (5, 2, 0),
                 "": (0, 1, 0)}
        for out, want in cases.items():
            with patch.object(tw.subprocess, "run", return_value=_proc(out, 0 if out.startswith("3") else 1)):
                p, f, s, _, _ = tw.run_one(Path("t.py"))
            self.assertEqual((p, f, s), want, out)

    def test_discover_files_and_dirs(self):
        d = _tree()
        self.assertEqual([p.name for p in tw.discover([str(d)])], ["test_c.py", "test_a.py", "test_b.py"])   # sorted paths: sub/ first
        self.assertEqual(tw.discover([str(d / "helper.py")]), [])


class TestIntegration(unittest.TestCase):
    def test_emit_routes_to_tests_category(self):
        stub = _notify_stub()
        with patch.dict(sys.modules, {"nova_notify": stub}):
            tw._emit("title", "body", "warning", no_slack=False)
        kw = stub.notify.call_args.kwargs
        self.assertEqual((kw["category"], kw["source"], kw["level"]), ("tests", "nova_test_watch", "warning"))


class TestFunctional(unittest.TestCase):
    def _main(self, argv, results):
        stub = _notify_stub()
        with patch.dict(sys.modules, {"nova_notify": stub}), patch.object(sys, "argv", ["x", *argv]), \
             patch.object(tw, "run_one", side_effect=results), redirect_stdout(io.StringIO()) as out:
            rc = tw.main()
        return rc, stub.notify, out.getvalue()

    def test_mixed_run_reports_and_exits_1(self):
        d = _tree()
        rc, notify, out = self._main([str(d)], [(3, 0, 0, 0.1, ""), (1, 1, 0, 0.1, "FAILED x"), (2, 0, 1, 0.1, "")])
        self.assertEqual(rc, 1)
        self.assertIn("=== FAIL — 3 files · 6 passed · 1 failed · 1 skipped", out)
        titles = [c[0][0] for c in notify.call_args_list]
        self.assertEqual(titles[0], "Test run starting — 3 files")
        self.assertIn("❌ test_a.py — 1 failed", titles)          # discovery order: sub/test_c, test_a, test_b
        self.assertEqual(titles[-1], "Test run complete — FAIL")

    def test_quiet_emits_only_failures_and_summary(self):
        d = _tree()
        rc, notify, _ = self._main([str(d), "--quiet"], [(1, 0, 0, 0.1, "")] * 3)
        self.assertEqual(rc, 0)
        self.assertEqual([c[0][0] for c in notify.call_args_list],
                         ["Test run starting — 3 files", "Test run complete — PASS"])

    def test_no_files(self):
        rc, notify, out = self._main([str(TMP / "nothing_here")], [])
        self.assertEqual(rc, 0)
        notify.assert_not_called()
        self.assertIn("No test files found.", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--no-slack", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch("subprocess.run", side_effect=AssertionError("import must not run pytest")):
            _load()


if __name__ == "__main__":
    unittest.main()
