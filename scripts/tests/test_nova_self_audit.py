#!/usr/bin/env python3
"""Tests for nova_self_audit.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Since 2026-10-09 (organ audit M14) self audit is a thin wrapper over `nova_reconciler.py --self-audit`;
the behaviour tests for the checks themselves live in tests/test_nova_reconciler.py."""
import atexit
import importlib.util
import io
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_self_audit.py"
SRC = SCRIPT.read_text()
CODE = SRC.split('"""', 2)[2]   # source minus the module docstring

_TMP = tempfile.TemporaryDirectory()
atexit.register(_TMP.cleanup)
HOME = Path(_TMP.name)


def _load():
    spec = importlib.util.spec_from_file_location("sa_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sa = _load()
R = sa.R                       # the real nova_reconciler module the wrapper delegates to


def _quiet_log():
    """Never let the wrapper's merge line reach ~/.openclaw/logs."""
    lg = logging.getLogger("nova_self_audit")
    for h in list(lg.handlers):
        lg.removeHandler(h)
        h.close()
    lg.addHandler(logging.NullHandler())
    return patch.object(R, "_AUDIT_LOGGER", lg)


class TestSecurity(unittest.TestCase):
    def test_no_credentials_no_shell_no_direct_posting(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        for banned in ("shell=True", "os.system", "urlopen", "post_both", "subprocess"):
            self.assertNotIn(banned, CODE, banned)


class TestPerformance(unittest.TestCase):
    def test_wrapper_overhead_is_negligible(self):
        with _quiet_log(), patch.object(R, "run_audit", return_value=0):
            t = time.monotonic()
            for _ in range(10_000):
                sa.run_audit()
            self.assertLess(time.monotonic() - t, 2.0)


class TestRetry(unittest.TestCase):
    def test_probes_fail_open_through_the_wrapper(self):
        # RETRY GAP: the reconciler's _port_listening/_process_running make one attempt each; down reads as an issue
        with _quiet_log(), patch.object(R, "_port_listening", return_value=False), \
             patch.object(R, "_process_running", return_value=False), \
             patch.object(R, "audit_scripts", return_value=([], [], 0, 0, 0)), \
             patch.object(R, "AUDIT_STATE_FILE", HOME / "state.json"), \
             patch.object(R, "self_audit_post", Mock()) as post, redirect_stdout(io.StringIO()):
            n = R.run_audit(post=post)
        self.assertEqual(n, len(R.EXPECTED_SERVICES) + len(R.EXPECTED_PROCESSES))
        post.assert_called_once()


class TestUnit(unittest.TestCase):
    def test_reexports_point_at_the_reconciler(self):
        self.assertIs(sa.audit_scripts, R.audit_scripts)
        self.assertIs(sa.audit_services, R.audit_services)
        self.assertIs(sa.audit_processes, R.audit_processes)
        self.assertIs(sa.audit_docs, R.audit_docs)
        self.assertIs(sa.slack_post, R.self_audit_post)
        self.assertIs(sa.EXPECTED_SERVICES, R.EXPECTED_SERVICES)
        self.assertEqual(sa.MERGED, "2026-10-09")


class TestIntegration(unittest.TestCase):
    def test_imports_the_reconciler_not_a_copy(self):
        self.assertIn("import nova_reconciler as R", SRC)
        for gone in ("def _port_listening", "def _scripts_in_scheduler", "def audit_services", "logging.basicConfig"):
            self.assertNotIn(gone, SRC, gone)


class TestFunctional(unittest.TestCase):
    def test_run_audit_logs_the_merge_and_delegates(self):
        lg = logging.getLogger("nova_self_audit_test_probe")
        lg.propagate = False
        with self.assertLogs(lg, level="INFO") as cm, patch.object(R, "_AUDIT_LOGGER", lg), \
             patch.object(R, "run_audit", return_value=3) as ra:
            self.assertEqual(sa.run_audit(), 3)
        ra.assert_called_once_with()
        self.assertTrue(any("merged into nova_reconciler --self-audit on 2026-10-09" in m for m in cm.output))

    def test_error_path_propagates(self):
        with _quiet_log(), patch.object(R, "run_audit", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                sa.run_audit()


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_main_is_guarded(self):
        # no --help; running the script probes the fleet, so the frame check is the import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertIn("run_audit()\n    sys.exit(0)", SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_self_audit"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
