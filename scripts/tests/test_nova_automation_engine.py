#!/usr/bin/env python3
"""Tests for nova_automation_engine.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
Every light / HomeKit power / scene call is mocked: nothing in the house is ever switched."""
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_automation_engine.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="automation-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("automation_engine_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(Path, "home", classmethod(lambda c: TMP)):
        spec.loader.exec_module(mod)
    return mod


ae = _load()
assert str(ae.LOG_FILE).startswith(str(TMP))
# module-level stubs: no Hue, no HomeKit, no scenes, no PG pool from any test
ae.hue_set_light = AsyncMock(return_value=True)
ae._hk_power = MagicMock(return_value=True)
ae.run_scene = AsyncMock(return_value=True)
ae.get_pool = AsyncMock(side_effect=OSError("offline: pool stubbed"))


class _Conn:
    def __init__(self, fetchval=None, fetchrow=None, fetch=()):
        self._fv, self._fr, self._f = fetchval, fetchrow, list(fetch)
        self.executed = []

    async def fetchval(self, *a): self.executed.append(a); return self._fv
    async def fetchrow(self, *a): self.executed.append(a); return self._fr
    async def fetch(self, *a):
        self.executed.append(a)
        return self._f.pop(0) if self._f else []
    async def execute(self, *a): self.executed.append(a)


class _Pool:
    def __init__(self, conn): self.conn = conn
    def acquire(self):
        conn = self.conn
        class _Ctx:
            async def __aenter__(s): return conn
            async def __aexit__(s, *e): return False
        return _Ctx()


def _reset():
    ae._last_actions.clear(); ae._rule_history.clear(); ae._zone_on.clear(); ae._patterns.clear()
    ae._guest_state.update({"active": False, "unknown_devices": [], "since": None})
    ae.hue_set_light.reset_mock(); ae._hk_power.reset_mock()


def _resp(obj):
    r = MagicMock(); r.read.return_value = json.dumps(obj).encode(); return r


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(ae.DB_DSN, r"://[^@/]+:[^@/]+@")   # no password in the DSN

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"(execute|fetch\w*)\(\s*f[\"']")
        self.assertIn("VALUES ('automation_engine', 'automation', 'trigger', $1, 'info')", SRC)

    def test_hue_brightness_is_clamped(self):
        import urllib.request
        hue = _load().hue_set_light   # fresh, unstubbed copy; urlopen mocked
        with patch.object(urllib.request, "urlopen") as m:
            self.assertTrue(asyncio.run(hue(45, True, brightness=9999)))
        self.assertEqual(json.loads(m.call_args[0][0].data)["bri"], 254)

    def test_trigger_rejects_bad_input(self):
        class Req:
            async def json(self): raise ValueError("bad")
        r = asyncio.run(ae.handle_trigger(Req()))
        self.assertEqual(r.status, 400)
        class Req2:
            async def json(self): return {}
        self.assertEqual(asyncio.run(ae.handle_trigger(Req2())).status, 400)


class TestPerformance(unittest.TestCase):
    def test_cooldown_10k_keys_fast_and_history_bounded(self):
        _reset()
        t0 = time.perf_counter()
        for i in range(10_000):
            ae.action_cooldown(f"k{i}"); ae._rule_history.append({"i": i})
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(ae._rule_history), 200)         # deque maxlen keeps memory bounded


class TestRetry(unittest.TestCase):
    def test_presence_fetch_failure_fails_open(self):
        # RETRY GAP: rule_presence_lights — one urlopen attempt; failure returns quietly, no lights
        _reset()
        import urllib.request
        with patch.object(urllib.request, "urlopen", MagicMock(side_effect=OSError("down"))) as m:
            asyncio.run(ae.rule_presence_lights())
        self.assertEqual(m.call_count, 1)
        ae.hue_set_light.assert_not_called()

    def test_is_dark_falls_back_to_clock_when_pg_down(self):
        # RETRY GAP: is_dark — PG failure is one attempt then a clock fallback (bool, never raises)
        self.assertIsInstance(asyncio.run(ae.is_dark()), bool)

    def test_hk_power_failure_returns_false(self):
        # RETRY GAP: _hk_power — single attempt, False on failure
        import urllib.request
        fresh = _load()
        fresh.LOG_FILE = TMP / "a.log"
        with patch.object(urllib.request, "urlopen", MagicMock(side_effect=OSError("x"))) as m:
            self.assertFalse(fresh._hk_power("Bug Zapper", True))
        self.assertEqual(m.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_action_cooldown(self):
        _reset()
        self.assertFalse(ae.action_cooldown("x", 600))
        self.assertTrue(ae.action_cooldown("x", 600))
        ae._last_actions["x"] = time.time() - 700
        self.assertFalse(ae.action_cooldown("x", 600))

    def test_is_dark_uses_lux_threshold(self):
        with patch.object(ae, "get_pool", AsyncMock(return_value=_Pool(_Conn(fetchval=5)))):
            self.assertTrue(asyncio.run(ae.is_dark()))
        with patch.object(ae, "get_pool", AsyncMock(return_value=_Pool(_Conn(fetchval=500)))):
            self.assertFalse(asyncio.run(ae.is_dark()))

    def test_guest_detection_filters_known(self):
        _reset()
        clients = [{"client_name": "iPhone-Jordan", "client_mac": "a", "ip": "1"},
                   {"client_name": "Kitchen-Thing", "client_mac": "k", "ip": "2"},
                   {"client_name": "Strange-Laptop", "client_mac": "s", "ip": "3"}]
        conn = _Conn(fetch=[clients, [{"client_mac": "k"}]])
        with patch.object(ae, "get_pool", AsyncMock(return_value=_Pool(conn))):
            asyncio.run(ae.rule_guest_detection())
        self.assertTrue(ae._guest_state["active"])
        self.assertEqual([d["name"] for d in ae._guest_state["unknown_devices"]], ["Strange-Laptop"])


class TestIntegration(unittest.TestCase):
    def test_climate_alert_writes_shared_observation(self):
        _reset()
        conn = _Conn(fetchrow={"temp_f": 110, "humidity": 10, "feels_like_f": 112}, fetch=[[]])
        with patch.object(ae, "get_pool", AsyncMock(return_value=_Pool(conn))):
            asyncio.run(ae.rule_climate_alerts())
        inserts = [a for a in conn.executed if "INSERT INTO shared_observations" in a[0]]
        self.assertEqual(len(inserts), 1)
        self.assertIn("110", inserts[0][1])

    def test_patterns_learned_then_predict(self):
        _reset()
        conn = _Conn(fetch=[[{"dow": 1, "hour": 22, "room": "bedroom", "occurrences": 12}]])
        with patch.object(ae, "get_pool", AsyncMock(return_value=_Pool(conn))):
            asyncio.run(ae.rule_learn_patterns())
        self.assertEqual(ae._patterns["1:22"][0]["room"], "bedroom")


class TestFunctional(unittest.TestCase):
    def test_presence_lights_golden_path(self):
        _reset()
        import urllib.request
        occ = {"ok": True, "occupancy": {"jordan": {"room": "kitchen", "confidence": 0.9, "home": True}}}
        with patch.object(urllib.request, "urlopen", return_value=_resp(occ)), \
             patch.object(ae, "is_dark", AsyncMock(return_value=True)):
            asyncio.run(ae.rule_presence_lights())
        self.assertEqual([c.args[0] for c in ae.hue_set_light.call_args_list], ae.ROOM_LIGHTS["kitchen"])
        self.assertEqual(ae._rule_history[-1]["rule"], "presence_lights")

    def test_presence_lights_refuses_in_daylight(self):
        _reset()
        import urllib.request
        occ = {"ok": True, "occupancy": {"jordan": {"room": "kitchen", "confidence": 0.9, "home": True}}}
        with patch.object(urllib.request, "urlopen", return_value=_resp(occ)), \
             patch.object(ae, "is_dark", AsyncMock(return_value=False)):
            asyncio.run(ae.rule_presence_lights())
        ae.hue_set_light.assert_not_called()

    def test_presence_devices_on_then_off(self):
        _reset()
        fresh = {"ep": time.time() - 10}
        with patch.object(ae, "get_pool", AsyncMock(return_value=_Pool(_Conn(fetchrow=fresh)))), \
             patch.object(ae, "is_dark", AsyncMock(return_value=False)):
            asyncio.run(ae.rule_presence_devices())
        self.assertTrue(ae._zone_on["patio"])
        self.assertNotIn("office", ae._zone_on)               # dark_only zone stays off in daylight
        stale = {"ep": time.time() - ae.PRESENCE_OFF_DELAY_S - 60}
        # source alive (fetchval = a recent reading from the source) -> genuinely vacant -> OFF
        with patch.object(ae, "get_pool", AsyncMock(return_value=_Pool(_Conn(fetchrow=stale, fetchval=time.time())))):
            asyncio.run(ae.rule_presence_devices())
        self.assertFalse(ae._zone_on["patio"])
        self.assertEqual([c.args[1] for c in ae._hk_power.call_args_list].count(False), 2)

    def test_presence_devices_hold_when_sensor_dead(self):
        # P8 dead-man: a silent presence source is not an empty room — never power OFF on it
        _reset()
        ae._zone_on["patio"] = True
        stale = {"ep": time.time() - ae.PRESENCE_OFF_DELAY_S - 60}
        with patch.object(ae, "get_pool", AsyncMock(return_value=_Pool(_Conn(fetchrow=stale, fetchval=None)))):
            asyncio.run(ae.rule_presence_devices())
        self.assertTrue(ae._zone_on["patio"])
        self.assertNotIn(False, [c.args[1] for c in ae._hk_power.call_args_list])

    def test_kill_switch_holds_state_and_guard_refuses_lock_scene(self):
        import nova_safety_guards as g
        with patch.object(g, "kill_engaged", return_value=True):
            self.assertFalse(ae._actuation_ok("hue light 3 off"))
        with patch.object(g, "kill_engaged", return_value=False), \
             patch.object(ae, "_report_block") as rb:
            self.assertFalse(ae._actuation_ok("scene Lock Up", scene="Lock Up"))
            self.assertTrue(ae._actuation_ok("scene movie", scene="movie"))
            self.assertFalse(ae._actuation_ok("power Garage Door Opener on"))
        self.assertTrue(rb.called)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help/--selftest: main() binds :37468 and starts the rule loops, so only import is smoke-tested
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import importlib.util as u, sys; sp=u.spec_from_file_location('ae', sys.argv[1]);"
                "m=u.module_from_spec(sp); sp.loader.exec_module(m); print('ok')")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(TMP)})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
