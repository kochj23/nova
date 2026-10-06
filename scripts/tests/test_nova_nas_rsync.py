#!/usr/bin/env python3
"""Tests for nova_nas_rsync.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Every rsync / ssh / mount / Keychain call is mocked; a failed preflight is proven to transfer nothing."""
import importlib.util
import io
import json
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


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nr = _load("nova_nas_rsync_t", SCRIPTS / "nova_nas_rsync.py")
nr.notify = MagicMock()                                    # notification bus stubbed at load
nr.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=RuntimeError("unmocked subprocess")),
                                      TimeoutExpired=subprocess.TimeoutExpired)
SRC = (SCRIPTS / "nova_nas_rsync.py").read_text()
_TMP = Path(tempfile.mkdtemp())
FAKE_CFG = types.SimpleNamespace(notify_local=MagicMock())

STATS = ("Number of regular files transferred: 1,234\n"
         "Total transferred file size: 5,000,000,000 bytes\n")


def _cp(rc=0, out="", err=""):
    return types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _q():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token|pw)\s*=\s*['\"][A-Za-z0-9+/]{8,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn("BatchMode=yes", nr.SSH_OPTS)

    def test_replication_is_additive_only(self):
        self.assertNotIn("--delete", SRC)
        with patch.object(nr.subprocess, "run", return_value=_cp(0, STATS)) as run, _q():
            nr.rsync_share(dict(nr.SHARES[0], dest=str(_TMP)))
        self.assertFalse(any(a.startswith("--delete") for a in run.call_args[0][0]))

    def test_mount_password_from_keychain_and_url_encoded(self):
        calls = []

        def fake(cmd, **k):
            calls.append(cmd)
            return _cp(0, "p@ss/w:rd\n") if cmd[0] == "security" else _cp()
        with patch.object(nr.subprocess, "run", side_effect=fake), \
                patch.object(nr.os.path, "ismount", return_value=False), \
                patch.object(nr.os, "makedirs"), _q():
            self.assertFalse(nr.ensure_mounted(dict(nr.SHARES[0])))
        self.assertEqual(calls[0][:2], ["security", "find-internet-password"])
        mount_url = calls[1][1]
        self.assertIn("p%40ss%2Fw%3Ard@", mount_url)
        self.assertNotIn("p@ss/w:rd", mount_url)


class TestPerformance(unittest.TestCase):
    def test_stats_parse_on_10k_line_output(self):
        out = "".join(f"file{i}.mkv\n" for i in range(10_000)) + STATS
        t0 = time.perf_counter()
        with patch.object(nr.subprocess, "run", return_value=_cp(0, out)), _q():
            r = nr.rsync_share(dict(nr.SHARES[0], dest=str(_TMP)))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual((r["files_transferred"], r["bytes_transferred"]), (1234, 5_000_000_000))


class TestRetry(unittest.TestCase):
    def test_mount_self_heal_falls_back_to_osascript(self):
        state = {"mounted": False}

        def fake(cmd, **k):
            if cmd[0] == "security":
                return _cp(0, "pw\n")
            if cmd[0] == "/usr/bin/osascript":
                state["mounted"] = True
            return _cp()
        run = MagicMock(side_effect=fake)
        with patch.object(nr.subprocess, "run", run), \
                patch.object(nr.os.path, "ismount", side_effect=lambda p: state["mounted"]), \
                patch.object(nr.os, "makedirs"), _q():
            self.assertTrue(nr.ensure_mounted(dict(nr.SHARES[0])))
        self.assertEqual([c[0][0][0] for c in run.call_args_list], ["security", "mount_smbfs", "/usr/bin/osascript"])

    def test_rsync_timeout_fails_open(self):
        # RETRY GAP: rsync_share — one rsync attempt; the nightly run is the retry
        with patch.object(nr.subprocess, "run", side_effect=subprocess.TimeoutExpired("rsync", 1)), _q():
            r = nr.rsync_share(dict(nr.SHARES[0], dest=str(_TMP)))
        self.assertEqual((r["status"], r["error"]), ("error", "rsync timed out (4h)"))

    def test_no_keychain_credential_no_mount_attempt(self):
        with patch.object(nr.subprocess, "run", return_value=_cp(0, "")) as run, \
                patch.object(nr.os.path, "ismount", return_value=False), _q():
            self.assertFalse(nr.ensure_mounted(dict(nr.SHARES[0])))
        self.assertEqual(run.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_missing_destination(self):
        r = nr.rsync_share(dict(nr.SHARES[0], dest=str(_TMP / "nope")))
        self.assertEqual(r["status"], "error")
        self.assertIn("not mounted", r["error"])

    def test_nonzero_rsync_is_warning(self):
        with patch.object(nr.subprocess, "run", return_value=_cp(23, "garbage")), _q():
            r = nr.rsync_share(dict(nr.SHARES[0], dest=str(_TMP)))
        self.assertEqual((r["status"], r["files_transferred"], r["exit_code"]), ("warning", 0, 23))

    def test_preflight_flags_full_disk_and_bad_ssh(self):
        def fake(cmd, **k):
            if cmd[0] == "df":
                return _cp(0, "Filesystem 512-blocks Used Available Capacity Mounted\n/dev/x 1 1 1 97% /v\n")
            if cmd[0] == "ssh":
                return _cp(255, "", "Permission denied")
            return _cp()
        with patch.object(nr.subprocess, "run", side_effect=fake), \
                patch.object(nr.os.path, "ismount", return_value=True), \
                patch.object(nr.os, "access", return_value=True), \
                patch.object(nr.Path, "is_dir", return_value=True):
            errs = nr.preflight_checks()
        self.assertTrue(any("97% full" in e for e in errs))
        self.assertTrue(any("SSH to Synology" in e for e in errs))


class TestIntegration(unittest.TestCase):
    def test_run_sync_writes_observation_and_notifies(self):
        nr.notify.reset_mock()
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value = cur
        ok = {"name": "nas", "status": "ok", "files_transferred": 3, "bytes_transferred": 10, "duration_s": 1}
        with patch.object(nr, "preflight_checks", return_value=[]), patch.object(nr, "rsync_share", return_value=ok), \
                patch("psycopg2.connect", return_value=conn), patch.dict(sys.modules, {"nova_config": FAKE_CFG}), \
                patch.object(nr.Path, "home", return_value=_TMP), _q():
            nr.run_sync()
        sql, params = cur.execute.call_args[0]
        self.assertIn("INSERT INTO shared_observations", sql)
        self.assertEqual(json.loads(params[1])["total_files"], 6)
        self.assertEqual(nr.notify.call_args[1]["dedup_key"], "nas-rsync-daily")
        state = json.loads((_TMP / ".openclaw/workspace/state/nova_nas_rsync.json").read_text())
        self.assertEqual(state["total_files"], 6)


class TestFunctional(unittest.TestCase):
    def test_preflight_failure_transfers_nothing(self):
        nr.notify.reset_mock()
        FAKE_CFG.notify_local.reset_mock()
        with patch.object(nr, "preflight_checks", return_value=["nas: destination not mounted"]), \
                patch.object(nr, "rsync_share") as rs, patch.dict(sys.modules, {"nova_config": FAKE_CFG}), _q():
            res = nr.run_sync()
        rs.assert_not_called()
        self.assertEqual(res[0]["name"], "preflight")
        self.assertEqual(nr.notify.call_args[1]["level"], "critical")
        FAKE_CFG.notify_local.assert_called_once()

    def test_db_failure_still_notifies(self):
        nr.notify.reset_mock()
        bad = {"name": "nas", "status": "warning", "files_transferred": 0, "bytes_transferred": 0, "duration_s": 1}
        with patch.object(nr, "preflight_checks", return_value=[]), patch.object(nr, "rsync_share", return_value=bad), \
                patch("psycopg2.connect", side_effect=RuntimeError("pg down")), \
                patch.dict(sys.modules, {"nova_config": FAKE_CFG}), patch.object(nr.Path, "home", return_value=_TMP), \
                _q() as out:
            nr.run_sync()
        self.assertIn("DB write failed", out.getvalue())
        self.assertEqual(nr.notify.call_args[1]["level"], "warning")


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_nas_rsync.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_nas_rsync"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
