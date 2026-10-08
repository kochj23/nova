#!/usr/bin/env python3
"""7-category gap tests for nova_ha_poller.py (2026-10-08: log-once fix, gps_tracker heartbeat).

Complements tests/test_nova_ha_poller.py: HTTP retry/backoff (_urlopen_json), Keychain timeout,
the tracker state only advancing after its row lands, person mapping, poll_loop wiring and a
main() startup smoke. HA / PG / Keychain are all stubbed. Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import io
import json
import re
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("ha_poller_7cat", SCRIPTS / "nova_ha_poller.py")
ha = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ha)
ha.LOG_FILE = Path(tempfile.mkdtemp()) / "nova_ha_poller.log"      # never touch the real log
SRC = (SCRIPTS / "nova_ha_poller.py").read_text()


class _Pool:
    def __init__(self, fail_times=0):
        self.calls = []; self.fail_times = fail_times

    def acquire(self):
        pool = self

        class _Ctx:
            async def __aenter__(s): return s
            async def __aexit__(s, *a): return False

            async def execute(s, sql, *args):
                if pool.fail_times > 0:
                    pool.fail_times -= 1
                    raise RuntimeError("pg down")
                pool.calls.append((sql, args))
        return _Ctx()


class _Resp:
    def __init__(self, obj): self.obj = obj
    def read(self): return json.dumps(self.obj).encode()


def _reset(pool=None):
    ha._pool = pool
    for d in (ha._prev_light_state, ha._prev_media_state, ha._prev_motion_state, ha._prev_scene_state,
              ha._prev_tracker_state, ha._last_tracker_write):
        d.clear()
    ha._access_token = None; ha._token_expires = 0


def _q():
    return redirect_stdout(io.StringIO())


def _tracker(eid, state):
    return {"entity_id": eid, "state": state, "attributes": {"battery_level": 50, "gps_accuracy": 5}}


def _tables(pool):
    return [re.search(r"INSERT INTO ([\w.]+)", s).group(1) for s, _ in pool.calls]


class TestSecurity(unittest.TestCase):
    def test_keychain_is_argv_not_shell_and_bounded(self):
        with patch.object(ha.subprocess, "run", return_value=MagicMock(returncode=0, stdout="tok\n")) as run:
            self.assertEqual(ha._keychain("nova-hass-refresh-token"), "tok")
        args, kw = run.call_args
        self.assertIsInstance(args[0], list)
        self.assertNotIn("shell", kw)
        self.assertEqual(kw["timeout"], 15)

    def test_refresh_token_never_logged_on_failure(self):
        _reset()
        with patch.object(ha, "_keychain", return_value="SECRET-REFRESH-123"), \
                patch.object(ha.urllib.request, "urlopen", side_effect=OSError("refused")), \
                patch.object(ha.time, "sleep"), _q() as out:
            self.assertIsNone(ha.get_ha_token())
        self.assertNotIn("SECRET-REFRESH-123", out.getvalue())
        self.assertNotIn("SECRET-REFRESH-123", ha.LOG_FILE.read_text() if ha.LOG_FILE.exists() else "")

    def test_ha_is_loopback_and_dsn_passwordless(self):
        self.assertTrue(ha.HA_URL.startswith("http://127.0.0.1"))
        self.assertNotIn(":", ha.DB_DSN.split("@")[0].replace("postgresql://", ""))

    def test_unknown_trackers_are_ignored(self):
        pool = _Pool(); _reset(pool)
        with _q():
            asyncio.run(ha.write_device_tracker([_tracker("device_tracker.neighbor_phone", "home")]))
        self.assertEqual(pool.calls, [])                    # only residents are recorded


class TestPerformance(unittest.TestCase):
    def test_http_backoff_is_bounded(self):
        sleeps = []
        req = ha.urllib.request.Request("http://127.0.0.1:1/x")
        with patch.object(ha.urllib.request, "urlopen", side_effect=OSError("x")), \
                patch.object(ha.time, "sleep", sleeps.append):
            with self.assertRaises(OSError):
                ha._urlopen_json(req, 1)
        self.assertEqual(sleeps, [1.0, 2.0])

    def test_steady_trackers_write_nothing_between_heartbeats(self):
        pool = _Pool(); _reset(pool)
        states = [_tracker("device_tracker.jordan_iphone", "home")] * 1000
        with _q():
            asyncio.run(ha.write_device_tracker(states))
        self.assertEqual(len(pool.calls), 2)                # first sight: one row + one observation, not 2000


class TestRetry(unittest.TestCase):
    def test_states_recover_on_second_attempt(self):
        ha._access_token = "t"; ha._token_expires = time.time() + 100
        uo = MagicMock(side_effect=[OSError("blip"), _Resp([{"entity_id": "light.x", "state": "on"}])])
        with patch.object(ha.urllib.request, "urlopen", uo), patch.object(ha.time, "sleep"), _q():
            self.assertEqual(ha.ha_get_states()[0]["entity_id"], "light.x")
        self.assertEqual(uo.call_count, 2)

    def test_token_exchange_retries(self):
        _reset()
        uo = MagicMock(side_effect=[OSError("blip"), OSError("blip"), _Resp({"access_token": "acc"})])
        with patch.object(ha, "_keychain", return_value="r"), patch.object(ha.urllib.request, "urlopen", uo), \
                patch.object(ha.time, "sleep"):
            self.assertEqual(ha.get_ha_token(), "acc")
        self.assertEqual(uo.call_count, 3)

    def test_keychain_hang_returns_none_and_logs(self):
        with patch.object(ha.subprocess, "run", side_effect=subprocess.TimeoutExpired("security", 15)), _q() as out:
            self.assertIsNone(ha._keychain("nova-hass-refresh-token"))
        self.assertIn("timed out", out.getvalue())

    def test_failed_tracker_write_is_retried_next_poll(self):
        # 2026-10-08 fix: state used to be marked seen BEFORE the insert, so a PG blip on
        # "arrived home" lost the row (and its observation) until the next zone change.
        pool = _Pool(fail_times=1); _reset(pool)
        st = [_tracker("device_tracker.jordan_iphone", "home")]
        with _q():
            with self.assertRaises(RuntimeError):
                asyncio.run(ha.write_device_tracker(st))
            asyncio.run(ha.write_device_tracker(st))
        self.assertEqual(_tables(pool), ["telemetry.presence", "shared_observations"])
        self.assertIn("arrived home", pool.calls[1][1][0])


class TestUnit(unittest.TestCase):
    def test_person_mapping(self):
        pool = _Pool(); _reset(pool)
        with _q():
            asyncio.run(ha.write_device_tracker([_tracker("device_tracker.tricia_iphone", "not_home"),
                                                 _tracker("device_tracker.Jordan_Watch", "home")]))
        people = [a[0] for s, a in pool.calls if "telemetry.presence" in s]
        self.assertEqual(people, ["amy", "jordan"])
        amy = next(a for s, a in pool.calls if "telemetry.presence" in s and a[0] == "amy")
        self.assertEqual(amy[1], 0.0)
        self.assertEqual(json.loads(amy[2])["source"], "device_tracker.tricia_iphone")

    def test_stdout_is_logfile_false_for_other_streams(self):
        with _q():
            self.assertFalse(ha._stdout_is_logfile())

    def test_heartbeat_constant(self):
        self.assertEqual(ha.GPS_HEARTBEAT_S, 900)


class TestIntegration(unittest.TestCase):
    def test_poll_loop_one_tick_feeds_every_writer(self):
        pool = _Pool(); _reset(pool)
        states = [{"entity_id": "light.office_lamp", "state": "on", "attributes": {"brightness": 200}},
                  _tracker("device_tracker.jordan_iphone", "home")]
        ticks = []

        async def fake_sleep(s):
            ticks.append(s)
            if len(ticks) >= 2:
                ha._shutdown = True
        try:
            with patch.object(ha, "ha_get_states", return_value=states), \
                    patch.object(ha.asyncio, "sleep", fake_sleep), _q():
                asyncio.run(ha.poll_loop())
        finally:
            ha._shutdown = False
        self.assertIn("telemetry.presence", _tables(pool))
        methods = [s for s, _ in pool.calls if "gps_tracker" in s]
        self.assertEqual(len(methods), 1)


class TestFunctional(unittest.TestCase):
    def test_arrive_then_leave_golden_path(self):
        pool = _Pool(); _reset(pool)
        with _q():
            asyncio.run(ha.write_device_tracker([_tracker("device_tracker.jordan_iphone", "home")]))
            asyncio.run(ha.write_device_tracker([_tracker("device_tracker.jordan_iphone", "not_home")]))
        obs = [a[0] for s, a in pool.calls if "shared_observations" in s]
        self.assertEqual(obs, ["GPS: jordan arrived home", "GPS: jordan left home"])

    def test_ha_down_tick_writes_nothing_and_logs(self):
        pool = _Pool(); _reset(pool)
        ha._access_token = "t"; ha._token_expires = time.time() + 100
        with patch.object(ha.urllib.request, "urlopen", side_effect=OSError("refused")), \
                patch.object(ha.time, "sleep"), _q() as out:
            self.assertIsNone(ha.ha_get_states())
        self.assertIn("HA API error", out.getvalue())
        self.assertEqual(pool.calls, [])


class TestFrame(unittest.TestCase):
    def test_main_starts_and_shuts_down_cleanly(self):
        _reset()
        ha._shutdown = True
        try:
            with patch.object(ha, "get_ha_token", return_value=None), patch.object(ha.signal, "signal"), _q() as out:
                asyncio.run(ha.main())
        finally:
            ha._shutdown = False
        log = out.getvalue()
        self.assertIn("starting", log)
        self.assertIn("HA authentication failed — will retry", log)
        self.assertIn("Shutdown complete", log)


if __name__ == "__main__":
    unittest.main()
