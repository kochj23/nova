"""Tests for nova_ingest_service_manuals.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import nova_ingest_service_manuals as S  # noqa: E402

SRC = (SCRIPTS / "nova_ingest_service_manuals.py").read_text()


class _Cur:
    def __init__(self, rows=(), has_service=True):
        self.rows, self.has_service, self.sql, self._last = list(rows), has_service, [], None

    def execute(self, sql, params=None):
        self.sql.append((sql, params)); self._last = sql

    def fetchone(self):
        return (1,) if self.has_service else None

    def fetchall(self):
        return self.rows


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_sql_has_no_interpolated_values(self):
        for line in SRC.splitlines():
            if "cur.execute(f" in line:
                self.assertNotRegex(line, r"\{(?!svc_col)")   # only the constant column expression is formatted

    def test_low_value_and_leaked_titles_are_refused(self):
        for t in ("NAVPERS 10622 leaked copy", "MCRP 3-02 Dictionary of terms", "AFPAM 10-100 index"):
            self.assertFalse(S.keep("navy" if "NAV" in t else "marines" if "MCRP" in t else "air_force", t), t)


class TestReleaseAndDedup(unittest.TestCase):
    def test_restricted_markings_block_ingest(self):
        for marked in ("DISTRIBUTION STATEMENT C: Distribution authorized to U.S. Government agencies only",
                       "FOR OFFICIAL USE ONLY", "NOFORN", "CUI"):
            self.assertTrue(S.restricted("cover page\n" + marked + "\nbody"), marked)
        self.assertFalse(S.restricted("DISTRIBUTION STATEMENT A: Approved for public release; distribution is unlimited."))
        self.assertFalse(S.restricted("x" * S.RESTRICT_SCAN_CHARS + " FOUO"))   # only the opening pages count

    def test_mirror_prefixes_and_spacing_do_not_split_a_publication(self):
        self.assertEqual(S.pub_key("ERIC ED123456: Opticalman 3 & 2 NAVEDTRA 10215"),
                         S.pub_key("Opticalman 3 & 2 NAVEDTRA 10215"))
        self.assertEqual(S.pub_key("MCWP 3 11.2 Marine Rifle Squad"), S.pub_key("MCWP 3-11.2 Marine Rifle Squad"))

    def test_navy_off_target_titles_dropped(self):
        for t in ("DTIC AD0663541: CIC test study NAVPERS 1", "Army and navy manual for debaters",
                  "Navy Medicine Owners' and Operators' Manual 2011"):
            self.assertFalse(S.keep("navy", t), t)


class TestPerformance(unittest.TestCase):
    def test_pick_10k_titles_fast(self):
        docs = [(f"id{i}", f"MCWP 3-{i % 500}.1 Machine Guns", "") for i in range(10000)]
        t0 = time.time()
        out = S.pick("marines", docs, set())
        self.assertLess(time.time() - t0, 2.0)
        self.assertEqual(len(out), 500)                   # one per publication number


class TestRetry(unittest.TestCase):
    def test_search_reuses_the_army_retrying_get(self):
        calls = []
        def fake_open(req, timeout=60):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("archive.org 503")
            return mock.Mock(read=lambda: b'{"response": {"docs": []}}')
        with mock.patch.object(S.army.urllib.request, "urlopen", side_effect=fake_open), \
             mock.patch.object(S.army.time, "sleep"), mock.patch.object(S.army.ni, "log"):
            self.assertEqual(S.search("navy"), [])
        self.assertEqual(len(calls), 3)


class TestUnit(unittest.TestCase):
    def test_keep_per_service(self):
        self.assertTrue(S.keep("navy", "Seabee Combat Handbook Navedtra 10479 B"))
        self.assertFalse(S.keep("navy", "British BlueJacket"))
        self.assertTrue(S.keep("marines", "Strategy - MCDP 1-1 - United States Marine Corps"))
        self.assertTrue(S.keep("air_force", "AFPAM 10-100 Airman's Manual"))
        self.assertFalse(S.keep("air_force", "Gamepro Strategy Guides Starfox Flight Manual"))
        self.assertFalse(S.keep("air_force", "FM 57-1 Doctrine for Airborne Operations"))
        self.assertFalse(S.keep("space_force", "Royal Space Force - The Wings Of Honneamise Pamphlet"))
        self.assertTrue(S.keep("national_guard", "The National Guard Manual (Basic)"))
        self.assertFalse(S.keep("national_guard", "Installation Restoration Program Remedial Investigation National Guard training"))

    def test_pub_key_groups_copies(self):
        self.assertEqual(S.pub_key("MCWP 3-15.1 Machine Guns"), S.pub_key("mcwp_3-15.1 machine guns and machine gunnery"))
        self.assertEqual(S.pub_key(""), "")

    def test_pick_skips_already_seen_publications(self):
        seen = {S.pub_key("MCDP 1-1 Strategy")}
        out = S.pick("marines", [("a", "MCDP 1-1 Strategy", ""), ("b", "MCDP 1 Warfighting", "")], seen)
        self.assertEqual([o[0] for o in out], ["b"])


class TestIntegration(unittest.TestCase):
    def test_reuses_army_helpers_and_labels_by_service(self):
        self.assertIn("import nova_ingest_army_manuals as army", SRC)
        self.assertIn('f"military_doctrine_{a.service}"', SRC)
        self.assertEqual(set(S.SERVICES), {"navy", "air_force", "marines", "space_force", "national_guard"})


class TestFunctional(unittest.TestCase):
    def _run(self, rows=(), target=3, dry=False):
        cur = _Cur(rows)
        conn = mock.Mock(cursor=lambda: cur)
        remembered = []
        with mock.patch.object(S.army, "_connect", return_value=conn), \
             mock.patch.object(S, "search", return_value=[("x1", "MCDP 1 Warfighting", "1997"),
                                                          ("x2", "MCDP 1 Warfighting (copy)", "1997"),
                                                          ("x3", "MCWP 3-15.1 Machine Guns", "")]), \
             mock.patch.object(S.army, "ocr_text", return_value="text"), \
             mock.patch.object(S.ni, "clean_text", side_effect=lambda t: t), \
             mock.patch.object(S.ni, "chunk_prose", return_value=["a", "b"]), \
             mock.patch.object(S.ni, "is_garbage", return_value=False), \
             mock.patch.object(S.ni, "remember", side_effect=lambda t, src, *a: remembered.append(src) or True), \
             mock.patch.object(S.ni, "notify"), mock.patch.object(S.ni, "log"), mock.patch.object(S.time, "sleep"):
            rc = S.main(["--service", "marines", "--target", str(target)] + (["--dry-run"] if dry else []))
        return rc, cur, remembered

    def test_golden_path_stops_at_the_cap_and_dedups(self):
        rc, cur, remembered = self._run(target=3)
        self.assertEqual(rc, 0)
        self.assertEqual(len(remembered), 3)               # cap honoured mid-publication
        self.assertEqual(set(remembered), {"military_doctrine_marines"})
        inserted = [p for s, p in cur.sql if s.startswith("INSERT INTO ia_ingest_seen")]
        self.assertEqual([p[0] for p in inserted], ["x1", "x3"])   # the copy of MCDP 1 was skipped

    def test_dry_run_writes_no_rows(self):
        rc, cur, _ = self._run(target=10, dry=True)
        self.assertFalse(any(s.startswith(("INSERT", "CREATE", "ALTER")) for s, _ in cur.sql))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ingest_service_manuals.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)


if __name__ == "__main__":
    unittest.main()
