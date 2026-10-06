#!/usr/bin/env python3
"""Tests for nova_ups_shutdown.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

This script powers off the fleet. EVERY process call goes through `run()`/`subprocess.run`, and every test
replaces it with a recorder: no ssh, ping, pmset or shutdown ever executes. The dry-run path and the
--simulate-percent refusal are proven not to command anything."""
import importlib.util
import io
import os
import re
import runpy
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ups_shutdown.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_ups_test_"))


def _load():
    spec = importlib.util.spec_from_file_location("nupsshut", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("subprocess.run", side_effect=AssertionError("import must not run anything")):
        spec.loader.exec_module(mod)
    mod.LOG = TMP / "ups_shutdown.log"
    return mod


ups = _load()


class Fleet:
    """Recorder for run(): pmset reports `power`/`pct`; ping/ssh succeed unless listed as dead."""
    def __init__(self, on_battery=True, pct=80, dead=(), ssh_fail=()):
        self.on_battery, self.pct, self.dead, self.ssh_fail = on_battery, pct, set(dead), set(ssh_fail)
        self.cmds = []; self.down = set()

    def __call__(self, cmd, timeout=30):
        self.cmds.append(cmd)
        if cmd[0] == "pmset":
            src = "'UPS Power'" if self.on_battery else "'AC Power'"
            pct = f" {self.pct}%;" if self.pct is not None else ""
            return subprocess.CompletedProcess(cmd, 0, f"Now drawing from {src}\n -UPS1500{pct} discharging", "")
        if cmd[0] == "ping":
            h = cmd[-1]
            return subprocess.CompletedProcess(cmd, 1 if (h in self.dead or h in self.down) else 0, "", "")
        if cmd[0] == "ssh":
            target = cmd[-2]; host = target.split("@")[-1]
            if host in self.dead or host in self.ssh_fail:
                return subprocess.CompletedProcess(cmd, 255, "", "unreachable")
            if cmd[-1] in ("sudo -n shutdown -h now", "poweroff"):       # the real power-off commands only
                self.down.add(host)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    def ssh_hosts(self):
        return [c[-2] for c in self.cmds if c[0] == "ssh"]


def _main(fleet, dry_run=False, force=False, simulate=None):
    with patch.object(ups, "run", side_effect=fleet), patch.object(ups.time, "sleep"), \
         patch.object(ups, "SIMULATE_PCT", simulate), redirect_stdout(io.StringIO()) as out:
        rc = ups.main(dry_run, force)
    return rc, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("shell=True", SRC)

    def test_dry_run_commands_nothing(self):
        fleet = Fleet(pct=5)
        rc, out = _main(fleet, dry_run=True, force=True)
        self.assertEqual(rc, 0)
        self.assertEqual([c for c in fleet.cmds if c[0] in ("ssh", "ping", "sudo")], [])
        self.assertIn("DRY-RUN would shut down 192.168.1.2", out)

    def test_simulate_refused_outside_dry_run(self):
        calls = []
        with patch.object(subprocess, "run", side_effect=lambda *a, **k: calls.append(a) or (_ for _ in ()).throw(
                AssertionError("refusal path must not run anything"))), \
             patch.object(sys, "argv", [str(SCRIPT), "--simulate-percent", "5"]):
            with self.assertRaises(SystemExit) as cm:
                runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertIn("refusing to simulate", str(cm.exception.code))
        self.assertEqual(calls, [])

    def test_ssh_is_batch_mode_and_network_gear_never_targeted(self):
        self.assertIn("BatchMode=yes", SRC)
        all_hosts = {h for _, hosts, _ in ups.WAVES for h in hosts}
        self.assertNotIn("192.168.1.1", all_hosts)     # UDM
        self.assertNotIn("192.168.1.24", all_hosts)    # switch
        self.assertNotIn("192.168.1.6", all_hosts)     # the orchestrator itself


class TestPerformance(unittest.TestCase):
    def test_wait_down_is_bounded(self):
        fleet = Fleet()
        t0 = time.perf_counter()
        with patch.object(ups, "run", side_effect=fleet), patch.object(ups.time, "sleep"), \
             patch.object(ups.time, "time", side_effect=[0, 0, 60, 120, 200]), redirect_stdout(io.StringIO()) as out:
            ups.wait_down(["192.168.1.7"], timeout=150)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertIn("STILL UP after 150s", out.getvalue())


class TestRetry(unittest.TestCase):
    def test_ssh_fails_twice_then_succeeds(self):
        results = [subprocess.CompletedProcess([], 255, "", ""), subprocess.CompletedProcess([], 255, "", ""),
                   subprocess.CompletedProcess([], 0, "", "")]
        with patch.object(ups, "run", side_effect=results) as r, patch.object(ups.time, "sleep") as sl:
            res, tries = ups.ssh_retry("h", "true")
        self.assertEqual((res.returncode, tries, r.call_count), (0, 3, 3))
        self.assertEqual(sl.call_count, 2)

    def test_ssh_gives_up_after_three(self):
        with patch.object(ups, "run", return_value=subprocess.CompletedProcess([], 255, "", "")) as r, \
             patch.object(ups.time, "sleep"):
            res, tries = ups.ssh_retry("h", "true")
        self.assertEqual((res.returncode, tries, r.call_count), (255, 3, 3))

    def test_run_timeout_becomes_124(self):
        with patch.object(ups.subprocess, "run", side_effect=subprocess.TimeoutExpired("ssh", 1)):
            self.assertEqual(ups.run(["ssh"]).returncode, 124)


class TestUnit(unittest.TestCase):
    def test_ssh_target_auth_map(self):
        self.assertEqual(ups.ssh_target("192.168.1.69"), ("root@192.168.1.69", "poweroff"))
        self.assertEqual(ups.ssh_target("192.168.1.7"), ("192.168.1.7", "sudo -n shutdown -h now"))

    def test_power_parsing(self):
        with patch.object(ups, "run", side_effect=Fleet(on_battery=True, pct=42)), patch.object(ups, "SIMULATE_PCT", None):
            self.assertEqual(ups.studio_power(), (True, 42))
            self.assertEqual(ups.rack_percent(), 42.0)
        with patch.object(ups, "run", side_effect=Fleet(on_battery=False, pct=None)), patch.object(ups, "SIMULATE_PCT", None):
            self.assertEqual(ups.studio_power(), (False, None))
            self.assertIsNone(ups.rack_percent())         # unreadable is None, never "fine"

    def test_unreachable_host_is_not_reported_as_shut_down(self):
        with patch.object(ups, "run", side_effect=Fleet(dead={"192.168.1.7"})), patch.object(ups.time, "sleep"), \
             redirect_stdout(io.StringIO()) as out:
            self.assertFalse(ups.shutdown_host("192.168.1.7", dry_run=False))
        self.assertIn("ALREADY UNREACHABLE", out.getvalue())


class TestIntegration(unittest.TestCase):
    def test_preflight_probes_real_binary(self):
        fleet = Fleet(ssh_fail={"192.168.1.9"})
        with patch.object(ups, "run", side_effect=fleet), patch.object(ups.time, "sleep"), redirect_stdout(io.StringIO()) as out:
            rc = ups.preflight()
        self.assertEqual(rc, 1)
        probes = {c[-2]: c[-1] for c in fleet.cmds if c[0] == "ssh"}
        self.assertEqual(probes["192.168.1.2"], "sudo -n true && command -v shutdown >/dev/null")
        self.assertEqual(probes["root@192.168.1.69"], "command -v poweroff >/dev/null")
        self.assertIn("NOT ARMED", out.getvalue())
        self.assertFalse(fleet.down)                       # preflight never powers anything off


class TestFunctional(unittest.TestCase):
    def test_mains_ok_does_nothing(self):
        fleet = Fleet(on_battery=False)
        rc, out = _main(fleet)
        self.assertEqual(rc, 0)
        self.assertEqual(fleet.ssh_hosts(), [])
        self.assertIn("nothing to do", out)

    def test_outage_high_battery_only_bedroom_wave(self):
        fleet = Fleet(on_battery=True, pct=80)
        rc, _ = _main(fleet)
        self.assertEqual(set(fleet.ssh_hosts()), {"192.168.1.252", "192.168.1.250", "192.168.1.251"})

    def test_outage_low_battery_waves_in_order(self):
        fleet = Fleet(on_battery=True, pct=20)
        _main(fleet)
        order = fleet.ssh_hosts()
        self.assertLess(order.index("192.168.1.10"), order.index("192.168.1.2"))      # leaves before primary
        self.assertLess(order.index("192.168.1.2"), order.index("root@192.168.1.69"))  # primary before storage
        self.assertEqual(order[-1], "root@192.168.1.9")                                # NVR last

    def test_unreadable_battery_skips_rack_waves(self):
        fleet = Fleet(on_battery=True, pct=None)
        _, out = _main(fleet)
        self.assertNotIn("192.168.1.2", fleet.ssh_hosts())
        self.assertIn("RACK BATTERY UNREADABLE", out)

    def test_flicker_stands_down(self):
        seq = iter([True, False])
        fleet = Fleet()
        def run(cmd, timeout=30):
            if cmd[0] == "pmset":
                fleet.on_battery = next(seq, False)
            return fleet(cmd, timeout)
        with patch.object(ups, "run", side_effect=run), patch.object(ups.time, "sleep"), redirect_stdout(io.StringIO()) as out:
            ups.main(False, False)
        self.assertEqual(fleet.ssh_hosts(), [])
        self.assertIn("standing down", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--preflight", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        _load()                                             # _load forbids any subprocess during import


if __name__ == "__main__":
    unittest.main()
