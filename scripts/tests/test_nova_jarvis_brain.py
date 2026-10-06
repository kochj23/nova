#!/usr/bin/env python3
"""Tests for nova_jarvis_brain.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_jarvis_brain.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # asyncpg/aiohttp are real + installed; nothing else happens at import
    return mod


JB = _load("jarvis_brain_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="jarvis-test-"))
JB.LOG_FILE = TMP / "nova_jarvis_brain.log"
JB.FRAME_DIR = TMP / "frames"


class _FixedDT(datetime):
    """datetime stand-in whose now() reports a chosen hour (the brain reads datetime.now().hour)."""
    hour_override = 12

    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 10, 5, cls.hour_override, 30, tzinfo=tz)
        return base


def _at_hour(h):
    _FixedDT.hour_override = h
    return mock.patch.object(JB, "datetime", _FixedDT)


def _s(eid, state, **attrs):
    return {"entity_id": eid, "state": state, "attributes": attrs}


HOME = _s("device_tracker.jordan_s_iphone", "home")


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d


class _FakePool:
    """asyncpg pool stand-in: records every execute(sql, *args)."""
    def __init__(self, result="INSERT 0 1"):
        self.calls, self.result = [], result

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(_):
                conn = types.SimpleNamespace()
                async def execute(sql, *args):
                    pool.calls.append((sql, args)); return pool.result
                conn.execute = execute
                return conn
            async def __aexit__(_, *a): return False
        return _Ctx()


def _reset_cb():
    JB._vision_cb.update({"fails": 0, "open_until": 0.0, "logged_open": False})


class _Quiet(unittest.TestCase):
    def setUp(self):
        self._buf = io.StringIO(); self._rs = redirect_stdout(self._buf); self._rs.__enter__()
        _reset_cb()
        JB._current_activity = {"state": "unknown", "confidence": 0.0, "since": None, "signals": {}}
        JB._last_activity_write = None
        JB._environment.clear()

    def tearDown(self):
        self._rs.__exit__(None, None, None)


class TestSecurity(_Quiet):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_ha_token_comes_from_keychain(self):
        self.assertIn("nova-hass-refresh-token", SRC)
        with mock.patch.object(JB.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="rt\n")) as sp, \
             mock.patch.object(JB.urllib.request, "urlopen", return_value=_Resp({"access_token": "at"})) as uo:
            self.assertEqual(JB.get_ha_token(), "at")
        self.assertEqual(sp.call_args[0][0][:2], ["security", "find-generic-password"])
        self.assertIn(b"refresh_token=rt", uo.call_args[0][0].data)

    def test_vision_frames_never_leave_the_box(self):
        # DLP: interior frames go to local Ollama only — no cloud vision endpoint anywhere in the source
        for needle in ("api.openai.com", "anthropic.com", "openrouter", "generativelanguage", "claude"):
            self.assertNotIn(needle, SRC.lower())
        self.assertTrue(JB.OLLAMA_URL.startswith("http://127.0.0.1:"))
        img = TMP / "f.jpg"; img.write_bytes(b"\xff\xd8jpeg")
        with mock.patch.object(JB.urllib.request, "urlopen", return_value=_Resp({"message": {"content": "a room"}})) as uo:
            JB.vision_describe(str(img))
        self.assertEqual(uo.call_args[0][0].full_url, f"{JB.OLLAMA_URL}/api/chat")

    def test_sql_uses_positional_parameters(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        for sql in ("VALUES ($1, 'scene', $2, 0.8)", "VALUES (now(), 'jordan', $1, $2, $3, $4)", "SELECT 'jarvis_brain', 'environmental', $1, $2, 'info'"):
            self.assertIn(sql, SRC)


class TestPerformance(_Quiet):
    def test_classify_10k_entities_fast(self):
        states = [HOME] + [_s(f"light.room{i}", "on" if i % 2 else "off") for i in range(5_000)] + \
                 [_s(f"media_player.dev{i}", "idle") for i in range(5_000)]
        with _at_hour(14):
            t0 = time.perf_counter()
            for _ in range(20):
                JB.classify_activity(states, {})
            dt = time.perf_counter() - t0
        self.assertLess(dt, 3.0)


class TestRetry(_Quiet):
    def test_vision_circuit_opens_after_threshold_and_stops_calling(self):
        img = TMP / "g.jpg"; img.write_bytes(b"x")
        with mock.patch.object(JB.urllib.request, "urlopen", side_effect=OSError("ollama stalled")) as uo:
            for _ in range(JB.VISION_FAIL_THRESHOLD):
                self.assertIsNone(JB.vision_describe(str(img)))
            self.assertEqual(uo.call_count, JB.VISION_FAIL_THRESHOLD)
            self.assertGreater(JB._vision_cb["open_until"], time.time())
            self.assertTrue(JB._vision_cb["logged_open"])
            self.assertIsNone(JB.vision_describe(str(img)))          # circuit open: no call made
            self.assertEqual(uo.call_count, JB.VISION_FAIL_THRESHOLD)
        self.assertIn("circuit OPEN", self._buf.getvalue())

    def test_vision_success_closes_the_circuit(self):
        img = TMP / "h.jpg"; img.write_bytes(b"x")
        JB._vision_cb.update({"fails": 2, "logged_open": True})
        with mock.patch.object(JB.urllib.request, "urlopen", return_value=_Resp({"message": {"content": "<think>hm</think> dim room"}})):
            self.assertEqual(JB.vision_describe(str(img)), "dim room")
        self.assertEqual((JB._vision_cb["fails"], JB._vision_cb["logged_open"]), (0, False))
        self.assertIn("Vision recovered", self._buf.getvalue())

    def test_ha_calls_are_one_shot_and_fail_open(self):
        # RETRY GAP: get_ha_token / ha_get_states — a single urlopen each; failures return None / []
        with mock.patch.object(JB.subprocess, "run", return_value=types.SimpleNamespace(returncode=0, stdout="rt")), \
             mock.patch.object(JB.urllib.request, "urlopen", side_effect=OSError("ha down")) as uo:
            self.assertIsNone(JB.get_ha_token())
            self.assertEqual(JB.ha_get_states(), [])
        self.assertEqual(uo.call_count, 2)
        with mock.patch.object(JB.subprocess, "run", return_value=types.SimpleNamespace(returncode=1, stdout="")):
            self.assertIsNone(JB._keychain("nova-hass-refresh-token"))
            self.assertIsNone(JB.get_ha_token())


class TestUnit(_Quiet):
    def test_classify_away_wins(self):
        with _at_hour(14):
            st, conf, sig = JB.classify_activity([_s("device_tracker.jordan_s_iphone", "not_home"), _s("light.office", "on")], {})
        self.assertEqual((st, conf, sig["jordan_home"]), ("away", 0.95, False))

    def test_classify_deep_work_meeting_entertainment_cooking(self):
        with _at_hour(14):
            self.assertEqual(JB.classify_activity([HOME, _s("light.office_desk", "on")], {"room": "office"})[0], "deep_work")
            st, _, sig = JB.classify_activity([HOME, _s("light.office_desk", "on"), _s("media_player.office_tv", "playing", app_name="Zoom")], {})
            self.assertEqual((st, sig["media_playing"]), ("meeting", {"office": "Zoom"}))
            st, _, sig = JB.classify_activity([HOME, _s("media_player.living_room_tv", "playing", app_name="Plex"), _s("switch.onkyo_tx_nr", "on")], {})
            self.assertEqual((st, sig["onkyo_on"]), ("entertainment", True))
            self.assertEqual(JB.classify_activity([HOME, _s("light.kitchen_main", "on")], {})[0], "cooking")

    def test_classify_sleeping_at_night_and_presence_signal(self):
        with _at_hour(2):
            st, conf, sig = JB.classify_activity([HOME], {"room": "master_bedroom"})
        self.assertEqual((st, sig["all_lights_off"], sig["presence_room"], sig["hour"]), ("sleeping", True, "master_bedroom", 2))
        self.assertLessEqual(conf, 0.95)

    def test_vision_unreadable_frame(self):
        self.assertIsNone(JB.vision_describe(str(TMP / "missing.jpg")))
        self.assertIn("cannot read frame", self._buf.getvalue())
        self.assertEqual(JB._vision_cb["fails"], 0)                # not counted against the breaker

    def test_log_writes_redirected_file(self):
        JB.log("ping", "WARN")
        self.assertIn("[WARN] ping", JB.LOG_FILE.read_text())


class TestIntegration(_Quiet):
    def test_phase5_writes_on_change_then_heartbeat(self):
        pool = _FakePool()
        states = [HOME, _s("light.office_desk", "on")]
        with mock.patch.object(JB.urllib.request, "urlopen", return_value=_Resp({"occupancy": {"jordan": {"room": "office"}}})), _at_hour(14):
            asyncio.run(JB.phase5_activity_classifier(pool, states))
            asyncio.run(JB.phase5_activity_classifier(pool, states))        # unchanged, fresh -> no write
            JB._last_activity_write = _FixedDT.now(timezone.utc) - timedelta(seconds=JB.ACTIVITY_HEARTBEAT_SEC + 1)
            asyncio.run(JB.phase5_activity_classifier(pool, states))        # heartbeat due
        self.assertEqual(len(pool.calls), 2)
        self.assertIn("INSERT INTO telemetry.activity", pool.calls[0][0])
        self.assertEqual(pool.calls[0][1][0], "deep_work")
        self.assertEqual(json.loads(pool.calls[0][1][3]), {"heartbeat": False})
        self.assertEqual(json.loads(pool.calls[1][1][3]), {"heartbeat": True})
        self.assertEqual(json.loads(pool.calls[0][1][2])["presence_room"], "office")
        self.assertEqual(JB._current_activity["state"], "deep_work")

    def test_phase6_patio_heat_needs_a_person_not_a_lamp(self):
        hot = [HOME, _s("sensor.outdoor", "101", device_class="temperature"), _s("light.patio_string", "on"), _s("light.kitchen", "on")]
        pool = _FakePool()
        with _at_hour(15):
            asyncio.run(JB.phase6_environmental_awareness(pool, hot))
            self.assertEqual(pool.calls, [])
            asyncio.run(JB.phase6_environmental_awareness(pool, hot + [_s("binary_sensor.patio_motion", "on")]))
        self.assertEqual(pool.calls[0][1][0], "patio_heat")
        self.assertIn("WHERE NOT EXISTS", pool.calls[0][0])          # 2h dedup lives in the SQL
        self.assertIn("Suggestion:", self._buf.getvalue())

    def test_phase6_office_late_and_lights_off_home(self):
        pool = _FakePool(result="INSERT 0 0")
        with _at_hour(23):
            asyncio.run(JB.phase6_environmental_awareness(pool, [HOME, _s("light.office_desk", "on")]))
        self.assertEqual([c[1][0] for c in pool.calls], ["office_late"])
        self.assertNotIn("Suggestion:", self._buf.getvalue())      # suppressed row -> no log line
        pool = _FakePool()
        with _at_hour(12):
            asyncio.run(JB.phase6_environmental_awareness(pool, [HOME, _s("light.office_desk", "off")]))
        self.assertEqual([c[1][0] for c in pool.calls], ["lights_off_home"])


class TestFunctional(_Quiet):
    def test_phase3_describes_fresh_frames_only(self):
        JB.FRAME_DIR.mkdir(parents=True, exist_ok=True)
        fresh = JB.FRAME_DIR / "interior_living_room_latest.jpg"; fresh.write_bytes(b"x")
        stale = JB.FRAME_DIR / "interior_kitchen_alley_latest.jpg"; stale.write_bytes(b"x")
        old = time.time() - 2000; os.utime(stale, (old, old))
        pool = _FakePool()
        with mock.patch.object(JB.urllib.request, "urlopen", return_value=_Resp({"message": {"content": "one person reading, dim"}})) as uo:
            asyncio.run(JB.phase3_visual_understanding(pool))
        self.assertEqual(uo.call_count, 1)
        self.assertEqual(pool.calls[0][1], ("interior_living_room", "one person reading, dim"))
        self.assertEqual(JB._environment["living_room"]["description"], "one person reading, dim")
        self.assertNotIn("kitchen", JB._environment)

    def test_brain_loop_skips_cycle_without_ha_and_survives_errors(self):
        async def _run():
            with mock.patch.object(JB, "get_pool", mock.AsyncMock(return_value=_FakePool())), \
                 mock.patch.object(JB, "ha_get_states", side_effect=[[], [HOME], RuntimeError("x")]), \
                 mock.patch.object(JB, "phase5_activity_classifier", mock.AsyncMock()) as p5, \
                 mock.patch.object(JB, "phase6_environmental_awareness", mock.AsyncMock(side_effect=RuntimeError("p6"))) as p6:
                n = {"i": 0}
                async def _sleep(_):
                    n["i"] += 1
                    if n["i"] >= 4:
                        JB._shutdown = True
                with mock.patch.object(JB.asyncio, "sleep", _sleep):
                    await JB.brain_loop()
                return p5.await_count, p6.await_count
        JB._shutdown = False
        try:
            p5, p6 = asyncio.run(_run())
        finally:
            JB._shutdown = False
        self.assertEqual((p5, p6), (1, 1))
        out = self._buf.getvalue()
        self.assertIn("No HA states available", out)
        self.assertIn("Brain loop error: p6", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_jarvis_brain; print(nova_jarvis_brain.VERSION)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), JB.VERSION)


if __name__ == "__main__":
    unittest.main()
