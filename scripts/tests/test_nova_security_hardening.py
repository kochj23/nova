#!/usr/bin/env python3
"""Tests for nova_security_hardening.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import ast
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_security_hardening.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sh = _load("sh", SCRIPT)


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.body).encode()


def _guarded(fn, *a, urlopen=None, **k):
    """Run `fn` with the memory server mocked AND every host-changing primitive armed to record calls.
    Returns (result, stdout, {primitive: call_count})."""
    primitives = {}
    out = io.StringIO()
    with patch.object(sh.urllib.request, "urlopen", urlopen or MagicMock(return_value=_Resp({"id": 1}))), \
         patch.object(sh.subprocess, "run") as run, patch.object(sh.subprocess, "Popen") as popen, \
         patch.object(sh.subprocess, "check_output") as co, patch.object(sh.subprocess, "call") as call, \
         patch("os.system") as system, redirect_stdout(out):
        result = fn(*a, **k)
        primitives = {"run": run.call_count, "Popen": popen.call_count, "check_output": co.call_count,
                      "call": call.call_count, "os.system": system.call_count}
    return result, out.getvalue(), primitives


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_no_host_changing_call_exists_in_the_source(self):
        # subprocess is imported but the module never calls it: the hardening steps are documented, not executed
        calls = {ast.unparse(n.func) for n in ast.walk(ast.parse(SRC)) if isinstance(n, ast.Call)}
        self.assertFalse({c for c in calls if c.startswith(("subprocess.", "os.system", "os.popen"))}, calls)
        self.assertNotIn("shell=True", SRC)

    def test_every_tier_and_scan_runs_without_touching_the_host(self):
        for fn in (sh.hardening_tier_1, sh.hardening_tier_2, sh.run_nmap_scan, sh.main):
            _, _, prim = _guarded(fn)
            self.assertEqual(set(prim.values()), {0}, f"{fn.__name__} invoked a host primitive: {prim}")

    def test_memory_server_is_a_fleet_internal_http_endpoint(self):
        self.assertTrue(sh.MEMORY_URL.startswith("http://memory-server.digitalnoise.net:18790"))


class TestPerformance(unittest.TestCase):
    def test_log_10k_lines_under_bound(self):
        t0 = time.perf_counter()
        with redirect_stdout(io.StringIO()):
            for i in range(10_000):
                sh.log(f"line {i}")
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_nmap_report_assembly_bounded(self):
        t0 = time.perf_counter()
        for _ in range(200):
            _guarded(sh.run_nmap_scan)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_remember_fails_open_on_a_dead_memory_server(self):
        # RETRY GAP: remember()/urlopen — one attempt; any error returns None and nothing escapes
        u = MagicMock(side_effect=urllib.error.URLError("down"))
        rid, _, _ = _guarded(sh.remember, "t", urlopen=u)
        self.assertIsNone(rid)
        self.assertEqual(u.call_count, 1)

    def test_tiers_survive_a_dead_memory_server(self):
        u = MagicMock(side_effect=ConnectionRefusedError())
        for fn in (sh.hardening_tier_1, sh.hardening_tier_2):
            res, out, _ = _guarded(fn, urlopen=u)
            self.assertIsNone(res)
            self.assertIn("Hardening Tier", out)
        report, _, _ = _guarded(sh.run_nmap_scan, urlopen=u)
        self.assertEqual(report["status"], "Network secure")


class TestUnit(unittest.TestCase):
    def test_remember_returns_the_server_id_and_posts_json(self):
        u = MagicMock(return_value=_Resp({"id": 42}))
        rid, _, _ = _guarded(sh.remember, "hello", urlopen=u, source="security")
        self.assertEqual(rid, 42)
        req = u.call_args[0][0]
        self.assertEqual(req.full_url, f"{sh.MEMORY_URL}/remember")
        self.assertEqual(json.loads(req.data), {"text": "hello", "source": "security"})
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(u.call_args[1]["timeout"], 5)

    def test_remember_handles_a_body_without_id(self):
        rid, _, _ = _guarded(sh.remember, "x", urlopen=MagicMock(return_value=_Resp({})))
        self.assertIsNone(rid)

    def test_log_prefixes_a_clock(self):
        _, out, _ = _guarded(sh.log, "hi")
        self.assertRegex(out, r"^\[\d\d:\d\d:\d\d\] hi\n$")

    def test_nmap_report_shape(self):
        report, _, _ = _guarded(sh.run_nmap_scan)
        self.assertEqual(set(report), {"timestamp", "total_devices", "new_devices", "anomalies", "status"})
        self.assertEqual((report["new_devices"], report["anomalies"]), (0, 0))


class TestIntegration(unittest.TestCase):
    def test_each_step_remembers_with_the_security_source(self):
        u = MagicMock(return_value=_Resp({"id": 1}))
        _guarded(sh.hardening_tier_1, urlopen=u); _guarded(sh.hardening_tier_2, urlopen=u); _guarded(sh.run_nmap_scan, urlopen=u)
        payloads = [json.loads(c[0][0].data) for c in u.call_args_list]
        self.assertEqual([p["source"] for p in payloads], ["security"] * 3)
        self.assertEqual([p["text"].split(":")[0] for p in payloads], ["Tier 1 hardening", "Tier 2 hardening", "Network scan"])

    def test_memory_port_matches_the_fleet_memory_server(self):
        cfg = (SCRIPTS / "nova_config.py").read_text()
        self.assertIn("18790", cfg)
        self.assertIn(":18790", sh.MEMORY_URL)


class TestFunctional(unittest.TestCase):
    def test_main_golden_path_only_announces_readiness(self):
        u = MagicMock(return_value=_Resp({"id": 1}))
        res, out, prim = _guarded(sh.main, urlopen=u)
        self.assertIsNone(res)
        self.assertEqual(out.count("ready"), 3)
        self.assertEqual(u.call_count, 0)                         # main() itself never even reaches the memory server
        self.assertEqual(set(prim.values()), {0})

    def test_error_path_nmap_scan_with_dead_memory_still_returns_a_report(self):
        report, out, prim = _guarded(sh.run_nmap_scan, urlopen=MagicMock(side_effect=OSError("boom")))
        self.assertEqual(report["total_devices"], 288)
        self.assertIn("Running weekly NMAP scan", out)
        self.assertEqual(set(prim.values()), {0})


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_security_hardening"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)

    def test_script_entry_only_prints_readiness(self):
        # main() makes no network or subprocess call (proved above), so the real entry point is safe to smoke
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Threat reporting system ready", r.stdout)


if __name__ == "__main__":
    unittest.main()
