#!/usr/bin/env python3
"""7-category gap tests for nova_backup_monitor.py — the 2026-10-06 healthy digest (files, size,
duration, errors per job) and the notify / PG retries added here. PG and nova_notify are mocked.
Base suite: test_nova_backup_monitor.py. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_backup_monitor_7cat.py
"""
import importlib.util
import io
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_backup_monitor.py"


def _load():
    spec = importlib.util.spec_from_file_location("backup_monitor_7cat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    stub = types.ModuleType("nova_notify"); stub.notify = MagicMock()
    with patch.dict(sys.modules, {"nova_notify": stub}):
        spec.loader.exec_module(mod)
    return mod


bm = _load()


class Cur:
    def __init__(self, age_h=1.0, latest=(0, True, "nova-backup:nas:incremental", 3725, 1234, 5_000_000_000, 2)):
        self.age_h, self.latest, self._r = age_h, latest, None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "max(ts)" in sql:
            self._r = (datetime.now(timezone.utc) - timedelta(hours=self.age_h),)
        else:
            self._r = self.latest

    def fetchone(self):
        return self._r


def conn_for(cur):
    c = MagicMock(); c.cursor.return_value = cur
    return c


class _Quiet(unittest.TestCase):
    def setUp(self):
        r = redirect_stdout(io.StringIO()); self.out = r.__enter__(); self.addCleanup(r.__exit__, None, None, None)
        p = patch.object(bm.time, "sleep"); self.sleep = p.start(); self.addCleanup(p.stop)
        p2 = patch.object(bm.nova_notify, "notify"); self.notify = p2.start(); self.addCleanup(p2.stop)


class TestSecurity(_Quiet):
    def test_job_prefix_bound_not_interpolated(self):
        cur = MagicMock(); cur.__enter__.return_value = cur
        cur.fetchone.side_effect = [(None,), (None,)]
        with patch.object(bm.psycopg2, "connect", return_value=conn_for(cur)):
            bm.check()
        for call in cur.execute.call_args_list:
            self.assertIn("%s", call.args[0])
            self.assertTrue(call.args[1][0].endswith("%"))

    def test_digest_carries_no_paths_or_hosts(self):
        line = bm._describe("nova-backup:nas", 1.0, "nova-backup:nas:incremental", 60, 1, 1, 0)
        self.assertNotRegex(line, r"/Volumes|/Users|\d+\.\d+\.\d+\.\d+")


class TestPerformance(_Quiet):
    def test_connect_timeout_and_bounded_backoff(self):
        with patch.object(bm.psycopg2, "connect", side_effect=OSError("down")) as c, self.assertRaises(OSError):
            bm.check()
        self.assertEqual(c.call_args.kwargs["connect_timeout"], 10)
        self.assertLessEqual(sum(a.args[0] for a in self.sleep.call_args_list), 15)

    def test_notify_backoff_bounded(self):
        self.notify.side_effect = RuntimeError("bus")
        bm.emit("t", "b", "warning", "k")
        self.assertLessEqual(sum(a.args[0] for a in self.sleep.call_args_list), 10)


class TestRetry(_Quiet):
    def test_pg_blip_recovers(self):
        with patch.object(bm.psycopg2, "connect", side_effect=[OSError("failover"), conn_for(Cur())]):
            self.assertEqual(bm.check(), [])
        self.sleep.assert_called_once_with(5)

    def test_notify_blip_recovers_without_stdout_fallback(self):
        self.notify.side_effect = [RuntimeError("bus"), None]
        bm.emit("Backup stale/failed: nas", "late", "critical", "backup-stale:nas")
        self.assertEqual(self.notify.call_count, 2)
        self.assertEqual(self.out.getvalue(), "")

    def test_stale_alert_survives_bus_outage(self):
        self.notify.side_effect = RuntimeError("bus down")
        with patch.object(bm.psycopg2, "connect", return_value=conn_for(Cur(age_h=100))):
            issues = bm.check()
        self.assertEqual(len(issues), len(bm.JOBS))
        self.assertIn("[critical] Backup stale/failed", self.out.getvalue())


class TestUnit(_Quiet):
    def test_describe_formats_hours_and_size(self):
        line = bm._describe("nova-backup:nas", 11.84, "nova-backup:nas:full", 3725, 1234, 5_000_000_000, 2)
        self.assertEqual(line, "nas (full): 11.8h ago · 1,234 files, 5,000.0 MB in 1h02m · 2 errors")

    def test_describe_tolerates_nulls(self):
        self.assertEqual(bm._describe("nova-backup:x", 0.5, "nova-backup:x:incremental", None, None, None, None),
                         "x (incremental): 0.5h ago · 0 files, 0.0 MB in 0m00s · 0 errors")


class TestIntegration(_Quiet):
    def test_healthy_digest_one_line_per_job(self):
        with patch.object(bm.psycopg2, "connect", return_value=conn_for(Cur())):
            bm.check()
        kw = self.notify.call_args.kwargs
        self.assertEqual((kw["title"], kw["level"], kw["dedup_key"]), ("Backups healthy", "info", "backup-digest"))
        self.assertEqual(len(kw["body"].splitlines()), len(bm.JOBS))


class TestFunctional(_Quiet):
    def test_failed_latest_run_reported_not_digested(self):
        cur = Cur(latest=(23, False, "nova-backup:nas:incremental", 10, 0, 0, 5))
        with patch.object(bm.psycopg2, "connect", return_value=conn_for(cur)):
            issues = bm.check()
        self.assertTrue(all("FAILED (rc=23)" in i[1] for i in issues))
        self.assertNotIn("Backups healthy", [c.kwargs["title"] for c in self.notify.call_args_list])


class TestFrame(unittest.TestCase):
    def test_compiles_and_main_guard(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('if __name__ == "__main__":', SCRIPT.read_text())


if __name__ == "__main__":
    unittest.main()
