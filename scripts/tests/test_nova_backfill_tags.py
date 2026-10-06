#!/usr/bin/env python3
"""Tests for nova_backfill_tags.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

main() rewrites Hugo posts: every test points HUGO_ROOT/LOG_FILE at a tempdir and mocks extract_tags."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_backfill_tags.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("backfill_tags_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bt = _load()
_TD = tempfile.TemporaryDirectory()
_PATCHES = []


def setUpModule():
    for p in (patch.object(bt, "LOG_FILE", Path(_TD.name) / "logs" / "bt.log"),
              patch.object(bt, "HUGO_ROOT", Path(_TD.name) / "hugo-none"),
              patch.object(bt, "extract_tags", side_effect=AssertionError("unmocked extract_tags")),
              patch.object(bt.time, "sleep")):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


POST = '---\ntitle: "Glass Ocean"\ncategories: ["dreams"]\ntags: ["surreal"]\n---\nA dream about glass.\n'
GOOD = '---\ntitle: "Ok"\ncategories: ["essays"]\ntags: ["memory systems", "vector search"]\n---\nBody.\n'
NOTAGS = '---\ntitle: X\ncategories: ["art"]\n---\nBody text.\n'


class _Site:
    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self._p = patch.object(bt, "HUGO_ROOT", self.root); self._p.start()
        return self

    def add(self, cat, name, text):
        d = self.root / "content" / cat; d.mkdir(parents=True, exist_ok=True)
        (d / name).write_text(text)
        return d / name

    def __exit__(self, *a):
        self._p.stop(); self.td.cleanup()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_tags_are_json_escaped(self):
        with _Site() as s:
            f = s.add("dreams", "a.md", POST)
            with patch.object(bt, "extract_tags", return_value=['quote " here', "x\ny"]):
                self.assertTrue(bt.process_file(f, "dreams"))
            line = [ln for ln in f.read_text().splitlines() if ln.startswith("tags:")][0]
        self.assertEqual(line, 'tags: ["quote \\" here", "x\\ny"]')   # one line, quotes/newlines escaped

    def test_only_touches_known_categories_and_skips_index(self):
        with _Site() as s, patch.object(bt, "extract_tags", return_value=["a b", "c d"]) as et, \
                redirect_stdout(io.StringIO()):
            s.add("private", "p.md", POST)
            s.add("dreams", "_index.md", POST)
            bt.main()
        et.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_needs_backfill_10k_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            bt.needs_backfill(["surreal", "dark"] if i % 2 else ["a", "b"])
        self.assertLess(time.perf_counter() - t0, 0.5)


class TestRetry(unittest.TestCase):
    def test_extractor_error_counted_and_run_continues(self):
        # RETRY GAP: process_file()/extract_tags — one attempt per post; an error is logged and the loop continues
        with _Site() as s, patch.object(bt, "extract_tags", side_effect=[RuntimeError("ollama down"), ["new tag", "x y"]]) as et, \
                redirect_stdout(io.StringIO()) as out:
            s.add("dreams", "a.md", POST); s.add("dreams", "b.md", POST)
            bt.main()
        self.assertEqual(et.call_count, 2)
        self.assertIn("1 updated, 0 skipped, 1 errors", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_needs_backfill_edges(self):
        self.assertTrue(bt.needs_backfill([]))
        self.assertTrue(bt.needs_backfill(["anything"]))
        self.assertTrue(bt.needs_backfill(["Surreal", "DARK"]))
        self.assertFalse(bt.needs_backfill(["surreal", "glass ocean"]))

    def test_good_tags_untouched(self):
        with _Site() as s:
            f = s.add("essays", "g.md", GOOD)
            self.assertFalse(bt.process_file(f, "essays"))
            self.assertEqual(f.read_text(), GOOD)

    def test_empty_extraction_leaves_file(self):
        with _Site() as s, patch.object(bt, "extract_tags", return_value=[]):
            f = s.add("dreams", "a.md", POST)
            self.assertFalse(bt.process_file(f, "dreams"))
            self.assertEqual(f.read_text(), POST)


class TestIntegration(unittest.TestCase):
    def test_uses_shared_tag_extractor_with_title_body(self):
        import nova_tag_extractor
        self.assertIn("from nova_tag_extractor import extract_tags", SRC)
        self.assertTrue(callable(nova_tag_extractor.extract_tags))
        with _Site() as s, patch.object(bt, "extract_tags", return_value=["a b", "c d"]) as et:
            bt.process_file(s.add("dreams", "a.md", POST), "dreams")
        title, body, cat = et.call_args.args
        self.assertEqual((title, cat, et.call_args.kwargs["n"]), ("Glass Ocean", "dreams", 5))
        self.assertEqual(body, "A dream about glass.")

    def test_inserts_tags_after_categories_when_missing(self):
        with _Site() as s, patch.object(bt, "extract_tags", return_value=["pixel art", "light"]):
            f = s.add("art", "n.md", NOTAGS)
            self.assertTrue(bt.process_file(f, "art"))
            self.assertIn('categories: ["art"]\ntags: ["pixel art", "light"]', f.read_text())


class TestFunctional(unittest.TestCase):
    def test_golden_path_main(self):
        with _Site() as s, patch.object(bt, "extract_tags", return_value=["glass ocean", "dream logic"]), \
                redirect_stdout(io.StringIO()) as out:
            a = s.add("dreams", "a.md", POST)
            s.add("essays", "g.md", GOOD)
            bt.main()
            self.assertIn('tags: ["glass ocean", "dream logic"]', a.read_text())
        self.assertIn("1 updated, 1 skipped, 0 errors", out.getvalue())
        self.assertTrue(bt.LOG_FILE.exists())
        self.assertTrue(str(bt.LOG_FILE).startswith(_TD.name))

    def test_missing_content_dir_is_noop(self):
        with redirect_stdout(io.StringIO()) as out:
            bt.main()
        self.assertIn("0 updated, 0 skipped, 0 errors", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_backfill_tags"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
