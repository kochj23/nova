#!/usr/bin/env python3
"""Tests for nova_cross_linker.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_cross_linker.py").read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_cross_linker_t", SCRIPTS / "nova_cross_linker.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cl = _load()


def _post(root, cat, slug, title, body):
    d = root / "content" / cat
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{slug}.md").write_text(f'---\ntitle: "{title}"\n---\n{body}\n')


class _Hugo:
    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        _post(self.root, "dreams", "lighthouse", "The Lighthouse Keeper Dreams", "A lighthouse at the end of the pier glows softly")
        _post(self.root, "essays", "entropy", "Entropy and Gardens", "Gardens fight entropy every single morning")
        _post(self.root, "essays", "_index", "Index", "skip me")
        _post(self.root, "about", "me", "About", "skip")
        self.p = mock.patch.object(cl, "HUGO_ROOT", self.root)
        self.p.start()
        return self

    def __exit__(self, *a):
        self.p.stop()
        self.td.cleanup()


def _urlopen_returning(data):
    r = mock.Mock()
    r.read.return_value = json.dumps(data).encode()
    return mock.patch.object(cl.urllib.request, "urlopen", return_value=r)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_query_is_url_quoted_and_truncated(self):
        with _urlopen_returning([]) as uo:
            cl._recall("a b&source=evil" + "x" * 1000)
        url = uo.call_args[0][0]
        self.assertNotIn("&source=evil", url)
        self.assertTrue(url.endswith("&source=journal_published"))
        self.assertLess(len(url), 800)

    def test_frontmatter_escapes_quotes(self):
        out = cl.format_related_frontmatter([{"title": 'Say "hi"', "url": "/x/", "category": "c"}])
        self.assertIn('title: "Say \\"hi\\""', out)


class TestPerformance(unittest.TestCase):
    def test_title_overlap_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            cl._title_overlap("the lighthouse keeper sleeps", "The Lighthouse Keeper Dreams")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_recall_fails_open(self):
        # RETRY GAP: _recall — one urlopen attempt; any failure returns []
        with mock.patch.object(cl.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertEqual(cl._recall("q"), [])
        self.assertEqual(uo.call_count, 1)

    def test_find_related_with_dead_memory_server(self):
        with _Hugo(), mock.patch.object(cl.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(cl.find_related("anything", "dreams", "lighthouse"), [])


class TestUnit(unittest.TestCase):
    def test_title_overlap_edges(self):
        self.assertFalse(cl._title_overlap("anything", "Nova with that"))   # only stop words
        self.assertTrue(cl._title_overlap("entropy here", "Entropy"))       # single word needs 1
        self.assertFalse(cl._title_overlap("entropy only", "Entropy and Gardens"))

    def test_format_empty(self):
        self.assertEqual(cl.format_related_frontmatter([]), "")

    def test_recall_dict_shape(self):
        with _urlopen_returning({"memories": [{"text": "x"}]}):
            self.assertEqual(cl._recall("q"), [{"text": "x"}])

    def test_published_posts_skip_rules(self):
        with _Hugo():
            posts = cl._get_published_posts()
        slugs = {p["slug"] for p in posts.values()}
        self.assertEqual(slugs, {"lighthouse", "entropy"})


class TestIntegration(unittest.TestCase):
    def test_find_related_feeds_frontmatter(self):
        mems = [{"text": "Gardens fight entropy every single morning and more", "score": 0.9}]
        with _Hugo(), _urlopen_returning(mems):
            rel = cl.find_related("my dream about gardens", "dreams", "lighthouse")
        self.assertEqual(rel, [{"url": "/essays/entropy/", "title": "Entropy and Gardens", "category": "📝 Essays"}])
        fm = cl.format_related_frontmatter(rel)
        self.assertTrue(fm.startswith("related:\n  - title: \"Entropy and Gardens\""))

    def test_every_category_has_emoji(self):
        self.assertEqual(set(cl.CATEGORY_URLS), set(cl.CATEGORY_EMOJI))


class TestFunctional(unittest.TestCase):
    def test_same_category_and_low_score_excluded(self):
        mems = [{"text": "A lighthouse at the end of the pier glows", "score": 0.99},   # same category
                {"text": "Gardens fight entropy every single morning", "score": 0.5}]  # below min_score
        with _Hugo(), _urlopen_returning(mems):
            self.assertEqual(cl.find_related("t", "dreams", "other"), [])

    def test_no_published_posts_skips_recall(self):
        with tempfile.TemporaryDirectory() as td, mock.patch.object(cl, "HUGO_ROOT", Path(td)), \
             mock.patch.object(cl.urllib.request, "urlopen") as uo:
            self.assertEqual(cl.find_related("t", "dreams", "x"), [])
        uo.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        self.assertNotIn("__main__", SRC)   # library module: nothing runs on import
        r = subprocess.run([sys.executable, "-c", "import nova_cross_linker"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
