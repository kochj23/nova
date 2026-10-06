#!/usr/bin/env python3
"""Tests for nova_security_surface_monitor.py — the 7 house categories (Security, Performance, Retry,
Unit, Integration, Functional, Frame). Written by Jordan Koch (via Claude).

crt.sh, dig, nmap and the breaking-news alert subprocess are all mocked; state/log go to a tempdir."""
import importlib.util
import io
import ipaddress
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
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sm = _load("nova_security_surface_monitor_t", SCRIPTS / "nova_security_surface_monitor.py")
_TMP = Path(tempfile.mkdtemp())
sm.LOG_FILE = _TMP / "surface.log"
sm.STATE_FILE = _TMP / "state.json"
sm.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=RuntimeError("unmocked subprocess")),
                                      TimeoutExpired=subprocess.TimeoutExpired,
                                      CalledProcessError=subprocess.CalledProcessError)
SRC = (SCRIPTS / "nova_security_surface_monitor.py").read_text()


def _recent(days=1):
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")


class _Resp:
    def __init__(self, obj):
        self.obj = obj

    def read(self):
        return json.dumps(self.obj).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _dig(answers):
    """subprocess.run stand-in answering `dig +short <rtype> <domain>`."""
    def run(cmd, **k):
        if cmd[0] == "dig":
            out = answers.get((cmd[3], cmd[2]), "")
            return types.SimpleNamespace(returncode=0, stdout=out)
        raise AssertionError(f"unexpected command {cmd}")
    return run


def _q():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_unexpected_ca_alerts_letsencrypt_does_not(self):
        certs = [{"id": 1, "common_name": "nova.digitalnoise.net", "issuer_name": "C=US, O=Let's Encrypt, CN=R11",
                  "not_before": _recent()},
                 {"id": 2, "common_name": "evil.digitalnoise.net", "issuer_name": "C=XX, O=Shady CA",
                  "not_before": _recent()}]
        st = {"certs_seen": []}
        with patch.object(sm.urllib.request, "urlopen", return_value=_Resp(certs)), _q():
            alerts = sm.check_cert_transparency(st)
        self.assertEqual(len(alerts), 1)                     # one per domain with the shady cert
        self.assertIn("Shady CA", alerts[0][1])
        self.assertNotIn("Let's Encrypt", alerts[0][1])

    def test_dns_moving_outside_cloudflare_alerts(self):
        st = {"dns_records": {"digitalnoise.net": {"A": ["104.16.1.1"]}}}
        with patch.object(sm.subprocess, "run", side_effect=_dig({("digitalnoise.net", "A"): "203.0.113.9\n"})), _q():
            alerts = sm.check_dns_records(st)
        self.assertTrue(any("A record" in t for t, _ in alerts))


class TestPerformance(unittest.TestCase):
    def test_cloudflare_check_10k_ips(self):
        ips = [str(ipaddress.ip_address("104.16.0.0") + i) for i in range(10_000)]
        t0 = time.perf_counter()
        self.assertTrue(sm._all_cloudflare(ips))
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_crtsh_failure_skips_domain(self):
        # RETRY GAP: check_cert_transparency/urlopen — one attempt per domain, logged, no alert
        with patch.object(sm.urllib.request, "urlopen", side_effect=OSError("crt.sh 502")) as uo, _q():
            self.assertEqual(sm.check_cert_transparency({}), [])
        self.assertEqual(uo.call_count, len(sm.MONITORED_DOMAINS))
        self.assertIn("crt.sh check failed", sm.LOG_FILE.read_text())

    def test_nmap_missing_and_timeout_fail_open(self):
        with patch.object(sm.subprocess, "run", side_effect=FileNotFoundError), _q():
            self.assertEqual(sm.check_port_exposure({}), [])

        def run(cmd, **k):
            if cmd[0] == "which":
                return types.SimpleNamespace(returncode=0)
            raise subprocess.TimeoutExpired(cmd, 60)
        with patch.object(sm.subprocess, "run", side_effect=run), _q():
            self.assertEqual(sm.check_port_exposure({}), [])

    def test_alert_subprocess_failure_swallowed(self):
        with patch.object(sm.subprocess, "run", side_effect=OSError("no python")), _q():
            sm.fire_alert("t", "d")
        self.assertIn("Alert fire failed", sm.LOG_FILE.read_text())


class TestUnit(unittest.TestCase):
    def test_all_cloudflare_edges(self):
        self.assertFalse(sm._all_cloudflare([]))
        self.assertFalse(sm._all_cloudflare(["not-an-ip"]))
        self.assertTrue(sm._all_cloudflare(["2606:4700::1", "172.67.1.1"]))
        self.assertFalse(sm._all_cloudflare(["172.67.1.1", "8.8.8.8"]))

    def test_old_and_seen_certs_ignored(self):
        certs = [{"id": 5, "issuer_name": "Shady", "not_before": _recent(30)},
                 {"id": 6, "issuer_name": "Shady", "not_before": _recent()},
                 {"id": 7, "issuer_name": "Shady", "not_before": "garbage"}]
        with patch.object(sm.urllib.request, "urlopen", return_value=_Resp(certs)), _q():
            self.assertEqual(sm.check_cert_transparency({"certs_seen": ["6"]}), [])

    def test_state_roundtrip_and_corrupt(self):
        sm.save_state({"certs_seen": ["1"], "last_check": "x"})
        self.assertEqual(sm.load_state()["certs_seen"], ["1"])
        sm.STATE_FILE.write_text("{bad")
        self.assertEqual(sm.load_state(), {"certs_seen": [], "last_check": None})


class TestIntegration(unittest.TestCase):
    def test_cloudflare_rotation_benign_and_removed_record_alerts(self):
        st = {"dns_records": {"digitalnoise.net": {"A": ["104.16.1.1"], "MX": ["10 mx.example."]}}}
        with patch.object(sm.subprocess, "run", side_effect=_dig({("digitalnoise.net", "A"): "104.16.9.9\n"})), _q():
            alerts = sm.check_dns_records(st)
        self.assertEqual([t for t, _ in alerts], ["DNS record removed: digitalnoise.net MX"])
        self.assertEqual(st["dns_records"]["digitalnoise.net"]["A"], ["104.16.9.9"])

    def test_alert_routes_to_journal_breaking(self):
        with patch.object(sm.subprocess, "run") as run, _q():
            sm.fire_alert("Trigger", "Details")
        argv = run.call_args[0][0]
        self.assertEqual(Path(argv[1]).name, "nova_journal_security.py")
        self.assertEqual(argv[2:], ["breaking", "Trigger", "Details"])


class TestFunctional(unittest.TestCase):
    def test_run_fires_at_most_three_alerts_and_saves_state(self):
        alerts = [(f"t{i}", "d") for i in range(5)]
        with patch.object(sm, "check_cert_transparency", return_value=alerts), \
                patch.object(sm, "check_dns_records", return_value=[]), \
                patch.object(sm, "check_port_exposure", return_value=[]), \
                patch.object(sm, "fire_alert") as fa, patch.object(sm.time, "sleep"), _q():
            sm.run()
        self.assertEqual(fa.call_count, 3)
        self.assertIsNotNone(json.loads(sm.STATE_FILE.read_text())["last_check"])

    def test_unexpected_port_detected(self):
        out = "53/tcp open domain\n443/tcp open https\n3389/tcp open ms-wbt-server\n"

        def run(cmd, **k):
            return types.SimpleNamespace(returncode=0, stdout=out)
        with patch.object(sm.subprocess, "run", side_effect=run), _q():
            alerts = sm.check_port_exposure({})
        self.assertEqual(len(alerts), 1)
        self.assertIn("'3389/tcp'", alerts[0][1].split("Unexpected:")[1])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_security_surface_monitor"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
