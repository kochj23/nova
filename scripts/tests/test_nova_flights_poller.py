#!/usr/bin/env python3
"""Tests for nova_flights_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_flights_poller.py"
SRC = SCRIPT.read_text()


def _stubs():
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    return {"nova_notify": nn}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stubs()):       # notify bus stubbed at import; restored after
        spec.loader.exec_module(mod)
    return mod


fp = _load("fp_mod", SCRIPT)


class _Resp:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class _Cur:
    """Answers registry lookups from `registry` and recent-ping lookups from `pinged`."""
    def __init__(self, registry=None, pinged=()):
        self.registry = registry or {}; self.pinged = set(pinged); self.sql = []; self._last = None

    def execute(self, sql, params=None):
        self.sql.append((" ".join(sql.split()), params)); self._last = (sql, params)

    def fetchone(self):
        sql, params = self._last
        if "aircraft_registry" in sql and sql.startswith("SELECT"):
            return (self.registry[params[0]],) if params[0] in self.registry else None
        if "overhead_flights" in sql and sql.startswith("SELECT"):
            return (1,) if params[0] in self.pinged else None
        return None

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur):
        self.cur = cur; self.closed = False; self.autocommit = False

    def cursor(self):
        return self.cur

    def close(self):
        self.closed = True


def _ac(**kw):
    base = {"hex": "a1b2c3", "alt_baro": 1500, "dst": 0.9, "category": "A7", "squawk": "1200", "t": "R44",
            "flight": "N123AB ", "r": "N123AB", "dir": 90, "gs": 80, "track": 180, "baro_rate": 0, "mlat": []}
    base.update(kw)
    return base


def _run(aircraft, cur=None, hexdb=None):
    cur = cur or _Cur()
    conn = _Conn(cur)
    fp.notify.reset_mock()

    def urlopen(req, timeout=10):
        url = req.full_url
        if "adsb.lol" in url:
            return _Resp({"ac": aircraft})
        if hexdb is None:
            raise urllib.error.HTTPError(url, 404, "nf", {}, None)
        if isinstance(hexdb, Exception):
            raise hexdb
        return _Resp(hexdb)
    with patch.object(fp.urllib.request, "urlopen", side_effect=urlopen), \
         patch.object(fp.psycopg2, "connect", return_value=conn), redirect_stdout(io.StringIO()) as out:
        rc = fp.main()
    return rc, cur, conn, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", fp.DSN)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        rc, cur, _, _ = _run([_ac(hex="x'; select 1; --", flight="evil")])
        for sql, params in cur.sql:
            self.assertNotIn("select 1", sql)
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"aircraft_registry", "telemetry.overhead_flights"})

    def test_feed_carries_a_user_agent_and_timeout(self):
        with patch.object(fp.urllib.request, "urlopen", return_value=_Resp({"ac": []})) as u:
            fp.fetch()
        req, kw = u.call_args[0][0], u.call_args[1]
        self.assertEqual(req.get_header("User-agent"), "Nova/flights")
        self.assertEqual(kw["timeout"], 10)


class TestPerformance(unittest.TestCase):
    def test_compass_10k_bearings_under_bound(self):
        t0 = time.perf_counter()
        out = [fp.compass(b) for b in range(10_000)]
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(set(out), set(fp.COMPASS))

    def test_10k_aircraft_filtered_under_bound(self):
        aircraft = [_ac(hex=f"{i:06x}", alt_baro=20000) for i in range(10_000)]   # all above ceiling
        t0 = time.perf_counter()
        rc, cur, _, out = _run(aircraft)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(cur.sql, [])
        self.assertIn("0 aircraft over 91506", out)


class TestRetry(unittest.TestCase):
    def test_feed_failure_fails_open_rc0_no_pg(self):
        # RETRY GAP: fetch()/urllib.request.urlopen — one attempt; failure prints and returns 0 (next 30s poll retries)
        attempts = []

        def boom(*a, **k):
            attempts.append(1); raise OSError("feed down")
        with patch.object(fp.urllib.request, "urlopen", side_effect=boom), patch.object(fp.psycopg2, "connect") as pg, \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(fp.main(), 0)
        self.assertEqual(len(attempts), 1); pg.assert_not_called()
        self.assertIn("feed unreachable", out.getvalue())

    def test_hexdb_5xx_is_not_cached_but_404_is(self):
        # RETRY GAP: lookup_operator()/hexdb.io — no in-process retry; a 5xx/network error skips the cache
        # write so the NEXT pass re-queries, while a 404 caches NULL to stop re-querying forever.
        cur = _Cur()
        with patch.object(fp.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("u", 503, "x", {}, None)):
            self.assertIsNone(fp.lookup_operator("abc", cur))
        self.assertEqual(cur.ran("INSERT INTO aircraft_registry"), [])
        cur = _Cur()
        with patch.object(fp.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("u", 404, "x", {}, None)):
            self.assertIsNone(fp.lookup_operator("abc", cur))
        self.assertEqual(cur.ran("INSERT INTO aircraft_registry")[0][1], ("abc", None, None, None, None))

    def test_notify_failure_is_swallowed_and_row_still_logged(self):
        fp.notify.side_effect = RuntimeError("bus down")
        try:
            rc, cur, _, out = _run([_ac()])
        finally:
            fp.notify.side_effect = None
        self.assertEqual(rc, 0)
        self.assertEqual(len(cur.ran("INSERT INTO telemetry.overhead_flights")), 1)
        self.assertIn("notify failed for a1b2c3", out)


class TestUnit(unittest.TestCase):
    def test_compass(self):
        self.assertEqual(fp.compass(0), "N"); self.assertEqual(fp.compass(359), "N")
        self.assertEqual(fp.compass(45), "NE"); self.assertEqual(fp.compass("180"), "S")
        self.assertEqual(fp.compass(None), "?"); self.assertEqual(fp.compass("x"), "?")

    def test_fetch_returns_ac_list(self):
        with patch.object(fp.urllib.request, "urlopen", return_value=_Resp({"ac": [1, 2]})):
            self.assertEqual(fp.fetch(), [1, 2])
        with patch.object(fp.urllib.request, "urlopen", return_value=_Resp({})):
            self.assertEqual(fp.fetch(), [])

    def test_lookup_operator_cached_hit_skips_http(self):
        cur = _Cur(registry={"abc": "LAPD Air Support"})
        with patch.object(fp.urllib.request, "urlopen") as u:
            self.assertEqual(fp.lookup_operator("abc", cur), "LAPD Air Support")
        u.assert_not_called()

    def test_lookup_operator_fetches_and_caches(self):
        cur = _Cur()
        with patch.object(fp.urllib.request, "urlopen", return_value=_Resp({"RegisteredOwners": " Burbank Heli ", "Registration": "N1",
                                                                               "Manufacturer": "Robinson", "Type": "R44"})):
            self.assertEqual(fp.lookup_operator("abc", cur), "Burbank Heli")
        self.assertEqual(cur.ran("INSERT INTO aircraft_registry")[0][1], ("abc", "N1", "Burbank Heli", "Robinson", "R44"))

    def test_filters_altitude_and_distance(self):
        rc, cur, _, _ = _run([_ac(alt_baro="ground"), _ac(alt_baro=10000), _ac(dst=3.1), _ac(dst=None), _ac(hex="ok", alt_baro=9999, dst=3.0)])
        ins = cur.ran("INSERT INTO telemetry.overhead_flights")
        self.assertEqual(len(ins), 1); self.assertEqual(ins[0][1][0], "ok")


class TestIntegration(unittest.TestCase):
    def test_cooldown_uses_the_notified_column_as_state(self):
        rc, cur, _, _ = _run([_ac()], cur=_Cur(pinged={"a1b2c3"}))
        q = cur.ran("WHERE hex=%s AND notified")[0]
        self.assertEqual(q[1], ("a1b2c3", "45 minutes"))
        fp.notify.assert_not_called()
        self.assertFalse(cur.ran("INSERT INTO telemetry.overhead_flights")[0][1][17])   # notified=False

    def test_emergency_squawk_always_pings_even_in_cooldown(self):
        rc, cur, _, _ = _run([_ac(squawk="7700", category="A1")], cur=_Cur(pinged={"a1b2c3"}))
        fp.notify.assert_called_once()
        args, kw = fp.notify.call_args
        self.assertTrue(args[0].startswith("🚨 EMERGENCY squawk 7700"))
        self.assertEqual(kw["level"], "warning"); self.assertNotIn("dedup_window_s", kw["meta"])

    def test_notify_payload_shape_for_helicopter(self):
        rc, cur, _, _ = _run([_ac()], hexdb={"RegisteredOwners": "LAPD"})
        args, kw = fp.notify.call_args
        self.assertEqual(args[0], "🚁 Robinson R44 — LAPD (N123AB) overhead — 1500 ft, 0.9 NM E")
        self.assertEqual(kw["dedup_key"], "flight:a1b2c3"); self.assertEqual(kw["category"], "flights")
        self.assertEqual(kw["meta"]["dedup_window_s"], 45 * 60)
        self.assertEqual(kw["source"], "nova_flights_poller.py")


class TestFunctional(unittest.TestCase):
    def test_golden_path_logs_and_pings(self):
        aircraft = [_ac(), _ac(hex="plane1", category="A3", alt_baro=3000, dst=1.0, t="B738", flight="SWA123", r=""),
                    _ac(hex="plane2", category="A3", alt_baro=8000, dst=2.0, t="ZZZ9")]
        rc, cur, conn, out = _run(aircraft)
        self.assertEqual(rc, 0)
        rows = [p for _, p in cur.ran("INSERT INTO telemetry.overhead_flights")]
        self.assertEqual([r[0] for r in rows], ["a1b2c3", "plane1", "plane2"])
        self.assertEqual([r[17] for r in rows], [True, True, False])        # heli, low pass, plain overflight
        self.assertEqual(rows[1][5], "Boeing 737-800"); self.assertEqual(rows[2][5], "ZZZ9")
        self.assertEqual(json.loads(rows[0][18])["hex"], "a1b2c3")
        self.assertEqual(fp.notify.call_count, 2)
        self.assertTrue(fp.notify.call_args_list[1][0][0].startswith("✈️ Low pass — Boeing 737-800 (SWA123)"))
        self.assertIn("3 aircraft over 91506 (<10000ft), 2 pinged.", out)
        self.assertTrue(conn.closed and conn.autocommit)

    def test_pg_down_after_feed_raises(self):
        with patch.object(fp.urllib.request, "urlopen", return_value=_Resp({"ac": [_ac()]})), \
             patch.object(fp.psycopg2, "connect", side_effect=OSError("pg down")):
            with self.assertRaises(OSError):
                fp.main()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        snippet = ("import urllib.request; urllib.request.urlopen = lambda *a, **k: (_ for _ in ()).throw(AssertionError('net at import'))\n"
                   "import nova_flights_poller as m; assert m.ALT_CEILING_FT == 10000")
        r = subprocess.run([sys.executable, "-c", snippet], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
