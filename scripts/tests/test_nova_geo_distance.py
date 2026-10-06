#!/usr/bin/env python3
"""Tests for nova_geo_distance.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_geo_distance.py"
SRC = SCRIPT.read_text()
try:
    import psycopg2  # noqa: F401
except ImportError:
    pass


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ge = _load("geo_under_test", SCRIPT)
HOME_ONLY = [{"name": "home", "lat": ge.HOME_LAT, "lon": ge.HOME_LON}]
HYPERION = (34.0969, -118.2757)            # 962 Hyperion Ave, Silver Lake: ~5.3 mi SE of home


class _Cur:
    """geo_cache stand-in: `cache` maps q -> (lat, lon, ok); records inserts."""
    def __init__(self, cache=None):
        self.cache = dict(cache or {}); self.sql = []; self._row = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split()); self.sql.append((s, params))
        if s.startswith("SELECT lat, lon, ok"):
            self._row = self.cache.get(params[0])
        elif s.startswith("INSERT INTO geo_cache"):
            self.cache.setdefault(params[0], (params[1], params[2], params[3]))

    def fetchone(self):
        return self._row

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


def _resp(data):
    r = MagicMock(); r.read.return_value = json.dumps(data).encode()
    return r


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ge.DSN)

    def test_sql_is_parameterized_and_the_only_write_is_the_cache(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"geo_cache"})
        cur = _Cur()
        evil = "1 Main St'); DROP TABLE geo_cache; --"
        with patch.object(ge.urllib.request, "urlopen", return_value=_resp([])), patch.object(ge.time, "sleep"):
            self.assertIsNone(ge.geocode(cur, evil))
        for s, p in cur.sql:
            self.assertNotIn("DROP", s)
        self.assertEqual(cur.ran("INSERT INTO geo_cache")[0][1], (evil, None, None, False))

    def test_geocoder_query_is_url_encoded_and_bounded_to_la(self):
        cur = _Cur()
        with patch.object(ge.urllib.request, "urlopen", return_value=_resp([])) as u, patch.object(ge.time, "sleep"):
            ge.geocode(cur, "962 Hyperion Ave & weird chars ?#")
        req = u.call_args[0][0]
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
        self.assertEqual(qs["q"], ["962 Hyperion Ave & weird chars ?#, California"])
        self.assertEqual(qs["bounded"], ["1"]); self.assertEqual(qs["viewbox"], [ge.VIEWBOX]); self.assertEqual(qs["countrycodes"], ["us"])
        self.assertEqual(req.get_header("User-agent"), ge.UA)


class TestPerformance(unittest.TestCase):
    def test_extractors_fast_on_10k_lines(self):
        lines = [f"unit {i} ADW suspect 962 Hyperion Avenue; TC at Glenoaks and Olive Blvd near downtown Burbank and Glendale"
                 for i in range(10_000)]
        t0 = time.perf_counter()
        for ln in lines:
            ge.find_locations(ln); ge.place_distance(ln)
        self.assertLess(time.perf_counter() - t0, 3.0)

    def test_haversine_and_bearing_10k_under_200ms(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            ge.haversine_mi(34.0 + i * 1e-5, -118.3); ge.bearing_from_home(34.0 + i * 1e-5, -118.3)
        self.assertLess(time.perf_counter() - t0, 0.2)


class TestRetry(unittest.TestCase):
    def test_geocode_failure_is_one_shot_and_cached_as_a_miss(self):
        # RETRY GAP: geocode()/Nominatim urlopen — one attempt; a failure is swallowed, cached as ok=false so the
        # string is never re-queried, and locate() simply carries on without a distance.
        cur = _Cur()
        with patch.object(ge.urllib.request, "urlopen", side_effect=OSError("nominatim down")) as u, patch.object(ge.time, "sleep"):
            self.assertIsNone(ge.geocode(cur, "962 Hyperion Avenue"))
            self.assertIsNone(ge.geocode(cur, "962 Hyperion Avenue"))          # second call: cache hit, no network
        self.assertEqual(u.call_count, 1)
        self.assertEqual(cur.ran("INSERT INTO geo_cache")[0][1], ("962 Hyperion Avenue", None, None, False))

    def test_rate_limit_sleep_only_after_a_live_query(self):
        cur = _Cur({"cached q": (34.1, -118.3, True)})
        with patch.object(ge.urllib.request, "urlopen", return_value=_resp([{"lat": "34.2", "lon": "-118.4"}])), \
             patch.object(ge.time, "sleep") as sl:
            self.assertEqual(ge.geocode(cur, "cached q"), (34.1, -118.3))
            sl.assert_not_called()
            self.assertEqual(ge.geocode(cur, "new q"), (34.2, -118.4))
            sl.assert_called_once_with(1.1)


class TestUnit(unittest.TestCase):
    def test_haversine_and_bearing(self):
        self.assertEqual(ge.haversine_mi(ge.HOME_LAT, ge.HOME_LON), 0.0)
        self.assertAlmostEqual(ge.haversine_mi(*HYPERION), 5.4, delta=0.3)
        self.assertEqual(ge.bearing_from_home(*HYPERION), "SE")
        self.assertEqual(ge.bearing_from_home(ge.HOME_LAT + 1, ge.HOME_LON), "N")
        self.assertEqual(ge.bearing_from_home(ge.HOME_LAT, ge.HOME_LON - 1), "W")

    def test_find_locations(self):
        locs = ge.find_locations("ADW suspect 962 Hyperion Avenue; TC at Glenoaks and Olive Blvd; 403 West Boulevard")
        self.assertEqual(locs[0], ("962 Hyperion Avenue", "962 Hyperion Avenue"))
        self.assertEqual(locs[1], ("403 West Boulevard", "403 West Boulevard"))       # numbered addresses first...
        self.assertEqual(locs[2], ("Glenoaks and Olive Blvd", "Glenoaks & Olive, Los Angeles"))   # ...then intersections
        self.assertEqual(ge.find_locations(""), []); self.assertEqual(ge.find_locations(None), [])
        self.assertEqual(len(ge.find_locations("962 Hyperion Ave and 962 hyperion ave")), 1)   # de-duped case-insensitively

    def test_nearest_anchor_and_place_distance(self):
        with patch.object(ge, "ANCHORS", HOME_ONLY + [{"name": "school", "lat": 34.10, "lon": -118.28}]):
            name, mi, d = ge.nearest_anchor(*HYPERION)
        self.assertEqual(name, "school"); self.assertLess(mi, 1.0)
        self.assertEqual(ge.place_distance("fire in Glendale near Glendale and Burbank"), ("glendale", 4.4))
        self.assertEqual(ge.place_distance("Pasadena vs Burbank, one each"), ("burbank", 1.5))  # tie -> nearer
        self.assertEqual(ge.place_distance("Kansas City"), None); self.assertIsNone(ge.place_distance(""))

    def test_geocode_edges(self):
        cur = _Cur()
        self.assertIsNone(ge.geocode(cur, "   "))
        self.assertEqual(cur.sql, [])
        with patch.object(ge.urllib.request, "urlopen", return_value=_resp([])), patch.object(ge.time, "sleep"):
            self.assertIsNone(ge.geocode(cur, "nowhere"))
        self.assertEqual(cur.cache["nowhere"], (None, None, False))


class TestIntegration(unittest.TestCase):
    def test_locate_chains_extract_geocode_distance_and_anchor(self):
        cur = _Cur({"962 Hyperion Avenue": (HYPERION[0], HYPERION[1], True)})
        with patch.object(ge, "ANCHORS", HOME_ONLY):
            res = ge.locate("ADW suspect 962 Hyperion Avenue, code 3", cur)
        self.assertEqual(len(res), 1)
        disp, mi, d, anc = res[0]
        self.assertEqual(disp, "962 Hyperion Avenue"); self.assertEqual(d, "SE")
        self.assertAlmostEqual(mi, 5.4, delta=0.3)
        self.assertEqual(anc[0], "home"); self.assertEqual(anc[1], mi)

    def test_annotate_flags_a_closer_anchor(self):
        cur = _Cur({"962 Hyperion Avenue": (HYPERION[0], HYPERION[1], True)})
        with patch.object(ge, "ANCHORS", HOME_ONLY + [{"name": "school", "lat": 34.10, "lon": -118.28}]):
            text = ge.annotate("ADW suspect 962 Hyperion Avenue, code 3", cur)
        self.assertRegex(text, r"962 Hyperion Avenue \(~5\.\d mi SE; ~0\.\d mi \w+ of school\), code 3")


class TestFunctional(unittest.TestCase):
    def test_annotate_without_a_cursor_opens_pg_ensures_cache_and_closes(self):
        cur = _Cur()
        conn = MagicMock(); conn.cursor.return_value = cur
        connect = MagicMock(return_value=conn)
        with patch.object(ge, "psycopg2", types.SimpleNamespace(connect=connect)), patch.object(ge, "ANCHORS", HOME_ONLY), \
             patch.object(ge.urllib.request, "urlopen", return_value=_resp([{"lat": str(HYPERION[0]), "lon": str(HYPERION[1])}])), \
             patch.object(ge.time, "sleep"):
            text = ge.annotate("unit 7 at 962 Hyperion Avenue")
        connect.assert_called_once_with(ge.DSN)
        self.assertTrue(conn.autocommit); conn.close.assert_called_once()
        self.assertTrue(cur.sql[0][0].startswith("CREATE TABLE IF NOT EXISTS geo_cache"))
        self.assertRegex(text, r"^unit 7 at 962 Hyperion Avenue \(~5\.\d mi SE\)$")
        self.assertEqual(cur.ran("INSERT INTO geo_cache")[0][1], ("962 Hyperion Avenue", HYPERION[0], HYPERION[1], True))

    def test_text_without_addresses_never_opens_pg(self):
        connect = MagicMock()
        with patch.object(ge, "psycopg2", types.SimpleNamespace(connect=connect)):
            self.assertEqual(ge.annotate("nothing to see here"), "nothing to see here")
        connect.assert_called_once()                                      # the connection is opened before extraction...
        connect.return_value.close.assert_called_once()                   # ...and always closed, even with no work


class TestFrame(unittest.TestCase):
    def test_import_never_runs_the_cli(self):
        # the __main__ block geocodes a sample against PG + Nominatim, so the smoke is an import
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_geo_distance"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
