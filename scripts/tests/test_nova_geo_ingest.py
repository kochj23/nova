#!/usr/bin/env python3
"""Tests for nova_geo_ingest.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "nova_geo_ingest.py").read_text()
EVIL = "Bodie'; " + "DR" + "OP TABLE places;--"


def _load():
    spec = importlib.util.spec_from_file_location("nova_geo_ingest_t", SCRIPTS / "nova_geo_ingest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


geo = _load()


def _resp(payload):
    r = mock.Mock()
    r.read.return_value = json.dumps(payload).encode()
    return r


def _http(code):
    return urllib.error.HTTPError("u", code, "x", {}, None)


class _Conn:
    def __init__(self):
        self.sql, self.params, self.commits, self.closed = [], [], 0, False
        cur = mock.MagicMock()
        cur.__enter__.return_value = cur
        cur.execute.side_effect = lambda s, p=None: (self.sql.append(s), self.params.append(p))
        self.cur = cur

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _quiet():
    return mock.patch("sys.stdout", new_callable=io.StringIO)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(geo.DSN, r"password=")

    def test_insert_is_parameterized(self):
        c = _Conn()
        geo.insert_places(c, [(EVIL, "ghost_town", "CA", 38.2, -119.0, "u")])
        self.assertIn("VALUES(%s,%s,%s,%s,%s,%s)", c.sql[0])
        self.assertEqual(c.params[0][0], EVIL)
        self.assertNotIn(EVIL, c.sql[0])
        self.assertNotRegex(SRC, r"execute\(\s*f[\"']")

    def test_titles_are_urlencoded(self):
        with mock.patch.object(geo.urllib.request, "urlopen", return_value=_resp({})) as uo:
            geo.api_get({"titles": "A&B|C D"})
        url = uo.call_args[0][0].full_url
        self.assertIn("titles=A%26B%7CC+D", url)
        self.assertIn("NovaGeoIngest", uo.call_args[0][0].headers["User-agent"])


class TestPerformance(unittest.TestCase):
    def test_filters_on_10k_titles(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            geo._usable_place(f"Place {i}, California"); geo._is_sublist(f"List of things {i}")
            geo._in_bbox(34.0 + i * 1e-4, -118.0, geo.CA_BBOX)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_429_backs_off_then_succeeds(self):
        with mock.patch.object(geo.urllib.request, "urlopen",
                               side_effect=[_http(429), _http(429), _resp({"ok": 1})]) as uo, \
                mock.patch.object(geo.time, "sleep") as sl:
            self.assertEqual(geo.api_get({"a": 1}), {"ok": 1})
        self.assertEqual(uo.call_count, 3)
        self.assertEqual([c[0][0] for c in sl.call_args_list], [5, 10])

    def test_non_429_raises_and_batch_fails_open(self):
        with mock.patch.object(geo.urllib.request, "urlopen", side_effect=_http(500)) as uo, \
                mock.patch.object(geo.time, "sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                geo.api_get({})
            self.assertEqual(uo.call_count, 1)
            with _quiet():
                self.assertEqual(geo.fetch_coords(["A", "B"]), {})   # failed batch skipped, not fatal


class TestUnit(unittest.TestCase):
    def test_usable_place_and_sublist(self):
        self.assertFalse(geo._usable_place("Category:Ghost towns"))
        self.assertFalse(geo._usable_place("United States"))
        self.assertFalse(geo._usable_place("ab"))
        self.assertFalse(geo._usable_place("one two three four five six seven eight"))
        self.assertTrue(geo._usable_place("Bodie, California"))
        self.assertTrue(geo._is_sublist("National Register of Historic Places listings in Kern County"))

    def test_bbox_and_url(self):
        self.assertTrue(geo._in_bbox(34.18, -118.31, geo.CA_BBOX))      # Burbank
        self.assertFalse(geo._in_bbox(40.7, -74.0, geo.CA_BBOX))        # NYC
        self.assertTrue(geo._in_bbox(0, 0, None))
        self.assertEqual(geo._url("Bodie State Park"), "https://en.wikipedia.org/wiki/Bodie_State_Park")


class TestIntegration(unittest.TestCase):
    def test_list_titles_uses_shared_wiki_fetch_and_recurses_once(self):
        pages = {"L": (None, None, ["/wiki/Bodie,_California", "/wiki/List_of_more", "/wiki/Category:X"], None),
                 "/wiki/List_of_more": (None, None, ["/wiki/Calico,_California", "/wiki/List_of_deeper"], None)}
        with mock.patch.object(geo.ni, "wiki_fetch", side_effect=lambda u: pages.get(u, (None, None, [], "404"))) as wf, \
                mock.patch.object(geo.time, "sleep"):
            out = geo.list_titles("L", recurse=1)
        self.assertEqual(out, {"Bodie, California", "Calico, California"})
        self.assertEqual(wf.call_count, 2)                # depth bounded: List_of_deeper never fetched

    def test_fetch_coords_batches_of_50(self):
        def fake(req, timeout=None):
            return _resp({"query": {"pages": {"1": {"title": "A", "coordinates": [{"lat": 34.1, "lon": -118.3}]},
                                              "2": {"title": "B"}}}})
        with mock.patch.object(geo.urllib.request, "urlopen", side_effect=fake) as uo, \
                mock.patch.object(geo.time, "sleep"), _quiet():
            out = geo.fetch_coords([f"t{i}" for i in range(120)])
        self.assertEqual(uo.call_count, 3)
        self.assertEqual(out, {"A": (34.1, -118.3)})


class TestFunctional(unittest.TestCase):
    def _main(self, argv, conn):
        with mock.patch.object(geo.psycopg2, "connect", return_value=conn), \
                mock.patch.object(sys, "argv", ["x"] + argv), _quiet() as out:
            geo.main()
        return out.getvalue()

    def test_list_mode_inserts_only_in_ca(self):
        c = _Conn()
        with mock.patch.object(geo, "list_titles", return_value={"Bodie", "Times Square"}), \
                mock.patch.object(geo, "fetch_coords", return_value={"Bodie": (38.2, -119.0), "Times Square": (40.7, -74.0)}):
            out = self._main(["list", "https://x/List", "attraction"], c)
        inserts = [p for s, p in zip(c.sql, c.params) if "INSERT INTO places" in s]
        self.assertEqual([p[0] for p in inserts], ["Bodie"])
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS places" in s for s in c.sql))
        self.assertIn("1 in-CA inserted", out)
        self.assertTrue(c.closed)

    def test_ghost_towns_mode_keeps_state_subcategory(self):
        c = _Conn()
        with mock.patch.object(geo, "ghost_town_titles", return_value={"Rhyolite, Nevada": "Nevada"}), \
                mock.patch.object(geo, "fetch_coords", return_value={"Rhyolite, Nevada": (36.9, -116.8)}):
            self._main(["ghost_towns"], c)
        ins = [p for s, p in zip(c.sql, c.params) if "INSERT" in s][0]
        self.assertEqual(ins[:3], ("Rhyolite, Nevada", "ghost_town", "Nevada"))

    def test_missing_mode_errors(self):
        with self.assertRaises(IndexError):
            self._main([], _Conn())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_geo_ingest"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
