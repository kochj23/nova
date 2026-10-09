"""Tests for nova_ingest_marines_official.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import nova_ingest_marines_official as M  # noqa: E402

SCRIPT = SCRIPTS / "nova_ingest_marines_official.py"
SRC = SCRIPT.read_text()
PUB = "https://www.marines.mil/Portals/1/Publications/"

# Stub every outbound side effect at module load (Slack notify, logging to ~/.openclaw/logs).
_PATCHES = [mock.patch.object(M.ni, "notify"), mock.patch.object(M.ni, "log")]
for _p in _PATCHES:
    _p.start()


def tearDownModule():
    for p in _PATCHES:
        p.stop()


class _Cur:
    def __init__(self, rows=()):
        self.rows, self.sql = list(rows), []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cur):
        self.cur, self.autocommit = cur, False

    def cursor(self):
        return self.cur


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_sql_is_parameterized(self):
        self.assertNotIn("cur.execute(f", SRC)
        self.assertIn("WHERE service = %s", SRC)

    def test_restricted_documents_refused(self):
        for mark in ("FOR OFFICIAL USE ONLY", "CUI", "DISTRIBUTION STATEMENT C: Distribution authorized to U.S. "
                     "Government agencies only", "SECRET//NOFORN", "Distribution is limited to DoD"):
            self.assertFalse(M.public_release_ok(f"MCWP 3-1\n{mark}\nbody text"), mark)
        self.assertTrue(M.public_release_ok("DISTRIBUTION STATEMENT A: Approved for public release; "
                                            "distribution is unlimited.\nbody"))
        self.assertFalse(M.public_release_ok("   "))

    def test_identifier_is_pinned_to_marines_mil(self):
        self.assertTrue(M.canonical("http://marines.mil/portals/1/Publications/x.pdf?ver=1")
                        .startswith("https://www.marines.mil/"))


class TestPerformance(unittest.TestCase):
    def test_candidates_10k_rows_fast(self):
        rows = [(f"{PUB}MCRP%203-{i % 400}A.{i % 7}.pdf?ver={i}", f"2024{i:010d}") for i in range(10000)]
        t0 = time.time()
        out = M.candidates(rows, set(), set())
        self.assertLess(time.time() - t0, 3.0)
        self.assertLessEqual(len(out), 400 * 7)   # one per publication


class TestRetry(unittest.TestCase):
    def test_list_publications_uses_retrying_get(self):
        calls = []

        def fake_open(req, timeout=60):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("web.archive.org 503")
            return mock.Mock(read=lambda: b'[["original","timestamp"],["u","20240101000000"]]')
        with mock.patch.object(M.army.urllib.request, "urlopen", side_effect=fake_open), \
             mock.patch.object(M.army.time, "sleep"), mock.patch.object(M.army.ni, "log"):
            self.assertEqual(M.list_publications(), [("u", "20240101000000")])
        self.assertEqual(len(calls), 3)

    def test_pdftotext_failure_fails_open(self):
        # RETRY GAP: pdf_text — a local subprocess; a failure returns '' and the document is skipped.
        with mock.patch.object(M.subprocess, "run", side_effect=subprocess.TimeoutExpired("pdftotext", 300)):
            self.assertEqual(M.pdf_text(b"%PDF-1.7 fake"), "")


class TestUnit(unittest.TestCase):
    def test_title_of(self):
        self.assertEqual(M.title_of(f"{PUB}MCWP%203-11.2%20Marine%20Rifle%20Squad.pdf?ver=x"),
                         "MCWP 3-11.2 Marine Rifle Squad")
        self.assertEqual(M.title_of(f"{PUB}FMFRP_12-15.PDF"), "FMFRP 12-15")

    def test_norm_key(self):
        self.assertEqual(M.norm_key("MCWP 3 11.2 Marine Rifle Squad"), M.norm_key("MCWP 3-11.2"))
        self.assertEqual(M.norm_key("mcwp3 16 4"), "MCWP 3-16-4")
        self.assertEqual(M.norm_key("NAVMC 3500.78C"), M.norm_key("NAVMC 3500.78"))
        self.assertNotEqual(M.norm_key("MCRP 3-10A.3"), M.norm_key("MCRP 3-10A.4"))
        self.assertEqual(M.norm_key(""), "")

    def test_candidates_prefers_unlocked_and_drops_non_doctrine(self):
        rows = [(f"{PUB}MCTP%203-01A%20(SECURED).pdf", "20250101000000"),
                (f"{PUB}MCTP%203-01A.pdf", "20210101000000"),
                (f"{PUB}MCTP%203-01A%20GN.pdf", "20240101000000"),
                (f"{PUB}CMC%20Message.pdf", "20240101000000"),
                (f"{PUB}MCRP%205-12C%20Dictionary%20of%20terms.pdf", "20240101000000")]
        out = M.candidates(rows, set(), set())
        self.assertEqual([t for _u, t, _ts in out], ["MCTP 3-01A"])
        self.assertEqual(out[0][0], f"{PUB}MCTP%203-01A.pdf")

    def test_secured_only_copy_is_kept_with_clean_title(self):
        out = M.candidates([(f"{PUB}MCTP%2012-10E%20(SECURED).pdf", "20250101000000")], set(), set())
        self.assertEqual(out[0][1], "MCTP 12-10E")

    def test_pdf_text_rejects_non_pdf(self):
        self.assertEqual(M.pdf_text(b"<html>Access Denied</html>"), "")


class TestIntegration(unittest.TestCase):
    def test_reuses_shared_helpers(self):
        self.assertIs(M.svcm.pub_key, sys.modules["nova_ingest_service_manuals"].pub_key)
        for name in ("ni.remember", "ni.chunk_prose", "ni.clean_text", "ni.is_garbage", "army.get", "army._connect"):
            self.assertIn(name, SRC)
        self.assertNotRegex(SRC, r"def (remember|chunk_prose|clean_text|is_garbage|get|pub_key)\(")

    def test_source_table_and_service(self):
        self.assertEqual(M.SOURCE, "military_doctrine_marines")
        self.assertEqual(M.SERVICE, "marines")
        self.assertIn("INSERT INTO ia_ingest_seen", SRC)

    def test_already_ingested_publications_skipped(self):
        rows = [(f"{PUB}MCWP%203-11.2%20Marine%20Rifle%20Squad.pdf", "20221203000000"),
                (f"{PUB}MCDP%201%20Warfighting.pdf", "20240101000000")]
        seen = M.keys_of("MCWP 3 11.2 Marine Rifle Squad")       # Internet Archive spelling
        out = M.candidates(rows, seen, set())
        self.assertEqual([t for _u, t, _ts in out], ["MCDP 1 Warfighting"])


class TestFunctional(unittest.TestCase):
    def _run(self, seen_rows, text, target=50000):
        cur = _Cur(seen_rows)
        rows = [(f"{PUB}MCDP%201%20Warfighting.pdf", "20240101000000")]
        stored = []
        with mock.patch.object(M.army, "_connect", return_value=_Conn(cur)), \
             mock.patch.object(M, "list_publications", return_value=rows), \
             mock.patch.object(M.army, "get", return_value=b"%PDF-1.7"), \
             mock.patch.object(M, "pdf_text", return_value=text), \
             mock.patch.object(M.ni, "is_garbage", return_value=False), \
             mock.patch.object(M.ni, "remember", side_effect=lambda t, s, m, h, d: stored.append((t, s, m)) or True), \
             mock.patch.object(M.time, "sleep"):
            self.assertEqual(M.main(["--target", str(target)]), 0)
        return cur, stored

    def test_golden_path_stores_and_records(self):
        body = "\n\n".join(f"Paragraph {i} on maneuver warfare and the nature of war, at length." for i in range(40))
        cur, stored = self._run([], "DISTRIBUTION STATEMENT A: Approved for public release.\n\n" + body)
        self.assertTrue(stored)
        self.assertTrue(all(s == "military_doctrine_marines" and t.startswith("[MCDP 1 Warfighting]")
                            for t, s, _m in stored))
        ins = [p for q, p in cur.sql if q.startswith("INSERT")]
        self.assertEqual(ins[0][0], f"{PUB}MCDP%201%20Warfighting.pdf")
        self.assertEqual(ins[0][2:], (len(stored), "marines"))

    def test_restricted_document_records_zero_chunks(self):
        cur, stored = self._run([], "FOR OFFICIAL USE ONLY\n\nsome text that must not be stored at all")
        self.assertEqual(stored, [])
        self.assertEqual([p for q, p in cur.sql if q.startswith("INSERT")][0][2], 0)

    def test_stops_when_cap_already_reached(self):
        cur, stored = self._run([("x", "FMFRP 12-80", 50000)], "anything")
        self.assertEqual(stored, [])
        self.assertFalse([q for q, _p in cur.sql if q.startswith("INSERT")])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--target", r.stdout.decode())

    def test_import_does_not_run_main(self):
        self.assertRegex(SRC, re.compile(r'^if __name__ == "__main__":\n    sys.exit\(main\(\)\)', re.M))


if __name__ == "__main__":
    unittest.main()
