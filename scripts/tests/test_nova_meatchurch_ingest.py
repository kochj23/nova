#!/usr/bin/env python3
"""Tests for nova_meatchurch_ingest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
The site, the memory server, nova_notify and time.sleep are mocked; state lives in a tempdir."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_meatchurch_ingest.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="meatchurch-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("meatchurch_ingest_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mc = _load()
# module-level stubs: no site/memory HTTP, no notify, no real sleeping, state in a tempdir
mc.STATE_FILE = TMP / "state.json"
mc.notify = MagicMock()
mc.log = MagicMock()
mc.time = types.SimpleNamespace(sleep=MagicMock())
mc.urllib = types.SimpleNamespace(request=types.SimpleNamespace(
    Request=mc.urllib.request.Request, urlopen=MagicMock(side_effect=OSError("offline"))))

INDEX = '<a href="/blogs/recipes/brisket-101">x</a><a href="/blogs/recipes/brisket-101">dup</a>' \
        '<a href="/blogs/recipes">all</a><a href="/blogs/recipes/smoked-wings">w</a>'
RECIPE = "<html><h1>Texas Brisket</h1><script>var x=1</script><p>" + " ".join(["smoke"] * 400) + "</p></html>"


def _cm(body):
    r = MagicMock(); r.__enter__.return_value.read.return_value = body.encode(); return r


def _site(url, retries=3):
    if "/blogs/recipes/" in url:
        return RECIPE
    return INDEX if "page=" not in url else None


def _main(argv):
    with patch.object(sys, "argv", ["nova_meatchurch_ingest.py"] + argv):
        mc.main()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_links_restricted_to_site_recipe_paths(self):
        html = '<a href="https://evil.test/blogs/recipes/x">e</a><a href="/blogs/recipes/../../admin">t</a>' \
               '<a href="/blogs/recipes/ok-one">ok</a>'
        self.assertEqual(mc.find_recipe_links(html), [mc.BASE_URL + "/blogs/recipes/ok-one"])

    def test_scripts_and_styles_stripped(self):
        out = mc.strip_html("<style>.a{}</style><script>steal()</script><p>Rub</p>")
        self.assertEqual(out, "Rub")


class TestPerformance(unittest.TestCase):
    def test_chunk_100k_words(self):
        t0 = time.perf_counter()
        chunks = mc.chunk_text("word " * 100_000)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertTrue(all(len(c) <= mc.CHUNK_SIZE + 10 for c in chunks))
        self.assertLessEqual(mc.MAX_PAGES, 30)                 # index crawl is bounded


class TestRetry(unittest.TestCase):
    def test_fetch_retries_with_backoff(self):
        mc.time.sleep.reset_mock()
        m = MagicMock(side_effect=[OSError("a"), OSError("b"), _cm("page")])
        with patch.object(mc.urllib.request, "urlopen", m):
            self.assertEqual(mc.fetch("https://x.test"), "page")
        self.assertEqual(m.call_count, 3)
        self.assertEqual([c.args[0] for c in mc.time.sleep.call_args_list], [1, 2])

    def test_fetch_gives_up_after_three(self):
        m = MagicMock(side_effect=OSError("down"))
        with patch.object(mc.urllib.request, "urlopen", m):
            self.assertIsNone(mc.fetch("https://x.test"))
        self.assertEqual(m.call_count, 3)


class TestUnit(unittest.TestCase):
    def test_chunk_edges(self):
        self.assertEqual(mc.chunk_text("too few words"), [])
        self.assertEqual(len(mc.chunk_text(" ".join(["w"] * 40))), 1)

    def test_extract_title(self):
        self.assertEqual(mc.extract_title("<h1 class='t'> Pork Butt </h1>", "u"), "Pork Butt")
        self.assertEqual(mc.extract_title("", "https://x/blogs/recipes/smoked-mac-cheese/"), "Smoked Mac Cheese")

    def test_remember_dedups_and_dry_run(self):
        done = set()
        self.assertTrue(mc.remember("chunk", {}, done, dry_run=True))
        self.assertFalse(mc.remember("chunk", {}, done, dry_run=True))
        mc.STATE_FILE.write_text("{bad")
        self.assertEqual(mc.load_state()["ingested_urls"], [])


class TestIntegration(unittest.TestCase):
    def test_remember_posts_to_recipes_vector(self):
        with patch.object(mc.urllib.request, "urlopen") as m:
            self.assertTrue(mc.remember("brisket text", {"title": "B"}, set()))
        req = m.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/remember?async=1"))
        self.assertEqual(json.loads(req.data)["source"], "recipes")

    def test_progress_goes_through_notify_bus(self):
        mc.notify.reset_mock()
        mc.slack_post(":cut_of_meat: *Title*\nbody")
        self.assertEqual(mc.notify.call_args[0][0], "cut_of_meat: Title")
        self.assertEqual(mc.notify.call_args[1]["dedup_key"], "meatchurch-ingest")


class TestFunctional(unittest.TestCase):
    def test_full_run_ingests_and_saves_state(self):
        if mc.STATE_FILE.exists():
            mc.STATE_FILE.unlink()
        mc.notify.reset_mock()
        stored = []
        with patch.object(mc, "fetch", side_effect=_site), \
             patch.object(mc, "remember", side_effect=lambda t, m, d, dry_run=False: stored.append(m) or True):
            _main([])
        self.assertEqual(sorted({m["url"] for m in stored}),
                         [mc.BASE_URL + "/blogs/recipes/brisket-101", mc.BASE_URL + "/blogs/recipes/smoked-wings"])
        st = json.loads(mc.STATE_FILE.read_text())
        self.assertEqual(len(st["ingested_urls"]), 2)
        self.assertIn("Complete", mc.notify.call_args[0][0])

    def test_dry_run_writes_no_state_and_posts_nothing(self):
        # regression for the fix: --dry-run used to record URLs as ingested, so --resume skipped them forever
        if mc.STATE_FILE.exists():
            mc.STATE_FILE.unlink()
        mc.notify.reset_mock()
        with patch.object(mc, "fetch", side_effect=_site), \
             patch.object(mc.urllib.request, "urlopen") as net:
            _main(["--dry-run"])
        net.assert_not_called()
        mc.notify.assert_not_called()
        self.assertFalse(mc.STATE_FILE.exists())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(callable(mc.main))


if __name__ == "__main__":
    unittest.main()
