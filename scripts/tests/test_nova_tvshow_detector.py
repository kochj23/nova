#!/usr/bin/env python3
"""Tests for nova_tvshow_detector.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import builtins
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_tvshow_detector.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="tvdet-test-"))
TSV = TMP / "tv_matches.tsv"

SEINFELD = {"name": "Seinfeld", "premiered": "1989-07-05", "network": {"name": "NBC"}, "webChannel": None}
STRANGER = {"name": "Stranger Things", "premiered": "2016-07-15", "network": None, "webChannel": {"name": "Netflix"}}


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


class _Cur:
    def __init__(self, shows): self.shows = shows; self.sql = []
    def execute(self, sql, params=None): self.sql.append(sql)
    def fetchall(self): return [(s,) for s in self.shows]


def _run(shows, answers, urlopen_exc=None):
    """Load (= run) the module-level script with PG, TVMaze, sleep and the /tmp output file all stubbed.
    `answers` maps the quoted query to a TVMaze dict (or an exception to raise). Returns (mod, cur, urls, stdout)."""
    cur = _Cur(shows); urls = []
    conn = types.SimpleNamespace(cursor=lambda: cur, close=lambda: None)

    def fake_urlopen(url, timeout=None):
        urls.append(url)
        if urlopen_exc:
            raise urlopen_exc
        q = url.split("q=", 1)[1]
        a = answers.get(q)
        if isinstance(a, Exception):
            raise a
        return _Resp(a if a is not None else {})

    real_open = builtins.open

    def fake_open(path, *a, **k):
        if str(path) == "/tmp/tv_matches.tsv":
            path = TSV
        return real_open(path, *a, **k)

    spec = importlib.util.spec_from_file_location("tvdet_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch("psycopg2.connect", lambda *a, **k: conn), patch("urllib.request.urlopen", fake_urlopen), \
         patch("time.sleep", lambda s: None), patch.object(builtins, "open", fake_open), \
         redirect_stdout(io.StringIO()) as out:
        spec.loader.exec_module(mod)
    return mod, cur, urls, out.getvalue()


EMPTY = _run([], {})[0]      # a loaded instance for the pure functions


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", SRC)

    def test_sql_is_a_constant_and_the_script_never_deletes(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertNotIn("DELETE", SRC.upper().replace("NEVER DELETES", ""))
        self.assertNotIn("UPDATE ", SRC)
        mod, cur, _, _ = _run(["Seinfeld"], {"Seinfeld": SEINFELD})
        self.assertEqual(cur.sql, ["SELECT DISTINCT show FROM media_prune_proposals WHERE status='proposed' ORDER BY show"])

    def test_query_is_url_encoded_before_it_reaches_tvmaze(self):
        urls = []
        with patch("urllib.request.urlopen", lambda url, timeout=None: urls.append(url) or _Resp({})):
            EMPTY.tvmaze("Law & Order: SVU / 2nd")
        self.assertEqual(urls, ["https://api.tvmaze.com/singlesearch/shows?q=Law%20%26%20Order%3A%20SVU%20/%202nd"])


class TestPerformance(unittest.TestCase):
    def test_clean_on_10k_titles_is_fast(self):
        titles = [f"Show {i} 📺 (2019) #tag" for i in range(10_000)]
        t0 = time.perf_counter()
        out = [EMPTY.clean(t) for t in titles]
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(out[7], "Show 7")


class TestRetry(unittest.TestCase):
    def test_tvmaze_retries_on_429_then_succeeds(self):
        calls, sleeps = [], []

        def flaky(url, timeout=None):
            calls.append(url)
            if len(calls) < 3:
                raise urllib.error.HTTPError(url, 429, "rate", {}, None)
            return _Resp(SEINFELD)
        with patch("urllib.request.urlopen", flaky), patch("time.sleep", sleeps.append):
            self.assertEqual(EMPTY.tvmaze("Seinfeld")["name"], "Seinfeld")
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [2, 2])

    def test_three_429s_give_up_fail_open(self):
        with patch("urllib.request.urlopen", lambda u, timeout=None: (_ for _ in ()).throw(urllib.error.HTTPError(u, 429, "r", {}, None))), \
             patch("time.sleep", lambda s: None):
            self.assertIsNone(EMPTY.tvmaze("x"))

    def test_other_errors_are_one_shot_and_fail_open(self):
        # RETRY GAP: tvmaze() — a 404/500 or a socket error returns None on the first attempt; the show is skipped
        calls = []

        def nf(url, timeout=None):
            calls.append(url); raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        with patch("urllib.request.urlopen", nf):
            self.assertIsNone(EMPTY.tvmaze("nothing"))
        self.assertEqual(len(calls), 1)
        with patch("urllib.request.urlopen", lambda u, timeout=None: (_ for _ in ()).throw(OSError("down"))):
            self.assertIsNone(EMPTY.tvmaze("nothing"))


class TestUnit(unittest.TestCase):
    def test_clean_strips_emoji_tags_and_years(self):
        self.assertEqual(EMPTY.clean("Seinfeld (1989)"), "Seinfeld")
        self.assertEqual(EMPTY.clean("MrBeast 🎮 #shorts #gaming"), "MrBeast")
        self.assertEqual(EMPTY.clean("   "), "")
        self.assertEqual(EMPTY.clean("The Office (US) (2005)"), "The Office (US)")

    def test_tvmaze_returns_parsed_json(self):
        with patch("urllib.request.urlopen", lambda u, timeout=None: _Resp(STRANGER)):
            self.assertEqual(EMPTY.tvmaze("Stranger Things")["webChannel"]["name"], "Netflix")


class TestIntegration(unittest.TestCase):
    def test_strong_match_needs_name_premiere_and_broadcast_network(self):
        shows = ["Seinfeld", "Stranger Things", "Totally Made Up Channel"]
        mod, cur, urls, out = _run(shows, {"Seinfeld": SEINFELD, "Stranger%20Things": STRANGER,
                                           "Totally%20Made%20Up%20Channel": {"name": "Totally Different", "premiered": "2001-01-01", "network": {"name": "ABC"}}})
        self.assertEqual(mod.matches, ["Seinfeld\tSeinfeld\t1989\tNBC\t1.00"])   # Netflix-only and weak-ratio rows excluded
        self.assertEqual(TSV.read_text(), "Seinfeld\tSeinfeld\t1989\tNBC\t1.00\n")
        self.assertEqual(mod.checked, 3)

    def test_short_names_are_counted_but_never_queried(self):
        mod, cur, urls, out = _run(["Ab", "Seinfeld"], {"Seinfeld": SEINFELD})
        self.assertEqual(mod.checked, 2)
        self.assertEqual(len(urls), 1)
        self.assertIn("q=Seinfeld", urls[0])


class TestFunctional(unittest.TestCase):
    def test_golden_run_reports_and_lists_matches(self):
        mod, cur, urls, out = _run(["Seinfeld (1989)", "Cheers"], {"Seinfeld": SEINFELD, "Cheers": None})
        self.assertIn("[tv-detector] checked 2 shows, found 1 strong real-TV matches. -> /tmp/tv_matches.tsv", out)
        self.assertIn("  Seinfeld (1989)\tSeinfeld\t1989\tNBC\t1.00", out)

    def test_tvmaze_outage_still_finishes_with_zero_matches(self):
        mod, cur, urls, out = _run(["Seinfeld", "Cheers"], {}, urlopen_exc=OSError("network down"))
        self.assertEqual(mod.matches, [])
        self.assertEqual(TSV.read_text(), "")
        self.assertIn("checked 2 shows, found 0 strong", out)


class TestFrame(unittest.TestCase):
    DRIVER = (
        "import builtins, runpy, sys, types\n"
        "from unittest.mock import patch\n"
        "sys.path.insert(0, %r)\n"
        "cur = types.SimpleNamespace(execute=lambda *a: None, fetchall=lambda: [])\n"
        "conn = types.SimpleNamespace(cursor=lambda: cur)\n"
        "ro = builtins.open\n"
        "with patch('psycopg2.connect', lambda *a, **k: conn), patch('urllib.request.urlopen', None), "
        "patch.object(builtins, 'open', lambda p, *a, **k: ro(%r if str(p) == '/tmp/tv_matches.tsv' else p, *a, **k)):\n"
        "    runpy.run_path(%r, run_name='__main__')\n"
    ) % (str(SCRIPTS), str(TSV), str(SCRIPT))

    def test_script_runs_end_to_end_with_pg_and_tvmaze_stubbed(self):
        r = subprocess.run([sys.executable, "-c", self.DRIVER], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("[tv-detector] checked 0 shows, found 0 strong real-TV matches.", r.stdout)

    def test_compiles_and_is_a_one_shot_script(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("def main", SRC)          # module-level script: there is no main() an import could skip


if __name__ == "__main__":
    unittest.main()
