#!/usr/bin/env python3
"""Tests for nova_articles_to_memory.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_articles_to_memory.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("articles_to_memory_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


am = _load()
_PATCHES = []


def setUpModule():
    for p in (patch.object(am.urllib.request, "urlopen", side_effect=OSError("offline")),
              patch.object(am.psycopg2, "connect", side_effect=AssertionError("unmocked PG"))):
        p.start(); _PATCHES.append(p)


def tearDownModule():
    while _PATCHES:
        _PATCHES.pop().stop()


BODY = "This is the article body. " * 10
ART = f'---\ntitle: "Glass Ocean"\ndate: 2026-06-01\ntags: ["a", "b"]\n---\n*Published by Nova*\n{BODY}\n'


class _Site:
    def __enter__(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name) / "nova-journal"
        self.content = self.root / "content"; self.content.mkdir(parents=True)
        self._p = patch.object(am, "HUGO_CONTENT", self.content); self._p.start()
        return self

    def add(self, rel, text=ART):
        f = self.content / rel; f.parent.mkdir(parents=True, exist_ok=True); f.write_text(text)
        return f

    def __exit__(self, *a):
        self._p.stop(); self.td.cleanup()


def _ok():
    r = MagicMock(); r.__enter__.return_value = r
    return r


def _pg(paths=()):
    cur = MagicMock(); cur.fetchall.return_value = [(p,) for p in paths]
    conn = MagicMock(); conn.cursor.return_value = cur
    return conn, cur


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_existing_paths_sql_parameterized(self):
        conn, cur = _pg(["dreams/a.md"])
        self.assertEqual(am.existing_paths(cur), {"dreams/a.md"})
        sql, params = cur.execute.call_args.args
        self.assertEqual(params, ("nova_articles",))
        self.assertIn("source=%s", sql)

    def test_articles_marked_public_and_authored_by_nova(self):
        with _Site() as s, patch.object(am.urllib.request, "urlopen", return_value=_ok()) as u:
            self.assertTrue(am.remember_article(s.add("dreams/2026-06-01-glass.md")))
        body = json.loads(u.call_args.args[0].data)
        self.assertEqual(body["metadata"]["privacy"], "public")
        self.assertEqual(body["metadata"]["author"], "nova")


class TestPerformance(unittest.TestCase):
    def test_parse_many_files_fast(self):
        with _Site() as s:
            files = [s.add(f"d/{i}.md") for i in range(300)]
            t0 = time.perf_counter()
            parsed = [am.parse_md(f) for f in files]
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertTrue(all(parsed))


class TestRetry(unittest.TestCase):
    def test_post_failure_fails_open(self):
        # RETRY GAP: _store()/_post — one POST to /remember; failure logs and returns False (next reconcile retries)
        with _Site() as s, patch.object(am.urllib.request, "urlopen", side_effect=OSError("503")) as u, \
                redirect_stdout(io.StringIO()) as out:
            self.assertFalse(am.remember_article(s.add("dreams/a.md")))
        self.assertEqual(u.call_count, 1)
        self.assertIn("store failed dreams/a.md", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_parse_md_fields_and_byline_stripped(self):
        with _Site() as s:
            a = am.parse_md(s.add("x/a.md"))
        self.assertEqual((a["title"], a["date"], a["tags"]), ("Glass Ocean", "2026-06-01", ["a", "b"]))
        self.assertFalse(a["body"].startswith("*Published"))

    def test_parse_md_rejects(self):
        with _Site() as s:
            self.assertIsNone(am.parse_md(s.add("x/nofm.md", "just text " * 20)))
            self.assertIsNone(am.parse_md(s.add("x/short.md", "---\ntitle: t\n---\ntiny")))
            self.assertIsNone(am.parse_md(s.root / "missing.md"))
            bad = am.parse_md(s.add("x/badtags.md", f"---\ntags: [oops\n---\n{BODY}"))
        self.assertEqual((bad["tags"], bad["title"]), ([], "badtags"))

    def test_base_url_from_config(self):
        with _Site() as s:
            self.assertIsNone(am._base_url())
            (s.root / "hugo.toml").write_text('baseURL = "https://journal.example.org/"\n')
            self.assertEqual(am._base_url(), "https://journal.example.org")


class TestIntegration(unittest.TestCase):
    def test_metadata_and_url_shape(self):
        with _Site() as s, patch.object(am.urllib.request, "urlopen", return_value=_ok()) as u:
            (s.root / "hugo.toml").write_text('baseURL = "https://j.example.org/"\n')
            am.remember_article(s.add("dreams/2026-06-01-glass.md"))
        req = u.call_args.args[0]
        self.assertTrue(req.full_url.endswith("/remember?async=1"))
        body = json.loads(req.data)
        self.assertEqual((body["source"], body["tier"]), ("nova_articles", "long_term"))
        m = body["metadata"]
        self.assertEqual((m["section"], m["slug"], m["path"]), ("dreams", "2026-06-01-glass", "dreams/2026-06-01-glass.md"))
        self.assertEqual(m["url"], "https://j.example.org/dreams/glass/")
        self.assertTrue(body["text"].startswith("Glass Ocean\n\n"))


class TestFunctional(unittest.TestCase):
    def test_remember_all_skips_seen_and_index(self):
        conn, cur = _pg(["dreams/old.md"])
        with _Site() as s, patch.object(am.psycopg2, "connect", return_value=conn), \
                patch.object(am.urllib.request, "urlopen", return_value=_ok()) as u, redirect_stdout(io.StringIO()):
            s.add("dreams/old.md"); s.add("dreams/new.md"); s.add("dreams/_index.md"); s.add("x/bad.md", "nope")
            added, skipped = am.remember_all()
        self.assertEqual((added, skipped), (1, 2))
        self.assertEqual(u.call_count, 1)
        conn.close.assert_called_once()

    def test_missing_content_dir(self):
        with patch.object(am, "HUGO_CONTENT", Path(tempfile.gettempdir()) / "no-such-hugo-xyz"), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(am.remember_all(), (0, 0))
        self.assertIn("content dir not found", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_single_missing_file_exits_1_offline(self):
        r = subprocess.run([sys.executable, str(PATH), str(Path(tempfile.gettempdir()) / "nope-xyz.md")],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn("skipped/failed", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_articles_to_memory"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
