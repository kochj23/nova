#!/usr/bin/env python3
"""Tests for nova_network_sentinel.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
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
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_network_sentinel.py"
SRC = SCRIPT.read_text()


def _stub_modules():
    nn = types.ModuleType("nova_notify")
    nn.notify = MagicMock(return_value=True)
    return {"nova_notify": nn}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    # The script execs nova_config.py by file path at import (Keychain + resolver side effects); make that
    # loader return None so the try/except leaves `nova_config = None`, which is a path the script supports.
    with patch.dict(sys.modules, _stub_modules()), patch("importlib.util.spec_from_file_location", return_value=None):
        spec.loader.exec_module(mod)
    return mod


ns = _load("network_sentinel_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="sentinel-test-"))
ns.STATE_DIR = TMP / "state"
ns.BASELINE_FILE = ns.STATE_DIR / "network_baseline.json"
ns.LATEST_FILE = ns.STATE_DIR / "network_scan_latest.json"
ns.LOG_FILE = TMP / "logs" / "network_sentinel.log"
ns.JOURNAL_DIR = TMP / "journal"


def _xml(hosts):
    """hosts: list of (ip, hostname, [(port, state, service, product, version)], up)."""
    out = ['<?xml version="1.0"?><nmaprun>']
    for ip, hn, ports, up in hosts:
        out.append(f'<host><status state="{"up" if up else "down"}"/><address addr="{ip}" addrtype="ipv4"/>')
        out.append('<address addr="AA:BB:CC:DD:EE:01" addrtype="mac" vendor="Ubiquiti"/>')
        if hn:
            out.append(f'<hostnames><hostname name="{hn}" type="PTR"/></hostnames>')
        out.append("<ports>")
        for p, st, svc, prod, ver in ports:
            out.append(f'<port protocol="tcp" portid="{p}"><state state="{st}"/>'
                       f'<service name="{svc}" product="{prod}" version="{ver}"/></port>')
        out.append("</ports></host>")
    out.append("</nmaprun>")
    return "".join(out)


SAMPLE_XML = _xml([
    ("192.168.1.2", "nova-core", [(22, "open", "ssh", "OpenSSH", "9.6"), (5432, "open", "postgresql", "PostgreSQL", "16")], True),
    ("192.168.1.40", "", [(23, "open", "telnet", "", ""), (80, "closed", "http", "", "")], True),
    ("192.168.1.99", "ghost", [(22, "open", "ssh", "", "")], False),
    ("192.168.1.253", "honeypot", [(21, "open", "ftp", "", ""), (23, "open", "telnet", "", "")], True),
])


def _host(ports, hostname="h", vendor="v"):
    return {"hostname": hostname, "mac": "", "vendor": vendor, "scanned_at": "t",
            "ports": {f"{p}/tcp": {"service": "s", "version": ""} for p in ports}}


def _nmap_ok(xml=SAMPLE_XML):
    return MagicMock(return_value=types.SimpleNamespace(returncode=0, stdout=xml, stderr=""))


class _Cur:
    def __init__(self):
        self.sql, self.params, self.closed = [], [], False

    def execute(self, sql, params=None):
        self.sql.append(" ".join(sql.split())); self.params.append(params)

    def close(self):
        self.closed = True


def _pg(cur):
    return MagicMock(return_value=types.SimpleNamespace(cursor=lambda: cur, close=MagicMock(), autocommit=False))


def _clean():
    import shutil
    shutil.rmtree(TMP, ignore_errors=True)
    ns.notify = MagicMock(return_value=True)


def _run_main(argv, nmap=None, cur=None):
    cur = cur or _Cur()
    with patch.object(ns.subprocess, "run", nmap or _nmap_ok()), patch("psycopg2.connect", _pg(cur)), \
         patch.object(sys, "argv", ["nova_network_sentinel.py", *argv]), redirect_stdout(io.StringIO()) as out:
        ns.main()
    return cur, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_and_nmap_has_no_shell(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"shared_observations"})
        self.assertNotIn("shell=True", SRC)
        self.assertIn('"--open", "-oX", "-", SUBNET', SRC)              # fixed argv, no user-controlled pieces

    def test_hostile_hostname_stays_data(self):
        evil = "x&quot;); DROP TABLE shared_observations; --"
        with redirect_stdout(io.StringIO()):
            hosts = ns.parse_nmap_xml(_xml([("192.168.1.50", evil, [(445, "open", "smb", "", "")], True)]))
        findings = ns.compare_baseline(hosts, {})
        cur = _Cur()
        with patch("psycopg2.connect", _pg(cur)), redirect_stdout(io.StringIO()):
            ns.record_to_pg(findings, 1)
        self.assertNotIn("DROP", cur.sql[0])
        self.assertIn("DROP", cur.params[0][2])

    def test_honeypot_is_never_a_finding(self):
        hp = {ns.HONEYPOT_IP: _host([21, 23, 1433], hostname="honeypot")}
        f = ns.compare_baseline(hp, {})
        self.assertEqual(f, {"new_hosts": [], "removed_hosts": [], "new_ports": [], "removed_ports": [], "risky_services": []})
        self.assertEqual(ns.compare_baseline({}, hp)["removed_hosts"], [])


class TestPerformance(unittest.TestCase):
    def test_compare_10k_hosts_fast(self):
        cur = {f"10.0.{i // 250}.{i % 250}": _host([22, 80, 445 if i % 7 else 6379]) for i in range(10_000)}
        base = {ip: _host([22, 80]) for ip in list(cur)[:9_000]}
        t0 = time.perf_counter()
        f = ns.compare_baseline(cur, base)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(f["new_hosts"]), 1_000)
        self.assertEqual(len(f["new_ports"]), 9_000)
        self.assertEqual(len(f["risky_services"]), 10_000)

    def test_parse_2k_host_xml_fast(self):
        xml = _xml([(f"10.1.{i // 250}.{i % 250}", f"h{i}", [(22, "open", "ssh", "OpenSSH", "9")], True) for i in range(2_000)])
        t0 = time.perf_counter()
        with redirect_stdout(io.StringIO()):
            hosts = ns.parse_nmap_xml(xml)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(hosts), 2_000)


class TestRetry(unittest.TestCase):
    def test_run_scan_fails_open_on_every_nmap_failure(self):
        # RETRY GAP: run_scan — nmap is invoked once; timeout / missing binary / non-zero exit all return {} (no alert storm)
        _clean()
        for err in (subprocess.TimeoutExpired("nmap", 900), FileNotFoundError("nmap")):
            with patch.object(ns.subprocess, "run", side_effect=err), redirect_stdout(io.StringIO()):
                self.assertEqual(ns.run_scan(), {})
        bad = MagicMock(return_value=types.SimpleNamespace(returncode=1, stdout="", stderr="boom"))
        with patch.object(ns.subprocess, "run", bad), redirect_stdout(io.StringIO()):
            self.assertEqual(ns.run_scan(), {})
        self.assertEqual(bad.call_count, 1)
        self.assertIn("nmap error: boom", ns.LOG_FILE.read_text())

    def test_notify_and_pg_failures_are_swallowed(self):
        # RETRY GAP: notify_results/record_to_pg — one attempt each; failures are logged, the run completes
        _clean()
        ns.notify = MagicMock(side_effect=OSError("bus down"))
        with redirect_stdout(io.StringIO()):
            ns.notify_results({"risky_services": []}, 3)
        with patch("psycopg2.connect", side_effect=OSError("pg down")), redirect_stdout(io.StringIO()):
            ns.record_to_pg({"risky_services": []}, 3)
        text = ns.LOG_FILE.read_text()
        self.assertIn("Notification failed: bus down", text); self.assertIn("PG write failed: pg down", text)

    def test_bad_xml_returns_empty(self):
        # RETRY GAP: parse_nmap_xml — malformed output yields {} so main() aborts instead of diffing garbage
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ns.parse_nmap_xml("<nmaprun><host>"), {})


class TestUnit(unittest.TestCase):
    def test_parse_nmap_xml_skips_down_hosts_and_closed_ports(self):
        with redirect_stdout(io.StringIO()):
            hosts = ns.parse_nmap_xml(SAMPLE_XML)
        self.assertEqual(set(hosts), {"192.168.1.2", "192.168.1.40", "192.168.1.253"})
        core = hosts["192.168.1.2"]
        self.assertEqual(core["hostname"], "nova-core"); self.assertEqual(core["vendor"], "Ubiquiti")
        self.assertEqual(core["ports"]["5432/tcp"], {"service": "postgresql", "version": "PostgreSQL 16"})
        self.assertEqual(list(hosts["192.168.1.40"]["ports"]), ["23/tcp"])
        self.assertEqual(hosts["192.168.1.40"]["ports"]["23/tcp"]["version"], "")

    def test_compare_baseline_categories(self):
        cur = {"192.168.1.2": _host([22, 5432]), "192.168.1.9": _host([23], hostname="", vendor="Acme")}
        base = {"192.168.1.2": _host([22, 80]), "192.168.1.5": _host([22], hostname="gone")}
        f = ns.compare_baseline(cur, base)
        self.assertEqual([h["ip"] for h in f["new_hosts"]], ["192.168.1.9"])
        self.assertEqual(f["removed_hosts"], [{"ip": "192.168.1.5", "hostname": "gone"}])
        self.assertEqual(f["new_ports"], [{"ip": "192.168.1.2", "hostname": "h", "port": "5432/tcp", "service": "s"}])
        self.assertEqual(f["removed_ports"], [{"ip": "192.168.1.2", "hostname": "h", "port": "80/tcp"}])
        self.assertEqual(sorted((r["ip"], r["service_name"], r["severity"]) for r in f["risky_services"]),
                         [("192.168.1.2", "PostgreSQL", "high"), ("192.168.1.9", "Telnet", "critical")])

    def test_journal_tone_ladder(self):
        empty = {"new_hosts": [], "new_ports": [], "risky_services": []}
        self.assertIn("posture: GREEN POSTURE", ns.generate_journal_entry(empty, 5))
        new = dict(empty, new_hosts=[{"ip": "1.2.3.4", "hostname": "", "vendor": "", "ports": ["22/tcp"]}])
        self.assertIn("posture: YELLOW POSTURE", ns.generate_journal_entry(new, 5))
        high = dict(empty, risky_services=[{"ip": "a", "hostname": "", "port": 445, "service_name": "SMB", "severity": "high", "description": "d"}])
        self.assertIn("posture: AMBER POSTURE", ns.generate_journal_entry(high, 5))
        crit = dict(empty, risky_services=[{"ip": "a", "hostname": "", "port": 23, "service_name": "Telnet", "severity": "critical", "description": "d"}])
        e = ns.generate_journal_entry(crit, 5)
        self.assertIn("posture: RED POSTURE", e); self.assertIn("IMMEDIATE ACTION REQUIRED", e)
        self.assertIn("- **a** (unnamed) — Telnet (port 23): d", e)

    def test_load_baseline_missing_is_empty(self):
        _clean()
        self.assertEqual(ns.load_baseline(), {})
        with redirect_stdout(io.StringIO()):
            ns.save_baseline({"192.168.1.2": _host([22])})
        self.assertEqual(list(ns.load_baseline()), ["192.168.1.2"])


class TestIntegration(unittest.TestCase):
    def test_notify_is_the_shared_bus_with_security_category(self):
        self.assertIn("from nova_notify import notify", SRC)
        _clean()
        f = {"new_hosts": [{"ip": "192.168.1.9", "hostname": "", "vendor": "Acme", "ports": []}], "new_ports": [],
             "risky_services": [{"ip": "192.168.1.9", "hostname": "", "port": 23, "service_name": "Telnet", "severity": "critical", "description": "d"}]}
        with redirect_stdout(io.StringIO()):
            ns.notify_results(f, 7)
        kw = ns.notify.call_args[1]
        self.assertEqual(ns.notify.call_args[0][0], "Network Sentinel — Daily Scan (🔴 RED)")
        self.assertEqual((kw["level"], kw["category"], kw["dedup_key"]), ("critical", "security", "network-sentinel-daily"))
        self.assertEqual(kw["meta"], {"host_count": 7, "critical": 1, "high": 0, "new_hosts": 1, "new_ports": 0})
        self.assertIn("• 192.168.1.9 (Acme)", kw["body"]); self.assertIn("🚨 1 CRITICAL:", kw["body"])

    def test_quiet_scan_is_info_with_no_body_drift(self):
        _clean()
        with redirect_stdout(io.StringIO()):
            ns.notify_results({"new_hosts": [], "new_ports": [], "risky_services": []}, 9)
        kw = ns.notify.call_args[1]
        self.assertEqual(kw["level"], "info"); self.assertIn("No drift from baseline", kw["body"])

    def test_parse_then_compare_then_record_shape(self):
        cur = _Cur()
        with redirect_stdout(io.StringIO()):
            hosts = ns.parse_nmap_xml(SAMPLE_XML)
            f = ns.compare_baseline(hosts, {})
            with patch("psycopg2.connect", _pg(cur)) as pg:
                ns.record_to_pg(f, len(hosts))
        pg.assert_called_once_with("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")
        self.assertIn("'nova', 'security', 'network-sentinel'", cur.sql[0])
        obs, sev, meta = cur.params[0]
        self.assertEqual(obs, "Network scan: 3 hosts, 2 new, 0 new ports, 1 critical exposures")
        self.assertEqual(sev, "critical")
        self.assertEqual(json.loads(meta)["risky_services"][0]["ip"], "192.168.1.2")
        self.assertTrue(cur.closed)


class TestFunctional(unittest.TestCase):
    def test_golden_path_first_run_sets_baseline_and_reports(self):
        _clean()
        cur, out = _run_main([])
        self.assertTrue(ns.BASELINE_FILE.exists() and ns.LATEST_FILE.exists())
        self.assertEqual(set(json.loads(ns.LATEST_FILE.read_text())), {"192.168.1.2", "192.168.1.40", "192.168.1.253"})
        journal = list(ns.JOURNAL_DIR.glob("*-network-posture-assessment.md"))
        self.assertEqual(len(journal), 1)
        self.assertIn("posture: RED POSTURE", journal[0].read_text())           # .40 telnet is critical even on first run
        self.assertEqual(ns.notify.call_args[1]["level"], "critical")
        self.assertEqual(len(cur.sql), 1)
        self.assertIn("Assessment complete: 2 risky services, 0 new hosts", out)

    def test_second_run_against_baseline_flags_drift(self):
        _clean()
        with redirect_stdout(io.StringIO()):
            ns.save_baseline({"192.168.1.2": _host([22])})
        cur, out = _run_main(["--no-notify"])
        ns.notify.assert_not_called()
        meta = json.loads(cur.params[0][2])
        self.assertEqual([h["ip"] for h in meta["new_hosts"]], ["192.168.1.40"])
        self.assertEqual(meta["new_ports"][0]["port"], "5432/tcp")

    def test_scan_only_and_set_baseline_short_circuit(self):
        _clean()
        cur, out = _run_main(["--scan-only"])
        self.assertIn("192.168.1.2      nova-core", out); self.assertFalse(ns.BASELINE_FILE.exists()); self.assertEqual(cur.sql, [])
        cur, out = _run_main(["--set-baseline"])
        self.assertTrue(ns.BASELINE_FILE.exists()); self.assertEqual(cur.sql, []); ns.notify.assert_not_called()

    def test_failed_scan_aborts_before_any_write(self):
        _clean()
        bad = MagicMock(return_value=types.SimpleNamespace(returncode=1, stdout="", stderr="no route"))
        cur, out = _run_main([], nmap=bad)
        self.assertIn("Scan returned no data — aborting", out)
        self.assertFalse(ns.LATEST_FILE.exists()); self.assertEqual(cur.sql, []); ns.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_without_scanning(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        code = ("import sys, types, importlib.util\n"
                "nn = types.ModuleType('nova_notify'); nn.notify = lambda *a, **k: True\n"
                "sys.modules['nova_notify'] = nn\n"
                "importlib.util.spec_from_file_location = lambda *a, **k: None\n"
                "import subprocess\n"
                "subprocess.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError('nmap ran'))\n"
                "sys.argv = ['nova_network_sentinel.py', '--help']\n"
                "import runpy; runpy.run_path('nova_network_sentinel.py', run_name='__main__')\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--set-baseline", r.stdout); self.assertIn("--no-notify", r.stdout)


if __name__ == "__main__":
    unittest.main()
