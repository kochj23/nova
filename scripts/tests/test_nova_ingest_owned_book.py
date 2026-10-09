"""Tests for nova_ingest_owned_book.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import nova_ingest_owned_book as O  # noqa: E402

SRC = (SCRIPTS / "nova_ingest_owned_book.py").read_text()
SAMPLE = "\n".join(["Front", "Table of Contents", "Introduction", "The First Book of Moses, Genesis",
                    "The Book of Ruth", "", "Introduction", "Some intro text.", "",
                    "The First Book of Moses, Genesis", "In the beginning God created the heaven. 1 And the earth.",
                    "", "The Book of Ruth", "Ruth went to the field. 2 She gleaned."])


class TestSecurity(unittest.TestCase):
    def test_private_source_and_privacy_tag(self):
        self.assertEqual(O.SOURCE, "private_document")
        items = O.passages("Ruth", "text " * 10, "Translator")
        self.assertTrue(all(m["privacy"] == "private" for _t, m in items))

    def test_no_credentials_hardcoded(self):
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")


class TestPerformance(unittest.TestCase):
    def test_passages_of_2mb_fast(self):
        body = " ".join(["word"] * 400000)
        t0 = time.time()
        out = O.passages("Big", body, "T")
        self.assertLess(time.time() - t0, 3.0)
        self.assertGreater(len(out), 500)


class TestRetry(unittest.TestCase):
    def test_no_external_call_so_nothing_to_retry(self):
        # The script reads a local text file and writes through nova_ingest; it makes no network calls.
        self.assertNotIn("urlopen", SRC)


class TestUnit(unittest.TestCase):
    def test_books_found_in_order_from_toc(self):
        bs = O.books(SAMPLE)
        self.assertEqual([t for t, _ in bs], ["The First Book of Moses, Genesis", "The Book of Ruth"])
        self.assertIn("God created", dict(bs)["The First Book of Moses, Genesis"])

    def test_passages_keep_book_and_number(self):
        out = O.passages("Ruth", " ".join(["w"] * 1000), "Asher Wilson (2024)", size=500)
        self.assertGreater(len(out), 1)
        self.assertEqual([m["passage"] for _t, m in out], list(range(1, len(out) + 1)))
        self.assertTrue(out[0][0].startswith("[Ruth (passage 1)] "))

    def test_empty_body_gives_no_passages(self):
        self.assertEqual(O.passages("Empty", "", "T"), [])


class TestIntegration(unittest.TestCase):
    def test_uses_shared_ingest_helpers(self):
        self.assertIn("import nova_ingest as ni", SRC)
        self.assertIn("ni.remember(", SRC)


class TestFunctional(unittest.TestCase):
    def test_golden_path_stores_private_passages(self):
        f = Path(os.environ.get("TMPDIR", "/tmp")) / "nova_owned_sample.txt"
        f.write_text(SAMPLE)
        got = []
        with mock.patch.object(O.ni, "remember", side_effect=lambda t, s, m, d: got.append((s, m["privacy"])) or True), \
             mock.patch.object(O.ni, "notify"):
            self.assertEqual(O.main([str(f), "--title", "T", "--translator", "X"]), 0)
        self.assertTrue(got)
        self.assertEqual(set(got), {("private_document", "private")})

    def test_dry_run_stores_nothing(self):
        f = Path(os.environ.get("TMPDIR", "/tmp")) / "nova_owned_sample.txt"
        f.write_text(SAMPLE)
        with mock.patch.object(O.ni, "remember") as r, mock.patch.object(O.ni, "notify") as n:
            O.main([str(f), "--title", "T", "--translator", "X", "--dry-run"])
        r.assert_not_called(); n.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ingest_owned_book.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)


if __name__ == "__main__":
    unittest.main()
