#!/usr/bin/env python3
"""Tests for nova_ingest_movie_scripts.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ingest_movie_scripts.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ms = _load("movie_scripts_under_test", SCRIPT)

INDEX_HTML = ('<a href="/Movie Scripts/Alien Script.html">Alien</a>'
              '<a href="/Movie Scripts/Godfather, The Script.html">The Godfather</a>'
              '<a href="/Movie Scripts/Heat Script.html">Heat</a>'
              '<a href="/Movie Scripts/Matrix, The Script.html">Matrix</a>')
PAGES = {
    "https://imsdb.com/Movie%20Scripts/Alien%20Script.html": '<a href="/scripts/Alien.html">Read</a>',
    "https://imsdb.com/Movie%20Scripts/Heat%20Script.html": '<p>no raw link here</p>',
    "https://imsdb.com/Movie%20Scripts/Matrix%2C%20The%20Script.html": '<a href="/scripts/Matrix,-The.html">Read</a>',
}


class _Resp:
    def __init__(self, body): self._b = body.encode()
    def read(self): return self._b


def _web(pages=None, index=INDEX_HTML):
    seen = []

    def fake(req, timeout=None):
        url = req.full_url
        seen.append(url)
        if url == ms.ALL_SCRIPTS:
            return _Resp(index)
        body = (pages or PAGES).get(url)
        if body is None:
            raise OSError(f"404 {url}")
        return _Resp(body)
    fake.seen = seen
    return fake


def _run_main(argv=("x",), have=frozenset(), web=None, films=("Alien", "Heat", "The Matrix", "Casablanca")):
    runs = []
    web = web or _web()
    with patch.object(sys, "argv", list(argv)), patch.object(ms, "already_have", lambda: set(have)), \
         patch("urllib.request.urlopen", web), patch.object(ms.subprocess, "run", lambda cmd, **kw: runs.append(cmd)), \
         patch.object(ms.time, "sleep", lambda s: None), patch.object(ms, "TOP_FILMS", list(films)), \
         redirect_stdout(io.StringIO()) as out:
        ms.main()
    return runs, web.seen, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ms.DSN)

    def test_memory_lookup_is_parameterized_and_read_only(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", SRC))
        self.assertIn("WHERE source=%s\", (VECTOR,)", SRC)

    def test_ingest_is_spawned_without_a_shell_and_only_for_imsdb_urls(self):
        self.assertNotIn("shell=True", SRC)
        evil = {"https://imsdb.com/Movie%20Scripts/Alien%20Script.html": '<a href="/scripts/x; rm -rf /.html">Read</a>'}
        runs, seen, out = _run_main(web=_web(evil), films=("Alien",))
        self.assertEqual(runs[0][:4], ["/opt/homebrew/bin/python3", ms.INGEST, "url", "https://imsdb.com/scripts/x; rm -rf /.html"])
        self.assertTrue(all(isinstance(a, str) for a in runs[0]))     # argv list: the string is data, never a shell command

    def test_index_hrefs_are_quoted_before_fetch(self):
        with patch("urllib.request.urlopen", _web()) as w:
            self.assertEqual(ms.raw_script_url("/Movie Scripts/Matrix, The Script.html"), "https://imsdb.com/scripts/Matrix,-The.html")


class TestPerformance(unittest.TestCase):
    def test_norm_and_index_on_10k_titles_fast(self):
        titles = [f"The Film {i}: Part II (1999)" for i in range(10_000)]
        t0 = time.perf_counter()
        normed = [ms.norm(t) for t in titles]
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(normed[3], "film 3 part ii 1999")
        html = "".join(f'<a href="/Movie Scripts/Film {i} Script.html">x</a>' for i in range(10_000))
        with patch("urllib.request.urlopen", _web(index=html)):
            t0 = time.perf_counter()
            idx = ms.imsdb_index()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(idx), 10_000)


class TestRetry(unittest.TestCase):
    def test_pg_outage_fails_open_to_an_empty_have_set(self):
        # RETRY GAP: already_have()/psycopg2 — one attempt; failure returns set() so the run proceeds (re-ingest
        # is idempotent at the vector layer) instead of aborting
        with patch("psycopg2.connect", lambda *a, **k: (_ for _ in ()).throw(OSError("pg down"))):
            self.assertEqual(ms.already_have(), set())

    def test_per_film_resolve_failure_is_skipped_not_fatal(self):
        # RETRY GAP: raw_script_url()/fetch — one attempt per film; an exception is reported and the loop continues
        runs, seen, out = _run_main(films=("Alien", "Casablanca", "Heat"))
        self.assertIn("! Heat: no raw link", out)
        self.assertIn("not available: Casablanca", out)
        self.assertEqual(len(runs), 1)

    def test_index_fetch_failure_is_loud(self):
        # RETRY GAP: imsdb_index()/fetch — no retry; with no index there is nothing to do, so the error propagates
        with patch("urllib.request.urlopen", lambda req, timeout=None: (_ for _ in ()).throw(OSError("imsdb down"))), \
             patch.object(sys, "argv", ["x"]), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                ms.main()


class TestUnit(unittest.TestCase):
    def test_norm(self):
        self.assertEqual(ms.norm("The Godfather"), "godfather")
        self.assertEqual(ms.norm("Godfather, The"), "godfather")   # IMSDb's trailing-article form folds to the same key
        self.assertEqual(ms.norm("Se7en"), "se7en")
        self.assertEqual(ms.norm("  A   Clockwork  Orange! "), "clockwork orange")
        self.assertEqual(ms.norm(""), "")
        self.assertEqual(ms.norm("WALL-E"), "walle")

    def test_imsdb_index_shape(self):
        with patch("urllib.request.urlopen", _web()):
            idx = ms.imsdb_index()
        self.assertEqual(idx["alien"], "/Movie Scripts/Alien Script.html")
        self.assertEqual(idx["godfather"], "/Movie Scripts/Godfather, The Script.html")
        self.assertEqual(len(idx), 4)

    def test_raw_script_url_none_when_missing(self):
        with patch("urllib.request.urlopen", _web()):
            self.assertIsNone(ms.raw_script_url("/Movie Scripts/Heat Script.html"))
            self.assertEqual(ms.raw_script_url("/Movie Scripts/Alien Script.html"), "https://imsdb.com/scripts/Alien.html")

    def test_fetch_sets_user_agent(self):
        seen = []

        def fake(req, timeout=None):
            seen.append(req.get_header("User-agent")); return _Resp("ok")
        with patch("urllib.request.urlopen", fake):
            self.assertEqual(ms.fetch("https://imsdb.com/x"), "ok")
        self.assertEqual(seen, [ms.UA])

    def test_already_have_reads_metadata_urls(self):
        class _Cur:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def execute(self, sql, params): self.params = params
            def fetchall(self): return [("https://imsdb.com/scripts/Alien.html",), (None,)]

        class _Conn:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def cursor(self): return _Cur()
        with patch("psycopg2.connect", lambda *a, **k: _Conn()):
            self.assertEqual(ms.already_have(), {"https://imsdb.com/scripts/Alien.html"})


class TestIntegration(unittest.TestCase):
    def test_ingest_is_delegated_to_nova_ingest_url_mode_with_the_vector(self):
        self.assertTrue(Path(ms.INGEST).exists(), ms.INGEST)
        runs, seen, out = _run_main(films=("Alien",))
        self.assertEqual(runs, [["/opt/homebrew/bin/python3", ms.INGEST, "url", "https://imsdb.com/scripts/Alien.html",
                                 "--source", "movie_scripts", "--target", "2000"]])

    def test_norm_joins_the_top_list_to_the_index(self):
        with patch("urllib.request.urlopen", _web()):
            idx = ms.imsdb_index()
        self.assertIn(ms.norm("The Matrix"), idx)      # "Matrix, The" (IMSDb) and "The Matrix" (TOP_FILMS) normalise to the same key
        self.assertEqual(idx.get(ms.norm("Alien")), "/Movie Scripts/Alien Script.html")
        self.assertTrue(len(ms.TOP_FILMS) >= 90)


class TestFunctional(unittest.TestCase):
    def test_golden_path_ingests_new_skips_known_and_reports(self):
        runs, seen, out = _run_main(have={"https://imsdb.com/scripts/Matrix,-The.html"})
        self.assertEqual(len(runs), 1)
        self.assertIn("+ Alien -> https://imsdb.com/scripts/Alien.html", out)
        self.assertIn("= The Matrix: already in memory, skip", out)
        self.assertIn("! Heat: no raw link", out)
        self.assertIn("matched 3/4 on IMSDb; ingested 1; not on IMSDb: 1", out)
        self.assertIn("not available: Casablanca", out)

    def test_dry_run_never_spawns_ingest(self):
        runs, seen, out = _run_main(argv=("x", "--dry-run"))
        self.assertEqual(runs, [])
        self.assertIn("+ Alien -> ", out)
        self.assertIn("ingested 0", out)

    def test_resolve_error_path_continues(self):
        def flaky(req, timeout=None):
            if "Alien" in req.full_url:
                raise OSError("reset")
            return _web()(req, timeout)
        flaky.seen = []
        runs, seen, out = _run_main(web=flaky, films=("Alien", "The Matrix"))
        self.assertIn("! Alien: resolve failed (reset)", out)
        self.assertEqual(len(runs), 1)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_ingest_movie_scripts as m; print(m.VECTOR)"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "movie_scripts")

    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
