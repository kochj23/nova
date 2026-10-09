"""Tests for nova_ingest_bible.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import nova_ingest_bible as B  # noqa: E402

SRC = (SCRIPTS / "nova_ingest_bible.py").read_text()


def _sample(n_books=39):
    toc = [f"Book {i}" for i in range(n_books)]
    body = []
    for i, t in enumerate(toc):
        body += [t, "", f"1:1 In the beginning of book {i}.", "", "1:2 A second verse that wraps",
                 "onto the next line.", "", "2:1 Chapter two opens. ***"]
    return "\n".join(["Header", "The Old Testament of the King James Version of the Bible", *toc,
                      B.NT_HEADING, "The Gospel According to Saint Matthew", "", *body,
                      B.NT_HEADING, "1:1 New Testament text."])


class TestSecurity(unittest.TestCase):
    def test_no_credentials_and_fixed_public_source(self):
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")
        self.assertTrue(B.URL.startswith("https://www.gutenberg.org/"))

    def test_stops_before_the_new_testament(self):
        books = B.split_books(_sample())
        self.assertFalse(any("New Testament text" in body for _b, body in books))


class TestPerformance(unittest.TestCase):
    def test_chunk_10k_verses_fast(self):
        vs = [(c, v, "word " * 20) for c in range(1, 101) for v in range(1, 101)]
        t0 = time.time()
        out = B.chunks("Psalms", vs)
        self.assertLess(time.time() - t0, 2.0)
        self.assertGreater(len(out), 100)


class TestRetry(unittest.TestCase):
    def test_fetch_retries_then_succeeds(self):
        calls = []
        def fake(req, timeout=120):
            calls.append(1)
            if len(calls) < 3:
                raise OSError("reset")
            return mock.Mock(read=lambda: b"ok")
        with mock.patch.object(B.urllib.request, "urlopen", side_effect=fake), mock.patch.object(B.ni, "log"):
            self.assertEqual(B.fetch(_sleep=lambda s: None), "ok")
        self.assertEqual(len(calls), 3)


class TestUnit(unittest.TestCase):
    def test_split_books_maps_titles_to_canonical_names(self):
        books = B.split_books(_sample())
        self.assertEqual([b for b, _ in books], B.OT_BOOKS)
        self.assertIn("1:1 In the beginning of book 0.", books[0][1])

    def test_verses_join_wrapped_lines_and_strip_separator(self):
        vs = B.verses("1:1 Alpha.\n\n1:2 Beta that\nwraps.\n\n2:1 Gamma. ***")
        self.assertEqual(vs, [(1, 1, "Alpha."), (1, 2, "Beta that wraps."), (2, 1, "Gamma.")])

    def test_chunks_never_cross_a_chapter_and_carry_refs(self):
        out = B.chunks("Ruth", [(1, 1, "a"), (1, 2, "b"), (2, 1, "c")])
        self.assertEqual([m["chapter"] for _t, m in out], [1, 2])
        self.assertTrue(out[0][0].startswith("[Ruth 1:1-2 (KJV)]"))
        self.assertEqual(out[1][1]["verses"], "1-1")


class TestIntegration(unittest.TestCase):
    def test_uses_shared_ingest_and_bible_source(self):
        self.assertIn("import nova_ingest as ni", SRC)
        self.assertEqual(B.SOURCE, "bible")
        self.assertEqual(len(B.OT_BOOKS), 39)


class TestFunctional(unittest.TestCase):
    def test_golden_path_remembers_every_passage(self):
        f = Path(os.environ.get("TMPDIR", "/tmp")) / "nova_bible_sample.txt"
        f.write_text(_sample())
        got = []
        with mock.patch.object(B.ni, "remember", side_effect=lambda t, s, m, d: got.append((s, m["book"])) or True), \
             mock.patch.object(B.ni, "notify"), mock.patch.object(B.ni, "log"):
            self.assertEqual(B.main(["--file", str(f)]), 0)
        self.assertEqual({s for s, _ in got}, {"bible"})
        self.assertEqual(len(got), 39 * 2)            # two chapters per sample book

    def test_dry_run_stores_nothing(self):
        f = Path(os.environ.get("TMPDIR", "/tmp")) / "nova_bible_sample.txt"
        f.write_text(_sample())
        with mock.patch.object(B.ni, "remember") as r, mock.patch.object(B.ni, "notify") as n:
            B.main(["--file", str(f), "--dry-run"])
        r.assert_not_called(); n.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ingest_bible.py"), "--help"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
