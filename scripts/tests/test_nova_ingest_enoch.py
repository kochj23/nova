"""Tests for nova_ingest_enoch.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import nova_ingest_enoch as E  # noqa: E402

SRC = (SCRIPTS / "nova_ingest_enoch.py").read_text()
SAMPLE = """\
Some front matter of the scan.

CHAP.  I.

1.  The word of the blessing of Enoch, how he blessed the elect.

12  ENOCH.

2.  From them I heard all things, and understood what I saw.

*  to the rejection of.  N.B.  The Italic words in the text.

CHAP.  II.

3.  Upon their account I spoke and conversed.

PRINTED BY WILLIAM CLOWES AND SONS, LIMITED, LONDON AND BECCLES.
"""


class TestSecurity(unittest.TestCase):
    def test_no_credentials_and_public_source(self):
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")
        self.assertTrue(E.URL.startswith("https://archive.org/"))

    def test_only_translation_text_is_kept(self):
        ps = E.paragraphs(E.translation_text(SAMPLE))
        self.assertFalse(any("front matter" in p or "CLOWES" in p.upper() for p in ps))


class TestPerformance(unittest.TestCase):
    def test_pack_20k_paragraphs_fast(self):
        paras = [f"{i}. " + "word " * 30 for i in range(20000)]
        t0 = time.time()
        out = E.passages(paras)
        self.assertLess(time.time() - t0, 2.0)
        self.assertGreater(len(out), 100)


class TestRetry(unittest.TestCase):
    def test_fetch_retries_then_succeeds(self):
        calls = []
        def fake(req, timeout=300):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("reset")
            return type("R", (), {"read": lambda self: b"ok"})()
        with mock.patch.object(E.urllib.request, "urlopen", side_effect=fake), mock.patch.object(E.ni, "log"):
            self.assertEqual(E.fetch(_sleep=lambda s: None), "ok")
        self.assertEqual(len(calls), 3)


class TestUnit(unittest.TestCase):
    def test_translation_starts_at_chapter_one(self):
        self.assertTrue(E.translation_text(SAMPLE).lstrip().startswith("CHAP."))

    def test_missing_start_heading_fails_loudly(self):
        with self.assertRaises(ValueError):
            E.translation_text("no headings here")

    def test_footnotes_and_running_heads_removed(self):
        ps = E.paragraphs(E.translation_text(SAMPLE))
        self.assertEqual(ps, ["1. The word of the blessing of Enoch, how he blessed the elect.",
                              "2. From them I heard all things, and understood what I saw.",
                              "3. Upon their account I spoke and conversed."])

    def test_passages_tag_book_and_passage(self):
        out = E.passages(["a" * 800, "b" * 800])
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0][1]["book"], "1 Enoch")
        self.assertEqual([m["passage"] for _t, m in out], [1, 2])


class TestIntegration(unittest.TestCase):
    def test_uses_shared_ingest_and_apocrypha_source(self):
        self.assertIn("import nova_ingest as ni", SRC)
        self.assertEqual(E.SOURCE, "apocrypha")
        self.assertNotEqual(E.SOURCE, "bible")


class TestFunctional(unittest.TestCase):
    def test_golden_path_stores_under_apocrypha(self):
        f = Path(os.environ.get("TMPDIR", "/tmp")) / "nova_enoch_sample.txt"
        f.write_text(SAMPLE)
        got = []
        with mock.patch.object(E.ni, "remember", side_effect=lambda t, s, m, d: got.append(s) or True), \
             mock.patch.object(E.ni, "notify"), mock.patch.object(E.ni, "log"):
            self.assertEqual(E.main(["--file", str(f)]), 0)
        self.assertEqual(set(got), {"apocrypha"})

    def test_dry_run_stores_nothing(self):
        f = Path(os.environ.get("TMPDIR", "/tmp")) / "nova_enoch_sample.txt"
        f.write_text(SAMPLE)
        with mock.patch.object(E.ni, "remember") as r, mock.patch.object(E.ni, "notify") as n:
            E.main(["--file", str(f), "--dry-run"])
        r.assert_not_called(); n.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ingest_enoch.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)


if __name__ == "__main__":
    unittest.main()
