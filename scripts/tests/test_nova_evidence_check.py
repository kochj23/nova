#!/usr/bin/env python3
"""Tests for nova_evidence_check.py (evidence before belief, incident #3675), one per house
category: functional, security, privacy, performance, regression, integration, docs.
Written by Jordan Koch (via Claude)."""
import importlib.util
import re
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ec = _load("evc", SCRIPTS / "nova_evidence_check.py")
SRC = (SCRIPTS / "nova_evidence_check.py").read_text()
APPLE = ("client @0x1 192.168.1.43#54948 (attester.gateway.fe2.apple-dns.net): query: "
         "attester.gateway.fe2.apple-dns.net IN HTTPS + (192.168.1.138)")
C2 = "client @0x1 192.168.1.9#5555 (beacon-c2-check.xyz): query: beacon-c2-check.xyz IN A + (192.168.1.138)"


class _Cur:
    """A cursor stub that answers the evidence queries in order and records writes."""
    def __init__(self, answers):
        self.answers = list(answers); self.sql = []

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if sql.startswith("SET LOCAL"):
            raise RuntimeError("autocommit")  # the real cursor does this too; must be swallowed

    def fetchone(self):
        return self.answers.pop(0)

    def fetchall(self):
        return self.answers.pop(0)


class TestFunctional(unittest.TestCase):
    def test_selftest_passes(self):
        ec.demo()

    def test_check_end_to_end_detector_fault_files_one_bug(self):
        cur = _Cur([
            [("2026-10-05", "nova-core", "named", APPLE)],          # raw rows
            (1380, 15, 16),                                          # history counts
            (1, 0),                                                  # false positives, real
            [("192.168.1.43", "Amys-iPhone (transient)")],           # who
            None,                                                    # no bug in the last 7 days
            (77,),                                                   # inserted queue id
        ])
        b = ec.check(cur, title="Suspicious DNS", category="suspicious_dns", source="nova_syslog_server.py",
                     dedup_key="syslog-threat-suspicious_dns-192.168.1.2")
        self.assertEqual(b["verdict"], "detector_fault")
        self.assertEqual(b["queue_id"], 77)
        self.assertIn("Amys-iPhone", b["text"])
        self.assertIn("CHRONIC", b["text"])
        self.assertTrue(any("INSERT INTO claude_queue" in q for q in cur.sql))

    def test_supported_claim_gets_playbook_with_real_client(self):
        cur = _Cur([[("t", "nova-core", "named", C2)], (3, 2, 1), (0, 1), [("192.168.1.9", "Rack-Pi (infra)")]])
        b = ec.check(cur, title="x", category="suspicious_dns", source="nova_syslog_server.py",
                     dedup_key="syslog-threat-suspicious_dns-192.168.1.2")
        self.assertEqual(b["verdict"], "supported")
        self.assertIn("beacon-c2-check.xyz", b["advice"])
        self.assertIn("Rack-Pi", b["advice"])
        self.assertIsNone(b["queue_id"])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_only_write_is_the_queue_row(self):
        writes = [m.group(0) for m in re.finditer(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+[\w.]+", SRC)]
        self.assertEqual(writes, ["INSERT INTO claude_queue"])

    def test_sql_is_parameterized(self):
        # no f-string SQL: every execute carries %s placeholders, never interpolated values
        for m in re.finditer(r'oc\.execute\(\s*f"', SRC):
            self.fail(f"f-string SQL at {m.start()}")


class TestPrivacy(unittest.TestCase):
    def test_render_truncates_raw_lines(self):
        b = {"verdict": "supported", "note": "n", "raw": [{"message": "x" * 2000, "app": "a", "host": "h"}],
             "who": {}, "history": {}, "advice": ""}
        self.assertLess(len(ec.render(b)), 400)


class TestPerformance(unittest.TestCase):
    def test_rechecks_fast_on_10k_lines(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            ec.recheck_suspicious_dns(APPLE if i % 2 else C2); ec.recheck_ips("Nova fleet DNS sync")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRegression(unittest.TestCase):
    def test_incident_3675_line_is_a_detector_fault(self):
        self.assertEqual(ec.verdict_for([{"message": APPLE}], "suspicious_dns")[0], "detector_fault")

    def test_fleet_dns_sync_is_not_an_ips_hit(self):
        self.assertFalse(ec.recheck_ips("Starting nova-dns-sync.service - Nova fleet DNS sync (UniFi -> BIND)...")[0])

    def test_failure_is_fail_open(self):
        class Boom:
            def execute(self, *a): raise RuntimeError("db down")
        b = ec.check(Boom(), title="x", category="suspicious_dns", source="nova_syslog_server.py", dedup_key="k")
        self.assertEqual(b["verdict"], "unverified")
        self.assertIn("db down", b["note"])


class TestIntegration(unittest.TestCase):
    def test_rechecks_use_the_detectors_own_regexes(self):
        import nova_syslog_server as s
        self.assertTrue(hasattr(s, "DNS_QUERY_RE") and hasattr(s, "SUSPICIOUS_TLDS") and hasattr(s, "SUDO_RE"))
        self.assertNotIn("SUSPICIOUS_TLDS = ", SRC)   # one definition, in the detector

    def test_bug_filing_dedups_per_week(self):
        cur = _Cur([(5,)])
        self.assertEqual(ec.file_bug(cur, "src", "cat", "text"), 5)
        self.assertFalse(any("INSERT" in q for q in cur.sql))


class TestDocs(unittest.TestCase):
    def test_docstring_names_the_incident_and_modes(self):
        self.assertIn("#3675", SRC)
        for flag in ("--event", "--selftest"):
            self.assertIn(flag, SRC)


if __name__ == "__main__":
    unittest.main()
