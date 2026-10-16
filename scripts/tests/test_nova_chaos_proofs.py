#!/usr/bin/env python3
"""Tests for nova_chaos_proofs.py (Phase 0: prove never-red monitors can go red, sandboxed) —
7 house categories. Written by Jordan Koch (via Claude)."""
import importlib.util
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("cp_under_test", SCRIPTS / "nova_chaos_proofs.py")
CP = importlib.util.module_from_spec(spec)
spec.loader.exec_module(CP)
SRC = (SCRIPTS / "nova_chaos_proofs.py").read_text()


class Security(unittest.TestCase):
    def test_no_hardcoded_secrets(self):
        self.assertNotRegex(SRC, r"(?i)(password|api_key)\s*=\s*['\"][^'\"]+['\"]")

    def test_health_urls_rewritten_to_loopback(self):
        self.assertTrue(CP.rewrite_health_url("http://example.org:9/x", 5).startswith("http://127.0.0.1:5"))


class Performance(unittest.TestCase):
    def test_pure_helpers_fast(self):
        t = time.monotonic()
        for _ in range(5000):
            CP.parse_series("health:a@b/c"); CP.sli_kind("FROM telemetry.events WHERE x")
        self.assertLess(time.monotonic() - t, 2.0)


class Retry(unittest.TestCase):
    def test_closed_port_has_no_listener(self):
        import socket
        p = CP.closed_port()
        s = socket.socket(); s.settimeout(0.5)
        self.assertNotEqual(s.connect_ex(("127.0.0.1", p)), 0)
        s.close()


class Unit(unittest.TestCase):
    def test_parse_and_method(self):
        self.assertEqual(CP.parse_series("slo:chat"), {"kind": "slo", "name": "chat"})
        self.assertEqual(CP.method_for("slo:anything")[0], "slo_page")
        self.assertIsNone(CP.method_for("probe:x"))

    def test_markers_are_backdated_and_labelled(self):
        self.assertIn("interval '1 day'", CP.marker("health:a@b/c", 1, "x")[0])
        self.assertIn("'drill'", CP.marker("slo:chat", 1, "x")[0])
        sql, prm = CP.marker("task:slo", 1, "x")
        self.assertIn("'failure'", sql)
        self.assertTrue(prm[0].startswith("chaos-proof-"))


class Integration(unittest.TestCase):
    def test_sli_kind_maps_real_query_shapes(self):
        self.assertEqual(CP.sli_kind("FROM telemetry.probe_results WHERE probe = 'postgres'"), ("probe", "postgres"))
        self.assertEqual(CP.sli_kind("FROM scheduler_runs r WHERE r.task_id = 'morning_brief'")[0], "bluf")


class Functional(unittest.TestCase):
    def test_selftest_passes(self):
        self.assertEqual(CP.selftest(), 0)


class Frame(unittest.TestCase):
    def test_cli_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_chaos_proofs.py"), "--help"],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
