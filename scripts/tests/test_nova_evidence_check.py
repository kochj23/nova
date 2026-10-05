#!/usr/bin/env python3
"""Tests for nova_evidence_check.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame) plus the incident #3675 privacy/regression/docs checks.
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


# ── house categories added 2026-10-05 (Retry / Unit / Frame) ───────────────────

class _FailAt:
    """Cursor that raises when a query containing `needle` is executed (after SET LOCAL)."""
    def __init__(self, answers, needle):
        self.answers = list(answers); self.needle = needle; self.calls = 0; self.sql = []

    def execute(self, sql, params=None):
        self.calls += 1; self.sql.append(sql)
        if sql.startswith("SET LOCAL"):
            raise RuntimeError("autocommit")
        if self.needle in sql:
            raise RuntimeError("db down")

    def fetchone(self):
        return self.answers.pop(0)

    def fetchall(self):
        return self.answers.pop(0)


class TestRetry(unittest.TestCase):
    def test_raw_read_is_one_shot_and_fails_open(self):
        # RETRY GAP: check()/_raw_rows — a failing source read is tried once; verdict stays unverified
        cur = _FailAt([], "syslog_events")
        b = ec.check(cur, title="x", category="suspicious_dns", source="nova_syslog_server.py", dedup_key="k")
        self.assertEqual(b["verdict"], "unverified")
        self.assertIn("evidence check failed", b["note"])
        self.assertEqual(cur.calls, 2)                     # SET LOCAL + the single failed read, no retry
        self.assertTrue(b["text"].startswith("verdict: unverified"))

    def test_bug_filing_failure_never_clobbers_the_verdict(self):
        # RETRY GAP: file_bug()/claude_queue — one attempt; failure leaves queue_id None, verdict intact
        cur = _FailAt([[("t", "nova-core", "named", APPLE)], (3, 2, 1), (1, 0), []], "claude_queue")
        b = ec.check(cur, title="x", category="suspicious_dns", source="nova_syslog_server.py", dedup_key="k")
        self.assertEqual(b["verdict"], "detector_fault")
        self.assertIsNone(b["queue_id"])
        self.assertNotIn("Filed claude_queue", b["advice"])

    def test_main_without_event_never_touches_pg(self):
        # RETRY GAP: main()/psycopg2.connect is a hand-run debug entry with no retry; the gate before it is the
        # --event flag, so the default invocation returns usage (2) without a single connect attempt.
        import types
        attempts = []
        real_pg, real_argv = ec.psycopg2, sys.argv
        ec.psycopg2 = types.SimpleNamespace(connect=lambda *a, **k: attempts.append(1))
        sys.argv = ["nova_evidence_check.py"]
        try:
            self.assertEqual(ec.main(), 2)
        finally:
            ec.psycopg2, sys.argv = real_pg, real_argv
        self.assertEqual(attempts, [])


class TestUnit(unittest.TestCase):
    def test_selftest_runs_clean(self):
        ec.demo()

    def test_ip_from_dedup_key_edges(self):
        self.assertIsNone(ec.ip_from_dedup_key(None))
        self.assertIsNone(ec.ip_from_dedup_key(""))
        self.assertEqual(ec.ip_from_dedup_key("a-10.0.0.7-b"), "10.0.0.7")

    def test_verdict_for_edges(self):
        self.assertEqual(ec.verdict_for([{"message": "x"}], None)[0], "unverified")
        self.assertEqual(ec.verdict_for([], None)[0], "unverified")
        v, holds, note = ec.verdict_for([{"message": "nothing dns about this"}], "suspicious_dns")
        self.assertEqual((v, holds), ("detector_fault", False))
        self.assertIn("not a DNS query log", note)

    def test_rechecks(self):
        self.assertTrue(ec.recheck_auth("sudo: kochj : TTY=pts/0 ; COMMAND=/bin/ls")[0])
        self.assertFalse(ec.recheck_auth("a perfectly boring line")[0])
        self.assertFalse(ec.recheck_sensitive("nothing here")[0])
        self.assertTrue(ec.recheck_ips("GPL ATTACK_RESPONSE id check returned root")[0])

    def test_advice_and_render_edges(self):
        a = ec.advice_for("detector_fault", "ips", "src", [], {}, "note")
        self.assertIn('fired on "?"', a)
        self.assertEqual(ec.advice_for("supported", None, "src", [], {}, ""), "")
        self.assertIn("unknown client", ec.advice_for("supported", "auth_failure", "s", [], {}, ""))
        b = {"verdict": "unverified", "note": "n", "raw": [], "who": {"1.2.3.4": "pi (infra)"},
             "history": {"events": 60, "days": 9, "incidents": 0, "false_positive": 0, "chronic": True}, "advice": "do x"}
        t = ec.render(b)
        self.assertIn("who: 1.2.3.4 = pi (infra)", t)
        self.assertIn("CHRONIC", t)
        self.assertTrue(t.endswith("do: do x"))

    def test_chronic_edges(self):
        self.assertFalse(ec.chronic({}))
        self.assertFalse(ec.chronic(None))
        self.assertTrue(ec.chronic({"events": ec.CHRONIC_N, "real": 0}))
        self.assertFalse(ec.chronic({"events": ec.CHRONIC_N - 1, "real": 0}))


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        import os
        import subprocess
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_evidence_check.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all evidence-check assertions passed", r.stdout)

    def test_import_never_runs_main(self):
        import os
        import subprocess
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_evidence_check"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
