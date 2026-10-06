#!/usr/bin/env python3
"""Tests for bambu_printers.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import ipaddress
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "bambu_printers.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bp = _load("bp", SCRIPT)


def _by_serial(serial):
    """The lookup consumers should do (the serial is the stable identity, the IP floats)."""
    return next(((pid, m) for pid, m in bp.PRINTERS.items() if m["serial"] == serial), None)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token|access[_-]?code)\s*[=:]\s*['\"][A-Za-z0-9+/]{6,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_registry_carries_no_access_code_field(self):
        for pid, meta in bp.PRINTERS.items():
            self.assertEqual(set(meta), {"name", "ip", "serial"}, pid)
        self.assertIn("Keychain", SRC)                       # the access codes are documented as Keychain-only

    def test_no_network_or_shell_capability(self):
        self.assertNotRegex(SRC, r"^\s*(import|from)\s+(subprocess|socket|urllib|requests|paho|psycopg2)\b", "a registry must stay inert")


class TestPerformance(unittest.TestCase):
    def test_serial_lookup_10k_under_bound(self):
        serials = [m["serial"] for m in bp.PRINTERS.values()]
        t0 = time.perf_counter()
        for i in range(10_000):
            _by_serial(serials[i % len(serials)])
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_registry_makes_no_external_calls(self):
        # RETRY GAP: none — bambu_printers.py is a constant registry; there is no HTTP/subprocess/PG call to retry.
        import ast
        calls = [n for n in ast.walk(ast.parse(SRC)) if isinstance(n, ast.Call)]
        self.assertEqual(calls, [])                          # no call expressions at all, only a dict literal

    def test_unknown_serial_fails_open(self):
        self.assertIsNone(_by_serial("NOPE"))


class TestUnit(unittest.TestCase):
    def test_two_printers_with_valid_lan_ips(self):
        self.assertEqual(sorted(bp.PRINTERS), ["P1", "P2"])
        for meta in bp.PRINTERS.values():
            ip = ipaddress.ip_address(meta["ip"])
            self.assertTrue(ip.is_private)
            self.assertTrue(ipaddress.ip_address(meta["ip"]) in ipaddress.ip_network("192.168.1.0/24"))

    def test_serials_are_bambu_shaped_and_unique(self):
        serials = [m["serial"] for m in bp.PRINTERS.values()]
        self.assertEqual(len(serials), len(set(serials)))
        for s in serials:
            self.assertRegex(s, r"^00M09[A-Z0-9]{10}$")

    def test_ips_are_unique(self):
        ips = [m["ip"] for m in bp.PRINTERS.values()]
        self.assertEqual(len(ips), len(set(ips)))


class TestIntegration(unittest.TestCase):
    def test_consumers_import_the_registry_instead_of_redefining_it(self):
        for consumer in ("nova_bambu_watch.py", "nova_bambu_status.py"):
            src = (SCRIPTS / consumer).read_text()
            self.assertIn("bambu_printers", src, consumer)
            self.assertNotIn("00M09A362800690", src, f"{consumer} re-declares a serial instead of importing")

    def test_keychain_item_name_derives_from_serial(self):
        self.assertIn("nova-bambu-<serial>", SRC)
        for meta in bp.PRINTERS.values():
            self.assertRegex(f"nova-bambu-{meta['serial']}", r"^nova-bambu-00M09")


class TestFunctional(unittest.TestCase):
    def test_golden_path_lookup_by_serial_returns_name_and_ip(self):
        pid, meta = _by_serial("00M09A362800690")
        self.assertEqual((pid, meta["name"], meta["ip"]), ("P1", "Printer 1", "192.168.1.179"))

    def test_error_path_registry_is_not_mutated_by_consumers(self):
        snapshot = {k: dict(v) for k, v in bp.PRINTERS.items()}
        with self.assertRaises(KeyError):
            bp.PRINTERS["P3"]["ip"]
        self.assertEqual(bp.PRINTERS, snapshot)


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_exits_zero(self):
        r = subprocess.run([sys.executable, "-c", "import bambu_printers"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertNotIn("__main__", SRC)                    # a registry has no entry point


if __name__ == "__main__":
    unittest.main()
