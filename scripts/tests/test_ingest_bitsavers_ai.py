#!/usr/bin/env python3
"""Tests for ingest_bitsavers_ai.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch("logging.basicConfig"):          # never attach a FileHandler to the real log
        spec.loader.exec_module(mod)
    return mod


bs = _load("ingest_bitsavers_ai_t", SCRIPTS / "ingest_bitsavers_ai.py")
bs.nova_notify = MagicMock()                    # stub the notification bus at load
SRC = (SCRIPTS / "ingest_bitsavers_ai.py").read_text()


class _Resp:
    def __init__(self, body=b"", headers=None):
        self.body = body; self.headers = headers or {}

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


LISTING = b'<a href="a.pdf">a</a><a href="B.PDF">b</a><a href="sub/">s</a><a href="?C=N">x</a><a href="/up/">u</a>'


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_directory_parser_ignores_absolute_and_query_links(self):
        with patch.object(bs.urllib.request, "urlopen", return_value=_Resp(LISTING)):
            pdfs, subdirs = bs.fetch_directory("/x/")
        self.assertEqual(pdfs, ["a.pdf", "B.PDF"])
        self.assertEqual(subdirs, ["sub/"])           # no "/up/" escape, no "?C=N" sort link

    def test_oversized_pdf_refused_by_content_length(self):
        r = _Resp(b"x" * 200, {"Content-Length": str(60 * 1024 * 1024)})
        with patch.object(bs.urllib.request, "urlopen", return_value=r):
            self.assertIsNone(bs.download_pdf("https://e/x.pdf"))


class TestPerformance(unittest.TestCase):
    def test_chunk_text_large_input_fast(self):
        text = " ".join(f"word{i}" for i in range(10_000 * 10))
        t0 = time.perf_counter()
        chunks = bs.chunk_text(text)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertGreater(len(chunks), 200)


class TestRetry(unittest.TestCase):
    def test_store_memory_is_one_shot_and_fails_open(self):
        # RETRY GAP: store_memory — a failing vector POST is tried once and returns False
        m = MagicMock(side_effect=OSError("down"))
        with patch.object(bs.urllib.request, "urlopen", m):
            self.assertFalse(bs.store_memory("t", "v"))
        self.assertEqual(m.call_count, 1)

    def test_fetch_and_download_fail_open(self):
        # RETRY GAP: fetch_directory/download_pdf — one attempt each, safe empty defaults
        with patch.object(bs.urllib.request, "urlopen", side_effect=OSError("x")):
            self.assertEqual(bs.fetch_directory("/x/"), ([], []))
            self.assertIsNone(bs.download_pdf("https://e/x.pdf"))


class TestUnit(unittest.TestCase):
    def test_chunk_text_edges(self):
        self.assertEqual(bs.chunk_text("   "), [])
        self.assertEqual(bs.chunk_text("tiny"), [])          # < 50 chars dropped
        words = " ".join(f"w{i:04d}" for i in range(1000))
        chunks = bs.chunk_text(words, chunk_size=500, overlap=50)
        self.assertEqual(len(chunks), 3)
        self.assertTrue(chunks[1].startswith("w0450"))

    def test_download_small_body_is_none(self):
        with patch.object(bs.urllib.request, "urlopen", return_value=_Resp(b"tiny")):
            self.assertIsNone(bs.download_pdf("https://e/x.pdf"))

    def test_extract_text_bad_pdf_returns_empty(self):
        self.assertEqual(bs.extract_text_from_pdf(b"not a pdf"), "")

    def test_notify_splits_title_and_body(self):
        bs.nova_notify.reset_mock()
        bs.notify("Title line\nbody here")
        args, kw = bs.nova_notify.call_args
        self.assertEqual(args[0], "Title line")
        self.assertEqual(kw["body"], "body here")
        self.assertEqual(kw["dedup_key"], "bitsavers-ai-ingest")


class TestIntegration(unittest.TestCase):
    def test_uses_shared_config_and_notify_bus(self):
        import nova_config
        self.assertEqual(bs.VECTOR_URL, nova_config.VECTOR_URL)
        self.assertIn("from nova_notify import notify", SRC)

    def test_store_memory_posts_json_to_vector_url(self):
        seen = {}

        def fake(req, timeout=None):
            seen["url"] = req.full_url; seen["body"] = json.loads(req.data)
            return _Resp()
        with patch.object(bs.urllib.request, "urlopen", side_effect=fake):
            self.assertTrue(bs.store_memory("hello", "ai_foundations", {"k": 1}))
        self.assertEqual(seen["url"], bs.VECTOR_URL)
        self.assertEqual(seen["body"], {"text": "hello", "source": "ai_foundations", "metadata": {"k": 1}})

    def test_crawl_respects_max_pdfs(self):
        with patch.object(bs, "fetch_directory", return_value=(["a.pdf", "b.pdf", "c.pdf"], ["s/"])), \
                patch.object(bs.time, "sleep"):
            urls = bs.crawl_pdfs("/x/", max_depth=2, max_pdfs=4)
        self.assertEqual(len(urls), 4)
        self.assertEqual(urls[0], bs.BASE_URL + "/x/a.pdf")


class TestFunctional(unittest.TestCase):
    def setUp(self):
        bs.nova_notify.reset_mock()
        bs._stats.update(total_memories=0, total_pdfs=0, errors=0, recent_memories=[], last_notify=time.time())

    def test_ingest_collection_golden_path(self):
        coll = dict(bs.COLLECTIONS[0], target=3)
        store = MagicMock(return_value=True)
        with patch.object(bs, "crawl_pdfs", return_value=["https://e/p/one.pdf"]), \
                patch.object(bs, "download_pdf", return_value=b"%PDF"), \
                patch.object(bs, "extract_text_from_pdf", return_value=" ".join(["word"] * 3000)), \
                patch.object(bs, "store_memory", store), patch.object(bs.time, "sleep"):
            bs.ingest_collection(coll)
        self.assertEqual(store.call_count, 3)                 # stops at target
        self.assertEqual(store.call_args[0][1], coll["vector"])
        self.assertEqual(bs._stats["total_memories"], 3)
        self.assertIn("Completed", bs.nova_notify.call_args[0][0])

    def test_main_survives_a_failing_collection(self):
        with patch.object(bs, "ingest_collection", side_effect=RuntimeError("boom")):
            bs.main()
        self.assertEqual(bs._stats["errors"], len(bs.COLLECTIONS))
        titles = [c[0][0] for c in bs.nova_notify.call_args_list]
        self.assertTrue(any("Error in" in t for t in titles))
        self.assertIn("Complete", titles[-1])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            (Path(home) / ".openclaw/logs").mkdir(parents=True)
            r = subprocess.run([sys.executable, "-c", "import ingest_bitsavers_ai"], cwd=str(SCRIPTS),
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
