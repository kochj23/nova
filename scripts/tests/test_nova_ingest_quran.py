"""Tests for nova_ingest_quran.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import nova_ingest_quran as Q  # noqa: E402

SRC = (SCRIPTS / "nova_ingest_quran.py").read_text()
SAMPLE = "\n".join(["*** START OF THE PROJECT GUTENBERG EBOOK", "Preface text here.",
                    "SURA1 XCVI.-THICK BLOOD, OR CLOTS OF BLOOD [I.]", "Read in the name of thy Lord.",
                    "SURA II.-THE COW1 [XCI.]", "Alif Lam Mim. This is the Book.",
                    "SURA CXIV.-MEN", "Say, I seek refuge.", "*** END OF THE PROJECT GUTENBERG EBOOK"])


class TestSecurity(unittest.TestCase):
    def test_public_domain_source_and_no_credentials(self):
        self.assertIn("pg2800", Q.URL)
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")


class TestPerformance(unittest.TestCase):
    def test_passages_of_500kb_fast(self):
        t0 = time.time()
        out = Q.passages(2, "The Cow", "word " * 100000)
        self.assertLess(time.time() - t0, 3.0)
        self.assertGreater(len(out), 100)


class TestRetry(unittest.TestCase):
    def test_fetch_retries_then_succeeds(self):
        calls = []
        def fake(req, timeout=300):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("reset")
            return type("R", (), {"read": lambda self: b"ok"})()
        with mock.patch.object(Q.urllib.request, "urlopen", side_effect=fake), mock.patch.object(Q.ni, "log"):
            self.assertEqual(Q.fetch(_sleep=lambda s: None), "ok")
        self.assertEqual(len(calls), 3)


class TestUnit(unittest.TestCase):
    def test_roman_numerals(self):
        self.assertEqual([Q.roman(x) for x in ("I", "IV", "XCVI", "CXIV")], [1, 4, 96, 114])

    def test_suras_found_in_order_with_footnote_digits_handled(self):
        ss = Q.suras(SAMPLE)
        self.assertEqual([n for n, _t, _b in ss], [2, 96, 114])
        self.assertEqual(dict((n, t) for n, t, _b in ss)[2], "THE COW")
        self.assertIn("Alif Lam Mim", dict((n, b) for n, _t, b in ss)[2])

    def test_passages_tagged_by_sura(self):
        out = Q.passages(96, "Thick blood", "w " * 800)
        self.assertEqual(out[0][1]["sura"], 96)
        self.assertTrue(out[0][0].startswith("[Qur'an Sura 96 — Thick blood (Rodwell, passage 1)] "))

    def test_notes_flagged_in_metadata(self):
        self.assertTrue(Q.passages(1, "", "w " * 10)[0][1]["includes_translator_notes"])


class TestIntegration(unittest.TestCase):
    def test_uses_shared_ingest_and_quran_source(self):
        self.assertIn("import nova_ingest as ni", SRC)
        self.assertEqual(Q.SOURCE, "quran")


class TestFunctional(unittest.TestCase):
    def test_golden_path_stores_under_quran(self):
        f = Path(os.environ.get("TMPDIR", "/tmp")) / "nova_quran_sample.txt"
        f.write_text(SAMPLE)
        got = []
        with mock.patch.object(Q.ni, "remember", side_effect=lambda t, s, m, d: got.append(s) or True), \
             mock.patch.object(Q.ni, "notify"), mock.patch.object(Q.ni, "log"):
            self.assertEqual(Q.main(["--file", str(f)]), 0)
        self.assertEqual(set(got), {"quran"})

    def test_dry_run_stores_nothing(self):
        f = Path(os.environ.get("TMPDIR", "/tmp")) / "nova_quran_sample.txt"
        f.write_text(SAMPLE)
        with mock.patch.object(Q.ni, "remember") as r, mock.patch.object(Q.ni, "notify") as n:
            Q.main(["--file", str(f), "--dry-run"])
        r.assert_not_called(); n.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ingest_quran.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)


if __name__ == "__main__":
    unittest.main()
