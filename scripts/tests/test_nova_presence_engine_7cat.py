#!/usr/bin/env python3
"""7-category tests for nova_presence_engine.py (2026-10-08 changes: ble_rssi-only filter, noisy-OR
additive confidence, rooms, Amy as a resident, degraded feeds, wifi_rssi/gps, PG retry with backoff).
Complements test_nova_presence_engine.py. PG is a pure-Python fake; asyncio.sleep is patched; the HTTP
server is never bound. Written by Jordan Koch (via Claude)."""
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
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_presence_engine.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))
TMP = tempfile.TemporaryDirectory()

_spec = importlib.util.spec_from_file_location("nova_presence_engine_7cat", SCRIPT)
pe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pe)
pe.LOG_FILE = Path(TMP.name) / "presence.log"


class FakePool:
    def __init__(self, answers=None, fail_times=0):
        self.answers, self.fail_times, self.fetches, self.executed = answers or {}, fail_times, [], []

    def acquire(self):
        pool = self

        class Conn:
            async def fetch(self, sql, *args):
                pool.fetches.append((sql, args))
                if pool.fail_times:
                    pool.fail_times -= 1
                    raise ConnectionError("pg down")
                for needle, rows in pool.answers.items():
                    if needle in sql:
                        return rows
                return []

            async def execute(self, sql, *args):
                if pool.fail_times:
                    pool.fail_times -= 1
                    raise ConnectionError("pg down")
                pool.executed.append((sql, args))

        class Acq:
            async def __aenter__(self): return Conn()
            async def __aexit__(self, *a): return False
        return Acq()


def answers(stale=(), **kw):
    health = [{"method": f, "age_min": 999.0 if f in stale else 1.0} for f in pe.FEED_MAX_AGE_MIN]
    out = {"GROUP BY method": health}
    keys = {"mmwave": "method = 'mmwave'", "camera": "method = 'camera_vision'", "ble": "method = 'ble_rssi'",
            "motion": "telemetry.climate", "wifi": "telemetry.network", "vehicle": "method = 'vehicle_vision'",
            "wifi_rssi": "method = 'wifi_rssi'", "gps": "method = 'gps_tracker'"}
    for k, needle in keys.items():
        out[needle] = list(kw.get(k, ()))
    return out


TS = datetime.now(timezone.utc)


class _Base(unittest.TestCase):
    def setUp(self):
        self.pool = FakePool(answers())
        for p in (patch.object(pe, "_pool", self.pool), patch.dict(pe._home_state, clear=True),
                  patch.dict(pe._last_transition, clear=True), patch.dict(pe._occupancy, clear=True)):
            p.start()
            self.addCleanup(p.stop)
        self.out = io.StringIO()
        r = redirect_stdout(self.out)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)

    def occ(self, **kw):
        self.pool.answers = answers(**kw)
        return asyncio.run(pe.compute_occupancy())


# ── Security ─────────────────────────────────────────────────────────────────
class TestSecurity(_Base):
    def test_presence_never_leaves_local(self):
        # PII boundary: no outbound HTTP/Slack/cloud client in the engine — only PG and its own local API.
        for bad in ("requests", "urllib.request", "slack.com", "https://", "notify("):
            self.assertNotIn(bad, SRC)

    def test_room_path_is_reflected_as_data_only(self):
        pe._occupancy["jordan"] = {"room": "office", "confidence": 0.9}

        class Req:
            match_info = {"room": "office' OR '1'='1"}
        body = json.loads(asyncio.run(pe.handle_room(Req())).text)
        self.assertFalse(body["occupied"])
        self.assertEqual(self.pool.fetches + self.pool.executed, [])   # /room never touches PG

    def test_feed_health_passes_methods_as_bound_parameter(self):
        asyncio.run(pe.get_feed_health())
        sql, args = self.pool.fetches[0]
        self.assertIn("$1::text[]", sql)
        self.assertEqual(sorted(args[0]), sorted(pe.FEED_MAX_AGE_MIN))

    def test_garbage_confidence_is_clamped(self):
        self.assertLessEqual(pe._noisy_or([("ble_rssi", 7.0), ("mmwave", "1.0")]), 1.0)
        self.assertEqual(pe._noisy_or([("ble_rssi", -3), ("wifi_rssi", None)]), 0.0)


# ── Performance ──────────────────────────────────────────────────────────────
class TestPerformance(_Base):
    def test_noisy_or_large_evidence_fast(self):
        ev = [("ble_rssi", 0.5)] * 100_000
        t0 = time.perf_counter()
        self.assertAlmostEqual(pe._noisy_or(ev), 1.0, places=6)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_one_fetch_per_feed_no_n_plus_one(self):
        rows = [{"person": f"p{i}", "room": "office", "confidence": 0.9, "ts": TS} for i in range(5000)]
        self.occ(ble=rows, wifi_rssi=rows)
        self.assertEqual(len(self.pool.fetches), 9)   # health + 8 feeds, independent of row/person count


# ── Retry ────────────────────────────────────────────────────────────────────
class TestRetry(_Base):
    def _run(self, coro_fn, *args):
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)
        with patch.object(pe.asyncio, "sleep", fake_sleep):
            return asyncio.run(pe._retry(coro_fn, "step", *args)), sleeps

    def test_transient_pg_failure_retried_with_backoff(self):
        self.pool.fail_times = 2                 # health query fails twice, then succeeds
        result, sleeps = self._run(pe.compute_occupancy)
        self.assertIn("jordan", result)
        self.assertEqual(sleeps, [1.0, 2.0])
        self.assertEqual(self.out.getvalue().count("[WARN] step failed"), 2)   # never silent

    def test_gives_up_after_three_attempts_and_raises(self):
        self.pool.fail_times = 99
        with self.assertRaises(ConnectionError):
            self._run(pe.compute_occupancy)
        self.assertEqual(len(self.pool.fetches), pe.RETRY_ATTEMPTS)

    def test_persist_is_retried(self):
        self.pool.fail_times = 1
        _, sleeps = self._run(pe.persist_presence_state, {"amy": {"home": True, "room": "kitchen", "confidence": 0.8}})
        self.assertEqual(len(self.pool.executed), 1)
        self.assertEqual(sleeps, [1.0])


# ── Unit ─────────────────────────────────────────────────────────────────────
class TestUnit(_Base):
    def test_noisy_or_is_additive(self):
        one = pe._noisy_or([("ble_rssi", 1.0)])
        two = pe._noisy_or([("ble_rssi", 1.0), ("wifi_rssi", 1.0)])
        self.assertAlmostEqual(one, 0.70)
        self.assertAlmostEqual(two, 1 - 0.3 * 0.25)
        self.assertGreater(two, one)

    def test_degraded_feeds_missing_or_old(self):
        h = {f: 1.0 for f in pe.FEED_MAX_AGE_MIN}
        h["mmwave"], h["gps_tracker"] = None, 46.0
        self.assertEqual(pe.degraded_feeds(h), ["gps_tracker", "mmwave"])
        self.assertEqual(pe.degraded_feeds({f: float(m) for f, m in pe.FEED_MAX_AGE_MIN.items()}), [])

    def test_amy_is_a_resident(self):
        self.assertIn("amy", pe.PERSON_DEVICES)
        self.assertEqual(pe.PERSON_DEVICES["amy"]["phone_hostname"], "Amys-iPhone")

    def test_wifi_rssi_names_room(self):
        st = self.occ(wifi_rssi=[{"person": "jordan", "room": "living_room", "confidence": 0.9, "ts": TS}])["jordan"]
        self.assertEqual((st["home"], st["room"]), (True, "living_room"))

    def test_strongest_identity_room_wins(self):
        st = self.occ(ble=[{"person": "jordan", "room": "office", "confidence": 0.5, "ts": TS}],
                      wifi_rssi=[{"person": "jordan", "room": "kitchen", "confidence": 0.9, "ts": TS}])["jordan"]
        self.assertEqual(st["room"], "kitchen")

    def test_ble_query_filters_ble_rssi_only(self):
        asyncio.run(pe.get_ble_presence())
        sql = self.pool.fetches[0][0]
        self.assertIn("method = 'ble_rssi'", sql)
        self.assertNotIn("!=", sql)

    def test_live_wifi_room_outvotes_gps_not_home(self):
        st = self.occ(gps=[{"person": "jordan", "confidence": 0.0, "ts": TS}],
                      wifi_rssi=[{"person": "jordan", "room": "office", "confidence": 0.9, "ts": TS}])["jordan"]
        self.assertTrue(st["home"])
        self.assertEqual(st["room"], "office")


# ── Integration ──────────────────────────────────────────────────────────────
class TestIntegration(_Base):
    def test_every_resident_gets_a_presence_state_row(self):
        occ = self.occ(wifi=[{"client_name": "Amys-iPhone"}])
        self.assertTrue(occ["amy"]["home"])
        self.assertFalse(occ["jordan"]["home"])
        asyncio.run(pe.persist_presence_state(occ))
        rows = {a[0]: a[1] for _, a in self.pool.executed}
        self.assertEqual(rows, {"jordan": "away", "amy": "home"})

    def test_degraded_feed_is_not_queried_and_listed_in_detail(self):
        occ = self.occ(stale=("ble_rssi", "wifi_rssi"),
                       ble=[{"person": "jordan", "room": "office", "confidence": 1.0, "ts": TS}])
        self.assertFalse(any("method = 'ble_rssi'" in s for s, _ in self.pool.fetches))
        self.assertFalse(occ["jordan"]["home"])
        asyncio.run(pe.persist_presence_state(occ))
        detail = json.loads(self.pool.executed[0][1][4])
        self.assertEqual(detail["degraded_feeds"], ["ble_rssi", "wifi_rssi"])

    def test_only_presence_state_and_observations_are_written(self):
        pe._home_state["jordan"] = {"home": False, "room": "unknown", "since": TS}
        occ = self.occ(gps=[{"person": "jordan", "confidence": 0.99, "ts": TS}])
        asyncio.run(pe.check_transitions(occ))
        asyncio.run(pe.persist_presence_state(occ))
        self.assertTrue(self.pool.executed)
        for sql, _ in self.pool.executed:
            self.assertRegex(sql, r"INSERT INTO (presence_state|shared_observations)")


# ── Functional ───────────────────────────────────────────────────────────────
class TestFunctional(_Base):
    def _loop(self):
        async def fake_sleep(s):
            if s == pe.POLL_INTERVAL:
                pe._shutdown = True
        with patch.object(pe.asyncio, "sleep", fake_sleep), patch.object(pe, "_shutdown", False):
            asyncio.run(pe.presence_loop())

    def test_golden_tick_end_to_end(self):
        self.pool.answers = answers(
            ble=[{"person": "jordan", "room": "office", "confidence": 0.9, "ts": TS}],
            wifi_rssi=[{"person": "jordan", "room": "office", "confidence": 0.9, "ts": TS}],
            gps=[{"person": "jordan", "confidence": 0.99, "ts": TS}],
            mmwave=[{"room": "office", "confidence": 0.95, "ts": TS, "metadata": {}}],
            wifi=[{"client_name": "Jordans-iPhone"}])
        self._loop()
        j = pe._occupancy["jordan"]
        self.assertEqual(j["room"], "office")
        self.assertGreaterEqual(j["confidence"], 0.99)
        rows = {a[0]: a[1] for s, a in self.pool.executed if "presence_state" in s}
        self.assertEqual(rows, {"jordan": "office", "amy": "away"})
        health = json.loads(asyncio.run(pe.handle_health(None)).text)
        self.assertEqual(health["degraded_feeds"], [])

    def test_error_tick_retries_then_logs_and_writes_nothing(self):
        self.pool.fail_times = 99
        self._loop()
        out = self.out.getvalue()
        self.assertEqual(out.count("compute_occupancy failed"), 2)
        self.assertIn("Presence loop error: pg down", out)
        self.assertEqual(self.pool.executed, [])


# ── Frame ────────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_imports_and_exposes_entrypoints(self):
        code = ("import importlib.util as u;"
                f"s=u.spec_from_file_location('p', {str(SCRIPT)!r}); m=u.module_from_spec(s);"
                "s.loader.exec_module(m);"
                "assert callable(m.main) and callable(m.presence_loop) and callable(m._retry);"
                "print(sorted(m.PERSON_DEVICES))")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "HOME": TMP.name})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "['amy', 'jordan']")

    def test_compiles_and_guards_main(self):
        compile(SRC, str(SCRIPT), "exec")
        self.assertTrue(re.search(r'if __name__ == "__main__":\s+asyncio.run\(main\(\)\)', SRC))


if __name__ == "__main__":
    unittest.main()
