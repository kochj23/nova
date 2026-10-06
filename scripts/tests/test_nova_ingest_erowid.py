#!/usr/bin/env python3
"""Tests for nova_ingest_erowid.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
The module installs SIGINT/SIGTERM handlers at import, so the previous handlers are restored after the
load. requests (crawl + memory server), Slack (nova_config.post_both) and time.sleep are mocked;
STATE_FILE / LOG_FILE are redirected to a tempdir. Nothing is crawled or ingested."""
import importlib.util
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_ingest_erowid.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="erowid_"))


def _load():
    spec = importlib.util.spec_from_file_location("nova_ingest_erowid_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    old = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        spec.loader.exec_module(mod)
    finally:
        for s, h in old.items():
            signal.signal(s, h)
    mod.nova_config = types.SimpleNamespace(post_both=MagicMock())
    mod.STATE_FILE = TMP / "state.json"
    mod.LOG_FILE = TMP / "erowid.log"
    mod.print = lambda *a, **k: None
    return mod


er = _load()
PAGE = """<html><head><title>LSD Vault</title><script>var x=1;</script></head><body><nav>menu</nav>
<p>{body}</p>
<a href="/chemicals/lsd/lsd_effects.shtml">effects</a>
<a href="https://evil.example.com/x">offsite</a>
<a href="/images/a.jpg">img</a>
<a href="mailto:x@y.z">mail</a>
<a href="/chemicals/lsd/?q=1#frag">dup with query</a>
</body></html>"""
BODY = "Lysergic acid diethylamide is a potent psychedelic compound studied for decades. " * 30


def _resp(status=200, text="", ctype="text/html; charset=utf-8", js=None):
    return SimpleNamespace(status_code=status, text=text, headers={"content-type": ctype}, json=lambda: js or {})


class TestSecurity(unittest.TestCase):
    def test_no_credentials(self):
        self.assertIsNone(re.search(r"(password|api[_-]?key|token|secret)\s*=\s*['\"]", SRC, re.I))

    def test_crawl_stays_on_erowid_and_skips_assets(self):
        links = er.extract_links(PAGE.format(body="x"), "https://www.erowid.org/chemicals/lsd/")
        self.assertTrue(links)
        self.assertTrue(all(l.startswith(er.BASE_URL) for l in links))
        self.assertFalse(any(".jpg" in l or "mailto" in l or "evil" in l for l in links))
        self.assertFalse(any("?" in l or "#" in l for l in links))

    def test_scripts_stripped_from_text(self):
        self.assertNotIn("var x", er.extract_text(PAGE.format(body="hello")))


class TestPerformance(unittest.TestCase):
    def test_chunk_large_text_fast(self):
        text = "Sentence number one is here. " * 10_000
        t0 = time.perf_counter()
        chunks = er.chunk_text(text)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertTrue(all(len(c) <= er.CHUNK_SIZE + 100 for c in chunks))
        self.assertGreater(len(chunks), 300)


class TestRetry(unittest.TestCase):
    def test_fetch_and_remember_fail_open(self):
        # RETRY GAP: fetch_page()/remember()/random_mem() — one request each; errors -> None/False/""
        with patch.object(er.requests, "get", side_effect=OSError("dns")) as g:
            self.assertIsNone(er.fetch_page("https://www.erowid.org/x"))
        self.assertEqual(g.call_count, 1)
        with patch.object(er.requests, "post", side_effect=OSError("mem down")):
            self.assertFalse(er.remember("t" * 100, {}, set()))
            self.assertEqual(er.random_mem(), "")

    def test_failed_pages_count_errors_and_crawl_continues(self):
        with patch.object(er, "build_seed_queue", return_value=["https://www.erowid.org/a", "https://www.erowid.org/b"]), \
             patch.object(er, "fetch_page", return_value=None) as fp, patch.object(er.time, "sleep"):
            er.main()
        self.assertEqual(fp.call_count, 2)
        self.assertEqual(json.loads(er.STATE_FILE.read_text())["errors"], 2)
        er.STATE_FILE.unlink()

    def test_slack_failure_is_logged(self):
        with patch.object(er.nova_config, "post_both", side_effect=RuntimeError("slack")):
            er.notify("hi")
        self.assertIn("Slack notify failed", er.LOG_FILE.read_text())


class TestUnit(unittest.TestCase):
    def test_chunk_text_edges(self):
        self.assertEqual(er.chunk_text("tiny"), [])
        self.assertEqual(er.chunk_text("x" * 60), ["x" * 60])

    def test_is_garbage(self):
        self.assertTrue(er.is_garbage("short"))
        self.assertTrue(er.is_garbage("We use cookie tracking " + "word " * 30))
        self.assertTrue(er.is_garbage("a" * 80))
        self.assertFalse(er.is_garbage("This is a perfectly reasonable sentence about plants and their effects on people."))

    def test_remember_dedups_by_hash(self):
        seen = set()
        with patch.object(er.requests, "post", return_value=_resp(200)) as p:
            self.assertTrue(er.remember("same text", {}, seen))
            self.assertFalse(er.remember("same text", {}, seen))
        self.assertEqual(p.call_count, 1)
        with patch.object(er.requests, "post", return_value=_resp(409)):
            self.assertFalse(er.remember("other", {}, seen))
        self.assertEqual(len(seen), 2)

    def test_title_and_non_html(self):
        self.assertEqual(er.extract_title("<html></html>"), "Erowid Page")
        with patch.object(er.requests, "get", return_value=_resp(ctype="application/pdf")):
            self.assertIsNone(er.fetch_page("https://www.erowid.org/x.pdf"))


class TestIntegration(unittest.TestCase):
    def test_remember_payload_targets_pharmacology_vector(self):
        with patch.object(er.requests, "post", return_value=_resp(200)) as p:
            er.remember("text body", {"url": "u"}, set())
        url, kw = p.call_args[0][0], p.call_args.kwargs
        self.assertEqual(url, f"{er.MEMORY_SERVER}/remember")
        self.assertEqual((kw["json"]["source"], kw["json"]["tier"]), ("pharmacology", "long_term"))

    def test_seed_queue_has_priority_paths(self):
        seeds = er.build_seed_queue()
        self.assertEqual(seeds[0], "https://www.erowid.org/")
        self.assertIn("https://www.erowid.org/chemicals/", seeds)


class TestFunctional(unittest.TestCase):
    def test_crawl_one_page_ingests_and_saves_state(self):
        html = PAGE.format(body=BODY)
        with patch.object(er, "build_seed_queue", return_value=["https://www.erowid.org/chemicals/lsd/"]), \
             patch.object(er, "fetch_page", side_effect=lambda u: html if u.endswith("/lsd/") else None), \
             patch.object(er.requests, "post", return_value=_resp(200)) as post, \
             patch.object(er.time, "sleep"):
            er.nova_config.post_both.reset_mock()
            er.main()
        self.assertGreater(post.call_count, 1)
        st = json.loads(er.STATE_FILE.read_text())
        self.assertEqual(st["pages_scraped"], 1)
        self.assertEqual(st["chunks_ingested"], post.call_count)
        self.assertIn("https://www.erowid.org/chemicals/lsd/lsd_effects.shtml", st["visited"])
        self.assertIn("Complete", er.nova_config.post_both.call_args_list[-1][0][0])
        er.STATE_FILE.unlink()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_ingest_erowid"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")
        self.assertIsNot(signal.getsignal(signal.SIGINT), er._sig_handler)


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
