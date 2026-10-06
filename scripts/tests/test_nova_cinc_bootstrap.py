#!/usr/bin/env python3
"""Tests for nova_cinc_bootstrap.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
Every subprocess (psql, ssh, rsync --delete, curl|sudo bash, cinc-client) is mocked: nothing is installed,
pushed or converged, and the refusal paths (missing node JSON, unknown OS) are proven to stop early."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_cinc_bootstrap.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="cinc-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("cinc_bootstrap_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cb = _load()
# module-level stubs: no subprocess, no notification bus, node files in a tempdir
cb.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=OSError("offline: subprocess stubbed")))
cb.nova_notify = MagicMock()
cb.log = MagicMock()
cb.NODES_DIR = TMP


def _ok(stdout="", rc=0, stderr=""):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


class _Runner:
    """Answers each subprocess.run by its argv and records every call."""
    def __init__(self, converge_rc=0, os_out="Linux", installed=True):
        self.calls = []; self.converge_rc = converge_rc; self.os_out = os_out; self.installed = installed

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        joined = " ".join(argv)
        if argv[0] == "psql":
            return _ok()
        if "uname" in joined:
            return _ok(self.os_out) if self.os_out else _ok("", rc=255)
        if "which cinc-client" in joined or argv[:2] == ["which", "cinc-client"]:
            return _ok(rc=0 if self.installed else 1)
        if "cinc-client --local-mode" in joined:
            return _ok("Running handlers\n3 resources updated in 9 seconds", rc=self.converge_rc, stderr="boom")
        return _ok()


def _main(argv, runner):
    with patch.object(sys, "argv", ["nova_cinc_bootstrap.py"] + argv), patch.object(cb.subprocess, "run", runner):
        cb.main()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(cb.DB_DSN, r"://[^@/]+:[^@/]+@")

    def test_error_output_quotes_are_escaped(self):
        # SQL GAP: values are interpolated into psql -c strings (no bind params via psql -c); the one
        # free-text field that comes from a remote host (error_output) is quote-doubled and capped.
        r = _Runner()
        with patch.object(cb.subprocess, "run", r):
            cb.db_complete_run("abc", "failure", 1, error_output="x'; DROP TABLE t; --" + "y" * 5000)
        sql = r.calls[0][-1]
        self.assertIn("x''; DROP TABLE t", sql)
        self.assertLess(len(sql), 2400)

    def test_db_writes_are_argv_not_shell(self):
        self.assertNotIn("shell=True", SRC)
        r = _Runner()
        with patch.object(cb.subprocess, "run", r):
            cb.db_record_run("id", "n", "1.2.3.4", "converge")
        self.assertEqual(r.calls[0][:5], ["psql", "-U", "kochj", "-d", "nova_ops"])


class TestPerformance(unittest.TestCase):
    def test_converge_output_parse_on_10k_lines(self):
        out = "\n".join(["noise line"] * 10_000 + ["7 resources updated in 1 seconds"])
        t0 = time.perf_counter()
        with patch.object(cb.subprocess, "run", MagicMock(return_value=_ok(out))):
            rc, _, _, n = cb.run_converge("localhost", "mac-studio", TMP / "n.json")
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual((rc, n), (0, 7))


class TestRetry(unittest.TestCase):
    def test_db_record_failure_fails_open(self):
        # RETRY GAP: db_record_run / db_complete_run / db_upsert_node — one psql attempt, failure logged
        m = MagicMock(side_effect=OSError("psql missing"))
        with patch.object(cb.subprocess, "run", m):
            cb.db_record_run("i", "n", "ip", "converge")
            cb.db_complete_run("i", "failure", 1)
            cb.db_upsert_node("n", "ip", "linux", [], "failure")
        self.assertEqual(m.call_count, 3)

    def test_detect_os_ssh_failure_is_unknown(self):
        # RETRY GAP: detect_os — one ssh attempt; exceptions/timeouts yield "unknown"
        with patch.object(cb.subprocess, "run", MagicMock(side_effect=OSError("no route"))) as m:
            self.assertEqual(cb.detect_os("10.0.0.99"), "unknown")
        self.assertEqual(m.call_count, 1)
        with patch.object(cb.subprocess, "run", MagicMock(side_effect=OSError("x"))):
            self.assertFalse(cb.check_cinc_installed("10.0.0.99"))


class TestUnit(unittest.TestCase):
    def test_detect_os_parses(self):
        with patch.object(cb.subprocess, "run", MagicMock(return_value=_ok("Darwin"))):
            self.assertEqual(cb.detect_os("10.0.0.5"), "macos")
        with patch.object(cb.subprocess, "run", MagicMock(return_value=_ok("Linux"))):
            self.assertEqual(cb.detect_os("10.0.0.5"), "linux")

    def test_converge_why_run_flag_and_bad_count(self):
        r = MagicMock(return_value=_ok("abc resources updated"))
        with patch.object(cb.subprocess, "run", r):
            rc, _, _, n = cb.run_converge("10.0.0.5", "n", TMP / "n.json", why_run=True)
        self.assertEqual(n, 0)                                 # unparsable count -> 0, no crash
        self.assertTrue(r.call_args[0][0][-1].endswith("--why-run"))
        self.assertIn("sudo cinc-client", r.call_args[0][0][-1])

    def test_push_cookbooks_local_is_noop(self):
        r = MagicMock()
        with patch.object(cb.subprocess, "run", r):
            self.assertTrue(cb.push_cookbooks("localhost", TMP / "n.json"))
        r.assert_not_called()

    def test_notify_strips_emoji_codes(self):
        cb.nova_notify.reset_mock()
        cb.notify(":x: *Failed* — n\nbody", level="critical")
        title = cb.nova_notify.call_args[0][0]
        self.assertFalse(title.startswith(":"))
        self.assertEqual(cb.nova_notify.call_args[1]["body"], "body")


class TestIntegration(unittest.TestCase):
    def test_remote_push_rsyncs_cookbooks_then_scps_configs(self):
        r = _Runner()
        with patch.object(cb.subprocess, "run", r):
            self.assertTrue(cb.push_cookbooks("10.0.0.5", TMP / "n.json"))
        tools = [c[0] for c in r.calls]
        self.assertEqual(tools, ["ssh", "rsync", "scp", "scp"])
        self.assertTrue(r.calls[1][-1].endswith(f"{cb.REMOTE_COOKBOOKS}/"))

    def test_uses_shared_notification_bus(self):
        self.assertIn("from nova_notify import notify as nova_notify", SRC)
        self.assertIn("cinc_node_configs", SRC)
        self.assertIn("deployment_runs", SRC)


class TestFunctional(unittest.TestCase):
    def test_converge_golden_path_records_and_notifies(self):
        (TMP / "10-0-0-5.json").write_text("{}")
        cb.nova_notify.reset_mock()
        r = _Runner()
        _main(["--converge", "10.0.0.5"], r)
        sqls = [c[-1] for c in r.calls if c[0] == "psql"]
        self.assertTrue(sqls[0].startswith("INSERT INTO deployment_runs"))
        self.assertIn("status = 'success'", sqls[1])
        self.assertIn("resources_updated = 3", sqls[1])
        self.assertIn("cinc_node_configs", sqls[2])
        self.assertFalse(any("install.sh" in " ".join(c) for c in r.calls))   # --converge never installs
        self.assertEqual(cb.nova_notify.call_args[1]["level"], "info")

    def test_converge_failure_notifies_critical(self):
        (TMP / "10-0-0-6.json").write_text("{}")
        cb.nova_notify.reset_mock()
        _main(["--converge", "10.0.0.6"], _Runner(converge_rc=1))
        self.assertEqual(cb.nova_notify.call_args[1]["level"], "critical")

    def test_missing_node_json_refuses_before_any_command(self):
        r = _Runner()
        _main(["10.9.9.9"], r)
        self.assertEqual(r.calls, [])

    def test_unknown_os_aborts_before_install_or_push(self):
        (TMP / "10-0-0-7.json").write_text("{}")
        r = _Runner(os_out=None)
        _main(["10.0.0.7"], r)
        tools = [c[0] for c in r.calls]
        self.assertNotIn("rsync", tools)
        self.assertFalse(any("install.sh" in " ".join(c) for c in r.calls))
        self.assertIn("status = 'failure'", r.calls[-1][-1])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--why-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(cb.main))


if __name__ == "__main__":
    unittest.main()
