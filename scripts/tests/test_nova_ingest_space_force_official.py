"""Tests for nova_ingest_space_force_official.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import nova_ingest_space_force_official as M  # noqa: E402

SCRIPT = SCRIPTS / "nova_ingest_space_force_official.py"
SRC = SCRIPT.read_text()
SF = "https://www.spaceforce.mil/Portals/2/Documents/"
EPUB = "https://static.e-publishing.af.mil/production/1/"

# Stub every outbound side effect (Slack notify, logging to ~/.openclaw/logs): at module load for a direct
# run, and again in setUpModule, because another test file's tearDownModule can restore the real
# nova_ingest.notify (patch.stop() resets the shared module attribute) before this file's tests run.
_PATCHES = [mock.patch.object(M.ni, "notify"), mock.patch.object(M.ni, "log")]
for _p in _PATCHES:
    _p.start()


def setUpModule():
    for p in _PATCHES:
        p.stop()
        p.start()


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
        self.assertIn("VALUES (%s, %s, %s, %s)", SRC)

    def test_restricted_documents_refused(self):
        for mark in ("FOR OFFICIAL USE ONLY", "CUI", "CUI//SP-PRVCY", "SECRET//NOFORN",
                     "DISTRIBUTION STATEMENT D: Distribution authorized to DoD only",
                     "RELEASABILITY: Access to this publication is restricted"):
            self.assertFalse(M.public_release_ok(f"SPFI 10-1\n{mark}\nbody text"), mark)
        self.assertTrue(M.public_release_ok("RELEASABILITY: There are no releasability restrictions on this "
                                            "publication\nbody"))
        self.assertTrue(M.public_release_ok("Guardians protect CUI handled by partners.\nbody"))  # prose mention
        self.assertFalse(M.public_release_ok("   "))

    def test_air_force_wide_publications_left_to_air_force_run(self):
        for stem in ("dafi31-101", "afi91-202", "dafgm2023-13-602v1", "hafmd2-3"):
            self.assertIsNone(M.rank(f"{EPUB}ussf/publication/{stem}/{stem}.pdf", stem.upper()))


class TestPerformance(unittest.TestCase):
    def test_candidates_10k_rows_fast(self):
        rows = [(f"{EPUB}ussf/publication/spfi{i % 500}-{i % 9}/spfi{i % 500}-{i % 9}.pdf?v={i}", f"2024{i:010d}")
                for i in range(10000)]
        t0 = time.time()
        out = M.candidates(rows, set(), set())
        self.assertLess(time.time() - t0, 3.0)
        self.assertLessEqual(len(out), 500 * 9)   # one per publication


class TestRetry(unittest.TestCase):
    def test_list_publications_uses_retrying_get(self):
        calls = []

        def fake_open(req, timeout=60):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("web.archive.org 503")
            return mock.Mock(read=lambda: b'[["original","timestamp"],["u","20240101000000"]]')
        with mock.patch.object(M.army.urllib.request, "urlopen", side_effect=fake_open), \
             mock.patch.object(M.army.time, "sleep"), mock.patch.object(M.time, "sleep"), \
             mock.patch.object(M.army.ni, "log"):
            self.assertEqual(M.list_publications(["x/"]), [("u", "20240101000000")])
        self.assertEqual(len(calls), 3)

    def test_cdx_offline_page_retried_then_skipped(self):
        pages = [b"<html>Temporarily Offline</html>", b'[["original","timestamp"],["a","1"]]',
                 b"<html>offline</html>", b"<html>offline</html>", b"<html>offline</html>"]
        with mock.patch.object(M.army, "get", side_effect=pages) as g, mock.patch.object(M.time, "sleep"):
            self.assertEqual(M.list_publications(["p1/", "p2/"], tries=3), [("a", "1")])
        self.assertEqual(g.call_count, 5)

    def test_pdftotext_failure_fails_open(self):
        # RETRY GAP: pdf_text — a local subprocess; a failure returns '' and the document is skipped.
        with mock.patch.object(M.subprocess, "run", side_effect=subprocess.TimeoutExpired("pdftotext", 300)):
            self.assertEqual(M.pdf_text(b"%PDF-1.7 fake"), "")


class TestUnit(unittest.TestCase):
    def test_title_of(self):
        self.assertEqual(M.title_of(f"{EPUB}ussf/publication/spfi36-2903/spfi36-2903.pdf"), "SPFI 36-2903")
        self.assertEqual(M.title_of(f"{SF}Space%20Doctrine/SDP%203-0%20Operations%20(19%20July%202023)_1.pdf?ver=x"),
                         "SDP 3-0 Operations (19 July 2023)")
        self.assertEqual(M.title_of(f"{SF}GPC/USSF_Case_for_Change.pdf"), "USSF Case for Change")

    def test_canonical(self):
        self.assertEqual(M.canonical("http://starcom.spaceforce.mil/Portals/2/SDP%201-0.pdf?ver=1"),
                         "https://www.starcom.spaceforce.mil/Portals/2/SDP%201-0.pdf")
        self.assertEqual(M.canonical("https://www.spaceforce.mil//Portals/2/x.pdf"),
                         "https://www.spaceforce.mil/Portals/2/x.pdf")

    def test_rank_and_filters(self):
        self.assertEqual(M.rank(SF + "x.pdf", "SDP 4-0 Sustainment"), 0)
        self.assertEqual(M.rank(f"{EPUB}ssc/publication/sscman91-710v1/sscman91-710v1.pdf", "SSCMAN 91-710V1"), 1)
        self.assertEqual(M.rank(SF + "x.pdf", "GUARDIAN IDEAL"), 2)
        self.assertEqual(M.rank(SF + "x.pdf", "C-Note 6 - 2 Feb 23 -- Mission Command"), 3)
        for t in ("SDP 2-0 Intelligence Executive Summary", "SDP 3-0 Training Slides", "Col Fulmer Bio Aug 2021",
                  "DD Form 368 Request for Conditional Release", "FY24 USSF Interservice Transfer", "Space Opera"):
            self.assertIsNone(M.rank(SF + "x.pdf", t), t)

    def test_key_of(self):
        self.assertEqual(M.key_of("Space Capstone Publication 10 Aug 2020"), M.key_of("Space Capstone Publication"))
        self.assertEqual(M.key_of("C Note 32 25 Oct 24 Our Words Have Meaning"),
                         M.key_of("C-Note 32 10-25-24 Our Words Have Meaning"))
        self.assertNotEqual(M.key_of("C Note 3 Day One Message to the Force"),
                            M.key_of("C-Note 3 - 16 Dec 22 -- Fielding Combat-Ready Forces"))
        self.assertEqual(M.key_of("Future Operating Environment 2040 Final"),
                         M.key_of("Future Operating Environment 2040"))
        self.assertEqual(M.key_of("SDP 3-103 Missile Warning (Change 1 Final)"), "SDP 3-103")
        self.assertEqual(M.key_of(""), "")

    def test_candidates_latest_capture_and_doctrine_first(self):
        rows = [(f"{SF}Space%20Doctrine/SDP%203-102%20Old.pdf", "20240101000000"),
                (f"{SF}Space%20Doctrine/SDP%203-102%20New.pdf", "20250101000000"),
                (f"{SF}CSO%20C-Notes/C-Note%206%20-%202%20Feb%2023%20--%20Mission%20Command.pdf", "20240101000000"),
                (f"{EPUB}ussf/publication/spfh1-1/spfh1-1.pdf", "20230406030355"),
                (f"{SF}Templates/CC_PMA_Endorsement_Letter.pdf", "20240101000000")]
        out = M.candidates(rows, set(), set())
        self.assertEqual([t for _u, t, _ts in out], ["SDP 3-102 New", "SPFH 1-1",
                                                     "C-Note 6 - 2 Feb 23 -- Mission Command"])

    def test_pdf_text_rejects_non_pdf(self):
        self.assertEqual(M.pdf_text(b"<html>Access Denied</html>"), "")


class TestIntegration(unittest.TestCase):
    def test_reuses_shared_helpers(self):
        self.assertIs(M.svcm.pub_key, sys.modules["nova_ingest_service_manuals"].pub_key)
        for name in ("ni.remember", "ni.chunk_prose", "ni.clean_text", "ni.is_garbage", "army.get", "army._connect",
                     "svcm.pub_key"):
            self.assertIn(name, SRC)
        self.assertNotRegex(SRC, r"def (remember|chunk_prose|clean_text|is_garbage|get|pub_key)\(")

    def test_source_table_and_service(self):
        self.assertEqual(M.SOURCE, "military_doctrine_space_force")
        self.assertEqual(M.SERVICE, "space_force")
        self.assertIn("INSERT INTO ia_ingest_seen", SRC)
        self.assertEqual(M.PDFTOTEXT, "/opt/homebrew/bin/pdftotext")

    def test_already_ingested_publications_skipped(self):
        rows = [(f"{SF}Space%20Doctrine/SDP%203-0%20Operations.pdf", "20240101000000"),
                (f"{SF}Space%20Doctrine/SDP%204-0%20Sustainment.pdf", "20240101000000"),
                (f"{EPUB}ussf/publication/spfh1-1/spfh1-1.pdf", "20230406030355")]
        seen_keys = {M.key_of("SDP 3-0 Operations (19 July 2023)")}
        seen_ids = {M.canonical(rows[2][0])}
        self.assertEqual([t for _u, t, _ts in M.candidates(rows, seen_keys, seen_ids)], ["SDP 4-0 Sustainment"])

    def test_title_chain_through_wayback(self):
        u = f"{EPUB}ussf/publication/spfi10-201/spfi10-201.pdf"
        self.assertEqual(M.wayback_url(u, "20230604080312"), f"https://web.archive.org/web/20230604080312id_/{u}")
        self.assertEqual(M.key_of(M.title_of(u)), "SPFI 10-201")


class TestFunctional(unittest.TestCase):
    ROWS = [(f"{SF}Space%20Doctrine/SDP%205-0%20Planning.pdf", "20250101000000"),
            (f"{SF}Space%20Doctrine/SDP%206-0%20Mission%20Command.pdf", "20250101000000")]

    def _run(self, texts, stored=0, argv=("--target", "50000")):
        cur = _Cur([("https://archive.org/x", "DTIC ADA1: x", stored)])
        remembered = []

        def fake_remember(text, source, meta, done, dry):
            remembered.append((text, source, meta))
            return True
        with mock.patch.object(M.army, "_connect", return_value=_Conn(cur)), \
             mock.patch.object(M, "list_publications", return_value=self.ROWS), \
             mock.patch.object(M.army, "get", return_value=b"%PDF"), \
             mock.patch.object(M, "pdf_text", side_effect=texts), \
             mock.patch.object(M.ni, "remember", side_effect=fake_remember), \
             mock.patch.object(M.ni, "is_garbage", return_value=False), \
             mock.patch.object(M.ni, "clean_text", side_effect=lambda t: t), \
             mock.patch.object(M.time, "sleep"):
            with mock.patch.object(M.ni, "notify"):   # belt and braces: never reach the real Slack bus
                rc = M.main(list(argv))
        return rc, cur, remembered

    def test_golden_path_writes_memories_and_progress(self):
        body = "\n\n".join(f"Paragraph {i} about space operations planning and mission command." * 3 for i in range(5))
        rc, cur, mem = self._run([body, "CUI\n" + body])
        self.assertEqual(rc, 0)
        self.assertTrue(mem)
        self.assertTrue(all(m[1] == "military_doctrine_space_force" for m in mem))
        self.assertTrue(mem[0][0].startswith("[SDP 5-0 Planning] "))
        inserts = [p for s, p in cur.sql if s.startswith("INSERT INTO ia_ingest_seen")]
        self.assertEqual(len(inserts), 2)
        self.assertEqual(inserts[0][0], "https://www.spaceforce.mil/Portals/2/Documents/Space%20Doctrine/"
                                        "SDP%205-0%20Planning.pdf")
        self.assertEqual(inserts[0][3], "space_force")
        self.assertEqual(inserts[1][2], 0)   # the CUI-marked document stored nothing

    def test_cap_reached_does_nothing(self):
        rc, cur, mem = self._run(["text"], stored=50000)
        self.assertEqual(rc, 0)
        self.assertEqual(mem, [])
        self.assertFalse([s for s, _p in cur.sql if s.startswith("INSERT")])

    def test_fetch_error_skips_document(self):
        cur = _Cur([])
        with mock.patch.object(M.army, "_connect", return_value=_Conn(cur)), \
             mock.patch.object(M, "list_publications", return_value=self.ROWS), \
             mock.patch.object(M.army, "get", side_effect=OSError("boom")), \
             mock.patch.object(M.ni, "remember") as rem, mock.patch.object(M.ni, "notify"), \
             mock.patch.object(M.time, "sleep"):
            self.assertEqual(M.main([]), 0)
        rem.assert_not_called()
        self.assertFalse([s for s, _p in cur.sql if s.startswith("INSERT")])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--target", r.stdout)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(M.main))


if __name__ == "__main__":
    unittest.main()
