#!/usr/bin/env python3
"""Tests for nova_mac_share_mount.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_mac_share_mount.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


M = _load("mac_share_mount_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="mac-share-test-"))
NAS = M.MOUNTS[0]            # /Volumes/nas
MOUNT = NAS["mount"]
SYN, UNAS = M.SYNOLOGY, M.UNAS
REAL_NOTIFY = M.notify          # captured before any test patches it


class _Shell:
    """Fake subprocess.run: records every argv list, answers from a table. Nothing real runs."""
    def __init__(self, mount_src=None, ls_ok=True, keychain=None):
        self.calls, self.kwargs = [], []
        self.mount_src = mount_src            # device string the mount table shows for MOUNT (None = unmounted)
        self.ls_ok = ls_ok
        self.keychain = keychain or {}
        self.mount_lands = True               # whether a mount command updates the mount table

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd)); self.kwargs.append(kw)
        rc, out, err = 0, "", ""
        if cmd[0] == "/sbin/mount":
            out = f"{self.mount_src} on {MOUNT} (smbfs, nodev)\n" if self.mount_src else "devfs on /dev (devfs)\n"
        elif cmd[0] == "/bin/ls":
            rc = 0 if self.ls_ok else 1
        elif cmd[0] == "security":
            out = self.keychain.get(cmd[cmd.index("-s") + 1], "")
        elif cmd[0] == "whoami":
            out = "kochj"
        elif cmd[0] in ("umount",) or (cmd[0] == "sudo" and "umount" in cmd):
            self.mount_src = None
        elif cmd[0] == "mount_smbfs":
            if self.mount_lands:
                self.mount_src = f"//kochj@{UNAS}/nas"
        elif cmd[0] == "mount" and "-t" in cmd:
            if self.mount_lands:
                self.mount_src = f"//kochj@{SYN}/nas"
        return subprocess.CompletedProcess(cmd, rc, out, err)

    def names(self):
        return [c[0] for c in self.calls]

    def mutators(self):
        return [c for c in self.calls if c[0] in ("umount", "mount_smbfs", "mount") or (c[0] == "sudo" and "umount" in c)]


CREDS = {"nova-synology-username": "syn-user", "nova-synology-password": "s3cr3t",
         "nova-unas-username": "unas-user", "nova-unas-password": "p@ss/word"}


class _Base(unittest.TestCase):
    def setUp(self):
        self.sh = _Shell(keychain=dict(CREDS))
        self._p = [mock.patch.object(M.subprocess, "run", self.sh),
                   mock.patch.object(M, "notify", mock.MagicMock()),
                   mock.patch.object(M, "STATE_FILE", str(TMP / f"fails-{id(self)}.json"))]
        for p in self._p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._p])


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_safe_redacts_password_and_log_never_prints_it(self):
        self.assertEqual(M._safe("//kochj:hunter2@192.168.1.69/nas"), "//kochj:***@192.168.1.69/nas")
        self.assertEqual(M._safe(None), "")
        buf = io.StringIO()
        with redirect_stdout(buf):
            M.log("mount failed: //u:hunter2@h/x not permitted")
        self.assertNotIn("hunter2", buf.getvalue())
        self.assertIn("//u:***@h/x", buf.getvalue())

    def test_smb_url_quotes_password_and_uses_keychain(self):
        url = M._smb_url(UNAS, "nas")
        self.assertEqual(url, f"//unas-user:p%40ss%2Fword@{UNAS}/nas")
        self.assertIn("security", self.sh.names())

    def test_missing_creds_fail_open_without_mount_attempt(self):
        self.sh.keychain = {}
        with mock.patch.dict(sys.modules, {"nova_secrets": types.SimpleNamespace(get_secret=lambda s: "")}):
            self.assertIsNone(M._smb_url(UNAS, "nas"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertFalse(M.mount_primary(NAS))
        self.assertIn("cannot mount", buf.getvalue())
        self.assertNotIn("mount_smbfs", self.sh.names())

    def test_no_shell_true_anywhere(self):
        self.assertNotIn("shell=True", SRC)
        M.handle(NAS, check_only=False)
        self.assertTrue(all(not kw.get("shell") for kw in self.sh.kwargs))


class TestPerformance(unittest.TestCase):
    def test_redaction_and_host_lookup_fast_on_10k(self):
        srcs = [f"//kochj:pw{i}@{SYN if i % 2 else UNAS}/nas" for i in range(10_000)]
        t0 = time.perf_counter()
        for s in srcs:
            M._safe(s); M._host_of(s)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(_Base):
    def test_run_timeout_is_one_shot_and_fails_open(self):
        # RETRY GAP: run()/mount attempts are one-shot; a TimeoutExpired becomes rc 124, no retry
        with mock.patch.object(M.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 1)):
            r = M.run(["anything"])
        self.assertEqual(r.returncode, 124)
        self.assertEqual(r.stderr, "timeout")

    def test_escalation_counter_is_the_only_retry(self):
        for n in range(1, M.ESCALATE_AFTER):
            M.track_failure(MOUNT, "recover-failed", "x")
            self.assertFalse(M.notify.called, n)
        M.track_failure(MOUNT, "recover-failed", "x")
        self.assertTrue(M.notify.called)
        kw = M.notify.call_args.kwargs
        self.assertEqual(kw["dedup_key"], f"mac-share-recover-stuck-{MOUNT}")
        self.assertEqual(kw["meta"]["consecutive_failures"], M.ESCALATE_AFTER)
        self.assertEqual(json.loads(Path(M.STATE_FILE).read_text())[MOUNT], M.ESCALATE_AFTER)
        M.track_failure(MOUNT, "ok", "healthy")
        self.assertNotIn(MOUNT, json.loads(Path(M.STATE_FILE).read_text()))

    def test_keychain_falls_back_to_fleet_store_then_empty(self):
        self.sh.keychain = {}
        with mock.patch.dict(sys.modules, {"nova_secrets": types.SimpleNamespace(get_secret=lambda s: "from-pg")}):
            self.assertEqual(M.keychain("nova-unas-username"), "from-pg")
        with mock.patch.dict(sys.modules, {"nova_secrets": types.SimpleNamespace(get_secret=lambda s: None)}):
            self.assertEqual(M.keychain("nova-unas-username"), "")


class TestUnit(_Base):
    def test_host_of_and_share_for(self):
        self.assertEqual(M._host_of(f"//kochj@{SYN}/nas"), SYN)
        self.assertEqual(M._host_of("//kochj@NAS._afpovertcp._tcp.local/nas"), SYN)
        self.assertEqual(M._host_of(f"//kochj@{UNAS}/nas"), UNAS)
        self.assertIsNone(M._host_of("//x@10.0.0.1/y")); self.assertIsNone(M._host_of(None))
        self.assertEqual(M._share_for(UNAS, M.MOUNTS[1]), "External")
        self.assertEqual(M._share_for(SYN, M.MOUNTS[1]), "external")

    def test_health_states(self):
        self.sh.mount_src = None
        self.assertEqual(M.health(MOUNT), ("unmounted", None))
        self.sh.mount_src = f"//kochj@{UNAS}/nas"
        self.assertEqual(M.health(MOUNT)[0], "healthy")
        self.sh.ls_ok = False
        with mock.patch.object(M, "reachable", return_value=True):
            self.assertEqual(M.health(MOUNT)[0], "healthy")       # TCC: ls fails, server answers
        with mock.patch.object(M, "reachable", return_value=False):
            self.assertEqual(M.health(MOUNT)[0], "dead")

    def test_clear_order_plain_umount_before_force_before_sudo(self):
        M._clear(MOUNT)
        self.assertEqual(self.sh.calls, [["umount", MOUNT], ["umount", "-f", MOUNT], ["sudo", "-n", "umount", "-f", MOUNT]])

    def test_mount_source_and_mounted_from(self):
        self.sh.mount_src = f"//kochj@{SYN}/nas"
        self.assertEqual(M.mount_source(MOUNT), f"//kochj@{SYN}/nas")
        self.assertTrue(M._mounted_from(MOUNT, SYN)); self.assertFalse(M._mounted_from(MOUNT, UNAS))
        self.assertIsNone(M.mount_source("/Volumes/nope"))


class TestIntegration(_Base):
    def test_primary_fallback_roles_and_mount_table(self):
        self.assertEqual((M.PRIMARY, M.FALLBACK), (UNAS, SYN))
        self.assertEqual([m["mount"] for m in M.MOUNTS], ["/Volumes/nas", "/Volumes/external"])

    def test_fallback_mount_is_read_only_and_primary_is_rw(self):
        self.sh.mount_src = None
        self.assertTrue(M.mount_fallback_ro(NAS))
        ro = [c for c in self.sh.calls if c[0] == "mount"][0]
        self.assertEqual(ro[:5], ["mount", "-t", "smbfs", "-o", "ro"]); self.assertIn(SYN, ro[5]); self.assertEqual(ro[6], MOUNT)
        self.sh.calls.clear(); self.sh.mount_src = None
        self.assertTrue(M.mount_primary(NAS))
        rw = [c for c in self.sh.calls if c[0] == "mount_smbfs"][0]
        self.assertIn(UNAS, rw[1]); self.assertEqual(rw[2], MOUNT)
        self.assertIn(["sudo", "-n", "mkdir", "-p", MOUNT], self.sh.calls)   # mountpoint prepared first

    def test_notify_wrapper_targets_storage_category(self):
        fake = mock.MagicMock()
        with mock.patch.object(M, "notify", REAL_NOTIFY), \
             mock.patch.dict(sys.modules, {"nova_notify": types.SimpleNamespace(notify=fake)}):
            M.notify("hello", "info", meta={"a": 1})
        self.assertTrue(fake.call_args.args[0].startswith("Mac share mount: hello"))
        self.assertEqual(fake.call_args.kwargs["category"], "storage")
        self.assertEqual(fake.call_args.kwargs["dedup_key"], "mac-share-info")


class TestFunctional(_Base):
    def test_healthy_on_primary_is_left_alone(self):
        self.sh.mount_src = f"//kochj@{UNAS}/nas"
        with mock.patch.object(M, "reachable", return_value=True):
            self.assertEqual(M.handle(NAS, False)[0], "ok")
        self.assertEqual(self.sh.mutators(), [])

    def test_tcc_blocked_ls_never_tears_down_a_live_mount(self):
        self.sh.mount_src = f"//kochj@{UNAS}/nas"; self.sh.ls_ok = False
        with mock.patch.object(M, "reachable", return_value=True):
            self.assertEqual(M.handle(NAS, False)[0], "ok")
        self.assertEqual(self.sh.mutators(), [])

    def test_check_only_never_mutates(self):
        for src, up, want in ((f"//kochj@{SYN}/nas", True, "would-failback"), (None, True, "would-recover"),
                              (None, False, "would-failover")):
            self.sh.mount_src = src; self.sh.calls.clear()
            with mock.patch.object(M, "reachable", return_value=up):
                self.assertEqual(M.handle(NAS, True)[0], want)
            self.assertEqual(self.sh.mutators(), [])

    def test_failback_when_primary_returns(self):
        self.sh.mount_src = f"//kochj@{SYN}/nas"
        with mock.patch.object(M, "reachable", return_value=True):
            state, detail = M.handle(NAS, False)
        self.assertEqual(state, "failback")
        names = self.sh.names()
        self.assertLess(names.index("umount"), names.index("mount_smbfs"))
        self.assertIn(UNAS, [c for c in self.sh.calls if c[0] == "mount_smbfs"][0][1])
        self.assertEqual(M.notify.call_args.args[1], "info")

    def test_serving_read_only_while_primary_down(self):
        self.sh.mount_src = f"//kochj@{SYN}/nas"
        with mock.patch.object(M, "reachable", return_value=False):
            self.assertEqual(M.handle(NAS, False), ("ok", "UNAS primary still down; serving read-only from Synology"))
        self.assertEqual(self.sh.mutators(), [])

    def test_failover_read_only_when_primary_down(self):
        self.sh.mount_src = None
        with mock.patch.object(M, "reachable", return_value=False):
            self.assertEqual(M.handle(NAS, False)[0], "failover")
        ro = [c for c in self.sh.calls if c[0] == "mount"][0]
        self.assertEqual(ro[:5], ["mount", "-t", "smbfs", "-o", "ro"])
        self.assertEqual(M.notify.call_args.args[1], "warning")

    def test_nothing_mountable_is_down_and_critical(self):
        self.sh.mount_src = None; self.sh.mount_lands = False
        with mock.patch.object(M, "reachable", return_value=False):
            self.assertEqual(M.handle(NAS, False)[0], "down")
        self.assertEqual(M.notify.call_args.args[1], "critical")

    def test_main_rc_and_check_skips_tracking(self):
        with mock.patch.object(M, "handle", return_value=("down", "x")), \
             mock.patch.object(M, "track_failure") as tf, mock.patch.object(sys, "argv", ["m"]), redirect_stdout(io.StringIO()):
            self.assertEqual(M.main(), 1)
        self.assertEqual(tf.call_count, len(M.MOUNTS))
        with mock.patch.object(M, "handle", return_value=("ok", "x")), \
             mock.patch.object(M, "track_failure") as tf, mock.patch.object(sys, "argv", ["m", "--check"]):
            self.assertEqual(M.main(), 0)
        self.assertEqual(tf.call_count, 0)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_mac_share_mount"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
