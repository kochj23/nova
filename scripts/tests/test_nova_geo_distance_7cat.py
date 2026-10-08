"""7-category tests for nova_geo_distance's private home point (2026-10-08: the exact home
coordinates and street address were removed from the public repo; they now come from the private
service_config 'home' row with a zip-centroid fallback)."""
import importlib
import json
import pathlib
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import psycopg2  # noqa: E402
import nova_geo_distance as g  # noqa: E402

SRC = pathlib.Path(g.__file__).read_text()


def _conn(value):
    cur = MagicMock()
    cur.fetchone.return_value = None if value is None else (value,)
    c = MagicMock()
    c.cursor.return_value = cur
    return c


class TestSecurity(unittest.TestCase):
    def test_no_exact_point_or_street_in_source(self):
        self.assertNotIn("34.1679", SRC)
        self.assertNotIn("118.3148", SRC)
        self.assertNotRegex(SRC, r"\d+ S \w+ Pl")

    def test_query_is_constant_no_interpolation(self):
        c = _conn(json.dumps({"lat": 1, "lon": 2}))
        with patch.object(psycopg2, "connect", return_value=c):
            g._private_home()
        sql = c.cursor.return_value.execute.call_args[0][0]
        self.assertEqual(sql, "SELECT value FROM service_config WHERE key='home'")


class TestPerformance(unittest.TestCase):
    def test_haversine_fast(self):
        t = time.perf_counter()
        for _ in range(10000):
            g.haversine_mi(34.18, -118.31)
        self.assertLess(time.perf_counter() - t, 1.0)


class TestRetry(unittest.TestCase):
    def test_retries_then_falls_back(self):
        with patch.object(psycopg2, "connect", side_effect=psycopg2.OperationalError("down")) as m, \
                patch.object(g.time, "sleep"):
            self.assertEqual(g._private_home(), g.ZIP_CENTROID)
        self.assertEqual(m.call_count, 3)

    def test_recovers_on_second_try(self):
        good = _conn({"lat": 10.0, "lon": 20.0})
        with patch.object(psycopg2, "connect", side_effect=[psycopg2.OperationalError("x"), good]), \
                patch.object(g.time, "sleep"):
            self.assertEqual(g._private_home(), (10.0, 20.0))


class TestUnit(unittest.TestCase):
    def test_reads_json_string(self):
        with patch.object(psycopg2, "connect", return_value=_conn(json.dumps({"lat": 1.5, "lon": -2.5}))):
            self.assertEqual(g._private_home(), (1.5, -2.5))

    def test_missing_row_uses_centroid(self):
        with patch.object(psycopg2, "connect", return_value=_conn(None)):
            self.assertEqual(g._private_home(), g.ZIP_CENTROID)

    def test_malformed_row_uses_centroid(self):
        with patch.object(psycopg2, "connect", return_value=_conn("not json")):
            self.assertEqual(g._private_home(), g.ZIP_CENTROID)


class TestIntegration(unittest.TestCase):
    def test_distance_uses_loaded_home(self):
        with patch.object(g, "HOME_LAT", 0.0), patch.object(g, "HOME_LON", 0.0):
            self.assertAlmostEqual(g.haversine_mi(0.0, 0.0), 0.0, places=6)
            self.assertGreater(g.haversine_mi(1.0, 0.0), 60)


class TestFunctional(unittest.TestCase):
    def test_import_with_db_down_still_works(self):
        with patch.object(psycopg2, "connect", side_effect=psycopg2.OperationalError("down")), \
                patch("time.sleep"):
            mod = importlib.reload(g)
        self.assertEqual((mod.HOME_LAT, mod.HOME_LON), mod.ZIP_CENTROID)
        importlib.reload(g)  # restore real state for other tests


class TestFrame(unittest.TestCase):
    def test_module_exposes_api(self):
        for name in ("haversine_mi", "bearing_from_home", "locate", "annotate", "_private_home"):
            self.assertTrue(callable(getattr(g, name)))


if __name__ == "__main__":
    unittest.main()
