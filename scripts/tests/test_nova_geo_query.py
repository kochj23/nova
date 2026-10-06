#!/usr/bin/env python3
"""Tests for nova_geo_query.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import math
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_geo_query.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gq = _load("geo_query_under_test", SCRIPT)


class _Cur:
    def __init__(self, one=None, many=()):
        self.one, self.many, self.sql = one, list(many), []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return self.cur


def _pg(ops=None, mem=None, exc=None):
    """Patch psycopg2.connect so the ops DSN gets `ops` and the memories DSN gets `mem`."""
    calls = []

    def connect(dsn, **kw):
        calls.append(dsn)
        if exc:
            raise exc
        return _Conn(mem if "nova_memories" in dsn else ops)
    p = patch("psycopg2.connect", connect)
    p.calls = calls
    return p


def _main(argv, ops=None, mem=None):
    with _pg(ops or _Cur(), mem or _Cur()) as _, patch.object(sys, "argv", ["nova_geo_query.py", *argv]), \
         redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()) as err:
        code = 0
        try:
            gq.main()
        except SystemExit as e:
            code = e.code
    return code, out.getvalue(), err.getvalue()


ROWS = [("Bodie", "state park", "https://x/bodie", 212), ("Cerro Gordo", None, None, 290)]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", gq.MEM_DSN + gq.OPS_DSN)

    def test_user_values_travel_as_parameters_never_in_the_sql(self):
        # the only f-string in the SELECT splices the constant _DIST expression, which itself carries %s placeholders
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        cur = _Cur(many=[])
        with _pg(mem=cur):
            gq.nearest("ghost_town'; DROP TABLE places; --", 36.6, -121.9, 5)
        sql, params = cur.sql[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params, (36.6, -121.9, 36.6, "ghost_town'; DROP TABLE places; --", 5))
        self.assertEqual(sql.count("%s"), 5)

    def test_read_only_module(self):
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM|DROP TABLE)\b", SRC))


class TestPerformance(unittest.TestCase):
    def test_result_shaping_10k_rows_is_fast(self):
        cur = _Cur(many=[(f"place{i}", None, None, i) for i in range(10_000)])
        with _pg(mem=cur):
            t0 = time.perf_counter()
            out = gq.nearest("x", 1.0, 2.0, 10_000)
            dt = time.perf_counter() - t0
        self.assertLess(dt, 0.5)
        self.assertEqual(len(out), 10_000)
        self.assertIsInstance(out[-1]["miles"], float)

    def test_distance_expression_is_bounded_at_zero_distance(self):
        # least(1, ...) guards acos: evaluate the same formula in Python at d=0 with float overshoot
        lat = math.radians(36.6)
        val = math.cos(lat) * math.cos(lat) * math.cos(0) + math.sin(lat) * math.sin(lat)
        self.assertEqual(gq.EARTH_MI * math.acos(min(1, val)), 0.0)
        self.assertIn("least(1,", gq._DIST)


class TestRetry(unittest.TestCase):
    def test_from_point_never_touches_the_ops_db(self):
        # RETRY GAP: home_coords()/psycopg2.connect — one attempt; --from bypasses the lookup entirely
        p = _pg(exc=OSError("pg down"))
        with p:
            self.assertEqual(gq._origin(types.SimpleNamespace(**{"from": "36.6,-121.9"})), (36.6, -121.9, "the given point"))
        self.assertEqual(p.calls, [])

    def test_connect_failure_is_one_shot_and_surfaces_nonzero(self):
        # RETRY GAP: nearest()/categories() — one connect attempt each, no backoff; the CLI exits non-zero
        # rather than printing an empty (misleading) "no places found" answer.
        p = _pg(exc=OSError("pg down"))
        with p, patch.object(sys, "argv", ["nova_geo_query.py", "categories"]), redirect_stderr(io.StringIO()):
            with self.assertRaises(OSError):
                gq.main()
        self.assertEqual(len(p.calls), 1)

    def test_missing_home_fails_closed_with_usage_exit(self):
        code, out, err = _main(["nearest", "ghost_town"], ops=_Cur(one=None))
        self.assertEqual(code, 2)
        self.assertIn("No home coordinates set", err)
        self.assertEqual(out, "")


class TestUnit(unittest.TestCase):
    def test_home_coords_accepts_json_text_or_dict(self):
        with _pg(ops=_Cur(one=('{"lat": "36.6", "lon": "-121.9"}',))):
            self.assertEqual(gq.home_coords(), (36.6, -121.9, "home"))
        with _pg(ops=_Cur(one=({"lat": 1, "lon": 2, "label": "cabin"},))):
            self.assertEqual(gq.home_coords(), (1.0, 2.0, "cabin"))
        with _pg(ops=_Cur(one=None)):
            self.assertIsNone(gq.home_coords())

    def test_nearest_shapes_rows_and_handles_empty(self):
        with _pg(mem=_Cur(many=ROWS)):
            r = gq.nearest("ghost_town", 36.6, -121.9)
        self.assertEqual(r[0], {"name": "Bodie", "detail": "state park", "url": "https://x/bodie", "miles": 212.0})
        self.assertIsNone(r[1]["detail"])
        with _pg(mem=_Cur(many=[])):
            self.assertEqual(gq.nearest("nothing", 0, 0), [])

    def test_origin_parses_from_and_rejects_junk(self):
        self.assertEqual(gq._origin(types.SimpleNamespace(**{"from": "1.5,-2"})), (1.5, -2.0, "the given point"))
        with self.assertRaises(ValueError):
            gq._origin(types.SimpleNamespace(**{"from": "north,south"}))
        with self.assertRaises(ValueError):
            gq._origin(types.SimpleNamespace(**{"from": "1.0"}))


class TestIntegration(unittest.TestCase):
    def test_home_comes_from_service_config_geo_home(self):
        cur = _Cur(one=('{"lat": 36.6, "lon": -121.9}',))
        p = _pg(ops=cur)
        with p:
            gq.home_coords()
        self.assertIn("FROM service_config WHERE service='geo' AND key='home'", cur.sql[0][0])
        self.assertIn("dbname=nova_ops", p.calls[0])

    def test_nearest_reads_places_in_the_memories_db_ordered_by_distance(self):
        cur = _Cur(many=[])
        p = _pg(mem=cur)
        with p:
            gq.nearest("ghost_town", 36.6, -121.9, 3)
        self.assertIn("dbname=nova_memories", p.calls[0])
        sql = cur.sql[0][0]
        self.assertIn("FROM places WHERE category=%s AND lat IS NOT NULL ORDER BY miles LIMIT %s", sql)
        self.assertIn(f"{gq.EARTH_MI}*acos(least(1,", sql)

    def test_home_then_nearest_chain_queries_from_home(self):
        ops, mem = _Cur(one=({"lat": 36.6, "lon": -121.9, "label": "home"},)), _Cur(many=ROWS)
        code, out, _ = _main(["nearest", "ghost_town", "--json"], ops=ops, mem=mem)
        self.assertEqual(code, 0)
        self.assertEqual(mem.sql[0][1][:3], (36.6, -121.9, 36.6))
        self.assertEqual(json.loads(out)["from"], "home")


class TestFunctional(unittest.TestCase):
    def test_nearest_text_output(self):
        code, out, _ = _main(["nearest", "ghost_town", "--from", "36.6,-121.9", "--limit", "2"], mem=_Cur(many=ROWS))
        self.assertEqual(code, 0)
        self.assertEqual(out.splitlines()[0], "Nearest ghost town to the given point:")
        self.assertIn(" 212.0 mi  Bodie (state park)", out)
        self.assertIn(" 290.0 mi  Cerro Gordo\n", out)

    def test_nearest_json_output(self):
        code, out, _ = _main(["nearest", "ghost_town", "--from", "36.6,-121.9", "--json"], mem=_Cur(many=ROWS))
        doc = json.loads(out)
        self.assertEqual(doc["category"], "ghost_town")
        self.assertEqual([r["name"] for r in doc["results"]], ["Bodie", "Cerro Gordo"])

    def test_categories_and_empty_result_paths(self):
        code, out, _ = _main(["categories"], mem=_Cur(many=[("ghost_town", 412), ("ca_tourist_attraction", 90)]))
        self.assertEqual(code, 0)
        self.assertIn("Queryable place categories:", out)
        self.assertRegex(out, r"ghost_town\s+412 located")
        code, out, _ = _main(["nearest", "nope", "--from", "0,0"], mem=_Cur(many=[]))
        self.assertIn("No 'nope' places found. Try: nova_geo_query.py categories", out)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("nearest", r.stdout)
        self.assertIn("categories", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_geo_query"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
