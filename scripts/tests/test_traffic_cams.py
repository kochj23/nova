#!/usr/bin/env python3
"""Tests for traffic_cams.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "traffic_cams.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="traffic-cams-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("traffic_cams_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # a data module: loading it must never open a socket
    with patch("urllib.request.urlopen", side_effect=AssertionError("network at import")):
        spec.loader.exec_module(mod)
    return mod


tc = _load()
CAMS = tc.TRAFFIC_CAMERAS


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("import ", SRC)                     # pure data, no runtime behaviour to hijack

    def test_every_url_is_https_on_the_caltrans_public_host(self):
        for key, cam in CAMS.items():
            self.assertTrue(cam["url"].startswith("https://cwwp2.dot.ca.gov/data/d7/cctv/image/"), key)
            self.assertNotIn("?", cam["url"])                 # no embedded query/credentials
            self.assertNotIn("@", cam["url"])


class TestPerformance(unittest.TestCase):
    def test_10k_lookups_by_area_and_role_are_fast(self):
        keys = list(CAMS)
        t0 = time.perf_counter()
        fire = 0
        for i in range(10_000):
            cam = CAMS[keys[i % len(keys)]]
            if cam["role"] == "fire" and cam["area"] == "Pasadena":
                fire += 1
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertGreater(fire, 0)


class TestRetry(unittest.TestCase):
    def test_module_makes_no_external_call_to_retry(self):
        # RETRY GAP: none — traffic_cams.py is a static camera table; the fetch/retry lives in its consumers
        self.assertNotIn("urlopen", SRC)
        self.assertNotIn("subprocess", SRC)
        self.assertNotIn("psycopg2", SRC)
        self.assertEqual([n for n in dir(tc) if not n.startswith("_")], ["TRAFFIC_CAMERAS"])


class TestUnit(unittest.TestCase):
    def test_every_entry_is_complete_and_well_typed(self):
        for key, cam in CAMS.items():
            self.assertEqual(set(cam), {"name", "url", "area", "lat", "lon", "role"}, key)
            self.assertIn(cam["role"], ("commute", "fire"), key)
            self.assertIsInstance(cam["lat"], float); self.assertIsInstance(cam["lon"], float)
            self.assertTrue(34.0 < cam["lat"] < 34.4, key)      # LA basin bounds
            self.assertTrue(-118.5 < cam["lon"] < -118.0, key)
            self.assertTrue(cam["name"] and cam["area"])

    def test_keys_are_unique_slugs_matching_their_image_path(self):
        self.assertEqual(len(set(c["url"] for c in CAMS.values())), len(CAMS))
        for key, cam in CAMS.items():
            self.assertRegex(key, r"^[a-z0-9]+$")
            self.assertTrue(cam["url"].endswith(f"/{key}/{key}.jpg"), key)

    def test_areas_are_the_four_documented_neighbourhoods(self):
        self.assertEqual(set(c["area"] for c in CAMS.values()), {"Burbank", "Glendale", "Glassell Park", "Pasadena"})


class TestIntegration(unittest.TestCase):
    def test_consumers_import_the_table_rather_than_copying_it(self):
        importers = [p.name for p in SCRIPTS.glob("nova_*.py") if "from traffic_cams import TRAFFIC_CAMERAS" in p.read_text()]
        self.assertIn("nova_traffic_watch.py", importers)
        for p in importers:
            self.assertNotIn("cwwp2.dot.ca.gov/data/d7/cctv/image/", (SCRIPTS / p).read_text(), p)

    def test_fire_cameras_cover_every_area(self):
        by_area = Counter(c["area"] for c in CAMS.values() if c["role"] == "fire")
        self.assertEqual(set(by_area), {"Burbank", "Glendale", "Glassell Park", "Pasadena"})


class TestFunctional(unittest.TestCase):
    def test_cache_busted_snapshot_urls_follow_the_documented_recipe(self):
        # the docstring contract: append ?_=<ts> to the base url to defeat the ~1 min CDN cache
        ts = 1_700_000_000
        urls = {k: f"{c['url']}?_={ts}" for k, c in CAMS.items()}
        self.assertEqual(len(urls), len(CAMS))
        self.assertEqual(urls["i537olive"], "https://cwwp2.dot.ca.gov/data/d7/cctv/image/i537olive/i537olive.jpg?_=1700000000")

    def test_unknown_camera_key_is_a_clean_miss(self):
        self.assertIsNone(CAMS.get("nope"))
        with self.assertRaises(KeyError):
            CAMS["nope"]


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_exits_zero(self):
        r = subprocess.run([sys.executable, "-c", "import traffic_cams; print(len(traffic_cams.TRAFFIC_CAMERAS))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), str(len(CAMS)))


if __name__ == "__main__":
    unittest.main()
