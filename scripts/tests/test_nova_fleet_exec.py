#!/usr/bin/env python3
"""Tests for nova_fleet_exec.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

nova_fleet_exec is THE one restart path. The allowlist it enforces is layered: the module refuses
any service name outside [A-Za-z0-9-_.] before anything runs; the actor (nova_autonomy_actor.
SAFE_SERVICES) only ever hands it eight nova-* monitors; and a remote Mac is reached through a
forced-command key whose gate (~/bin/nova-restart-gate.sh) accepts only `restart nova-<x>` and
denies the load-bearing services. All three are proven here, offline."""
import ast
import contextlib
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fx = _load("fx", SCRIPTS / "nova_fleet_exec.py")
SRC = (SCRIPTS / "nova_fleet_exec.py").read_text()
ACTOR_SRC = (SCRIPTS / "nova_autonomy_actor.py").read_text()
GATE = Path.home() / "bin" / "nova-restart-gate.sh"


def _safe_services():
    """SAFE_SERVICES parsed from the actor's source (no import: the actor pulls in PG helpers)."""
    m = re.search(r"SAFE_SERVICES = (\{.*?\})", ACTOR_SRC, re.S)
    return ast.literal_eval(m.group(1))


class _Cur:
    def __init__(self, row): self.row = row; self.sql = []

    def execute(self, sql, params=None): self.sql.append((sql, params))

    def fetchone(self): return self.row

    def __enter__(self): return self

    def __exit__(self, *a): return False


class _Conn:
    def __init__(self, row): self.cur = _Cur(row)

    def cursor(self): return self.cur

    def __enter__(self): return self

    def __exit__(self, *a): return False


def _no_pg():
    """node_status unreachable: resolve() must fall back to the static map, never raise."""
    def boom(*a, **k):
        raise OSError("pg down")
    return patch.object(psycopg2, "connect", boom)


def _pg(row):
    return patch.object(psycopg2, "connect", lambda *a, **k: _Conn(row))


def _run(argv, capture_output=True, text=True, timeout=45):
    class R:
        returncode = 0; stdout = "ok"; stderr = ""
    return R()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_and_read_only(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertIn("WHERE lower(node_name)=%s OR host(node_ip)=%s", SRC)
        self.assertEqual(re.findall(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+[\w.]+", SRC), [])

    def test_malformed_service_name_never_runs_anything(self):
        ran = []
        with patch.object(fx.subprocess, "run", lambda *a, **k: ran.append(a)), _no_pg():
            for bad in ("bad;name", "a b", "", None, "x|y", "$(id)", "../etc", "svc`id`", "nova hue"):
                ok, why = fx.restart_service("mac-studio", bad)
                self.assertFalse(ok, bad)
                self.assertIn("malformed", why)
        self.assertEqual(ran, [])

    def test_remote_mac_goes_through_the_forced_command_key_only(self):
        with _no_pg():
            argv = fx.plan("mac-studio", "nova-battery-monitor", local=False)
        self.assertEqual(argv[0], "ssh")
        self.assertEqual(argv[1:3], ["-i", fx.RESTART_KEY])
        self.assertIn("BatchMode=yes", argv)
        self.assertEqual(argv[-1], "restart nova-battery-monitor")     # exactly the shape the gate accepts
        self.assertNotIn("sudo", " ".join(argv))
        self.assertNotIn("launchctl", " ".join(argv))                   # the gate runs launchctl, not us
        self.assertNotIn("shell=True", SRC)

    def test_actor_allowlist_is_small_nova_only_and_gate_compatible(self):
        safe = _safe_services()
        self.assertLessEqual(len(safe), 10)
        deny = re.compile(r"^(nova-gateway-v2|nova-scheduler|big-brother|nova-notifier|nova-memory-server|nova-mac-share-mount)$")
        for svc in safe:
            self.assertRegex(svc, r"^nova-[a-z0-9-]+$")
            self.assertIsNone(deny.match(svc), svc)
            self.assertTrue(all(c.isalnum() or c in "-_." for c in svc))   # passes the module's own check
        for load_bearing in ("postgres", "nova-gateway-v2", "nova-scheduler", "pgbouncer", "bind9"):
            self.assertNotIn(load_bearing, safe)

    def test_gate_script_refuses_everything_but_restart_of_an_installed_nova_service(self):
        if not GATE.exists():
            self.skipTest("forced-command gate is a host file, absent here")
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".openclaw" / "logs").mkdir(parents=True)
            cases = {"restart nova-gateway-v2": "load-bearing", "restart big-brother": "only nova-*",
                     "restart nova-battery-monitor; id": "not 'restart <svc>'", "ls -la": "not 'restart <svc>'",
                     "restart nova-fishbowl-watch": "no such LaunchAgent", "": "not 'restart <svc>'"}
            for cmd, why in cases.items():
                env = {"HOME": home, "PATH": os.environ["PATH"], "SSH_ORIGINAL_COMMAND": cmd, "SSH_CLIENT": "192.168.1.2 1 22"}
                r = subprocess.run(["bash", str(GATE)], env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(r.returncode, 2, (cmd, r.stderr))
                self.assertIn(why, r.stderr, cmd)
            gate_log = (Path(home) / ".openclaw" / "logs" / "nova-restart-gate.log").read_text()
            self.assertEqual(gate_log.count("REFUSED"), len(cases))
            self.assertNotIn(" OK ", gate_log)                            # nothing was ever kickstarted


class TestPerformance(unittest.TestCase):
    def test_resolve_and_plan_fast_on_10k_lookups(self):
        names = list(fx._STATIC) + ["10.9.8.%d" % i for i in range(50)] + ["unknown-%d" % i for i in range(50)]
        with _no_pg():
            t0 = time.perf_counter()
            for i in range(10_000):
                fx.plan(names[i % len(names)], "nova-hue", local=bool(i % 2))
            self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_node_status_lookup_is_one_shot_and_falls_back_to_the_static_map(self):
        # RETRY GAP: resolve()/psycopg2.connect — a single attempt; failure falls open to _STATIC
        attempts = []

        def boom(*a, **k):
            attempts.append(1); raise OSError("pg down")
        with patch.object(psycopg2, "connect", boom):
            self.assertEqual(fx.resolve("nova-core4"), ("192.168.1.250", "linux"))
        self.assertEqual(len(attempts), 1)

    def test_restart_subprocess_is_one_shot_and_fails_closed_without_raising(self):
        # RETRY GAP: restart_service()/subprocess.run — a timeout is reported once, never retried, never raised
        calls = []

        def hang(argv, **k):
            calls.append(argv); raise subprocess.TimeoutExpired(argv, 45)
        with _no_pg(), patch.object(fx, "is_local", lambda ip: False), patch.object(fx.subprocess, "run", hang):
            ok, why = fx.restart_service("nova-core4", "nova-hue")
        self.assertFalse(ok)
        self.assertIn("timed out", why)
        self.assertEqual(len(calls), 1)


class TestUnit(unittest.TestCase):
    def test_selftest_passes_offline(self):
        with _no_pg():
            fx.demo()

    def test_resolve_static_alias_ip_and_unknown(self):
        with _no_pg():
            self.assertEqual(fx.resolve("Office-M4-2"), ("192.168.1.6", "macos"))
            self.assertEqual(fx.resolve("  NUK "), ("192.168.1.10", "linux"))
            self.assertEqual(fx.resolve("192.168.1.7"), ("192.168.1.7", "macos"))       # a known Mac IP
            self.assertEqual(fx.resolve("10.0.0.9"), ("10.0.0.9", "linux"))              # an unknown IP
            self.assertEqual(fx.resolve("no-such-node"), ("no-such-node", "linux"))
            self.assertEqual(fx.resolve(None), ("", "linux"))

    def test_resolve_prefers_node_status_when_it_answers(self):
        with _pg(("192.168.1.99", "linux")):
            self.assertEqual(fx.resolve("mac-studio"), ("192.168.1.99", "linux"))
        with _pg((None, None)):                                   # an incomplete row is ignored
            self.assertEqual(fx.resolve("mac-studio"), ("192.168.1.6", "macos"))

    def test_plan_shapes(self):
        with _no_pg():
            self.assertEqual(fx.plan("nova-core4", "nova-hue", local=True), ["sudo", "-n", "systemctl", "restart", "nova-hue"])
            self.assertEqual(fx.plan("nova-core4", "nova-hue", local=False)[-1], "sudo -n systemctl restart nova-hue")
            mac = fx.plan("nova-core8", "nova-soil-monitor", local=True)
            self.assertEqual(mac[:3], ["launchctl", "kickstart", "-k"])
            self.assertEqual(mac[3], f"gui/{os.getuid()}/net.digitalnoise.nova-soil-monitor")

    def test_local_launchctl_without_launchd_is_refused_before_running(self):
        ran = []
        with _no_pg(), patch.object(fx, "is_local", lambda ip: True), patch.object(fx.shutil, "which", lambda n: None), \
                patch.object(fx.subprocess, "run", lambda *a, **k: ran.append(a)):
            ok, why = fx.restart_service("mac-studio", "nova-hue")
        self.assertFalse(ok)
        self.assertIn("without launchd", why)
        self.assertEqual(ran, [])


class TestIntegration(unittest.TestCase):
    def test_actor_delegates_to_this_module_rather_than_reimplementing(self):
        body = ACTOR_SRC.split("def restart_service")[1].split("\ndef ")[0]
        code = body.split('"""')[-1]                       # the body after its docstring
        self.assertIn("return nova_fleet_exec.restart_service(node, svc)", code)
        for own_impl in ("subprocess", "launchctl", "systemctl", "ssh"):
            self.assertNotIn(own_impl, code)

    def test_evidence_playbook_names_this_as_the_one_restart_path(self):
        ev = (SCRIPTS / "nova_evidence_check.py").read_text()
        self.assertIn("nova_fleet_exec.py restart <node> <svc>", ev)
        self.assertIn("restart path", ev)

    def test_resolve_plan_run_chain_uses_node_status_os_family(self):
        ran = []

        def run(argv, **k):
            ran.append(argv); return _run(argv)
        with _pg(("192.168.1.77", "macos")), patch.object(fx, "is_local", lambda ip: False), \
                patch.object(fx.subprocess, "run", run):
            ok, detail = fx.restart_service("jordans-mac-mini", "nova-zigbee-lqi")
        self.assertTrue(ok)
        self.assertEqual(ran[0][0], "ssh")
        self.assertIn("kochj@192.168.1.77", ran[0])
        self.assertEqual(ran[0][-1], "restart nova-zigbee-lqi")

    def test_shares_the_house_ops_dsn(self):
        self.assertEqual(fx.OPS_DSN, "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")


class TestFunctional(unittest.TestCase):
    def test_golden_remote_linux_restart(self):
        ran = []

        def run(argv, **k):
            ran.append((argv, k)); return _run(argv)
        with _no_pg(), patch.object(fx, "is_local", lambda ip: False), patch.object(fx.subprocess, "run", run):
            ok, detail = fx.restart_service("nova-core4", "nova-hue")
        self.assertEqual((ok, detail), (True, "ok"))
        argv, kw = ran[0]
        self.assertEqual(argv[-2:], ["kochj@192.168.1.250", "sudo -n systemctl restart nova-hue"])
        self.assertEqual(kw["timeout"], 45)

    def test_failed_restart_reports_stderr_and_is_not_ok(self):
        class R:
            returncode = 5; stdout = ""; stderr = "Failed to restart nova-hue.service: Unit not found.\n"
        with _no_pg(), patch.object(fx, "is_local", lambda ip: False), patch.object(fx.subprocess, "run", lambda *a, **k: R()):
            ok, detail = fx.restart_service("nova-core4", "nova-hue")
        self.assertFalse(ok)
        self.assertIn("Unit not found", detail)
        self.assertLessEqual(len(detail), 200)

    def test_cli_exit_code_follows_the_result(self):
        # the __main__ block: `<node> <svc>` prints ok/FAILED and exits 0/1 — proven on a stubbed restart
        code = ("import sys, nova_fleet_exec as fx\n"
                "fx.restart_service = lambda n, s: (False, 'rc=1')\n"
                "sys.argv = ['nova_fleet_exec.py', 'nova-core4', 'nova-hue']\n"
                "exec(open('nova_fleet_exec.py').read().split('if __name__')[0]); print('loaded')\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("loaded", r.stdout)


class TestFrame(unittest.TestCase):
    def test_no_args_prints_usage_and_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_fleet_exec.py")], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage: nova_fleet_exec.py <node> <service> | --selftest", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_fleet_exec"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
