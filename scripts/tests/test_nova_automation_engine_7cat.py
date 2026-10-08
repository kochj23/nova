#!/usr/bin/env python3
"""nova_automation_engine.py — 7-category gap tests (Security, Performance, Retry, Unit, Integration,
Functional, Frame) for the Proteus-guarded actuation path, the fail-safe presence lights and the
retry/backoff on every external call. Written by Jordan Koch (via Claude).

Nothing in the house is ever switched: urllib urlopen and subprocess.run are mocked, the guard's
block reporter (restraint_ledger + Slack) is mocked, and the kill switch is patched explicitly."""
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_automation_engine.py"
TMP = Path(tempfile.mkdtemp(prefix="automation-7cat-"))


def _load():
    spec = importlib.util.spec_from_file_location("automation_engine_7cat", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(Path, "home", classmethod(lambda c: TMP)):
        spec.loader.exec_module(mod)
    mod.LOG_FILE = TMP / "automation.log"
    mod._report_block = MagicMock()          # never write restraint_ledger / post Slack
    return mod


class _Conn:
    def __init__(self, fetchrow=None, fetchval=None):
        self._fr, self._fv, self.calls = fetchrow, fetchval, []

    async def fetchrow(self, *a): self.calls.append(a); return self._fr
    async def fetchval(self, *a): self.calls.append(a); return self._fv


class _Pool:
    def __init__(self, conn): self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self): return conn
            async def __aexit__(self, *a): return False
        return _Ctx()


class _Base(unittest.TestCase):
    def setUp(self):
        self.ae = _load()
        self.ps = [patch.object(self.ae._guards, "kill_engaged", return_value=False),
                   patch.object(self.ae.time, "sleep"),
                   patch.object(urllib.request, "urlopen"),
                   patch.object(self.ae.subprocess, "run")]
        _, self.sleep, self.urlopen, self.run = [p.start() for p in self.ps]
        self.urlopen.return_value.read.return_value = b"{}"
        self.run.return_value = subprocess.CompletedProcess([], 0, "", "")

    def tearDown(self):
        for p in self.ps:
            p.stop()


# ── Security ────────────────────────────────────────────────────────────────────
class TestSecurity(_Base):
    def test_guards_missing_fails_closed(self):
        self.ae._guards = None
        self.assertFalse(self.ae._actuation_ok("hue light 3 on"))
        self.assertFalse(self.ae._hk_power("Bug Zapper", True))
        self.urlopen.assert_not_called()

    def test_lock_and_garage_power_refused_before_any_http(self):
        for name in ("Front Door Lock", "Garage Door Opener"):
            self.assertFalse(self.ae._hk_power(name, True))
        self.urlopen.assert_not_called()
        self.assertEqual(self.ae._report_block.call_count, 2)

    def test_securing_scene_refused_via_trigger(self):
        req = MagicMock()
        req.json = AsyncMock(return_value={"scene": "Leave Home"})
        resp = asyncio.run(self.ae.handle_trigger(req))
        self.assertFalse(json.loads(resp.body)["ok"])
        self.run.assert_not_called()

    def test_device_name_cannot_inject_query_params(self):
        self.ae._hk_power("Lamp&on=true", False)
        url = self.urlopen.call_args[0][0]
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        self.assertEqual(q["name"], ["Lamp&on=true"])
        self.assertEqual(q["on"], ["false"])

    def test_scene_runs_as_argv_not_shell(self):
        asyncio.run(self.ae.run_scene("movie_mode; rm -rf /"))   # unknown, non-securing name -> allowed
        argv = self.run.call_args[0][0]
        self.assertIsInstance(argv, list)
        self.assertEqual(argv[-1], "movie_mode; rm -rf /")
        self.assertNotIn("shell", self.run.call_args.kwargs)


# ── Performance ─────────────────────────────────────────────────────────────────
class TestPerformance(_Base):
    def test_backoff_total_is_bounded(self):
        with self.assertRaises(OSError):
            self.ae._with_retry(MagicMock(side_effect=OSError("x")), "t")
        delays = [c[0][0] for c in self.sleep.call_args_list]
        self.assertEqual(len(delays), self.ae.RETRY_ATTEMPTS - 1)
        self.assertLessEqual(sum(delays), 2.0)

    def test_guard_refusal_reported_once_per_day_not_per_tick(self):
        for _ in range(500):
            self.ae._actuation_ok("power Front Door Lock on")
        self.assertEqual(self.ae._report_block.call_count, 1)

    def test_1000_guard_checks_fast(self):
        t0 = time.perf_counter()
        for i in range(1000):
            self.ae._actuation_ok(f"hue light {i % 50} on")
        self.assertLess(time.perf_counter() - t0, 2.0)


# ── Retry ───────────────────────────────────────────────────────────────────────
class TestRetry(_Base):
    def test_hue_retries_then_succeeds(self):
        self.urlopen.side_effect = [OSError("blip"), MagicMock()]
        self.assertTrue(asyncio.run(self.ae.hue_set_light(3, True, 100)))
        self.assertEqual(self.urlopen.call_count, 2)

    def test_hue_gives_up_after_three_and_logs(self):
        self.urlopen.side_effect = OSError("down")
        with patch.object(self.ae, "log") as lg:
            self.assertFalse(asyncio.run(self.ae.hue_set_light(3, False)))
        self.assertEqual(self.urlopen.call_count, 3)
        self.assertTrue(any("after 3 attempts" in c[0][0] for c in lg.call_args_list))

    def test_hk_power_exponential_backoff(self):
        self.urlopen.side_effect = OSError("down")
        self.assertFalse(self.ae._hk_power("Bug Zapper", True))
        self.assertEqual([c[0][0] for c in self.sleep.call_args_list], [0.5, 1.0])

    def test_scene_subprocess_timeout_retried(self):
        self.run.side_effect = [subprocess.TimeoutExpired("x", 15), subprocess.CompletedProcess([], 0)]
        self.assertTrue(asyncio.run(self.ae.run_scene("movie_mode")))
        self.assertEqual(self.run.call_count, 2)

    def test_scene_nonzero_rc_not_retried(self):
        self.run.return_value = subprocess.CompletedProcess([], 1)   # a refusal is final, not retried
        self.assertFalse(asyncio.run(self.ae.run_scene("movie_mode")))
        self.assertEqual(self.run.call_count, 1)

    def test_presence_fetch_recovers_on_second_attempt(self):
        good = MagicMock()
        good.read.return_value = json.dumps({"ok": True, "occupancy": {"jordan": {"home": False}}}).encode()
        self.urlopen.side_effect = [OSError("blip"), good]
        asyncio.run(self.ae.rule_presence_lights())
        self.assertEqual(self.urlopen.call_count, 2)


# ── Unit ────────────────────────────────────────────────────────────────────────
class TestUnit(_Base):
    def test_with_retry_first_success_no_sleep(self):
        self.assertEqual(self.ae._with_retry(lambda: 42, "t"), 42)
        self.sleep.assert_not_called()

    def test_with_retry_reraises_last_error(self):
        errs = [OSError("a"), OSError("b"), ValueError("last")]
        with self.assertRaises(ValueError):
            self.ae._with_retry(MagicMock(side_effect=errs), "t")

    def test_kill_switch_blocks_every_actuation(self):
        with patch.object(self.ae._guards, "kill_engaged", return_value=True):
            self.assertFalse(self.ae._actuation_ok("hue light 3 on"))
            self.assertFalse(self.ae._actuation_ok("scene movie_mode", scene="movie_mode"))
        self.ae._report_block.assert_not_called()      # kill switch is a hold, not a guard block

    def test_fail_safe_constants(self):
        self.assertGreater(self.ae.SOURCE_ALIVE_S, self.ae.PRESENCE_OFF_DELAY_S)
        self.assertGreater(self.ae.PRESENCE_OFF_DELAY_S, self.ae.PRESENCE_FRESH_S)


# ── Integration ─────────────────────────────────────────────────────────────────
class TestIntegration(_Base):
    def _zone_run(self, last_seen_age, alive):
        ae = self.ae
        ae.PRESENCE_DEVICE_ZONES = {"patio": {"devices": ["Bug Zapper"], "source": "fp300"}}
        ae._zone_on["patio"] = True
        conn = _Conn(fetchrow={"ep": time.time() - last_seen_age}, fetchval=alive)
        ae.get_pool = AsyncMock(return_value=_Pool(conn))
        asyncio.run(ae.rule_presence_devices())
        return conn

    def test_dead_sensor_holds_lights_through_real_guard_path(self):
        self._zone_run(3600, None)
        self.urlopen.assert_not_called()
        self.assertTrue(self.ae._zone_on["patio"])

    def test_live_sensor_vacancy_powers_off_through_guard_and_http(self):
        self._zone_run(3600, time.time() - 60)
        url = self.urlopen.call_args[0][0]
        self.assertIn("name=Bug%20Zapper", url)
        self.assertIn("on=false", url)
        self.assertFalse(self.ae._zone_on["patio"])

    def test_kill_switch_holds_zone_on(self):
        with patch.object(self.ae._guards, "kill_engaged", return_value=True):
            self._zone_run(3600, time.time() - 60)
        self.urlopen.assert_not_called()


# ── Functional ──────────────────────────────────────────────────────────────────
class TestFunctional(_Base):
    def _trigger(self, body):
        req = MagicMock()
        req.json = AsyncMock(return_value=body) if body is not None else AsyncMock(side_effect=ValueError)
        resp = asyncio.run(self.ae.handle_trigger(req))
        return resp.status, json.loads(resp.body)

    def test_trigger_golden_path(self):
        status, out = self._trigger({"scene": "movie_mode"})
        self.assertEqual((status, out), (200, {"ok": True, "scene": "movie_mode"}))
        self.assertIn("nova_home_control.py", " ".join(self.run.call_args[0][0]))

    def test_trigger_error_paths(self):
        self.assertEqual(self._trigger(None)[0], 400)
        self.assertEqual(self._trigger({})[0], 400)
        self.run.side_effect = OSError("python gone")
        self.assertEqual(self._trigger({"scene": "movie_mode"}), (200, {"ok": False, "scene": "movie_mode"}))
        self.assertEqual(self.run.call_count, 3)

    def test_health_and_rules_endpoints(self):
        h = json.loads(asyncio.run(self.ae.handle_health(MagicMock())).body)
        self.assertTrue(h["ok"])
        r = json.loads(asyncio.run(self.ae.handle_rules(MagicMock())).body)
        self.assertIn("presence_lights", r["rules"])


# ── Frame ───────────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_imports_in_fresh_interpreter_without_starting(self):
        env = dict(os.environ, HOME=str(TMP))
        code = (f"import importlib.util,sys; sys.path.insert(0,{str(SCRIPTS)!r});"
                f"s=importlib.util.spec_from_file_location('ae',{str(SCRIPT)!r});"
                "m=importlib.util.module_from_spec(s); s.loader.exec_module(m);"
                "assert callable(m.main) and m._with_retry and m.RETRY_ATTEMPTS==3; print('ok')")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("ok", r.stdout)

    def test_compiles(self):
        import py_compile
        py_compile.compile(str(SCRIPT), cfile=str(TMP / "ae.pyc"), doraise=True)


if __name__ == "__main__":
    unittest.main()
