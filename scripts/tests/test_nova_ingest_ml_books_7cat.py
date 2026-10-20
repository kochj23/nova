#!/usr/bin/env python3
"""nova_ingest_ml_books.py — 7 house categories (Security, Performance, Retry, Unit, Integration, Functional,
Frame). No network, no PG, no memory server: downloads, pdftotext and ni.remember are fakes. Written by Jordan
Koch (via Claude)."""
import io
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_ingest_ml_books as mb  # noqa: E402

PROSE = ("Batch normalization forces the empirical mean and variance of groups of activations to fixed values, "
         "which stabilises training of deep architectures and lets them use larger learning rates in practice. ")


def args(**k):
    d = dict(dry_run=False, pace=0.0, force=False, only=[], list=False, selftest=False)
    d.update(k)
    return types.SimpleNamespace(**d)


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestSecurity(unittest.TestCase):
    def test_non_pdf_response_is_never_written(self):
        with TemporaryDirectory() as d, patch.object(mb.urllib.request, "urlopen",
                                                     return_value=Resp(b"<html>bot challenge</html>")), \
             patch.object(mb.ni, "log"):
            dest = Path(d) / "x.pdf"
            self.assertFalse(mb.download("https://example.org/x.pdf", dest))
            self.assertFalse(dest.exists())

    def test_manifest_has_no_mirror_or_pirate_hosts(self):
        bad = ("vdoc.pub", "libgen", "z-lib", "freecomputerbooks.com", "pdfdrive", "dokumen.pub", "scribd")
        for b in mb.BOOKS:
            self.assertFalse(any(h in str(b[5]).lower() for h in bad), b[0])

    def test_html_crawl_stays_on_the_books_own_site(self):
        toc = "https://book.example.org/index.html"
        page = '<a href="ch1.html">1</a><a href="https://evil.example.net/ch2.html">2</a>'
        fetched = []
        with patch.object(mb.ni, "fetch", side_effect=lambda u: fetched.append(u) or page), \
             patch.object(mb.ni, "html_text", return_value=PROSE), patch.object(mb.time, "sleep"):
            mb.html_chapters(toc, r'href="([^"]+\.html)"', 0)
        self.assertTrue(all("evil.example.net" not in u for u in fetched))
        self.assertIn("https://book.example.org/ch1.html", fetched)

    def test_user_agent_is_honest(self):
        self.assertIn("NovaIngest", mb.UA["User-Agent"])


class TestPerformance(unittest.TestCase):
    def test_junk_filter_and_unshadow_on_2000_chunks(self):
        chunks = [PROSE * 3] * 2000
        t = time.perf_counter()
        for c in chunks:
            mb.is_junk(c)
            mb.unshadow(c)
        self.assertLess(time.perf_counter() - t, 2.0)

    def test_split_long_one_megabyte(self):
        t = time.perf_counter()
        out = mb.split_long("A sentence that ends. " * 50000, mb.ni.CHUNK_CHARS)
        self.assertLess(time.perf_counter() - t, 2.0)
        self.assertTrue(out)


class TestRetry(unittest.TestCase):
    def test_one_failed_book_does_not_stop_the_rest(self):
        two = [mb.BOOKS[0], mb.BOOKS[1]]
        calls = []

        def ingest(b, a, done):
            calls.append(b[0])
            if b[0] == two[0][0]:
                raise ConnectionError("reset")
            return 7
        with patch.object(mb, "BOOKS", two), patch.object(mb, "ingest_book", side_effect=ingest), \
             patch.object(mb, "already_ingested", return_value=0), patch.object(mb.ni, "log"), \
             patch.object(mb.ni, "notify"), patch.object(mb, "STORE", Path("/tmp")), redirect_stdout(io.StringIO()):
            self.assertEqual(mb.main([]), 0)
        self.assertEqual(calls, [two[0][0], two[1][0]])

    def test_already_downloaded_file_is_not_refetched(self):
        with TemporaryDirectory() as d:
            dest = Path(d) / "x.pdf"
            dest.write_bytes(b"%PDF" + b"0" * 20000)
            with patch.object(mb.urllib.request, "urlopen", side_effect=AssertionError("refetched")):
                self.assertTrue(mb.download("https://example.org/x.pdf", dest))

    def test_already_ingested_book_is_skipped_unless_forced(self):
        with patch.object(mb, "BOOKS", [mb.BOOKS[0]]), patch.object(mb, "already_ingested", return_value=981), \
             patch.object(mb, "ingest_book", return_value=0) as ib, patch.object(mb.ni, "log"), patch.object(mb, "STORE", Path("/tmp")), \
             redirect_stdout(io.StringIO()):
            mb.main([])
            ib.assert_not_called()
            mb.main(["--force"])
            ib.assert_called_once()


class TestUnit(unittest.TestCase):
    def test_selftest_pure_functions(self):
        with redirect_stdout(io.StringIO()):
            mb.selftest()

    def test_slugs_unique_and_kinds_known(self):
        self.assertEqual(len({b[0] for b in mb.BOOKS}), len(mb.BOOKS))
        self.assertTrue({b[4] for b in mb.BOOKS} <= {"pdf", "arxiv", "gdrive", "html", "ipynb", "article"})

    def test_skip_list_never_overlaps_manifest(self):
        titles = {b[1].lower() for b in mb.BOOKS}
        self.assertFalse(titles & {t.lower() for t, _ in mb.SKIPPED})


class TestIntegration(unittest.TestCase):
    def test_pdf_book_chunks_carry_book_page_and_license(self):
        stored = []
        page = (PROSE * 4 + "\n\n") * 3
        with TemporaryDirectory() as d, patch.object(mb, "STORE", Path(d)), \
             patch.object(mb, "download", return_value=True), \
             patch.object(mb.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, page + "\f" + page, "")), \
             patch.object(mb.ni, "remember", side_effect=lambda t, s, m, d_, dr: stored.append((t, s, m)) or 1), \
             patch.object(mb.time, "sleep"):
            n = mb.ingest_book(mb.BOOKS[0], args(), set())
        self.assertEqual(n, len(stored))
        text, source, meta = stored[0]
        self.assertTrue(text.startswith(f"[{mb.BOOKS[0][1]} — p.1]"))
        self.assertEqual(source, mb.SOURCE)
        self.assertEqual((meta["book"], meta["license"]), (mb.BOOKS[0][1], mb.BOOKS[0][3]))

    def test_arxiv_book_links_the_abstract_page(self):
        arx = next(b for b in mb.BOOKS if b[4] == "arxiv")
        seen = {}
        with TemporaryDirectory() as d, patch.object(mb, "STORE", Path(d)), \
             patch.object(mb, "download", side_effect=lambda u, dest: seen.setdefault("url", u) and True), \
             patch.object(mb, "pdf_chunks", return_value=[(1, PROSE * 3)]), \
             patch.object(mb.ni, "remember", side_effect=lambda t, s, m, d_, dr: seen.setdefault("meta", m) and 1):
            mb.ingest_book(arx, args(dry_run=True), set())
        self.assertEqual(seen["url"], f"https://arxiv.org/pdf/{arx[5]}")
        self.assertEqual(seen["meta"]["url"], f"https://arxiv.org/abs/{arx[5]}")


class TestFunctional(unittest.TestCase):
    def test_list_fetches_nothing_and_prints_both_lists(self):
        out = io.StringIO()
        with patch.object(mb.urllib.request, "urlopen", side_effect=AssertionError("fetched")), redirect_stdout(out):
            self.assertEqual(mb.main(["--list"]), 0)
        self.assertIn("INGEST udl", out.getvalue())
        self.assertIn("SKIP   Machine Learning Yearning", out.getvalue())

    def test_dry_run_notifies_nobody(self):
        with patch.object(mb, "BOOKS", [mb.BOOKS[0]]), patch.object(mb, "ingest_book", return_value=12), \
             patch.object(mb.ni, "notify") as notify, patch.object(mb.ni, "log"), patch.object(mb, "STORE", Path("/tmp")), \
             redirect_stdout(io.StringIO()):
            mb.main(["--dry-run"])
        notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        p = subprocess.run([sys.executable, str(SCRIPTS / "nova_ingest_ml_books.py"), "--selftest"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stderr[-300:])

    def test_help_exits_zero(self):
        p = subprocess.run([sys.executable, str(SCRIPTS / "nova_ingest_ml_books.py"), "--help"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0)

    def test_import_has_no_side_effects(self):
        self.assertTrue(callable(mb.main))
        self.assertIsInstance(mb.STORE, Path)


if __name__ == "__main__":
    unittest.main()
