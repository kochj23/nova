#!/usr/bin/env python3
"""Tests for nova_storage_failover.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

This script mounts/unmounts CIFS and rsyncs --delete over a live share. EVERY run() (sudo, mount,
umount, git, rsync), every TCP probe and notify is mocked; MOUNT, GIT_CACHE and the refresh
marker point into a tempdir. main() is never executed for real (not even --help: it has none)."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_storage_failover.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_storage_failover_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sf = _load()
UNAS, SYN = sf.TARGETS[0], sf.TARGETS[1]


def _ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


class _World(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.mount = root / "nova"
        self.cache = root / "cache" / "repo"
        ps = {"mount": mock.patch.object(sf, "MOUNT", str(self.mount)),
              "cache": mock.patch.object(sf, "GIT_CACHE", self.cache),
              "marker": mock.patch.object(sf, "REFRESH_MARKER", self.mount / ".last-git-refresh"),
              "run": mock.patch.object(sf, "run", return_value=_ok()),
              "reach": mock.patch.object(sf, "reachable", return_value=True),
              "notify": mock.patch.object(sf, "notify"),
              "src": mock.patch.object(sf, "current_source", return_value=None),
              "log": mock.patch.object(sf, "log")}
        self.m = {k: p.start() for k, p in ps.items()}
        self.addCleanup(lambda: ([p.stop() for p in ps.values()], self.td.cleanup()))

    def healthy(self, yes=True):
        (self.mount / "scripts").mkdir(parents=True, exist_ok=True)
        if yes:
            (self.mount / "scripts" / "a.py").write_text("x")

    def cmds(self):
        return [c[0][0] for c in self.m["run"].call_args_list]


class TestSecurity(_World):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertTrue(sf.REPO.startswith("github-nova:"))          # SSH alias + deploy key, never a token URL
        self.assertNotIn("https://", sf.REPO)
        for t in sf.TARGETS:
            self.assertTrue(t["creds"].startswith("/etc/"))            # CIFS creds live in root-only files

    def test_git_runs_as_user_when_root(self):
        with mock.patch.object(sf.os, "geteuid", return_value=0):
            self.assertEqual(sf._asuser(["git", "pull"])[:5], ["sudo", "-n", "-u", "kochj", "-H"])
        with mock.patch.object(sf.os, "geteuid", return_value=501):
            self.assertEqual(sf._asuser(["git", "pull"]), ["git", "pull"])

    def test_refuses_to_rsync_empty_tree_over_live_share(self):
        self.healthy()
        (self.cache / ".git").mkdir(parents=True)                    # checkout exists but has no scripts/
        self.assertFalse(sf.refresh_scripts_from_git())
        self.assertFalse(any(c[0] == "rsync" or "rsync" in c for c in self.cmds()))


class TestPerformance(_World):
    def test_healthy_primary_is_a_fast_noop(self):
        self.healthy()
        self.m["src"].return_value = UNAS["unc"]
        t0 = time.perf_counter()
        for _ in range(2_000):
            self.assertEqual(sf.main(), 0)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.m["run"].assert_not_called()
        self.m["reach"].assert_not_called()


class TestRetry(_World):
    def test_run_timeout_becomes_rc124(self):
        real_run = _load_run()
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 1)) as sr:
            r = real_run(["sleep", "9"], timeout=1)
        self.assertEqual(sr.call_count, 1)
        self.assertEqual((r.returncode, r.stderr), (124, "timeout"))

    def test_tries_each_target_in_order_then_reports_down(self):
        # Failover IS the retry: each target probed once in preference order; none -> critical notify, rc 1
        self.m["reach"].side_effect = [False, True]
        self.m["run"].side_effect = lambda cmd, timeout=30: (subprocess.CompletedProcess(cmd, 32, "", "host down")
                                                             if "mount" in cmd and "-t" in cmd else _ok())
        self.assertEqual(sf.main(), 1)
        self.assertEqual([c[0][0] for c in self.m["reach"].call_args_list], [UNAS["host"], SYN["host"]])
        self.assertEqual(self.m["notify"].call_args[0][1], "critical")


def _load_run():
    """The real run() (the fixture mocks sf.run); fetched from a fresh private copy."""
    return _load().run


class TestUnit(_World):
    def test_mount_healthy_requires_real_content(self):
        self.assertFalse(sf.mount_healthy())
        self.healthy(False)
        self.assertFalse(sf.mount_healthy())                          # empty canary dir = corpse mount
        self.healthy(True)
        self.assertTrue(sf.mount_healthy())

    def test_reachable_probe(self):
        with mock.patch("socket.create_connection", side_effect=OSError("refused")):
            self.assertFalse(_load().reachable("10.0.0.1"))
        with mock.patch("socket.create_connection") as cc:
            self.assertTrue(_load().reachable("10.0.0.1"))
        self.assertEqual(cc.call_args[0][0], ("10.0.0.1", 445))

    def test_refresh_skips_when_recent(self):
        self.healthy()
        (self.mount / ".last-git-refresh").touch()
        self.assertTrue(sf.refresh_scripts_from_git())
        self.m["run"].assert_not_called()


class TestIntegration(_World):
    def test_do_mount_uses_target_creds_and_checks_health(self):
        self.healthy()
        self.assertTrue(sf.do_mount(SYN))
        mount_cmd = [c for c in self.cmds() if "-t" in c][0]
        self.assertEqual(mount_cmd[:6], ["sudo", "-n", "mount", "-t", "cifs", SYN["unc"]])
        self.assertIn(f"credentials={SYN['creds']},{sf.MOUNT_OPTS}", mount_cmd)

    def test_clone_then_rsync_dereferences_links(self):
        self.healthy()
        def run(cmd, timeout=30):
            if "clone" in cmd:
                (self.cache / "scripts").mkdir(parents=True)
                (self.cache / "scripts" / "x.py").write_text("x")
            return _ok()
        self.m["run"].side_effect = run
        self.assertTrue(sf.refresh_scripts_from_git())
        rsync = [c for c in self.cmds() if "rsync" in c][0]
        self.assertIn("-L", rsync)
        self.assertIn("--delete", rsync)
        self.assertTrue(rsync[-1].endswith("/nova/scripts/"))
        self.assertTrue((self.mount / ".last-git-refresh").exists())


class TestFunctional(_World):
    def test_dead_mount_fails_over_to_synology_and_alerts(self):
        self.m["reach"].side_effect = lambda h: h == SYN["host"]
        def run(cmd, timeout=30):
            if "mount" in cmd and "-t" in cmd:
                self.healthy()
            return _ok()
        self.m["run"].side_effect = run
        with mock.patch.object(sf, "refresh_scripts_from_git", return_value=True) as rf:
            self.assertEqual(sf.main(), 0)
        cmds = self.cmds()
        self.assertEqual(cmds[0][:5], ["sudo", "-n", "umount", "-f", str(self.mount)])
        self.assertTrue(any(SYN["unc"] in c for c in cmds))
        rf.assert_called_once()
        self.assertIn("FAILED OVER to synology", self.m["notify"].call_args[0][0])

    def test_fail_back_to_primary_when_it_returns(self):
        self.healthy()
        self.m["src"].return_value = SYN["unc"]
        with mock.patch.object(sf, "refresh_scripts_from_git", return_value=True):
            self.assertEqual(sf.main(), 0)
        self.assertTrue(any(UNAS["unc"] in c for c in self.cmds()))
        self.assertEqual(self.m["notify"].call_args[0][1], "info")

    def test_fallback_serving_and_primary_still_down_is_noop(self):
        self.healthy()
        self.m["src"].return_value = SYN["unc"]
        self.m["reach"].return_value = False
        self.assertEqual(sf.main(), 0)
        self.m["run"].assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_storage_failover"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
