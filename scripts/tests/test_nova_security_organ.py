#!/usr/bin/env python3
"""Tests for nova_security_organ.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_security_organ.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


so = _load("security_organ_under_test", SCRIPT)
import nova_escalation  # noqa: E402  — offline: the two-man gate is stubbed (no HTTP probes, no PG)
_GATE = mock.patch.object(nova_escalation, "authorize",
                          side_effect=lambda *a, **k: {"allowed": True, "reason": "test", "keys": ["reasoning", "sensors"]})
_GATE.start()
KNOWN = "aa:bb:cc:dd:ee:01"
NEW = "aa:bb:cc:dd:ee:02"


class _Cur:
    def __init__(self, known=(KNOWN,), dhcp_rows=(), fail_dhcp=False, digest_rows=(), total=0):
        self.known, self.dhcp_rows, self.fail_dhcp = list(known), list(dhcp_rows), fail_dhcp
        self.digest_rows, self.total = list(digest_rows), total
        self.sql, self.params, self._last = [], [], ""
        self.connection = types.SimpleNamespace(rollback=mock.Mock())

    def execute(self, sql, params=None):
        if self.fail_dhcp and "FROM syslog_events" in sql:
            raise RuntimeError("no syslog table")
        self.sql.append(sql); self.params.append(params); self._last = sql

    def fetchall(self):
        s = self._last
        if "FROM telemetry.known_devices" in s and "first_seen >" in s:
            return self.digest_rows
        if "FROM telemetry.known_devices" in s:
            return [(m,) for m in self.known]
        if "FROM syslog_events" in s:
            return self.dhcp_rows
        return []

    def fetchone(self):
        return (self.total,) if "count(*)" in self._last else None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def writes(self, needle):
        return [p for s, p in zip(self.sql, self.params) if needle in s]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.commits = 0; self.autocommit = True

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def _client(mac, **kw):
    c = {"mac": mac, "name": "phone", "hostname": "phone", "ip": "192.168.1.50", "oui": "Apple", "is_wired": False,
         "essid": "home", "ap_mac": "00:00:00:00:00:01", "first_seen": time.time() - 60}
    c.update(kw); return c


FAKE_BOARD = types.ModuleType("nova_organ_board")
FAKE_BOARD.board = lambda cur: {}
FAKE_BOARD.someone_just_arrived = lambda b: None


def _cycle(cur, clients, dry_run=False, seed=False, arp=(), devices=()):
    calls = []
    with mock.patch.object(so.unifi, "_fetch_clients", return_value=clients), \
         mock.patch.object(so.unifi, "_fetch_devices", return_value=list(devices)), \
         mock.patch.object(so, "neighbor_clients", return_value=list(arp)), \
         mock.patch.object(so, "notify", side_effect=lambda *a, **k: calls.append((a, k))), \
         mock.patch.dict(sys.modules, {"nova_organ_board": FAKE_BOARD}), redirect_stdout(StringIO()):
        n = so.cycle(_Conn(cur), dry_run=dry_run, seed=seed)
    return n, calls


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_keychain_auth(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", so.DSN.lower())
        self.assertIn("Keychain", (SCRIPTS / "nova_unifi_poller.py").read_text())  # UniFi auth lives there

    def test_sql_is_parameterized_and_macs_normalized(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertNotIn('" %', SRC)
        cur = _Cur()
        so.dhcp_macs(cur, minutes=7)
        self.assertEqual(cur.params[-1], (7,))
        self.assertEqual(so.known_macs(_Cur(known=("AA:BB:CC:DD:EE:01",))), {KNOWN})

    def test_newcomer_alert_is_deduped_per_mac(self):
        cur = _Cur()
        _, calls = _cycle(cur, [_client(NEW)])
        self.assertEqual(calls[0][1]["dedup_key"], f"newdev:{NEW}")
        self.assertEqual(calls[0][1]["category"], "security")

    def test_test_alert_is_labelled(self):
        with mock.patch.object(so, "notify") as n, redirect_stdout(StringIO()):
            so.test_alert()
        self.assertTrue(n.call_args[0][0].startswith("[TEST] "))
        self.assertIs(n.call_args[1]["meta"]["test"], True)


class TestPerformance(unittest.TestCase):
    def test_mac_helpers_fast_on_10k(self):
        lines = [f"DHCPACK(br0) 192.168.1.{i % 250} 02:de:ad:{i % 99:02x}:ef:{i % 255:02x} host{i}" for i in range(10_000)]
        t0 = time.perf_counter()
        macs = {m.lower() for ln in lines for m in so.MAC_RE.findall(ln)}
        rand = sum(so.is_randomized(m) for m in macs)
        for i in range(10_000):
            so.level_for(1_000_000.0 - 10, {"unifi", "arp"}, None, 1_000_000.0)
        self.assertLess(time.perf_counter() - t0, 1.0); self.assertEqual(rand, len(macs))


class TestRetry(unittest.TestCase):
    def test_neighbor_and_dhcp_fail_open(self):
        # RETRY GAP: neighbor_clients / dhcp_macs — single attempt, safe empty default.
        with mock.patch.object(subprocess, "run", side_effect=OSError("no ip")):
            self.assertEqual(so.neighbor_clients(), [])
        cur = _Cur(fail_dhcp=True)
        self.assertEqual(so.dhcp_macs(cur), set())
        cur.connection.rollback.assert_called_once()

    def test_unifi_fetch_retries_once_after_relogin_then_degrades(self):
        cur = _Cur()
        with mock.patch.object(so.unifi, "_fetch_clients", return_value=None) as fc, \
             mock.patch.object(so.unifi, "_unifi_login", return_value=True) as lg, redirect_stdout(StringIO()):
            self.assertEqual(so.cycle(_Conn(cur)), 0)
        self.assertEqual((fc.call_count, lg.call_count), (2, 1))
        self.assertEqual(cur.writes("INSERT INTO health_checks")[0][1:], ("degraded", "UniFi client fetch failed"))


class TestUnit(unittest.TestCase):
    def test_is_randomized(self):
        self.assertTrue(so.is_randomized("02:de:ad:be:ef:01")); self.assertFalse(so.is_randomized("00:11:22:33:44:55"))
        self.assertFalse(so.is_randomized("garbage"))

    def test_level_for(self):
        now = 1_000_000.0
        self.assertEqual(so.level_for(None, {"unifi", "arp"}, None, now), ("critical", ""))
        self.assertIn("single witness", so.level_for(now - 10, {"unifi"}, None, now)[1])
        self.assertTrue(so.level_for(now - 10, {"unifi", "arp"}, ("jordan", 3), now)[1].startswith("jordan walked in"))
        # SPINNAKER: unifi + dhcp are both the UDM -> one independent witness -> warning
        lvl, why = so.level_for(None, {"unifi", "dhcp"}, None, now)
        self.assertEqual(lvl, "warning")
        self.assertIn("share one upstream", why)
        self.assertEqual(so.independent_witnesses({"unifi", "dhcp", "arp"}), 2)
        self.assertEqual(so.level_for(now - 2 * so.RECENT_S, {"unifi", "arp", "dhcp"}, None, now), ("warning", "UniFi has seen it before"))

    def test_critical_goes_through_two_man_gate(self):
        self.assertIn('E.authorize(cur, source="nova_security_organ"', SRC)
        self.assertIn("held at warning by the two-man rule", SRC)

    def test_describe_paths(self):
        t, b = so.describe(_client("02:de:ad:be:ef:01"))
        self.assertIn("via Wi-Fi home", t); self.assertIn("RANDOMIZED MAC", b)
        t, b = so.describe(_client(NEW, is_wired=True, sw_mac="sw", sw_port=3))
        self.assertIn("via wired", t); self.assertIn("switch sw port 3", b)
        t, b = so.describe({"mac": NEW, "_source": "arp"})
        self.assertIn("via ARP only", t); self.assertIn("ARP table only", b)

    def test_dhcp_mac_extraction(self):
        cur = _Cur(dhcp_rows=[("DHCPACK(br0) 192.168.1.50 DD:EE:FF:00:11:22 phone",), (None,)])
        self.assertEqual(so.dhcp_macs(cur), {"dd:ee:ff:00:11:22"})


class TestIntegration(unittest.TestCase):
    def test_new_device_is_alerted_and_recorded_once(self):
        cur = _Cur(dhcp_rows=[(f"DHCPACK(br0) 192.168.1.50 {NEW.upper()} phone",)])
        n, calls = _cycle(cur, [_client(KNOWN), _client(NEW)], arp=[{"mac": NEW, "_source": "arp"}])
        self.assertEqual(n, 1); self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["level"], "critical")
        self.assertEqual(calls[0][1]["meta"]["witnesses"], ["arp", "dhcp", "unifi"])
        self.assertEqual(cur.writes("INSERT INTO telemetry.known_devices"), [(NEW, "phone", "192.168.1.50")])
        self.assertEqual(cur.writes("INSERT INTO health_checks")[-1][1], "ok")

    def test_single_witness_is_held_at_warning(self):
        cur = _Cur()
        _, calls = _cycle(cur, [_client(NEW)])
        self.assertEqual(calls[0][1]["level"], "warning")
        self.assertIn("single witness", calls[0][1]["meta"]["held_because"])

    def test_seed_and_dry_run_modes(self):
        cur = _Cur()
        n, calls = _cycle(cur, [_client(NEW)], seed=True)
        self.assertEqual((n, calls), (1, [])); self.assertEqual(len(cur.writes("INSERT INTO telemetry.known_devices")), 1)
        cur = _Cur()
        n, calls = _cycle(cur, [_client(NEW)], dry_run=True)
        self.assertEqual((n, calls), (1, [])); self.assertEqual(cur.writes("INSERT INTO telemetry.known_devices"), [])

    def test_unifi_infrastructure_and_known_are_quiet(self):
        cur = _Cur()
        n, calls = _cycle(cur, [_client(KNOWN)], arp=[{"mac": "ff:ff:00:00:00:01", "_source": "arp"}],
                          devices=[{"mac": "ff:ff:00:00:00:01"}])
        self.assertEqual((n, calls), (0, []))


class TestFunctional(unittest.TestCase):
    def test_main_once_runs_a_cycle(self):
        cur = _Cur()
        with mock.patch.object(so.unifi, "_unifi_login", return_value=True), mock.patch("psycopg2.connect", return_value=_Conn(cur)), \
             mock.patch.object(so.unifi, "_fetch_clients", return_value=[_client(NEW)]), \
             mock.patch.object(so.unifi, "_fetch_devices", return_value=[]), mock.patch.object(so, "neighbor_clients", return_value=[]), \
             mock.patch.object(so, "notify") as n, mock.patch.dict(sys.modules, {"nova_organ_board": FAKE_BOARD}), \
             mock.patch.object(sys, "argv", ["nova_security_organ.py", "--once"]), redirect_stdout(StringIO()) as out:
            so.main()
        self.assertEqual(n.call_count, 1)
        self.assertIn("cycle done: 1 known devices, 1 new", out.getvalue())

    def test_digest_mode(self):
        cur = _Cur(digest_rows=[(NEW, "phone", "192.168.1.50", datetime(2026, 10, 5, 9, 0))], total=41)
        with mock.patch("psycopg2.connect", return_value=_Conn(cur)), mock.patch.object(so, "notify") as n, \
             mock.patch.object(sys, "argv", ["nova_security_organ.py", "--digest"]), redirect_stdout(StringIO()):
            so.main()
        self.assertEqual(n.call_args[1]["meta"], {"new_24h": 1, "known": 41})
        self.assertIn("1 device(s) first seen in the last 24 h (of 41 known)", n.call_args[0][1])

    def test_login_failure_exits(self):
        with mock.patch.object(so.unifi, "_unifi_login", return_value=False), mock.patch("psycopg2.connect") as c, \
             mock.patch.object(sys, "argv", ["nova_security_organ.py", "--once"]):
            with self.assertRaises(SystemExit):
                so.main()
        self.assertEqual(c.call_count, 0)


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr); self.assertIn("selftest ok", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch("psycopg2.connect", side_effect=AssertionError("main ran on import")):
            self.assertTrue(callable(_load("security_organ_import_probe", SCRIPT).main))


if __name__ == "__main__":
    unittest.main()
