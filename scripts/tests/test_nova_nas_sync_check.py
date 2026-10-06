#!/usr/bin/env python3
"""Tests for nova_nas_sync_check.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). find/wc/ssh, PG and the alerters are mocked; state goes to a tempdir.
Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_nas_sync_check.py").read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("nassync", SCRIPTS / "nova_nas_sync_check.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ns = _load()
ns.STATE_FILE = Path(_TMP.name) / "state/nas_sync.json"
ns.log = lambda *a, **k: None
ns.notify = MagicMock()                  # never alert from a test


class _Conn:
    def __init__(self):
        self.sql = []; self.autocommit = False

    def cursor(self):
        c = MagicMock()
        c.execute.side_effect = lambda sql, p=None: self.sql.append((sql, p))
        return c

    def close(self):
        pass


def _counts(local, remote):
    """Patch Popen (find) + run (wc / ssh) to report the given counts."""
    def run(argv, **k):
        return SimpleNamespace(stdout=f"{local}\n" if argv[0] == "wc" else f"{remote}\n", returncode=0)
    popen = MagicMock()
    popen.return_value.stdout = MagicMock()
    return patch.object(ns.subprocess, "Popen", popen), patch.object(ns.subprocess, "run", side_effect=run)


def _share(name="nas"):
    return {"name": name, "local": _TMP.name + "/", "remote": "kochj@192.168.1.11:/volume1/docker/nas/"}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn("BatchMode=yes", ns.SSH_OPTS)            # key auth only, never a password prompt

    def test_no_shell_and_db_write_parameterized(self):
        self.assertNotIn("shell=True", SRC)
        p1, p2 = _counts(10, 10)
        with p1 as po, p2 as run:
            ns.check_share_sync(_share())
        self.assertEqual(po.call_args[0][0][:2], ["find", _TMP.name + "/"])
        self.assertEqual(run.call_args_list[1][0][0][0], "ssh")
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))


class TestPerformance(unittest.TestCase):
    def test_10k_share_results_aggregate_fast(self):
        shares = [_share(f"s{i}") for i in range(10_000)]
        res = {"status": "ok", "name": "s", "sync_pct": 100.0, "local_files": 1, "remote_files": 1,
               "difference": 0, "direction": "matched"}
        t0 = time.perf_counter()
        with patch.object(ns, "SHARES", shares), patch.object(ns, "check_share_sync", return_value=res), \
             patch.object(ns.psycopg2, "connect", return_value=_Conn()):
            st = ns.run_check()
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(st["overall_sync_pct"], 100.0)


class TestRetry(unittest.TestCase):
    def test_ssh_failure_fails_open(self):
        # RETRY GAP: check_share_sync — one find + one ssh per nightly run; failure becomes a status=error row
        popen = MagicMock(); popen.return_value.stdout = MagicMock()
        calls = []

        def run(argv, **k):
            calls.append(argv[0])
            if argv[0] == "ssh":
                raise subprocess.TimeoutExpired("ssh", 600)
            return SimpleNamespace(stdout="5\n")

        with patch.object(ns.subprocess, "Popen", popen), patch.object(ns.subprocess, "run", side_effect=run):
            r = ns.check_share_sync(_share())
        self.assertEqual(calls, ["wc", "ssh"])
        self.assertEqual(r["status"], "error")
        self.assertIn("Remote count failed", r["error"])

    def test_db_failure_still_saves_state(self):
        with patch.object(ns, "SHARES", [_share()]), \
             patch.object(ns, "check_share_sync", return_value={"name": "nas", "status": "error", "error": "x"}), \
             patch.object(ns.psycopg2, "connect", side_effect=RuntimeError("pg down")), \
             patch("nova_config.notify_local") as nl:
            st = ns.run_check()
        self.assertTrue(ns.STATE_FILE.exists())
        self.assertEqual(st["overall_sync_pct"], 0.0)       # every share errored -> reported as 0% and alerted
        nl.assert_called_once()


class TestUnit(unittest.TestCase):
    def test_sync_math_and_direction(self):
        for (l, r), (pct, d) in {(100, 90): (90.0, "UNAS ahead"), (90, 100): (90.0, "Synology ahead"),
                                 (0, 0): (100.0, "matched"), (7, 7): (100.0, "matched")}.items():
            p1, p2 = _counts(l, r)
            with p1, p2:
                res = ns.check_share_sync(_share())
            self.assertEqual((res["sync_pct"], res["direction"], res["difference"]), (pct, d, abs(l - r)))

    def test_missing_mount(self):
        r = ns.check_share_sync({"name": "x", "local": "/no/such/mount/", "remote": "u@h:/p/"})
        self.assertEqual(r["status"], "error")
        self.assertIn("not found", r["error"])


class TestIntegration(unittest.TestCase):
    def test_writes_shared_observation_with_state(self):
        conn = _Conn()
        with patch.object(ns, "SHARES", [_share()]), \
             patch.object(ns, "check_share_sync", return_value={"name": "nas", "status": "ok", "sync_pct": 97.0,
                                                                "local_files": 100, "remote_files": 97,
                                                                "difference": 3, "direction": "UNAS ahead"}), \
             patch.object(ns.psycopg2, "connect", return_value=conn):
            ns.run_check()
        sql, params = conn.sql[0]
        self.assertIn("INSERT INTO shared_observations", sql)
        self.assertEqual(params[1], "info")
        self.assertEqual(json.loads(params[2])["overall_sync_pct"], 97.0)


class TestFunctional(unittest.TestCase):
    def test_badly_out_of_sync_alerts(self):
        ns.notify.reset_mock()
        p1, p2 = _counts(1000, 500)
        with patch.object(ns, "SHARES", [_share()]), p1, p2, \
             patch.object(ns.psycopg2, "connect", return_value=_Conn()), \
             patch("nova_config.notify_local") as nl:
            st = ns.run_check()
        self.assertEqual(st["overall_sync_pct"], 50.0)
        nl.assert_called_once()
        self.assertEqual(ns.notify.call_args[1]["dedup_key"], "nas-sync")
        self.assertEqual(json.loads(ns.STATE_FILE.read_text())["overall_sync_pct"], 50.0)

    def test_in_sync_is_quiet(self):
        ns.notify.reset_mock()
        p1, p2 = _counts(100, 99)
        with patch.object(ns, "SHARES", [_share()]), p1, p2, \
             patch.object(ns.psycopg2, "connect", return_value=_Conn()), patch("nova_config.notify_local") as nl:
            ns.run_check()
        nl.assert_not_called(); ns.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_nas_sync_check.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--serve", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_nas_sync_check"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
