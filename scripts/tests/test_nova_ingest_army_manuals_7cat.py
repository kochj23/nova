#!/usr/bin/env python3
"""7-category tests for nova_ingest_army_manuals.py (Security, Performance, Retry, Unit, Integration,
Functional, Frame). archive.org, PostgreSQL, Nova memory and Slack are all mocked — nothing is
downloaded and nothing is written. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_ingest_army_manuals_7cat.py
"""
import json
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_ingest_army_manuals as m  # noqa: E402

SRC = (SCRIPTS / "nova_ingest_army_manuals.py").read_text()


def fake_ni(remember=True):
    ni = types.SimpleNamespace(_shutdown=False)
    ni.log = MagicMock()
    ni.notify = MagicMock()
    ni.clean_text = lambda t: t
    ni.chunk_prose = lambda t: [p for p in t.split("\n\n") if p]
    ni.is_garbage = lambda c: c.startswith("@@")
    ni.remember = MagicMock(return_value=remember)
    return ni


def search_pages(*pages):
    """get() side effect for search(): each page a list of (identifier, title)."""
    bodies = [json.dumps({"response": {"docs": [{"identifier": i, "title": t, "date": "1990-01-01T00:00:00Z"}
                                                 for i, t in p]}}).encode() for p in pages]
    return bodies + [json.dumps({"response": {"docs": []}}).encode()]


class FakeCur:
    def __init__(self, seen=(), stored=0):
        self.seen, self.stored, self.calls, self._r = list(seen), stored, [], None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if sql.startswith("SELECT identifier"):
            self._r = [(s,) for s in self.seen]
        elif "sum(chunks)" in sql:
            self._r = [(self.stored,)]

    def fetchall(self):
        return self._r

    def fetchone(self):
        return self._r[0]


class TestSecurity(unittest.TestCase):
    def test_identifier_quoted_into_one_path_segment(self):
        urls = []

        def get(url, timeout=60):
            urls.append(url)
            return json.dumps({"result": []}).encode()
        with patch.object(m, "get", side_effect=get):
            m.ocr_text("../../evil?x=1#frag")
        self.assertTrue(urls[0].startswith("https://archive.org/metadata/..%2F..%2Fevil%3Fx%3D1%23frag/files"))

    def test_restricted_or_conspiracy_titles_skipped(self):
        for t in ("FM 3-21 LEAKED", "Technical Manual FOUO", "Field Manual classified annex",
                  "Army training re-education camps"):
            self.assertTrue(m.SKIP.search(t), t)

    def test_seen_rows_use_bound_parameters(self):
        self.assertNotRegex(SRC, r'execute\(f["\']')
        self.assertIn("VALUES (%s, %s, %s)", SRC)

    def test_https_only_and_no_secrets(self):
        self.assertNotRegex(SRC, r"http://|/Users/[a-z]|password\s*=")


class TestPerformance(unittest.TestCase):
    def test_search_pagination_is_capped(self):
        page = json.dumps({"response": {"docs": [{"identifier": "x", "title": "FM 1-1"}]}}).encode()
        with patch.object(m, "get", return_value=page) as g, patch.object(m, "ni", fake_ni()):
            out = m.search()
        self.assertEqual(g.call_count, m.MAX_PAGES)
        self.assertEqual(len(out), m.MAX_PAGES)

    def test_fetch_timeouts_set(self):
        with patch.object(m.urllib.request, "urlopen") as u:
            u.return_value.read.return_value = b"x"
            m.get("https://archive.org/x", timeout=7)
        self.assertEqual(u.call_args.kwargs["timeout"], 7)
        self.assertIn("timeout=180", SRC)

    def test_title_filter_fast_on_large_batch(self):
        titles = [f"FM {i}-{i} Rifle Marksmanship" if i % 2 else f"random scan {i}" for i in range(20000)]
        t = time.perf_counter()
        n = sum(1 for x in titles if m.PUB.search(x) and not m.SKIP.search(x))
        self.assertLess(time.perf_counter() - t, 1.0)
        self.assertEqual(n, 10000)


class TestRetry(unittest.TestCase):
    def test_get_retries_with_backoff_then_succeeds(self):
        ok = MagicMock(); ok.read.return_value = b"data"
        with patch.object(m.urllib.request, "urlopen", side_effect=[OSError("503"), ok]), \
                patch.object(m, "ni", fake_ni()), patch.object(m.time, "sleep") as sl:
            self.assertEqual(m.get("https://archive.org/x"), b"data")
        sl.assert_called_once_with(5)

    def test_get_reraises_after_three(self):
        ni = fake_ni()
        with patch.object(m.urllib.request, "urlopen", side_effect=OSError("down")) as u, \
                patch.object(m, "ni", ni), patch.object(m.time, "sleep") as sl:
            with self.assertRaises(OSError):
                m.get("https://archive.org/x")
        self.assertEqual(u.call_count, 3)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [5, 10])
        self.assertEqual(ni.log.call_count, 2)

    def test_pg_connect_retried(self):
        conn = MagicMock()
        with patch.object(m.psycopg2, "connect", side_effect=[psycopg2.OperationalError("x"), conn]), \
                patch.object(m, "ni", fake_ni()), patch.object(m.time, "sleep"):
            self.assertIs(m._connect(), conn)

    def test_failed_item_logged_and_run_continues(self):
        ni = fake_ni()
        cur = FakeCur()
        conn = MagicMock(); conn.cursor.return_value = cur

        def ocr(ident):
            if ident == "bad":
                raise OSError("gone")
            return "para one\n\npara two"
        with patch.object(sys, "argv", ["x"]), patch.object(m, "ni", ni), patch.object(m, "_connect", return_value=conn), \
                patch.object(m, "search", return_value=[("bad", "FM 1", ""), ("good", "FM 2", "")]), \
                patch.object(m, "ocr_text", side_effect=ocr), patch.object(m.time, "sleep"):
            m.main()
        self.assertTrue(any("bad: fetch failed" in c.args[0] for c in ni.log.call_args_list))
        self.assertEqual(ni.remember.call_count, 2)


class TestUnit(unittest.TestCase):
    def test_pub_regex(self):
        for t in ("FM 3-21.8 The Infantry Rifle Platoon", "TM 9-1005-249-10", "ATP 3-21.20", "DA PAM 350-38",
                  "U.S. Army Field Manual", "Training Circular 3-22.9"):
            self.assertTrue(m.PUB.search(t), t)
        for t in ("Vacation photos", "My Army of One mixtape"):
            self.assertFalse(m.PUB.search(t), t)

    def test_search_filters_and_trims(self):
        pages = search_pages([("a", "FM 7-8 Infantry"), ("b", "random"), ("c", "Field Manual LEAKED")])
        with patch.object(m, "get", side_effect=pages), patch.object(m, "ni", fake_ni()):
            out = m.search()
        self.assertEqual(out, [("a", "FM 7-8 Infantry", "1990-01-01")])

    def test_ocr_picks_djvu_text(self):
        files = json.dumps({"result": [{"name": "x.pdf"}, {"name": "x_djvu.txt"}]}).encode()
        with patch.object(m, "get", side_effect=[files, "té".encode()]) as g:
            self.assertEqual(m.ocr_text("x"), "té")
        self.assertTrue(g.call_args.args[0].endswith("/download/x/x_djvu.txt"))

    def test_ocr_without_text_is_empty(self):
        with patch.object(m, "get", return_value=json.dumps({"result": [{"name": "a.pdf"}]}).encode()):
            self.assertEqual(m.ocr_text("x"), "")


class TestIntegration(unittest.TestCase):
    def test_chunks_flow_to_remember_with_meta_and_seen_row(self):
        ni = fake_ni()
        cur = FakeCur()
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(sys, "argv", ["x", "--vector", "mil"]), patch.object(m, "ni", ni), \
                patch.object(m, "_connect", return_value=conn), \
                patch.object(m, "search", return_value=[("fm1", "FM 1 Leadership", "1999")]), \
                patch.object(m, "ocr_text", return_value="lead well\n\n@@junk\n\nlead again"), patch.object(m.time, "sleep"):
            m.main()
        args = ni.remember.call_args_list[0].args
        self.assertEqual(args[0], "[FM 1 Leadership] lead well")
        self.assertEqual(args[1], "mil")
        self.assertEqual(args[2]["url"], "https://archive.org/details/fm1")
        ins = [p for s, p in cur.calls if s.startswith("INSERT INTO ia_ingest_seen")]
        self.assertEqual(ins, [("fm1", "FM 1 Leadership", 2)])


class TestFunctional(unittest.TestCase):
    def _run(self, argv, cur, pubs, ni=None):
        ni = ni or fake_ni()
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(sys, "argv", ["x", *argv]), patch.object(m, "ni", ni), \
                patch.object(m, "_connect", return_value=conn), patch.object(m, "search", return_value=pubs), \
                patch.object(m, "ocr_text", return_value="a\n\nb\n\nc") as oc, patch.object(m.time, "sleep"):
            m.main()
        return ni, oc

    def test_resumes_skipping_seen_and_stops_at_target(self):
        cur = FakeCur(seen=["done1"], stored=0)
        pubs = [("done1", "FM 1", ""), ("p2", "FM 2", ""), ("p3", "FM 3", ""), ("p4", "FM 4", "")]
        ni, oc = self._run(["--target", "5"], cur, pubs)
        self.assertEqual([c.args[0] for c in oc.call_args_list], ["p2", "p3"])   # 3 + 3 >= 5
        self.assertIn("stopped", ni.notify.call_args.args[0])

    def test_dry_run_writes_no_seen_rows(self):
        cur = FakeCur()
        self._run(["--dry-run"], cur, [("p", "FM 9", "")])
        self.assertFalse(any(s.startswith("INSERT") for s, _ in cur.calls))

    def test_already_at_target_does_nothing(self):
        cur = FakeCur(stored=10 ** 6)
        _, oc = self._run([], cur, [("p", "FM 9", "")])
        oc.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_module_shape(self):
        for n in ("main", "search", "ocr_text", "get", "_connect"):
            self.assertTrue(callable(getattr(m, n)))

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_ingest_army_manuals.py"), "--help"],
                           capture_output=True, text=True, timeout=60, cwd=str(SCRIPTS))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--target", r.stdout)


if __name__ == "__main__":
    unittest.main()
