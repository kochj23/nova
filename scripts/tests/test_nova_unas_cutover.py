#!/usr/bin/env python3
"""Tests for nova_unas_cutover.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The cutover planner is read-only by design: these tests prove it only ever runs `ssh ... cat /proc/mdstat`
and a psql SELECT (both mocked), and that it refuses to green-light unless BOTH gates pass."""
import contextlib
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_unas_cutover.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nuc_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


uc = _load()
HEALTHY = "md2 : active raid5 sdc5[2] sdb5[1] sda5[0]\n      [3/3] [UUU]\n"
RESYNC = HEALTHY + "      [=>....]  resync = 12.5% (1/8) finish=900min\n"
PARITY_OK = "10-04 03:30 | nova-backup:nas:localdiff | 0 | t\n10-04 03:30 | nova-backup:external:localdiff | 0 | t\n"
PARITY_BAD = "10-04 03:30 | nova-backup:nas:localdiff | 17 | t\n"


def _cp(out="", rc=0):
    return subprocess.CompletedProcess([], rc, out, "")


def _router(mdstat, parity):
    calls = []
    def fake(cmd, timeout=30):
        calls.append(cmd)
        return _cp(mdstat if cmd[0] == "ssh" else parity)
    return fake, calls


def _main(argv, mdstat, parity):
    fake, calls = _router(mdstat, parity)
    buf = io.StringIO()
    with patch.object(uc, "run", side_effect=fake), patch.object(sys, "argv", argv), contextlib.redirect_stdout(buf):
        rc = uc.main()
    return rc, buf.getvalue(), calls


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_shell(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)

    def test_no_apply_mode_and_only_read_commands(self):
        self.assertNotIn('"--apply" in sys.argv', SRC)
        rc, out, calls = _main(["x"], HEALTHY, PARITY_OK)
        self.assertEqual([c[0] for c in calls], ["ssh", "psql"])
        self.assertEqual(calls[0][-1], "cat /proc/mdstat")
        self.assertTrue(calls[1][-1].lstrip().upper().startswith("SELECT"))
        self.assertIn("BatchMode=yes", calls[0])


class TestPerformance(unittest.TestCase):
    def test_gates_parse_large_outputs_fast(self):
        big = "\n".join(f"10-04 03:30 | nova-backup:j{i}:localdiff | 0 | t" for i in range(10_000))
        fake, _ = _router(HEALTHY * 1000, big)
        with patch.object(uc, "run", side_effect=fake):
            t0 = time.perf_counter()
            self.assertTrue(uc.gate_resync()[0]); self.assertTrue(uc.gate_parity()[0])
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_timeout_becomes_wait_not_crash(self):
        # RETRY GAP: run() — single attempt; a timeout returns rc=124 and the gate reads WAIT
        with patch.object(uc.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 30)) as r:
            ok, why = uc.gate_resync()
        self.assertEqual(r.call_count, 1)
        self.assertFalse(ok)
        self.assertIn("could not read /proc/mdstat", why)


class TestUnit(unittest.TestCase):
    def test_gate_resync(self):
        fake, _ = _router(RESYNC, "")
        with patch.object(uc, "run", side_effect=fake):
            ok, why = uc.gate_resync()
        self.assertFalse(ok)
        self.assertIn("resync 12.5%", why)

    def test_gate_parity(self):
        for parity, expect in ((PARITY_OK, True), (PARITY_BAD, False)):
            fake, _ = _router("", parity)
            with patch.object(uc, "run", side_effect=fake):
                self.assertEqual(uc.gate_parity()[0], expect)
        fake, _ = _router("", "")
        with patch.object(uc, "run", side_effect=fake):
            self.assertIn("no localdiff runs recorded", uc.gate_parity()[1])

    def test_plan_rendered_with_hosts(self):
        self.assertIn(f"//{uc.UNAS}/", uc.PLAN)
        self.assertIn(", ".join(uc.CORES), uc.PLAN)
        self.assertNotIn("%(", uc.PLAN)


class TestIntegration(unittest.TestCase):
    def test_parity_reads_backup_runs_localdiff(self):
        fake, calls = _router("", PARITY_OK)
        with patch.object(uc, "run", side_effect=fake):
            uc.gate_parity()
        self.assertIn("telemetry.backup_runs", calls[0][-1])
        self.assertIn("localdiff", calls[0][-1])


class TestFunctional(unittest.TestCase):
    def test_ready_prints_plan_exit_0(self):
        rc, out, _ = _main(["x"], HEALTHY, PARITY_OK)
        self.assertEqual(rc, 0)
        self.assertIn("READY to cut over.", out)
        self.assertIn("UNAS-PRIMARY CUTOVER", out)

    def test_not_ready_refuses(self):
        rc, out, _ = _main(["x"], RESYNC, PARITY_OK)
        self.assertEqual(rc, 1)
        self.assertIn("NOT READY", out)
        self.assertIn("do NOT execute until both gates read GO", out)
        rc, out, _ = _main(["x", "--check"], HEALTHY, PARITY_BAD)
        self.assertEqual(rc, 1)
        self.assertNotIn("UNAS-PRIMARY CUTOVER", out)          # --check prints gates only


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # every invocation ssh's to the Synology, so the frame check is an import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_unas_cutover"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
