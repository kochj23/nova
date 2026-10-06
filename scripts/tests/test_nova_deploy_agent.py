#!/usr/bin/env python3
"""Tests for nova_deploy_agent.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2          # noqa: F401
import psycopg2.extras   # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_deploy_agent.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_deploy_test_"))


def _load():
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    spec = importlib.util.spec_from_file_location("ndeploy", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_notify": nn}), patch("pathlib.Path.mkdir"), \
         patch("psycopg2.connect", side_effect=RuntimeError("offline")), \
         patch("subprocess.run", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    mod.BACKUP_DIR = TMP / "backups"; mod.BACKUP_DIR.mkdir(exist_ok=True)
    mod.notify = MagicMock(return_value=True)
    mod._annotate_grafana = MagicMock(); mod._trigger_security_scan = MagicMock()
    mod._update_security_baseline = MagicMock(); mod._observe = MagicMock()
    return mod


da = _load()


class _Rec:
    """Records every update_status call instead of touching PG."""
    def __init__(self): self.calls = []
    def __call__(self, did, status, **kw): self.calls.append((status, kw))


def _deploy(**kw):
    return {"id": 42, "target_service": "net.digitalnoise.test", "action": "restart", "payload": {}, **kw}


def _run_deploy(d, run_rc=0, health=True):
    rec = _Rec()
    with patch.object(da, "update_status", rec), patch.object(da.time, "sleep"), \
         patch.object(da.subprocess, "run", return_value=MagicMock(returncode=run_rc)) as run, \
         patch.object(da, "_check_health", return_value=health), redirect_stdout(io.StringIO()):
        da.execute_deploy(d)
    return rec.calls, run


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('"nova-grafana-password"', SRC)

    def test_update_status_values_are_parameterized(self):
        cur = MagicMock(); conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(da, "_conn", return_value=conn):
            da.update_status(1, "failed", error="x'; DROP TABLE deploy_requests;--")
        sql, params = cur.execute.call_args[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params, ["failed", "x'; DROP TABLE deploy_requests;--", 1])

    def test_unknown_action_never_runs_anything(self):
        calls, run = _run_deploy(_deploy(action="rm_everything"))
        run.assert_not_called()
        self.assertEqual(calls[-1][0], "failed")

    def test_launchctl_is_argv(self):
        calls, run = _run_deploy(_deploy())
        cmd = run.call_args_list[0][0][0]
        self.assertEqual(cmd[:3], ["launchctl", "kickstart", "-k"])
        self.assertTrue(cmd[3].endswith("/net.digitalnoise.test"))


class TestPerformance(unittest.TestCase):
    def test_10k_status_updates_build_fast(self):
        conn = MagicMock(); conn.cursor.return_value = MagicMock()
        t0 = time.perf_counter()
        with patch.object(da, "_conn", return_value=conn):
            for i in range(10_000):
                da.update_status(i, "success", health_check_status="healthy")
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_failed_health_check_rolls_back_once(self):
        # RETRY GAP: execute_deploy — a failed health check is not retried; the rollback line runs once
        calls, run = _run_deploy(_deploy(health_check_url="http://127.0.0.1:1/health", rollback_action="echo undo"), health=False)
        rb = [c for c in run.call_args_list if c[0][0][:2] == ["/bin/sh", "-c"]]
        self.assertEqual(len(rb), 1)
        self.assertEqual(calls[-1][0], "rolled_back")
        self.assertIn("Health check failed", calls[-1][1]["error"])

    def test_check_health_fails_open(self):
        fresh = _load()
        with patch("urllib.request.urlopen", side_effect=OSError("down")) as uo:
            self.assertFalse(fresh._check_health("http://127.0.0.1:1/h"))
        self.assertEqual(uo.call_count, 1)

    def test_run_loop_poll_error_survives(self):
        with patch.object(da, "get_pending", side_effect=[RuntimeError("pg down"), KeyboardInterrupt()]), \
             patch.object(da.time, "sleep"), redirect_stdout(io.StringIO()) as b:
            with self.assertRaises(KeyboardInterrupt):
                da.run_loop()
        self.assertIn("Poll error: pg down", b.getvalue())


class TestUnit(unittest.TestCase):
    def test_notify_slack_splits_title_body(self):
        da.notify.reset_mock()
        da.notify_slack("Title\nline2", level="critical")
        args, kw = da.notify.call_args
        self.assertEqual((args[0], kw["body"], kw["level"], kw["category"]), ("Title", "line2", "critical", "deploy"))

    def test_deploy_script_requires_payload(self):
        with self.assertRaises(ValueError):
            da._deploy_script("svc", {})
        with self.assertRaises(FileNotFoundError):
            da._deploy_script("svc", {"script_path": str(TMP / "nope.py"), "script_content": "x"})

    def test_grafana_auth_missing_keychain(self):
        with patch.object(da.subprocess, "check_output", side_effect=subprocess.CalledProcessError(44, "security")):
            self.assertIsNone(da._get_grafana_auth())


class TestIntegration(unittest.TestCase):
    def test_deploy_script_backs_up_then_writes_then_restarts(self):
        target = TMP / "svc_script.py"; target.write_text("old")
        with patch.object(da, "_restart_service") as rs, redirect_stdout(io.StringIO()):
            da._deploy_script("svc", {"script_path": str(target), "script_content": "new", "launchd_label": "lbl"})
        self.assertEqual(target.read_text(), "new")
        self.assertTrue(any(p.read_text() == "old" for p in da.BACKUP_DIR.glob("svc_script_*.py")))
        rs.assert_called_once_with("lbl")

    def test_pending_reads_deploy_requests(self):
        cur = MagicMock(); cur.fetchall.return_value = [{"id": 1}]
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(da, "_conn", return_value=conn):
            self.assertEqual(da.get_pending(), [{"id": 1}])
        self.assertIn("FROM deploy_requests", cur.execute.call_args[0][0])
        self.assertIn("status = 'pending'", cur.execute.call_args[0][0])


class TestFunctional(unittest.TestCase):
    def test_restart_with_health_check_success(self):
        da.notify.reset_mock()
        calls, _ = _run_deploy(_deploy(health_check_url="http://127.0.0.1:1/health"))
        self.assertEqual([c[0] for c in calls], ["in_progress", "success"])
        self.assertEqual(calls[-1][1], {"health_check_status": "healthy"})
        self.assertEqual(da.notify.call_args.kwargs["level"], "info")

    def test_failure_without_rollback_is_critical(self):
        da.notify.reset_mock()
        rec = _Rec()
        with patch.object(da, "update_status", rec), patch.object(da.time, "sleep"), \
             patch.object(da.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "launchctl")), \
             redirect_stdout(io.StringIO()):
            da.execute_deploy(_deploy())
        self.assertEqual(rec.calls[-1][0], "failed")
        self.assertEqual(da.notify.call_args.kwargs["level"], "critical")


class TestFrame(unittest.TestCase):
    def test_import_smoke_in_sandboxed_home(self):
        # The __main__ block binds :37471 and loops forever, so the frame check is an import in a throwaway HOME.
        home = tempfile.mkdtemp(prefix="deploy_home_")
        code = "import sys; sys.path.insert(0, sys.argv[1]); import nova_deploy_agent as m; print(m.PORT)"
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "37471")

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertLess(SRC.index("HTTPServer((\"127.0.0.1\", PORT)"), len(SRC))
        self.assertGreater(SRC.index("HTTPServer((\"127.0.0.1\", PORT)"), SRC.index('if __name__ == "__main__":'))


if __name__ == "__main__":
    unittest.main()
