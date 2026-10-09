"""Tests for nova_ingest_air_force_official.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import nova_ingest_air_force_official as A  # noqa: E402
import nova_ingest_service_manuals as S  # noqa: E402

SRC = (SCRIPTS / "nova_ingest_air_force_official.py").read_text()

# Stub every outbound side effect for the whole module (Slack notify, log file).
_PATCHERS = [mock.patch.object(A.ni, "notify"), mock.patch.object(A.ni, "log")]
for _p in _PATCHERS:
    _p.start()


def tearDownModule():
    for p in _PATCHERS:
        p.stop()


PUBLIC = ("BY ORDER OF THE\nSECRETARY OF THE AIR FORCE\n\nAIR FORCE MANUAL 11-202 Volume 3\n10 JANUARY 2022\n"
          "Flight Operations\nFLIGHT OPERATIONS\nCOMPLIANCE WITH THIS PUBLICATION IS MANDATORY\n"
          "RELEASABILITY: There are no releasability restrictions on this publication.\n" + "Body text. " * 400)


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
        self.assertNotIn("execute(f", SRC)
        self.assertIn("WHERE service = %s", SRC)

    def test_restricted_documents_are_refused(self):
        for marking in ("FOR OFFICIAL USE ONLY", "CONTROLLED UNCLASSIFIED INFORMATION", "\nCUI\n",
                        "RELEASABILITY: Access to this publication is restricted: downloadable from a .mil server only",
                        "RELEASABILITY: Requests for this publication must be made to the OPR",
                        "DISTRIBUTION STATEMENT D: Distribution authorized to DoD", "This document is not releasable"):
            ok, why = A.release_status("AIR FORCE TACTICS\n" + marking + "\n" + "text " * 100)
            self.assertFalse(ok, marking)

    def test_public_release_accepted(self):
        self.assertEqual(A.release_status(PUBLIC), (True, "public release"))
        self.assertTrue(A.release_status("DISTRIBUTION STATEMENT A: Approved for public release")[0])

    def test_pdftotext_runs_on_a_private_temp_copy_with_argv_list(self):
        with mock.patch.object(A.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=b"ok")) as run:
            self.assertEqual(A.pdf_text(b"%PDF-1.6 data"), "ok")
        argv = run.call_args[0][0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[0], A.PDFTOTEXT)
        self.assertFalse(Path(argv[-2]).exists())          # temp dir cleaned up
        self.assertIn("timeout", run.call_args[1])


class TestPerformance(unittest.TestCase):
    def test_candidates_10k_fast(self):
        caps = {A.PREFIXES[1]: {f"k{i}": ("20240101000000",
                                          f"http://static.e-publishing.af.mil/production/1/af_a3/publication/"
                                          f"afman11-{i % 2000}/afman11-{i % 2000}.pdf") for i in range(10000)}}
        t0 = time.time()
        out = A.candidates(caps, set(), set())
        self.assertLess(time.time() - t0, 3.0)
        self.assertEqual(len(out), 2000)                    # one per publication number

    def test_latest_captures_10k_fast(self):
        text = "\n".join(f"k{i % 100} 2020{i:010d} http://x/{i % 100}.pdf" for i in range(10000))
        t0 = time.time()
        self.assertEqual(len(A.latest_captures(text)), 100)
        self.assertLess(time.time() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_cdx_reuses_the_army_retrying_get(self):
        calls = []

        def fake_open(req, timeout=60):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("wayback 503")
            return mock.Mock(read=lambda: b"k 20240101000000 http://static.e-publishing.af.mil/a/afh10-222.pdf\n")
        with mock.patch.object(A.army.urllib.request, "urlopen", side_effect=fake_open), \
             mock.patch.object(A.army.time, "sleep"), mock.patch.object(A.army.ni, "log"):
            caps = A.cdx_captures("static.e-publishing.af.mil/production/1/")
        self.assertEqual(len(calls), 3)
        self.assertEqual(list(caps), ["k"])

    def test_pdftotext_failure_fails_open(self):
        # RETRY GAP: pdf_text — a local subprocess; a timeout/crash returns '' (document skipped), never raises
        with mock.patch.object(A.subprocess, "run", side_effect=subprocess.TimeoutExpired("pdftotext", 300)):
            self.assertEqual(A.pdf_text(b"%PDF-1.4"), "")
        self.assertEqual(A.pdf_text(b"<html>403</html>"), "")   # Wayback error page, not a PDF


class TestUnit(unittest.TestCase):
    def test_title_from_filename(self):
        self.assertEqual(A.title_from_filename("afman11-202v3.pdf"), "AFMAN 11-202V3")
        self.assertEqual(A.title_from_filename("AFTTP3-42.5.pdf"), "AFTTP 3-42.5")
        self.assertEqual(A.title_from_filename("dafi36-2903.pdf"), "DAFI 36-2903")
        self.assertIsNone(A.title_from_filename("afi91-202_usafasup.pdf"))     # supplement
        self.assertIsNone(A.title_from_filename("10afi10-101.pdf"))            # local unit
        self.assertIsNone(A.title_from_filename("1A8X1%20CFETP%202011.pdf"))
        self.assertIsNone(A.title_from_filename(""))

    def test_doctrine_title(self):
        self.assertEqual(A.doctrine_title("3-01-AFDP-COUNTERAIR.pdf"), "AFDP 3-01 Counterair")
        self.assertEqual(A.doctrine_title("3-60-Annex-TARGETING.pdf"), "AFDP 3-60 Targeting")
        self.assertEqual(A.doctrine_title("AFDP%203-85%20Electromagnetic%20Spectrum%20Ops.pdf"),
                         "AFDP 3-85 Electromagnetic Spectrum Ops")
        for skip in ("3-0-Summary-of-Key-Changes.pdf", "AF-GLOSSARY.pdf", "3-01-D02-AIR-Operations.pdf",
                     "AFD35%20Wargame%20Invitation%20Flyer.pdf", "Clear-Your-Cache.pdf"):
            self.assertIsNone(A.doctrine_title(skip), skip)

    def test_dedup_key_folds_daf(self):
        self.assertEqual(A.dedup_key("DAFMAN 91-203 Safety"), A.dedup_key("AFMAN 91-203"))
        self.assertNotEqual(A.dedup_key("AFMAN 91-203"), A.dedup_key("AFMAN 91-204"))

    def test_rank_prefers_doctrine_and_ops_manuals(self):
        order = sorted(["AFI 36-2903", "AFPD 10-1", "AFMAN 11-202V3", "AFTTP 3-1", "AFDD 2-0", "AFH 10-222",
                        "AFMAN 36-2100", "AFI 11-202"], key=lambda t: A.rank(t, 1))
        self.assertEqual(order, ["AFDD 2-0", "AFTTP 3-1", "AFMAN 11-202V3", "AFMAN 36-2100", "AFH 10-222",
                                 "AFI 11-202", "AFPD 10-1", "AFI 36-2903"])

    def test_latest_captures_keeps_newest_and_strips_query(self):
        caps = A.latest_captures("k?x=1 2019 http://a/x.pdf?y\nk 2024 http://a/x.pdf\nbad\n\n")
        self.assertEqual(caps, {"k": ("2024", "http://a/x.pdf")})
        self.assertEqual(A.latest_captures(""), {})

    def test_official_url(self):
        self.assertEqual(A.official_url("http://static.e-publishing.af.mil:80/production/1/a.pdf"),
                         "https://static.e-publishing.af.mil/production/1/a.pdf")

    def test_subject_line(self):
        self.assertEqual(A.subject_line(PUBLIC), "Flight Operations")
        tactics = "AFTTP 3-42.5\n1 NOVEMBER 2020\nTactical Doctrine\nAIRMAN'S MANUAL\nCOMPLIANCE..."
        self.assertEqual(A.subject_line(tactics), "Airman'S Manual")
        self.assertEqual(A.subject_line(""), "")

    def test_release_status_empty(self):
        self.assertTrue(A.release_status("")[0])      # empty text is rejected later by MIN_TEXT, not here


class TestIntegration(unittest.TestCase):
    def test_reuses_shared_helpers(self):
        self.assertIs(A.pub_key, S.pub_key)
        self.assertNotIn("def get(", SRC)
        self.assertNotIn("def remember(", SRC)
        self.assertIn("army.get(", SRC)
        for h in ("ni.remember(", "ni.chunk_prose(", "ni.clean_text(", "ni.is_garbage("):
            self.assertIn(h, SRC)

    def test_source_service_and_table(self):
        self.assertEqual((A.SOURCE, A.SERVICE), ("military_doctrine_air_force", "air_force"))
        self.assertIn("INSERT INTO ia_ingest_seen", SRC)

    def test_candidates_skip_already_ingested_and_prefer_doctrine_site(self):
        caps = {
            A.PREFIXES[0]: {"d": ("2025", "https://www.doctrine.af.mil/Portals/61/documents/3-01-AFDP-COUNTERAIR.pdf")},
            A.PREFIXES[1]: {"s1": ("2024", "http://static.e-publishing.af.mil/production/1/x/afdp3-01/afdp3-01.pdf"),
                            "s2": ("2024", "http://static.e-publishing.af.mil/production/1/x/afi36-1/afi36-1.pdf"),
                            "s3": ("2024", "http://static.e-publishing.af.mil/production/1/x/afh10-222v14/afh10-222v14.pdf"),
                            "s4": ("2024", "http://static.e-publishing.af.mil/production/1/x/dafman91-203/dafman91-203.pdf")},
            A.PREFIXES[2]: {"w": ("2012", "http://www.e-publishing.af.mil/shared/media/epubs/AFMAN91-203.pdf")},
        }
        seen_keys = {S.pub_key("AFH 10-222V14 Guide to Fighting Positions")}
        seen_ids = {"https://static.e-publishing.af.mil/production/1/x/afi36-1/afi36-1.pdf"}
        out = A.candidates(caps, seen_ids, seen_keys)
        self.assertEqual([t for _u, _w, t in out], ["AFDP 3-01 Counterair", "DAFMAN 91-203"])
        self.assertTrue(out[0][1].startswith("https://web.archive.org/web/2025id_/https://www.doctrine.af.mil/"))


class TestFunctional(unittest.TestCase):
    PUBS = [("https://static.e-publishing.af.mil/a/afman11-202v3.pdf", "https://web.archive.org/web/1id_/a",
             "AFMAN 11-202V3"),
            ("https://static.e-publishing.af.mil/a/afttp3-1.pdf", "https://web.archive.org/web/1id_/b", "AFTTP 3-1"),
            ("https://static.e-publishing.af.mil/a/afh1.pdf", "https://web.archive.org/web/1id_/c", "AFH 1-1")]

    def _run(self, texts, rows=(), target=100, dry=False, get_fail=False):
        cur = _Cur(rows)
        conn = mock.Mock(cursor=lambda: cur)
        remembered = []
        get = mock.Mock(side_effect=OSError("down")) if get_fail else mock.Mock(return_value=b"%PDF")
        with mock.patch.object(A.army, "_connect", return_value=conn), \
             mock.patch.object(A, "cdx_captures", return_value={}), \
             mock.patch.object(A, "candidates", return_value=self.PUBS), \
             mock.patch.object(A.army, "get", get), \
             mock.patch.object(A, "pdf_text", side_effect=texts), \
             mock.patch.object(A.ni, "clean_text", side_effect=lambda t: t), \
             mock.patch.object(A.ni, "chunk_prose", return_value=["a", "b"]), \
             mock.patch.object(A.ni, "is_garbage", return_value=False), \
             mock.patch.object(A.ni, "remember", side_effect=lambda t, src, meta, *a: remembered.append((t, src, meta)) or True), \
             mock.patch.object(A.time, "sleep"):
            rc = A.main(["--target", str(target)] + (["--dry-run"] if dry else []))
        return rc, cur, remembered

    def test_golden_path_skips_restricted_records_all_and_stops_at_cap(self):
        restricted = "RELEASABILITY: Access to this publication is restricted\n" + "x " * 2000
        rc, cur, rem = self._run([PUBLIC, restricted, PUBLIC], rows=[("old", "AFH 9-9", 97)], target=100)
        self.assertEqual(rc, 0)
        self.assertEqual(len(rem), 3)                                 # 97 stored + 2 + 1 = cap of 100
        self.assertEqual({s for _t, s, _m in rem}, {"military_doctrine_air_force"})
        self.assertTrue(rem[0][0].startswith("[AFMAN 11-202V3 Flight Operations] "))
        self.assertEqual(rem[0][2]["url"], self.PUBS[0][0])
        ins = [p for s, p in cur.sql if s.startswith("INSERT INTO ia_ingest_seen")]
        self.assertEqual([(p[0], p[2], p[3]) for p in ins],
                         [(self.PUBS[0][0], 2, "air_force"), (self.PUBS[1][0], 0, "air_force"),
                          (self.PUBS[2][0], 1, "air_force")])

    def test_dry_run_writes_nothing(self):
        _rc, cur, _rem = self._run([PUBLIC] * 3, dry=True)
        self.assertFalse(any(s.startswith("INSERT") for s, _ in cur.sql))

    def test_fetch_errors_do_not_stop_the_run(self):
        rc, cur, rem = self._run([], get_fail=True)
        self.assertEqual((rc, rem), (0, []))
        self.assertFalse(any(s.startswith("INSERT") for s, _ in cur.sql))   # retried next run


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ingest_air_force_official.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)


if __name__ == "__main__":
    unittest.main()
