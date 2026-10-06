#!/usr/bin/env python3
"""Tests for nova_nas_mount_watchdog.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Every subprocess (mount/ping/sudo/umount/mount_smbfs/security) is
mocked; no real mount is ever touched. Written by Jordan Koch (via Claude)."""
import importlib.util
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nm = _load("nova_nas_mount_watchdog_t", SCRIPTS / "nova_nas_mount_watchdog.py")
SRC = (SCRIPTS / "nova_nas_mount_watchdog.py").read_text()


class _Fake:
    """subprocess.run dispatcher. `mounted` = set of mount points the `mount` table reports."""
    def __init__(self, mounted=(), ping_ok=True, creds=("nova", "p@ss/w"), smb_rc=0, mount_after_smb=True):
        self.mounted = set(mounted); self.ping_ok = ping_ok; self.creds = creds
        self.smb_rc = smb_rc; self.mount_after_smb = mount_after_smb; self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        out, rc = "", 0
        if cmd[0] == "mount":
            out = "".join(f"//x@nas/s on {m} (smbfs)\n" for m in self.mounted)
        elif cmd[0] == "ping":
            rc = 0 if self.ping_ok else 2
        elif cmd[0] == "security":
            out = self.creds[0] if "nova-synology-username" in cmd else self.creds[1]
        elif cmd[0] == "whoami":
            out = "tester\n"
        elif cmd[0] == "mount_smbfs":
            rc = self.smb_rc
            if rc == 0 and self.mount_after_smb:
                self.mounted.add(cmd[-1])
        return types.SimpleNamespace(stdout=out, stderr="boom" if rc else "", returncode=rc)

    def ran(self, name):
        return [c for c in self.calls if c[0] == name or (c[0] == "sudo" and c[2] == name)]


def _go(fake, readable=(), fn=None):
    def listdir(p):
        if p in readable:
            return ["file"]
        raise OSError("not there")
    with mock.patch.object(nm.subprocess, "run", fake), mock.patch.object(nm.os, "listdir", listdir), \
         mock.patch.object(nm, "log"):
        return (fn or nm.main)()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token|pw)\s*=\s*['\"][A-Za-z0-9+/]{8,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_creds_from_keychain_and_password_url_encoded(self):
        f = _Fake()
        _go(f, fn=lambda: nm._ensure("/Volumes/nas", "nas"))
        self.assertEqual(len(f.ran("security")), 2)
        smb = f.ran("mount_smbfs")[0]
        self.assertIn("//nova:p%40ss%2Fw@192.168.1.11/nas", smb)

    def test_never_unmounts_a_readable_share(self):
        f = _Fake()
        self.assertEqual(_go(f, readable=("/Volumes/nas", "/Volumes/external")), 0)
        self.assertEqual(f.ran("umount"), [])
        self.assertEqual(f.ran("mount_smbfs"), [])


class TestPerformance(unittest.TestCase):
    def test_is_mounted_large_mount_table(self):
        f = _Fake(mounted=[f"/Volumes/v{i}" for i in range(10_000)])
        t0 = time.perf_counter()
        with mock.patch.object(nm.subprocess, "run", f):
            for _ in range(100):
                nm.is_mounted("/Volumes/v9999")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_keychain_falls_back_to_fleet_store(self):
        f = _Fake(creds=("", ""))
        fake_secrets = types.SimpleNamespace(get_secret=lambda s: "from-store")
        with mock.patch.dict(sys.modules, {"nova_secrets": fake_secrets}), mock.patch.object(nm.subprocess, "run", f):
            self.assertEqual(nm.keychain("nova-synology-username"), "from-store")

    def test_remount_failure_is_one_shot_and_returns_1(self):
        # RETRY GAP: _ensure()/mount_smbfs — one attempt per run; the 5-min scheduler is the retry
        f = _Fake(smb_rc=1)
        rc = _go(f, fn=lambda: nm._ensure("/Volumes/nas", "nas"))
        self.assertEqual(rc, 1)
        self.assertEqual(len(f.ran("mount_smbfs")), 1)


class TestUnit(unittest.TestCase):
    def test_is_mounted_exact_path(self):
        f = _Fake(mounted=["/Volumes/nas2"])
        with mock.patch.object(nm.subprocess, "run", f):
            self.assertFalse(nm.is_mounted("/Volumes/nas"))
            self.assertTrue(nm.is_mounted("/Volumes/nas2"))

    def test_keychain_total_failure_is_empty(self):
        f = _Fake(creds=("", ""))
        bad = types.SimpleNamespace(get_secret=mock.Mock(side_effect=RuntimeError("pg")))
        with mock.patch.dict(sys.modules, {"nova_secrets": bad}), mock.patch.object(nm.subprocess, "run", f):
            self.assertEqual(nm.keychain("x"), "")

    def test_watches_both_shares(self):
        self.assertEqual(set(nm.MOUNT_POINTS), {"/Volumes/nas", "/Volumes/external"})


class TestIntegration(unittest.TestCase):
    def test_mounted_but_unreadable_is_left_alone(self):
        f = _Fake(mounted=["/Volumes/nas", "/Volumes/external"])
        self.assertEqual(_go(f), 0)
        self.assertEqual(f.ran("umount"), [])

    def test_missing_creds_refuses_mount(self):
        f = _Fake(creds=("", ""))
        with mock.patch.dict(sys.modules, {"nova_secrets": types.SimpleNamespace(get_secret=lambda s: None)}):
            rc = _go(f, fn=lambda: nm._ensure("/Volumes/nas", "nas"))
        self.assertEqual(rc, 1)
        self.assertEqual(f.ran("mount_smbfs"), [])


class TestFunctional(unittest.TestCase):
    def test_remounts_dropped_share(self):
        f = _Fake(mounted=["/Volumes/external"])
        self.assertEqual(_go(f, readable=("/Volumes/external",)), 0)
        smb = f.ran("mount_smbfs")
        self.assertEqual(len(smb), 1)
        self.assertEqual(smb[0][-1], "/Volumes/nas")
        self.assertEqual(f.ran("umount")[0][-1], "/Volumes/nas")

    def test_nas_down_alerts_once_and_exits_zero(self):
        f = _Fake(ping_ok=False)
        notify = mock.Mock()
        with mock.patch.dict(sys.modules, {"nova_notify": types.SimpleNamespace(notify=notify)}):
            self.assertEqual(_go(f), 0)
        notify.assert_called_once()
        self.assertEqual(notify.call_args[1]["dedup_key"], "nas-unreachable")
        self.assertEqual(f.ran("mount_smbfs") + f.ran("umount"), [])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_nas_mount_watchdog"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
