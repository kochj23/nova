#!/usr/bin/env python3
"""Tests for nova_voldata_fast.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import builtins
import os
import re
import runpy
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_voldata_fast.py"
SRC = SCRIPT.read_text()
REAL_LOG = "/tmp/voldata-fast.log"


def _run(du_results=None, exc=None):
    """Execute the one-shot script with `du` mocked and its log redirected to a tempdir.
    Returns (log text, list of argv lists passed to subprocess.run)."""
    calls = []
    tmp = tempfile.mkdtemp()
    log = os.path.join(tmp, "voldata-fast.log")
    real_open = builtins.open

    def fake_run(argv, **kw):
        calls.append(argv)
        if exc:
            raise exc
        return (du_results or {}).get(argv[-1], MagicMock(returncode=0, stdout="1.0G\t/x\n", stderr=""))

    def redirected_open(path, *a, **k):
        if path == REAL_LOG:
            path = log
        return real_open(path, *a, **k)

    with patch("subprocess.run", fake_run), patch.object(builtins, "open", redirected_open):
        runpy.run_path(str(SCRIPT), run_name="voldata_test")
    return Path(log).read_text(), calls


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_du_is_invoked_by_absolute_path_without_a_shell(self):
        _, calls = _run()
        for argv in calls:
            self.assertEqual(argv[0], "/usr/bin/du")
        self.assertNotIn("shell=True", SRC)
        self.assertNotIn("os.system", SRC)

    def test_only_the_two_data_volumes_are_scanned(self):
        _, calls = _run()
        self.assertEqual([a[-1] for a in calls], ["/Volumes/Data", "/Volumes/MoreData"])
        self.assertNotIn("/Volumes/NAS", SRC)                                     # never the slow NAS

    def test_stderr_is_truncated_before_logging(self):
        out, _ = _run({"/Volumes/Data": MagicMock(returncode=1, stdout="", stderr="e" * 5000)})
        line = next(l for l in out.splitlines() if l.startswith("stderr: "))
        self.assertEqual(len(line), len("stderr: ") + 300)


class TestPerformance(unittest.TestCase):
    def test_log_assembly_fast_with_10k_line_du_output(self):
        big = MagicMock(returncode=0, stdout="".join(f"{i}G\t/Volumes/Data/d{i}\n" for i in range(10_000)), stderr="")
        t0 = time.perf_counter()
        out, _ = _run({"/Volumes/Data": big, "/Volumes/MoreData": big})
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(out.count("/Volumes/Data/d9999"), 2)

    def test_du_is_bounded_by_a_timeout_and_depth_one(self):
        self.assertIn("timeout=900", SRC)
        self.assertIn('"-d", "1"', SRC)


class TestRetry(unittest.TestCase):
    def test_du_failure_fails_open_and_the_other_volume_still_runs(self):
        # RETRY GAP: subprocess.run(du) — one attempt per volume; an exception is logged inline and the sweep continues
        out, calls = _run(exc=subprocess.TimeoutExpired(cmd="du", timeout=900))
        self.assertEqual(len(calls), 2)
        self.assertEqual(out.count("ERROR:"), 2)
        self.assertTrue(out.rstrip().endswith("FAST DONE"))

    def test_nonzero_rc_is_recorded_not_raised(self):
        out, _ = _run({"/Volumes/MoreData": MagicMock(returncode=2, stdout="", stderr="Permission denied")})
        self.assertIn("### /Volumes/MoreData (rc=2) ###", out)
        self.assertIn("stderr: Permission denied", out)


class TestUnit(unittest.TestCase):
    def test_log_is_sectioned_per_volume_with_trailing_newline(self):
        out, _ = _run()
        self.assertIn("### /Volumes/Data (rc=0) ###\n1.0G\t/x\n", out)
        self.assertIn("### /Volumes/MoreData (rc=0) ###", out)
        self.assertTrue(out.endswith("FAST DONE\n"))

    def test_empty_stderr_adds_no_stderr_line(self):
        out, _ = _run()
        self.assertNotIn("stderr:", out)


class TestIntegration(unittest.TestCase):
    def test_du_flags_match_the_audit_sibling_contract(self):
        _, calls = _run()
        self.assertEqual(calls[0][:4], ["/usr/bin/du", "-h", "-d", "1"])
        audit = (SCRIPTS / "nova_voldata_audit.py").read_text()
        self.assertIn("/Volumes/MoreData", audit)                                  # the slow audit covers the same volumes

    def test_log_write_happens_once_after_both_volumes(self):
        writes = []
        real_open = builtins.open

        def spy(path, *a, **k):
            if path == REAL_LOG:
                writes.append(a)
                return real_open(os.devnull, "w")
            return real_open(path, *a, **k)
        with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")), patch.object(builtins, "open", spy):
            runpy.run_path(str(SCRIPT), run_name="voldata_test")
        self.assertEqual(writes, [("w",)])


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_both_sections_and_done_marker(self):
        out, calls = _run({"/Volumes/Data": MagicMock(returncode=0, stdout="900G\t/Volumes/Data/xcode\n", stderr="")})
        self.assertEqual(len(calls), 2)
        self.assertIn("900G\t/Volumes/Data/xcode", out)
        self.assertEqual(out.count("###"), 4)
        self.assertIn("FAST DONE", out)

    def test_error_path_missing_volume_is_reported_inline(self):
        out, _ = _run(exc=FileNotFoundError("/Volumes/Data"))
        self.assertIn("### /Volumes/Data ERROR: /Volumes/Data", out)
        self.assertIn("FAST DONE", out)


class TestFrame(unittest.TestCase):
    def test_script_runs_end_to_end_in_a_subprocess_with_du_faked(self):
        # The script is a launchd one-shot with no __main__ guard by design (running == importing), so the
        # smoke run fakes du and redirects /tmp/voldata-fast.log before executing it.
        tmp = tempfile.mkdtemp()
        driver = textwrap.dedent(f"""
            import builtins, runpy, subprocess, types
            subprocess.run = lambda argv, **k: types.SimpleNamespace(returncode=0, stdout="1G\\t/x\\n", stderr="")
            _o = builtins.open
            builtins.open = lambda p, *a, **k: _o({tmp!r} + "/log" if p == "/tmp/voldata-fast.log" else p, *a, **k)
            runpy.run_path({str(SCRIPT)!r}, run_name="__main__")
        """)
        r = subprocess.run([sys.executable, "-c", driver], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertIn("FAST DONE", Path(tmp, "log").read_text())
        self.assertNotIn("__main__", SRC)


if __name__ == "__main__":
    unittest.main()
