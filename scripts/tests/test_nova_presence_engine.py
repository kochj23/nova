#!/usr/bin/env python3
"""Tests for nova_presence_engine.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The asyncpg pool is a pure-Python fake installed as the module's _pool (asyncpg itself is imported
normally, never swapped in sys.modules); LOG_FILE points at a tempdir; main() (which binds port 37465)
is never run."""
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
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_presence_engine.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))
TMP = tempfile.TemporaryDirectory()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pe = _load("nova_presence_engine_t", SCRIPT)
pe.LOG_FILE = Path(TMP.name) / "presence.log"
NOW = lambda: datetime.now(timezone.utc)  # noqa: E731  (computed at call time, never at import)


class FakeConn:
    def __init__(self, pool):
        self.pool = pool

    async def fetch(self, sql, *args):
        self.pool.fetches.append(sql)
        if self.pool.fail:
            raise ConnectionError("pg down")
        for needle, rows in self.pool.answers.items():
            if needle in sql:
                return rows
        return []

    async def execute(self, sql, *args):
        self.pool.executed.append((sql, args))


class _Acq:
    def __init__(self, pool): self.pool = pool
    async def __aenter__(self): return FakeConn(self.pool)
    async def __aexit__(self, *a): return False


class FakePool:
    def __init__(self, answers=None, fail=False):
        self.answers, self.fail, self.fetches, self.executed = answers or {}, fail, [], []

    def acquire(self): return _Acq(self)


def _answers(mmwave=(), camera=(), ble=(), motion=(), wifi=(), vehicle=()):
    return {"method = 'mmwave'": list(mmwave), "method = 'camera_vision'": list(camera),
            "method != 'mmwave'": list(ble), "telemetry.climate": list(motion),
            "telemetry.network": list(wifi), "method = 'vehicle_vision'": list(vehicle)}


class _Base(unittest.TestCase):
    def setUp(self):
        self.pool = FakePool(_answers())
        for p in (patch.object(pe, "_pool", self.pool), patch.dict(pe._home_state, clear=True),
                  patch.dict(pe._last_transition, clear=True), patch.dict(pe._occupancy, clear=True)):
            p.start()
            self.addCleanup(p.stop)
        self.out = io.StringIO()
        r = redirect_stdout(self.out)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)

    def occ(self, **kw):
        self.pool.answers = _answers(**kw)
        return asyncio.run(pe.compute_occupancy())["jordan"]


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(pe.DB_DSN, r"//[^@/]+:[^@/]+@")   # no password in the DSN

    def test_writes_are_parameterized(self):
        asyncio.run(pe.persist_presence_state({"o'neil": {"home": True, "room": "office", "confidence": 0.9}}))
        sql, args = self.pool.executed[0]
        self.assertIn("$1", sql)
        self.assertNotIn("o'neil", sql)
        self.assertEqual(args[0], "o'neil")


class TestPerformance(_Base):
    def test_fusion_over_large_signal_sets_fast(self):
        ts = NOW()
        motion = [{"room": f"hue_room{i}_motion_sensor_1", "ts": ts} for i in range(10_000)]
        wifi = [{"client_name": f"dev-{i}"} for i in range(10_000)]
        t0 = time.perf_counter()
        st = self.occ(motion=motion, wifi=wifi)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertFalse(st["home"])


class TestRetry(_Base):
    def test_loop_survives_db_failure(self):
        # RETRY GAP: presence_loop/compute_occupancy — no per-call retry; a PG error is logged and the next
        # POLL_INTERVAL tick is the retry. Prove one failing tick neither raises nor writes.
        self.pool.fail = True
        ticks = {"n": 0}

        async def fake_sleep(s):
            ticks["n"] += 1
            if ticks["n"] >= 2:
                pe._shutdown = True
        with patch.object(pe.asyncio, "sleep", fake_sleep), patch.object(pe, "_shutdown", False):
            asyncio.run(pe.presence_loop())
        self.assertIn("Presence loop error: pg down", self.out.getvalue())
        self.assertEqual(self.pool.executed, [])


class TestUnit(_Base):
    def test_no_signals_is_away_unknown(self):
        st = self.occ()
        self.assertEqual((st["room"], st["confidence"], st["home"]), ("unknown", 0, False))

    def test_single_mmwave_room_locates_person(self):
        st = self.occ(mmwave=[{"room": "office", "confidence": 1.0, "ts": NOW(), "metadata": None}],
                      camera=[{"room": "office", "confidence": 0.9, "ts": NOW()}],
                      motion=[{"room": "hue_office_motion_sensor_1", "ts": NOW()}])
        self.assertEqual(st["room"], "office")
        self.assertTrue(st["home"])
        self.assertEqual(set(st["signals"]), {"mmwave", "camera_vision", "hue_motion"})

    def test_ambiguous_mmwave_does_not_guess(self):
        st = self.occ(mmwave=[{"room": r, "confidence": 0.9, "ts": NOW(), "metadata": None} for r in ("office", "kitchen")])
        self.assertEqual(st["room"], "unknown")

    def test_weak_confidence_hides_room(self):
        st = self.occ(ble=[{"person": "jordan", "room": "garage", "confidence": 0.2, "ts": NOW()}])
        self.assertEqual(st["room"], "unknown")      # 0.2*0.22/1.08 < 0.3
        self.assertTrue(st["home"])                   # but BLE still says home


class TestIntegration(_Base):
    def test_reads_expected_tables_and_wifi_hostname(self):
        st = self.occ(wifi=[{"client_name": "Jordans-iPhone"}, {"client_name": None}])
        self.assertTrue(st["signals"]["wifi_home"])
        joined = "\n".join(self.pool.fetches)
        for table in ("telemetry.presence", "telemetry.climate", "telemetry.network"):
            self.assertIn(table, joined)

    def test_persist_maps_away_and_home(self):
        asyncio.run(pe.persist_presence_state({"a": {"home": False, "room": "office", "confidence": 0.1},
                                               "b": {"home": True, "room": "unknown", "confidence": 0.4}}))
        self.assertEqual([a[1] for _, a in self.pool.executed], ["away", "home"])


class TestFunctional(_Base):
    def test_arrival_writes_one_observation_with_dedup(self):
        pe._home_state["jordan"] = {"home": False, "room": "unknown", "since": NOW()}
        state = {"jordan": {"home": True, "room": "kitchen", "confidence": 0.8}}
        asyncio.run(pe.check_transitions(state))
        pe._home_state["jordan"]["home"] = False
        asyncio.run(pe.check_transitions(state))          # within 30 min: deduped
        self.assertEqual(len(self.pool.executed), 1)
        sql, args = self.pool.executed[0]
        self.assertIn("person_arrived", sql)
        self.assertIn("detected in kitchen", args[0])

    def test_departure_logged_only_after_real_home_stay(self):
        # Regression (fixed 2026-10-06): the >=1-minute check was inverted, so real departures were dropped
        # and only sub-minute flicker was logged.
        away = {"jordan": {"home": False, "room": "unknown", "confidence": 0.0}}
        pe._home_state["jordan"] = {"home": True, "room": "office", "since": NOW() - timedelta(minutes=2)}
        asyncio.run(pe.check_transitions(away))
        self.assertEqual(len(self.pool.executed), 1)
        sql, args = self.pool.executed[0]
        self.assertIn("person_left", sql)
        self.assertIn("last seen in office", args[0])

        self.pool.executed.clear(); pe._last_transition.clear()
        pe._home_state["jordan"] = {"home": True, "room": "office", "since": NOW() - timedelta(seconds=10)}
        asyncio.run(pe.check_transitions(away))
        self.assertEqual(self.pool.executed, [])            # sub-minute flicker ignored
        self.assertFalse(pe._home_state["jordan"]["home"])

    def test_http_handlers(self):
        pe._occupancy["jordan"] = {"room": "office", "confidence": 0.7}
        pe._home_state["jordan"] = {"home": True, "room": "office"}

        class Req:
            match_info = {"room": "office"}
        room = json.loads(asyncio.run(pe.handle_room(Req())).text)
        self.assertEqual((room["occupied"], room["people"][0]["person"]), (True, "jordan"))
        occ = json.loads(asyncio.run(pe.handle_occupancy(None)).text)
        self.assertTrue(occ["home_state"]["jordan"]["home"])
        self.assertTrue(json.loads(asyncio.run(pe.handle_health(None)).text)["ok"])


class TestFrame(unittest.TestCase):
    def test_import_never_binds_or_runs(self):
        code = ("import importlib.util as u;"
                f"s=u.spec_from_file_location('p', {str(SCRIPT)!r}); m=u.module_from_spec(s);"
                "s.loader.exec_module(m); print(m.HTTP_PORT, m._pool)")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "37465 None")
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
