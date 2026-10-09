"""Tests for nova_ingest_tanakh.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import nova_ingest_tanakh as T  # noqa: E402

SRC = (SCRIPTS / "nova_ingest_tanakh.py").read_text()
SHORT = [("Torah", "Genesis", ["GENESIS"]), ("Torah", "Exodus", ["EXODUS"]), ("Torah", "Leviticus", ["LEVITICUS"])]
SAMPLE = "\n".join(["Front matter", "GENESIS", "GENESIS", "I", "In the beginning God created the heaven and the earth. © Heb. Edom.",
                    "2 And the earth was unformed.", "GENESIS", "42", "EXODUS", "EXODUS",
                    "Now these are the names of the children of Israel.", "1 Moses went up.", "LEVITICUS", "LEVITICUS",
                    "And the LORD spoke."])


class TestSecurity(unittest.TestCase):
    def test_public_domain_scan_and_no_credentials(self):
        self.assertIn("tanakh-1917_202402", T.URL)
        self.assertNotIn("2026", T.URL)
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")


class TestPerformance(unittest.TestCase):
    def test_passages_of_1mb_fast(self):
        t0 = time.time()
        out = T.passages("Torah", "Genesis", "word " * 200000)
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
        with mock.patch.object(T.urllib.request, "urlopen", side_effect=fake), mock.patch.object(T.ni, "log"):
            self.assertEqual(T.fetch(_sleep=lambda s: None), "ok")
        self.assertEqual(len(calls), 3)


class TestUnit(unittest.TestCase):
    def test_books_in_tanakh_order_with_divisions(self):
        bs = T.split_books(SAMPLE, SHORT)
        self.assertEqual([b for _d, b, _x in bs], ["Genesis", "Exodus", "Leviticus"])
        self.assertEqual(bs[0][0], "Torah")

    def test_running_heads_and_footnotes_removed(self):
        body = dict((b, x) for _d, b, x in T.split_books(SAMPLE, SHORT))["Genesis"]
        self.assertNotIn("GENESIS", body)
        self.assertNotIn("©", body)
        self.assertNotIn(" 42 ", " " + body + " ")
        self.assertIn("In the beginning God created", body)

    def test_passages_keep_book_and_division(self):
        out = T.passages("Torah", "Genesis", "w " * 800)
        self.assertEqual(out[0][1]["division"], "Torah")
        self.assertTrue(out[0][0].startswith("[Genesis (JPS 1917, passage 1)] "))

    def test_missing_heading_fails_loudly(self):
        with self.assertRaises(ValueError):
            T.split_books("no books here", SHORT)


class TestIntegration(unittest.TestCase):
    def test_uses_shared_ingest_and_tanakh_source(self):
        self.assertIn("import nova_ingest as ni", SRC)
        self.assertEqual(T.SOURCE, "tanakh")
        self.assertEqual(len(T.ORDER), 39)


class TestFunctional(unittest.TestCase):
    def test_golden_path_stores_under_tanakh(self):
        f = Path(os.environ.get("TMPDIR", "/tmp")) / "nova_tanakh_sample.txt"
        f.write_text(SAMPLE)
        got = []
        with mock.patch.object(T, "ORDER", SHORT), mock.patch.object(T.ni, "remember", side_effect=lambda t, s, m, d: got.append(s) or True), \
             mock.patch.object(T.ni, "notify"), mock.patch.object(T.ni, "log"):
            self.assertEqual(T.main(["--file", str(f)]), 0)
        self.assertEqual(set(got), {"tanakh"})

    def test_dry_run_stores_nothing(self):
        f = Path(os.environ.get("TMPDIR", "/tmp")) / "nova_tanakh_sample.txt"
        f.write_text(SAMPLE)
        with mock.patch.object(T, "ORDER", SHORT), mock.patch.object(T.ni, "remember") as r, mock.patch.object(T.ni, "notify") as n:
            T.main(["--file", str(f), "--dry-run"])
        r.assert_not_called(); n.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ingest_tanakh.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)


if __name__ == "__main__":
    unittest.main()
