#!/usr/bin/env python3
"""Tests for nova_ha_poller.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ha = _load("nova_ha_poller_t", SCRIPTS / "nova_ha_poller.py")
ha.LOG_FILE = Path(tempfile.mkdtemp()) / "nova_ha_poller.log"     # never append to the real log
SRC = (SCRIPTS / "nova_ha_poller.py").read_text()


class _Pool:
    """asyncpg pool stand-in recording (sql, args)."""
    def __init__(self, fail=False):
        self.calls = []; self.fail = fail

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(s):
                return s

            async def __aexit__(s, *a):
                return False

            async def execute(s, sql, *args):
                if pool.fail:
                    raise RuntimeError("pg down")
                pool.calls.append((sql, args))
        return _Ctx()


class _Resp:
    def __init__(self, obj):
        self.obj = obj

    def read(self):
        return json.dumps(self.obj).encode()


def _reset(pool=None):
    ha._pool = pool
    for d in (ha._prev_light_state, ha._prev_media_state, ha._prev_motion_state, ha._prev_scene_state,
              ha._prev_tracker_state, ha._last_tracker_write):
        d.clear()
    ha._access_token = None; ha._token_expires = 0


def _q():
    return redirect_stdout(io.StringIO())


STATES = [
    {"entity_id": "light.office_lamp", "state": "on", "attributes": {"brightness": 200}},
    {"entity_id": "light.kitchen", "state": "off", "attributes": {}},
    {"entity_id": "media_player.garpod", "state": "playing", "attributes": {"app_name": "Music"}},
    {"entity_id": "sensor.hue_outdoor_temperature", "state": "71.5", "attributes": {"device_class": "temperature"}},
    {"entity_id": "binary_sensor.hue_outdoor_motion", "state": "on", "attributes": {"device_class": "motion"}},
    {"entity_id": "device_tracker.jordan_iphone", "state": "home", "attributes": {"battery_level": 80}},
]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ha.DB_DSN)

    def test_sql_uses_placeholders(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        self.assertIn("$1", SRC)

    def test_token_from_keychain_refresh(self):
        _reset()
        with patch.object(ha, "_keychain", return_value="refresh-xyz") as kc, \
                patch.object(ha.urllib.request, "urlopen", return_value=_Resp({"access_token": "acc"})) as uo:
            self.assertEqual(ha.get_ha_token(), "acc")
            self.assertEqual(ha.get_ha_token(), "acc")          # cached, no second exchange
        kc.assert_called_once_with("nova-hass-refresh-token")
        self.assertEqual(uo.call_count, 1)
        self.assertIn(b"refresh_token=refresh-xyz", uo.call_args[0][0].data)


class TestPerformance(unittest.TestCase):
    def test_analyzers_10k_states(self):
        states = STATES * 1700
        t0 = time.perf_counter()
        ha.analyze_lights(states); ha.analyze_media(states); ha.analyze_sensors(states)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_no_refresh_token_fails_open(self):
        _reset()
        with patch.object(ha, "_keychain", return_value=None), patch.object(ha.urllib.request, "urlopen") as uo, _q():
            self.assertIsNone(ha.ha_get_states())
        uo.assert_not_called()

    def test_ha_api_error_one_shot(self):
        # RETRY GAP: ha_get_states — one urlopen; poll_loop's next tick (30s) is the retry
        ha._access_token = "t"; ha._token_expires = time.time() + 100
        with patch.object(ha.urllib.request, "urlopen", side_effect=OSError("refused")) as uo, _q():
            self.assertIsNone(ha.ha_get_states())
        self.assertEqual(uo.call_count, 1)
        self.assertIn("HA API error", ha.LOG_FILE.read_text())

    def test_scene_write_failure_logged_not_raised(self):
        _reset(_Pool(fail=True))
        sc = [{"entity_id": "scene.movie", "state": "2026-01-01T00:00:00"}]
        asyncio.run(ha.write_scene_activations(sc))
        with _q():
            asyncio.run(ha.write_scene_activations([{"entity_id": "scene.movie", "state": "2026-01-02T00:00:00"}]))
        self.assertIn("Failed to log scene activation", ha.LOG_FILE.read_text())


class TestUnit(unittest.TestCase):
    def test_analyze_lights_brightness_threshold(self):
        r = ha.analyze_lights([{"entity_id": "light.office", "state": "on", "attributes": {"brightness": 5}},
                               {"entity_id": "light.hall", "state": "on", "attributes": {"brightness": 50}},
                               {"entity_id": "switch.x", "state": "on"}])
        self.assertEqual(r, {"office": False, "hall": True})

    def test_analyze_media_prefers_playing(self):
        r = ha.analyze_media([{"entity_id": "media_player.office", "state": "paused"},
                              {"entity_id": "media_player.officepod", "state": "playing"},
                              {"entity_id": "media_player.unknown", "state": "playing"}])
        self.assertEqual(list(r), ["office"])
        self.assertTrue(r["office"]["playing"])

    def test_analyze_sensors_bad_values_ignored(self):
        c, m = ha.analyze_sensors([{"entity_id": "sensor.hue_outdoor_t", "state": "unavailable",
                                    "attributes": {"device_class": "temperature"}}])
        self.assertEqual((c, m), ({}, {}))


class TestIntegration(unittest.TestCase):
    def test_analyzed_states_write_expected_tables(self):
        pool = _Pool(); _reset(pool)
        climate, motion = ha.analyze_sensors(STATES)

        async def go():
            await ha.write_climate(climate)
            await ha.write_presence_from_lights(ha.analyze_lights(STATES))
            await ha.write_motion(motion)
            await ha.write_device_tracker(STATES)
        with _q():
            asyncio.run(go())
        tables = [re.search(r"INSERT INTO ([\w.]+)", s).group(1) for s, _ in pool.calls]
        self.assertEqual(tables.count("telemetry.climate"), 1)
        self.assertIn("shared_observations", tables)
        climate_args = pool.calls[0][1]
        self.assertEqual(climate_args, (71.5, None, True))


class TestFunctional(unittest.TestCase):
    def test_transitions_only_and_scene_baseline(self):
        pool = _Pool(); _reset(pool)

        async def go():
            await ha.write_presence_from_lights({"office": False})
            await ha.write_presence_from_lights({"office": False})     # no change -> no write
            await ha.write_presence_from_lights({"office": True})      # off->on: presence + observation
            await ha.write_scene_activations([{"entity_id": "scene.a", "state": "t1"}])   # baseline
            await ha.write_scene_activations([{"entity_id": "scene.a", "state": "t2", "attributes": {"friendly_name": "Movie"}}])
        with _q():
            asyncio.run(go())
        self.assertEqual(len(pool.calls), 4)
        self.assertIn("Lights turned on in office", pool.calls[2][1])
        self.assertEqual(pool.calls[3][1], ("Movie",))

    def test_gps_tracker_heartbeats_but_observes_only_on_change(self):
        # 2026-10-08: change-only writes made a steady "home" look days stale to the presence engine.
        pool = _Pool(); _reset(pool)
        tracker = [s for s in STATES if s["entity_id"].startswith("device_tracker.")]
        with _q():
            asyncio.run(ha.write_device_tracker(tracker))     # first sight: row + observation
            asyncio.run(ha.write_device_tracker(tracker))     # unchanged, inside heartbeat: nothing
            ha._last_tracker_write["jordan"] -= ha.GPS_HEARTBEAT_S + 1
            asyncio.run(ha.write_device_tracker(tracker))     # heartbeat due: row only
        tables = [re.search(r"INSERT INTO ([\w.]+)", s).group(1) for s, _ in pool.calls]
        self.assertEqual(tables, ["telemetry.presence", "shared_observations", "telemetry.presence"])

    def test_log_writes_once_when_stdout_is_the_log_file(self):
        # launchd points stdout at LOG_FILE; log() used to print AND append -> every line twice.
        ha.LOG_FILE.write_text("")
        with open(ha.LOG_FILE, "a") as fh, redirect_stdout(fh):
            ha.log("once-only")
        self.assertEqual(ha.LOG_FILE.read_text().count("once-only"), 1)

    def test_climate_without_temperature_writes_nothing(self):
        pool = _Pool(); _reset(pool)
        asyncio.run(ha.write_climate({"illuminance_lux": 3}))
        self.assertEqual(pool.calls, [])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", "import nova_ha_poller"], cwd=str(SCRIPTS),
                               capture_output=True, text=True, timeout=30,
                               env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
