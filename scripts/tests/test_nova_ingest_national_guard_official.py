"""Tests for nova_ingest_national_guard_official.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Offline: the Wayback CDX/PDF fetches, pdftotext, PostgreSQL, Nova memory and Slack are all mocked.
Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_ingest_national_guard_official.py
"""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import nova_ingest_national_guard_official as G  # noqa: E402

SRC = (SCRIPTS / "nova_ingest_national_guard_official.py").read_text()
NGB = "https://www.ngbpmc.ng.mil/Portals/27/Publications/"
ANG = "https://static.e-publishing.af.mil/production/1/ang/publication/"


class _Cur:
    def __init__(self, rows=()):
        self.rows, self.sql = list(rows), []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))

    def fetchall(self):
        return self.rows


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_sql_is_parameterized(self):
        self.assertNotIn("cur.execute(f", SRC)
        self.assertIn("VALUES (%s, %s, %s, %s)", SRC)

    def test_restricted_markings_are_refused(self):
        for head in ("FOR OFFICIAL USE ONLY\nNGR 1-1", "CUI\nCNGBI 1.01", "U//FOUO", "DISTRIBUTION STATEMENT D: DoD only",
                     "Distribution authorized to U.S. Government agencies only", "SECRET",
                     "Access to this publication is restricted", "Not approved for public release"):
            self.assertFalse(G.is_public(head), head)

    def test_public_markings_pass(self):
        for head in ("UNCLASSIFIED\nDISTRIBUTION: A\nThis instruction is approved for public release",
                     "RELEASABILITY: There are no releasability restrictions on this publication.",
                     "Handling controlled unclassified information in the body of a regulation is discussed",
                     "Distribution: This pamphlet is available in electronic media only and intended for A, B, and C"):
            self.assertTrue(G.is_public(head), head)

    def test_pdftotext_called_without_shell(self):
        self.assertNotIn("shell=True", SRC)


class TestPerformance(unittest.TestCase):
    def test_candidates_10k_rows_fast(self):
        rows = [[f"2024{(i % 12) + 1:02d}01000000", f"{NGB}ngr/NGR%20{i % 800}-1_2021{(i % 12) + 1:02d}01.pdf"]
                for i in range(10000)]
        t0 = time.time()
        out = G.candidates(rows, set(), set())
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(len(out), 800)                    # one per publication number

    def test_list_archived_is_bounded(self):
        self.assertEqual(len(G.PREFIXES), 2)               # exactly one CDX query per official prefix
        self.assertIn('("limit", "20000")', SRC)


class TestRetry(unittest.TestCase):
    def test_fetches_use_the_army_retrying_get(self):
        calls = []

        def fake_open(req, timeout=60):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("web.archive.org 503")
            return mock.Mock(read=lambda: b"[]")
        with mock.patch.object(G.army.urllib.request, "urlopen", side_effect=fake_open), \
             mock.patch.object(G.army.time, "sleep"), mock.patch.object(G.army.ni, "log"), \
             mock.patch.object(G.time, "sleep"):
            self.assertEqual(G.list_archived(), [])
        self.assertEqual(len(calls), 4)                    # 2 failures + success, then the 2nd prefix

    def test_pdftotext_failure_fails_open(self):
        # RETRY GAP: pdf_text — a local subprocess; a timeout returns '' instead of raising.
        with mock.patch.object(G.subprocess, "run", side_effect=subprocess.TimeoutExpired("pdftotext", 180)), \
             mock.patch.object(G.ni, "log"):
            self.assertEqual(G.pdf_text(b"%PDF-1.6 x"), "")
        self.assertEqual(G.pdf_text(b"<html>not a pdf"), "")


class TestUnit(unittest.TestCase):
    def test_doc_title(self):
        self.assertEqual(G.doc_title(NGB + "NGR/NGR%20350-1_20210623.pdf"), "NGR 350-1")
        self.assertEqual(G.doc_title(NGB + "cngbi/CNGBI_2000_01C_20180814.pdf"), "CNGBI 2000.01C")
        self.assertEqual(G.doc_title(NGB + "NGPAM/ngbp%20750-59_20181201.pdf"), "NGBP 750-59")
        self.assertEqual(G.doc_title(ANG + "angi36-2001/angi36-2001.pdf"), "ANGI 36-2001")
        self.assertEqual(G.doc_title(""), "")

    def test_file_date(self):
        self.assertEqual(G.file_date(NGB + "NGR%20350-1_20210623.pdf", "20240101000000"), "20210623")
        self.assertEqual(G.file_date(NGB + "ngr%20350-1.pdf", "20240101000000"), "20240101")

    def test_candidates_skip_forms_bulletins_seen_and_keep_newest(self):
        rows = [["20220101000000", NGB + "ngr/ngr%20350-1.pdf"],
                ["20240101000000", NGB + "NGR/NGR%20350-1_20210623.pdf?ver=abc"],
                ["20240101000000", "https://www.ngbpmc.ng.mil/Portals/27/forms/ngb%20forms/NGB%2034-2.pdf"],
                ["20240101000000", NGB + "bulletins/2019/pb19_01.pdf"],
                ["20240101000000", NGB + "NGPAM/ngbp%20750-59_20181201.pdf"],
                ["20240101000000", ANG + "angi36-2001/angi36-2001.pdf"],
                ["20240101000000", "https://example.com/other.pdf"]]
        out = G.candidates(rows, {ANG + "angi36-2001/angi36-2001.pdf"}, {"NGBP 750-59"})
        self.assertEqual([c[0] for c in out], [NGB + "NGR/NGR%20350-1_20210623.pdf"])

    def test_wayback_url_is_raw_capture(self):
        self.assertEqual(G.wayback_url("https://a/b.pdf", "20240101"), "https://web.archive.org/web/20240101id_/https://a/b.pdf")


class TestIntegration(unittest.TestCase):
    def test_reuses_shared_helpers(self):
        self.assertIn("import nova_ingest as ni", SRC)
        self.assertIn("import nova_ingest_army_manuals as army", SRC)
        self.assertIn("svcman.pub_key", SRC)
        for fn in ("ni.remember", "ni.chunk_prose", "ni.clean_text", "ni.is_garbage", "army.get", "army._connect"):
            self.assertIn(fn, SRC)
        self.assertNotIn("def pub_key", SRC)

    def test_source_service_and_table(self):
        self.assertEqual(G.SOURCE, "military_doctrine_national_guard")
        self.assertEqual(G.SERVICE, "national_guard")
        self.assertIn("INSERT INTO ia_ingest_seen", SRC)
        self.assertEqual(G.PDFTOTEXT, "/opt/homebrew/bin/pdftotext")

    def test_title_and_pub_key_chain(self):
        a = G.svcman.pub_key(G.doc_title(NGB + "ngr/ngr%20350-1.pdf"))
        b = G.svcman.pub_key(G.doc_title(NGB + "NGR/NGR%20350-1_20210623.pdf"))
        self.assertEqual(a, b)


class TestFunctional(unittest.TestCase):
    PDF = b"%PDF-1.6 fake"

    def _run(self, rows=(), target=3, dry=False, head="UNCLASSIFIED\nDISTRIBUTION: A", get_exc=None):
        cur = _Cur(rows)
        conn = mock.Mock(cursor=lambda: cur)
        remembered, notes = [], []
        cdx = [["20240101000000", NGB + "NGR/NGR%20350-1_20210623.pdf"],
               ["20240101000000", NGB + "ngr/NGR%20500-1_20200101.pdf"]]

        def fake_text(data, first_pages=0):
            return head if first_pages else "body " * 200
        with mock.patch.object(G.army, "_connect", return_value=conn), \
             mock.patch.object(G, "list_archived", return_value=cdx), \
             mock.patch.object(G.army, "get", side_effect=get_exc, return_value=self.PDF), \
             mock.patch.object(G, "pdf_text", side_effect=fake_text), \
             mock.patch.object(G.ni, "clean_text", side_effect=lambda t: t), \
             mock.patch.object(G.ni, "chunk_prose", return_value=["a", "b"]), \
             mock.patch.object(G.ni, "is_garbage", return_value=False), \
             mock.patch.object(G.ni, "remember", side_effect=lambda t, src, *a: remembered.append(src) or True), \
             mock.patch.object(G.ni, "notify", side_effect=notes.append), mock.patch.object(G.ni, "log"), \
             mock.patch.object(G.time, "sleep"):
            rc = G.main(["--target", str(target)] + (["--dry-run"] if dry else []))
        return rc, cur, remembered, notes

    def test_golden_path_stops_at_the_cap(self):
        rc, cur, remembered, notes = self._run(target=3)
        self.assertEqual(rc, 0)
        self.assertEqual(len(remembered), 3)
        self.assertEqual(set(remembered), {"military_doctrine_national_guard"})
        ins = [p for s, p in cur.sql if s.startswith("INSERT INTO ia_ingest_seen")]
        self.assertEqual([(p[0], p[3]) for p in ins], [(NGB + "NGR/NGR%20350-1_20210623.pdf", "national_guard"),
                                                       (NGB + "ngr/NGR%20500-1_20200101.pdf", "national_guard")])
        self.assertEqual([p[2] for p in ins], [2, 1])
        self.assertTrue(notes)

    def test_existing_total_counts_toward_cap(self):
        rows = [("archive-x", "Old Guard Manual", 49999, "national_guard"), ("y", "FM 1", 500, "army")]
        _, _, remembered, _ = self._run(rows=rows, target=50000)
        self.assertEqual(len(remembered), 1)

    def test_restricted_document_is_recorded_with_zero_chunks(self):
        _, cur, remembered, _ = self._run(head="FOR OFFICIAL USE ONLY")
        self.assertEqual(remembered, [])
        self.assertTrue(all(p[2] == 0 for s, p in cur.sql if s.startswith("INSERT")))

    def test_fetch_error_does_not_stop_run(self):
        rc, cur, remembered, _ = self._run(get_exc=OSError("down"))
        self.assertEqual(rc, 0)
        self.assertEqual(remembered, [])

    def test_dry_run_writes_and_posts_nothing(self):
        _, cur, _, notes = self._run(target=10, dry=True)
        self.assertFalse(any(s.startswith(("INSERT", "CREATE", "ALTER")) for s, _ in cur.sql))
        self.assertEqual(notes, [])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ingest_national_guard_official.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        self.assertTrue(callable(G.main))


if __name__ == "__main__":
    unittest.main()
