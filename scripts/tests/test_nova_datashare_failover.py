#!/usr/bin/env python3
"""Tests for nova_datashare_failover.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
Nothing is ever mounted or unmounted: run(), reachable(), readable(), current_source() and notify are
mocked, and --check (the dry run) is proven to issue zero commands."""
import importlib.util
import json
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
SCRIPT = SCRIPTS / "nova_datashare_failover.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="datashare-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("datashare_failover_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


df = _load()
# module-level stubs: no mount/umount/sudo, no sockets, no notify, state file in a tempdir
df.STATE_FILE = TMP / "fails.json"
df.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=OSError("offline: subprocess stubbed")),
                                      TimeoutExpired=subprocess.TimeoutExpired,
                                      CompletedProcess=subprocess.CompletedProcess)
df.notify = MagicMock()
df.log = MagicMock()
SPEC = {"mount": "/mnt/nas", "primary_unc": "//192.168.1.69/nas", "secondary_unc": "//192.168.1.11/nas"}


class _World:
    """Patches every probe/actuator of handle() and records the commands it would run."""
    def __init__(self, src, healthy, primary_up, cmd_rc=0, ro_ok=True):
        self.cmds = []
        self.p = [patch.object(df, "applies_here", return_value=True),
                  patch.object(df, "current_source", return_value=src),
                  patch.object(df, "readable", side_effect=[healthy] + [ro_ok] * 5),
                  patch.object(df, "reachable", return_value=primary_up),
                  patch.object(df, "run", side_effect=lambda c, timeout=45: (
                      self.cmds.append(c), subprocess.CompletedProcess(c, cmd_rc, "", "err"))[1])]

    def __enter__(self):
        for p in self.p: p.start()
        return self

    def __exit__(self, *e):
        for p in self.p: p.stop()


def _reset():
    df.notify.reset_mock()
    if df.STATE_FILE.exists():
        df.STATE_FILE.unlink()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn("credentials=/etc/cifs-", df.RO_OPTS)      # creds come from a root-owned file

    def test_fallback_is_always_read_only(self):
        self.assertIn(",ro,", df.RO_OPTS)
        self.assertNotIn("shell=True", SRC)
        with _World(src=None, healthy=False, primary_up=False) as w:
            df.handle(SPEC, check_only=False)
        mounts = [c for c in w.cmds if "mount" in c and "-t" in c]
        self.assertEqual(len(mounts), 1)
        self.assertIn(df.RO_OPTS, mounts[0])
        self.assertTrue(all(c[:2] == ["sudo", "-n"] for c in w.cmds))   # never prompts for a password


class TestPerformance(unittest.TestCase):
    def test_track_failure_bounded_and_fast(self):
        _reset()
        t0 = time.perf_counter()
        for i in range(2000):
            df.track_failure(f"/m{i % 5}", "down", "x")
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(len(json.loads(df.STATE_FILE.read_text())), 5)   # one counter per mount, no growth


class TestRetry(unittest.TestCase):
    def test_run_timeout_becomes_rc_124(self):
        # RETRY GAP: run() — no retry; a timeout becomes a 124 CompletedProcess (fail open, no exception)
        m = MagicMock(side_effect=subprocess.TimeoutExpired("ls", 3))
        with patch.object(df.subprocess, "run", m):
            r = df.run(["ls"])
        self.assertEqual(r.returncode, 124)
        self.assertEqual(m.call_count, 1)

    def test_escalates_after_three_consecutive_failures_then_resets(self):
        _reset()
        for _ in range(2):
            df.track_failure("/mnt/nas", "recover-failed", "x")
        df.notify.assert_not_called()
        df.track_failure("/mnt/nas", "recover-failed", "x")
        self.assertEqual(df.notify.call_args[1]["meta"]["consecutive_failures"], 3)
        df.track_failure("/mnt/nas", "ok", "fine")
        self.assertEqual(json.loads(df.STATE_FILE.read_text()), {})

    def test_reachable_false_on_socket_error(self):
        import socket
        with patch.object(socket, "create_connection", side_effect=OSError("no route")):
            self.assertFalse(df.reachable("10.255.255.1"))


class TestUnit(unittest.TestCase):
    def test_readable_probes_as_kochj_when_root(self):
        rec = MagicMock(return_value=subprocess.CompletedProcess([], 0))
        with patch.object(df, "run", rec), patch.object(df.os, "geteuid", return_value=0):
            self.assertTrue(df.readable("/mnt/nas"))
        self.assertEqual(rec.call_args[0][0][:4], ["sudo", "-n", "-u", "kochj"])

    def test_load_fails_corrupt_file(self):
        df.STATE_FILE.write_text("{not json")
        self.assertEqual(df._load_fails(), {})

    def test_on_secondary(self):
        with patch.object(df, "current_source", return_value="//192.168.1.11/nas"):
            self.assertTrue(df.on_secondary("/mnt/nas", SPEC))
        with patch.object(df, "current_source", return_value="//192.168.1.69/nas"):
            self.assertFalse(df.on_secondary("/mnt/nas", SPEC))


class TestIntegration(unittest.TestCase):
    def test_roles_follow_cutover(self):
        self.assertEqual((df.PRIMARY, df.FALLBACK), (df.UNAS, df.SYNOLOGY))
        self.assertEqual(df.FALLBACK_CREDS, df.SYNOLOGY_CREDS)
        for s in df.MANAGED:
            self.assertIn(df.PRIMARY, s["primary_unc"])
            self.assertIn(df.FALLBACK, s["secondary_unc"])

    def test_notify_uses_shared_bus_and_fails_open(self):
        nn = types.ModuleType("nova_notify"); nn.notify = MagicMock()
        fresh = _load()
        with patch.dict(sys.modules, {"nova_notify": nn}):
            fresh.notify("x", "critical")
        self.assertEqual(nn.notify.call_args[1]["category"], "storage")
        nn.notify.side_effect = RuntimeError("bus down")
        with patch.dict(sys.modules, {"nova_notify": nn}):
            fresh.notify("x")                                    # must not raise


class TestFunctional(unittest.TestCase):
    def test_healthy_primary_is_left_alone(self):
        with _World(src="//192.168.1.69/nas", healthy=True, primary_up=True) as w:
            self.assertEqual(df.handle(SPEC, False)[0], "ok")
        self.assertEqual(w.cmds, [])                             # never unmount something readable

    def test_check_mode_changes_nothing(self):
        for src, healthy, up, want in [(None, False, False, "would-failover"),
                                       (None, False, True, "would-recover"),
                                       ("//192.168.1.11/nas", True, True, "would-failback")]:
            with _World(src=src, healthy=healthy, primary_up=up) as w:
                self.assertEqual(df.handle(SPEC, check_only=True)[0], want)
            self.assertEqual(w.cmds, [], want)

    def test_failover_then_down(self):
        df.notify.reset_mock()
        with _World(src=None, healthy=False, primary_up=False) as w:
            self.assertEqual(df.handle(SPEC, False)[0], "failover")
        self.assertIn("READ-ONLY", df.notify.call_args[0][0])
        with _World(src=None, healthy=False, primary_up=False, cmd_rc=32):
            self.assertEqual(df.handle(SPEC, False)[0], "down")
        self.assertEqual(df.notify.call_args[0][1], "critical")

    def test_main_returns_1_when_a_share_is_down(self):
        _reset()
        with patch.object(df, "handle", side_effect=[("down", "x"), ("skip", ""), ("ok", "")]):
            self.assertEqual(df.main(), 1)
        self.assertEqual(json.loads(df.STATE_FILE.read_text()), {"/mnt/nas": 1})


class TestFrame(unittest.TestCase):
    def test_check_mode_exits_cleanly_off_host(self):
        # --check is the dry run; on this Mac none of the managed mount points exist, so all are skipped
        if Path("/proc/mounts").exists():
            self.skipTest("only safe to smoke-run off the nova-cores")
        r = subprocess.run([sys.executable, str(SCRIPT), "--check"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(df.main))


if __name__ == "__main__":
    unittest.main()
