#!/usr/bin/env python3
"""Tests for nova_weekly_flights.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_weekly_flights.py"
SRC = SCRIPT.read_text()


@contextmanager
def _stubbed(mods):
    """Set sys.modules keys for the duration and restore ONLY those keys afterwards."""
    old = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load():
    # nova_local_burbank pulls in nova_journal (PG at import): stand it in for the load
    lb = types.ModuleType("nova_local_burbank")
    lb.call_llm = MagicMock(return_value="x" * 500); lb.publish = MagicMock(); lb.generate_image = MagicMock(return_value="/tmp/x.webp")
    with _stubbed({"nova_local_burbank": lb}):
        spec = importlib.util.spec_from_file_location("weekly_flights", SCRIPT)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


wf = _load()


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _route_net(answers):
    """urlopen stand-in for adsbdb lookups keyed by callsign in the URL."""
    def urlopen(req, timeout=None):
        cs = req.full_url.rsplit("/", 1)[1]
        a = answers.get(cs)
        if isinstance(a, Exception):
            raise a
        return _Resp(json.dumps(a or {"response": {}}).encode())
    return urlopen


def _route(origin, dest, oc="Portland", dc="Burbank"):
    return {"response": {"flightroute": {"origin": {"iata_code": origin, "municipality": oc},
                                         "destination": {"iata_code": dest, "municipality": dc}}}}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_route_api_needs_no_key_and_is_read_only(self):
        self.assertNotIn("Authorization", SRC)
        self.assertTrue(wf.ADSBDB.startswith("https://"))
        self.assertNotIn("INSERT", SRC); self.assertNotIn("UPDATE ", SRC)

    def test_flight_queries_filter_to_a_fixed_window(self):
        # only a constant 7-day interval and ints are interpolated into the SQL (no user value reaches it)
        self.assertIn("interval '7 days'", SRC)
        self.assertIsInstance(wf.ROUTE_LOOKUPS, int)


class TestPerformance(unittest.TestCase):
    def test_build_summary_10k_routes_fast(self):
        d = {"totals": {"sightings": 9, "callsigns": 3, "aircraft": 2, "heli_sightings": 1,
                        "closest_nm": 0.4, "highest_ft": 40000, "fastest_kt": 500},
             "operators": [], "orbiters": [], "types": [], "busiest_hours": [], "closest": [], "lowest": []}
        routes = {f"SWA{i}": {"origin": "PDX", "origin_city": "Portland", "dest": "BUR", "dest_city": "Burbank",
                              "text": "PDX (Portland) -> BUR (Burbank)"} for i in range(10_000)}
        t0 = time.perf_counter()
        out = wf.build_summary(d, routes)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertIn("TOP ORIGINS arriving into BUR", out)


class TestRetry(unittest.TestCase):
    def test_route_lookup_failure_is_one_shot_and_skipped(self):
        # RETRY GAP: enrich_routes/adsbdb GET — one attempt per callsign, bad ones silently dropped
        net = _route_net({"SWA1": OSError("timeout"), "AAL2": _route("JFK", "BUR")})
        u = MagicMock(side_effect=net)
        with patch.object(wf.urllib.request, "urlopen", u):
            routes = wf.enrich_routes([{"callsign": "SWA1"}, {"callsign": "AAL2"}, {"callsign": ""}])
        self.assertEqual(list(routes), ["AAL2"])
        self.assertEqual(u.call_count, 2)               # empty callsign never hits the network

    def test_main_aborts_when_llm_too_short(self):
        wf.call_llm.reset_mock(); wf.publish.reset_mock()
        wf.call_llm.return_value = "tiny"
        with patch.object(wf, "fetch_flight_data", return_value={"top_callsigns": []}), \
             patch.object(wf, "enrich_routes", return_value={}), patch.object(wf, "build_summary", return_value="s"), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(wf.main(), 1)
        wf.publish.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_enrich_parses_route_text(self):
        net = _route_net({"SWA1": _route("PDX", "BUR", "Portland", "Burbank")})
        with patch.object(wf.urllib.request, "urlopen", net):
            routes = wf.enrich_routes([{"callsign": " SWA1 "}])
        self.assertEqual(routes["SWA1"]["text"], "PDX (Portland) -> BUR (Burbank)")

    def test_build_summary_without_routes_is_honest(self):
        d = {"totals": {k: 0 for k in ("sightings", "callsigns", "aircraft", "heli_sightings",
                                       "closest_nm", "highest_ft", "fastest_kt")},
             "operators": [], "orbiters": [], "types": [], "busiest_hours": [], "closest": [], "lowest": []}
        out = wf.build_summary(d, {})
        self.assertIn("No from->to routes resolved", out)

    def test_home_airports_constant(self):
        self.assertEqual(wf.HOME_AIRPORTS, {"BUR", "KBUR"})


class TestIntegration(unittest.TestCase):
    def test_bur_arrivals_and_departures_split_by_home_airport(self):
        d = {"totals": {k: 1 for k in ("sightings", "callsigns", "aircraft", "heli_sightings",
                                       "closest_nm", "highest_ft", "fastest_kt")},
             "operators": [], "orbiters": [], "types": [], "busiest_hours": [], "closest": [], "lowest": []}
        routes = {"A": {"origin": "BUR", "origin_city": "Burbank", "dest": "SFO", "dest_city": "SF", "text": "BUR -> SFO"},
                  "B": {"origin": "SEA", "origin_city": "Seattle", "dest": "BUR", "dest_city": "Burbank", "text": "SEA -> BUR"},
                  "C": {"origin": "JFK", "origin_city": "NY", "dest": "LAX", "dest_city": "LA", "text": "JFK -> LAX"}}
        out = wf.build_summary(d, routes)
        self.assertIn("SF (SFO)", out.split("TOP DESTINATIONS")[1].split("TOP ORIGINS")[0])
        self.assertIn("Seattle (SEA)", out.split("TOP ORIGINS")[1])
        self.assertIn("transiting overflights", out)

    def test_fetch_flight_data_uses_overhead_flights(self):
        self.assertGreaterEqual(SRC.count("FROM telemetry.overhead_flights"), 5)
        self.assertIn("psycopg2.connect(DSN)", SRC)


class TestFunctional(unittest.TestCase):
    def test_main_golden_path_publishes(self):
        wf.call_llm.reset_mock(); wf.publish.reset_mock(); wf.generate_image.reset_mock()
        wf.call_llm.return_value = "A full and sarcastic flight report. " * 30
        with patch.object(wf, "fetch_flight_data", return_value={"top_callsigns": [{"callsign": "SWA1"}]}), \
             patch.object(wf, "enrich_routes", return_value={}), patch.object(wf, "build_summary", return_value="summary"), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(wf.main(), 0)
        wf.publish.assert_called_once()
        title, body, img = wf.publish.call_args.args
        self.assertTrue(title.startswith("What Flew Over Burbank This Week"))

    def test_image_failure_still_publishes(self):
        wf.call_llm.reset_mock(); wf.publish.reset_mock()
        wf.call_llm.return_value = "report " * 100; wf.generate_image.side_effect = RuntimeError("no gpu")
        try:
            with patch.object(wf, "fetch_flight_data", return_value={"top_callsigns": []}), \
                 patch.object(wf, "enrich_routes", return_value={}), patch.object(wf, "build_summary", return_value="s"), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(wf.main(), 0)
        finally:
            wf.generate_image.side_effect = None
        self.assertIsNone(wf.publish.call_args.args[2])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        code = ("import sys, types\n"
                "lb = types.ModuleType('nova_local_burbank')\n"
                "lb.call_llm = lb.publish = lb.generate_image = lambda *a, **k: None\n"
                "sys.modules['nova_local_burbank'] = lb\n"
                "import nova_weekly_flights as m\nprint(m.ROUTE_LOOKUPS)\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "75")


if __name__ == "__main__":
    unittest.main()
