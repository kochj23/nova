"""Tests for nova_ingest_book_of_mormon.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import nova_ingest_book_of_mormon as M  # noqa: E402

SRC = (SCRIPTS / "nova_ingest_book_of_mormon.py").read_text()
SAMPLE = "\n".join(["*** START OF THE PROJECT GUTENBERG EBOOK THE BOOK OF MORMON ***", "Title page.", "", "Contents", "",
                    "THE FIRST BOOK OF NEPHI HIS REIGN AND MINISTRY", "THE BOOK OF JACOB", "",
                    "THE FIRST BOOK OF NEPHI HIS REIGN AND MINISTRY (1 Nephi)", "An account of Lehi.",
                    "1 And it came to pass that I, Nephi, ...", "",
                    "THE BOOK OF JACOB (Jacob)", "The words of Jacob.",
                    "*** END OF THE PROJECT GUTENBERG EBOOK THE BOOK OF MORMON ***"])


class TestSecurity(unittest.TestCase):
    def test_public_domain_source_and_no_credentials(self):
        self.assertIn("epub/17/", M.URL)
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")


class TestPerformance(unittest.TestCase):
    def test_passages_of_1mb_fast(self):
        t0 = time.time()
        out = M.passages("Alma", "word " * 200000)
        self.assertLess(time.time() - t0, 3.0)
        self.assertGreater(len(out), 300)


class TestRetry(unittest.TestCase):
    def test_fetch_retries_then_succeeds(self):
        calls = []
        def fake(req, timeout=300):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("reset")
            return type("R", (), {"read": lambda self: b"ok"})()
        with mock.patch.object(M.urllib.request, "urlopen", side_effect=fake), mock.patch.object(M.ni, "log"):
            self.assertEqual(M.fetch(_sleep=lambda s: None), "ok")
        self.assertEqual(len(calls), 3)


class TestUnit(unittest.TestCase):
    def test_books_found_from_body_headings(self):
        bs = M.books(SAMPLE)
        self.assertEqual([n for n, _b in bs], ["1 Nephi", "Jacob"])
        self.assertIn("An account of Lehi", dict(bs)["1 Nephi"])

    def test_passages_tagged_by_book(self):
        out = M.passages("1 Nephi", "w " * 800)
        self.assertEqual(out[0][1]["book"], "1 Nephi")
        self.assertTrue(out[0][0].startswith("[Book of Mormon, 1 Nephi (passage 1)] "))

    def test_no_book_found_gives_empty_list(self):
        self.assertEqual(M.books("*** START OF x\nContents\n*** END OF x"), [])


class TestIntegration(unittest.TestCase):
    def test_uses_shared_ingest_and_book_of_mormon_source(self):
        self.assertIn("import nova_ingest as ni", SRC)
        self.assertEqual(M.SOURCE, "book_of_mormon")


class TestFunctional(unittest.TestCase):
    def test_golden_path_stores_under_book_of_mormon(self):
        f = Path(os.environ.get("TMPDIR", "/tmp")) / "nova_bom_sample.txt"
        f.write_text(SAMPLE)
        got = []
        with mock.patch.object(M.ni, "remember", side_effect=lambda t, s, m, d: got.append(s) or True), \
             mock.patch.object(M.ni, "notify"), mock.patch.object(M.ni, "log"):
            self.assertEqual(M.main(["--file", str(f)]), 0)
        self.assertEqual(set(got), {"book_of_mormon"})

    def test_dry_run_stores_nothing(self):
        f = Path(os.environ.get("TMPDIR", "/tmp")) / "nova_bom_sample.txt"
        f.write_text(SAMPLE)
        with mock.patch.object(M.ni, "remember") as r, mock.patch.object(M.ni, "notify") as n:
            M.main(["--file", str(f), "--dry-run"])
        r.assert_not_called(); n.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ingest_book_of_mormon.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)


if __name__ == "__main__":
    unittest.main()
