#!/usr/bin/env python3
"""Tests for nova_weather_blurb.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import runpy
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_weather_blurb.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wb = _load("weather_blurb_under_test", SCRIPT)
# Safety net: the module imports psycopg2/urllib lazily inside functions, so patch the real modules for the file.
_PG_DOWN = patch("psycopg2.connect", side_effect=AssertionError("pg must be mocked"))
_NET_DOWN = patch("urllib.request.urlopen", side_effect=AssertionError("network must be mocked"))


def setUpModule():
    _PG_DOWN.start(); _NET_DOWN.start()


def tearDownModule():
    _PG_DOWN.stop(); _NET_DOWN.stop()


def _row(ts=None, **over):
    base = {"ts": ts or datetime.now(timezone.utc), "temp_f": 78.4, "feels_like_f": 80.2, "humidity": 41.0,
            "wind_speed_mph": 6.2, "wind_gust_mph": 11.0, "wind_dir": 247, "pressure_in": 29.912, "uv_index": 7.0,
            "solar_radiation": 600.0, "dew_point_f": 52.0, "pm25": 9.0, "rain_rate_in": 0.0, "rain_daily_in": 0.0}
    base.update(over)
    return tuple(base[f] for f in wb._FIELDS)


def _pg(row):
    """psycopg2.connect stand-in whose cursor answers fetchone() with `row`; records the SQL."""
    conn = MagicMock(); conn.__enter__.return_value = conn
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = row
    return MagicMock(return_value=conn), cur


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d


NWS_POINTS = {"properties": {"forecast": "https://api.weather.gov/gridpoints/LOX/1,1/forecast"}}
NWS_FC = {"properties": {"periods": [
    {"name": "Today", "shortForecast": "Sunny", "temperature": 84, "temperatureUnit": "F"},
    {"name": "Tonight", "shortForecast": "Clear", "temperature": 58, "temperatureUnit": "F"},
    {"name": "Monday", "shortForecast": "Mostly Sunny", "temperature": 86, "temperatureUnit": "F"},
    {"name": "Monday Night", "shortForecast": "Clear", "temperature": 60, "temperatureUnit": "F"}]}}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", wb.PG_DSN)

    def test_sql_is_read_only_and_built_from_constant_identifiers(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", SRC))
        for f in wb._FIELDS:
            self.assertRegex(f, r"^[a-z][a-z0-9_]*$")          # the only interpolation is this allowlist of columns
        connect, cur = _pg(_row())
        with patch("psycopg2.connect", connect):
            wb.weather_snapshot()
        sql = cur.execute.call_args[0][0]
        self.assertEqual(sql, "SELECT " + ", ".join(wb._FIELDS) + " FROM telemetry.weather ORDER BY ts DESC LIMIT 1")
        self.assertEqual(connect.call_args[1]["connect_timeout"], 5)

    def test_nws_is_https_with_an_identifying_user_agent(self):
        uo = MagicMock(side_effect=[_Resp(NWS_POINTS), _Resp(NWS_FC)])
        with patch("urllib.request.urlopen", uo):
            wb.weather_forecast()
        for call in uo.call_args_list:
            req = call[0][0]
            self.assertTrue(req.full_url.startswith("https://api.weather.gov/"))
            self.assertIn("Nova-Journal", req.get_header("User-agent"))
            self.assertEqual(call[1]["timeout"], 10)


class TestPerformance(unittest.TestCase):
    def test_formatting_fast_on_10k_snapshots(self):
        snap = dict(zip(wb._FIELDS, _row(rain_daily_in=0.12)))
        t0 = time.perf_counter()
        with patch.object(wb, "weather_snapshot", lambda: snap):
            for i in range(10_000):
                wb._dir_name(i % 360); wb._fmt(i / 7, ".1f"); wb.weather_facts()
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_snapshot_fails_open_to_none(self):
        # RETRY GAP: weather_snapshot — a single psycopg2.connect; any failure returns None and the dateline degrades
        connect = MagicMock(side_effect=OSError("pg down"))
        with patch("psycopg2.connect", connect):
            self.assertIsNone(wb.weather_snapshot())
            self.assertEqual(wb.weather_facts(), "")
            self.assertIn("Backyard weather-station data is unavailable right now", wb.weather_intro_context())
            line = wb.weather_dateline_line()
        self.assertEqual(connect.call_count, 4)
        self.assertRegex(line, r"^\*Burbank · \w+, \w+ \d{1,2}, \d{4} · \d{1,2}:\d\d [AP]M\*\n\n$")

    def test_forecast_fails_open_to_empty_string(self):
        # RETRY GAP: weather_forecast — one NWS round-trip (two GETs), no backoff; '' on any failure
        uo = MagicMock(side_effect=OSError("nws down"))
        with patch("urllib.request.urlopen", uo):
            self.assertEqual(wb.weather_forecast(), "")
            self.assertEqual(wb.weather_forecast_context(), "")
        self.assertEqual(uo.call_count, 2)
        uo = MagicMock(side_effect=[_Resp(NWS_POINTS), _Resp({"properties": {}})])      # second hop malformed
        with patch("urllib.request.urlopen", uo):
            self.assertEqual(wb.weather_forecast(), "")


class TestUnit(unittest.TestCase):
    def test_dir_name(self):
        self.assertEqual(wb._dir_name(None), "")
        self.assertEqual([wb._dir_name(d) for d in (0, 22.5, 90, 180, 247, 270, 359, 360, 725)],
                         ["N", "NNE", "E", "S", "WSW", "W", "N", "N", "N"])
        self.assertEqual(wb._dir_name("45"), "NE")

    def test_fmt(self):
        self.assertEqual(wb._fmt(78.4, ".0f"), "78")
        self.assertEqual(wb._fmt("29.912", ".2f"), "29.91")
        self.assertIsNone(wb._fmt(None, ".0f"))
        self.assertIsNone(wb._fmt("n/a", ".0f"))

    def test_weather_facts_full_and_sparse(self):
        snap = dict(zip(wb._FIELDS, _row()))
        with patch.object(wb, "weather_snapshot", lambda: snap):
            self.assertEqual(wb.weather_facts(),
                             "78°F, feels like 80°F, 41% humidity, wind 6 mph WSW (gusts 11), 29.91 inHg, UV 7, PM2.5 9")
        snap = dict(zip(wb._FIELDS, _row(feels_like_f=78.0, wind_gust_mph=5.0, wind_dir=None, uv_index=None, pm25="x")))
        with patch.object(wb, "weather_snapshot", lambda: snap):
            self.assertEqual(wb.weather_facts(), "78°F, 41% humidity, wind 6 mph, 29.91 inHg")
        with patch.object(wb, "weather_snapshot", lambda: {f: None for f in wb._FIELDS}):
            self.assertEqual(wb.weather_facts(), "")

    def test_rain_only_when_relevant(self):
        def facts(**kw):
            with patch.object(wb, "weather_snapshot", lambda: dict(zip(wb._FIELDS, _row(**kw)))):
                return wb.weather_facts()
        self.assertTrue(facts(rain_rate_in=0.25, rain_daily_in=0.5).endswith("raining 0.25 in/hr"))
        self.assertTrue(facts(rain_rate_in=0.0, rain_daily_in=0.5).endswith('0.50" rain today'))
        self.assertNotIn("rain", facts(rain_rate_in=0.0, rain_daily_in=0.0))
        self.assertNotIn("rain", facts(rain_rate_in=None, rain_daily_in=None))


class TestIntegration(unittest.TestCase):
    def test_snapshot_maps_fields_and_flags_stale_rows(self):
        fresh = datetime.now(timezone.utc) - timedelta(minutes=5)
        connect, _ = _pg(_row(ts=fresh))
        with patch("psycopg2.connect", connect):
            snap = wb.weather_snapshot()
        self.assertEqual(set(snap), set(wb._FIELDS))
        self.assertNotIn("_stale", snap)
        connect, _ = _pg(_row(ts=datetime.now(timezone.utc) - timedelta(hours=2)))
        with patch("psycopg2.connect", connect):
            self.assertTrue(wb.weather_snapshot()["_stale"])
        connect, _ = _pg(None)
        with patch("psycopg2.connect", connect):
            self.assertIsNone(wb.weather_snapshot())

    def test_intro_and_dateline_carry_the_live_facts(self):
        connect, _ = _pg(_row())
        with patch("psycopg2.connect", connect):
            intro, line = wb.weather_intro_context(), wb.weather_dateline_line()
        self.assertIn("Burbank backyard station: 78°F, feels like 80°F", intro)
        self.assertTrue(intro.startswith("OPEN THE ARTICLE with a short, in-voice dateline blurb"))
        self.assertTrue(line.startswith("*Burbank · ") and line.endswith(" · 78°F, feels like 80°F, 41% humidity, wind 6 mph WSW (gusts 11), 29.91 inHg, UV 7, PM2.5 9*\n\n"))

    def test_forecast_context_wraps_nws_periods(self):
        uo = MagicMock(side_effect=[_Resp(NWS_POINTS), _Resp(NWS_FC)])
        with patch("urllib.request.urlopen", uo):
            ctx = wb.weather_forecast_context()
        self.assertEqual(uo.call_args_list[0][0][0].full_url, f"https://api.weather.gov/points/{wb.BURBANK_LAT},{wb.BURBANK_LON}")
        self.assertEqual(uo.call_args_list[1][0][0].full_url, NWS_POINTS["properties"]["forecast"])
        self.assertTrue(ctx.endswith("NWS Burbank forecast — Today: Sunny, 84°F. Tonight: Clear, 58°F. Monday: Mostly Sunny, 86°F."))
        self.assertNotIn("Monday Night", ctx)                         # only the first three periods


class TestFunctional(unittest.TestCase):
    def _main(self, connect):
        buf = io.StringIO()
        with patch("psycopg2.connect", connect), patch.object(sys, "argv", ["nova_weather_blurb.py"]), redirect_stdout(buf):
            runpy.run_path(str(SCRIPT), run_name="__main__")
        return buf.getvalue()

    def test_cli_golden_path_prints_snapshot_dateline_facts_and_intro(self):
        connect, _ = _pg(_row(rain_daily_in=0.3))
        out = self._main(connect)
        self.assertIn("snapshot: {'ts': ", out)
        self.assertRegex(out, r"dateline: \*Burbank · .* · 78°F, .*0\.30\" rain today\*")
        self.assertIn("facts: 78°F, feels like 80°F", out)
        self.assertIn("---\nOPEN THE ARTICLE with a short, in-voice dateline blurb", out)
        self.assertEqual(connect.call_count, 4)                      # one PG round-trip per public call, no caching

    def test_cli_with_pg_down_degrades_to_date_only(self):
        out = self._main(MagicMock(side_effect=OSError("pg down")))
        self.assertIn("snapshot: None", out)
        self.assertIn("facts: \n", out)
        self.assertIn("Backyard weather-station data is unavailable right now", out)


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_weather_blurb"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_import_never_connects(self):
        with patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            m = _load("weather_frame_probe", SCRIPT)
        self.assertEqual(m.PG_DSN, wb.PG_DSN)


if __name__ == "__main__":
    unittest.main()
