#!/usr/bin/env python3
"""Tests for nova_embodiment.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_embodiment.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


emb = _load("embodiment_under_test", SCRIPT)
STAMP = {"host": "test", "substrate": "test"}


class _FakeDT(datetime):
    """datetime with a pinned now() so the hour/weekday under test is deterministic."""
    fixed = datetime(2026, 10, 6, 14, 0)        # a weekday afternoon

    @classmethod
    def now(cls, tz=None):
        return cls.fixed if tz is None else cls.fixed.replace(tzinfo=tz)


class _Cur:
    """Cursor stub: first matching SQL substring wins; records every statement + params."""
    def __init__(self, rules=(), raise_on=()):
        self.rules, self.raise_on = list(rules), tuple(raise_on)
        self.sql, self.params, self._last = [], [], None

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = None
        for sub in self.raise_on:
            if sub in sql:
                raise RuntimeError(f"stub failure on {sub}")
        for sub, val in self.rules:
            if sub in sql:
                self._last = val(sql, params) if callable(val) else val
                return

    def fetchone(self):
        v = self._last
        return (v[0] if v else None) if isinstance(v, list) else v

    def fetchall(self):
        v = self._last
        return [] if v is None else (v if isinstance(v, list) else [v])

    def stmts(self, sub):
        return [(s, p) for s, p in zip(self.sql, self.params) if sub in s]


def _conn(cur):
    return types.SimpleNamespace(cursor=lambda *a, **k: cur, autocommit=False, close=lambda: None)


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _baseline(means, when):
    """Per-hour history rows for the pinned (weekday-type, hour): ten samples around each mean."""
    dow, hr = when.isoweekday(), when.hour
    wobble = (-1, 0, 1, 0, -1, 1, 0, 0, 1, -1)

    def rows(sql, params):
        key = next(k for k, sub in (("occupancy", "count(DISTINCT person)"), ("indoor_activity", "room <> ALL(%s) AND coalesce"),
                                    ("lights_on", "hue_light_history"), ("av_on", "av_state"), ("power_w", "telemetry.energy"))
                   if sub in sql)
        m = means[key]
        return [(dow, hr, m + w * (0.05 * abs(m) or 0.1)) for w in wobble]
    return rows


def _world(when=_FakeDT.fixed, presence=(("jordan", "office"), ("amy", "away")), act30=10, lights=3, av=0, power=400.0,
           any_indoor=5, means=None, cooldown=None, latest=None):
    means = means or {"occupancy": 1.0, "indoor_activity": 20.0, "lights_on": 3.0, "av_on": 0.2, "power_w": 400.0}
    return _Cur([("AS dow", _baseline(means, when)),
                 ("FROM telemetry.device_owner", [("jordan",), ("amy",)]),
                 ("DISTINCT ON (person)", list(presence)),
                 ("'30 minutes' AND person = ANY", (act30,) if act30 is not None else None),
                 ("DISTINCT ON (light_id)", (lights,) if lights is not None else None),
                 ("DISTINCT ON (device_id) device_id, power", (av,) if av is not None else None),
                 ("SELECT sum(watts)", (power,) if power is not None else None),
                 ("SELECT count(*) FROM telemetry.presence WHERE ts > now() - interval '30 minutes' AND room = ANY", (any_indoor,)),
                 ("SELECT room FROM telemetry.presence", ("office",)),
                 ("FROM service_registry", (5, 5, 0, 2)),
                 ("(evidence->>'memory_written')::boolean", cooldown),
                 ("INSERT INTO embodiment_state", (3, when)),
                 ("SELECT house_state, occupancy, evidence FROM embodiment_state", latest)])


def _compute(cur, when=_FakeDT.fixed):
    _FakeDT.fixed = when
    with mock.patch.object(emb, "datetime", _FakeDT), redirect_stdout(io.StringIO()):
        return emb.compute(cur)


def _run_main(cur, argv=(), urlopen=None, when=_FakeDT.fixed):
    _FakeDT.fixed = when

    def uo_default(req, timeout=60):
        if req.full_url.endswith("/remember"):
            return _Resp({"id": 77})
        return _Resp({"message": {"content": ""}})     # LLM silent → deterministic headline
    uo = urlopen or mock.MagicMock(side_effect=uo_default)
    buf = io.StringIO()
    with mock.patch.object(emb.psycopg2, "connect", return_value=_conn(cur)), \
         mock.patch.object(sys, "argv", ["nova_embodiment.py", *argv]), \
         mock.patch.object(emb.urllib.request, "urlopen", uo), \
         mock.patch.object(emb, "lineage_stamp", lambda **kw: STAMP), \
         mock.patch.object(emb, "datetime", _FakeDT), redirect_stdout(buf):
        rc = emb.main()
    return rc, uo, buf.getvalue()


OFF = {"lights": 12, "power": 2000.0, "act30": 100}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized_and_interpolations_are_module_ints(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertIsInstance(emb.BASELINE_DAYS, int)
        self.assertIsInstance(emb.MEM_COOLDOWN_HOURS, int)
        cur = _world(); _compute(cur)
        self.assertIn((["away", "nearby"], ["amy", "jordan"]), cur.params)   # lists bound as params

    def test_proprioception_not_surveillance(self):
        # occupancy is coarse: who is home, one active zone — never a per-person room/movement log
        st = _compute(_world())
        self.assertEqual(set(st["occupancy"]), {"residents_home", "residents_away", "resident_count_home",
                                                "any_indoor_presence", "active_area", "summary"})
        self.assertEqual(st["occupancy"]["residents_home"], ["jordan"])
        writes = {m.group(1) for m in re.finditer(r"(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"embodiment_state"})
        self.assertNotIn("nova_notify", SRC)                                 # raises no alerts

    def test_llm_cannot_invent_the_state(self):
        st = _compute(_world(presence=(), any_indoor=0))
        with mock.patch.object(emb, "llm", return_value="Someone is busy in the kitchen right now."):
            head, by = emb.name_state(st, ["x"])
        self.assertEqual(by, "deterministic")
        self.assertEqual(head, "The house is empty and quiet right now.")


class TestPerformance(unittest.TestCase):
    def test_signal_scoring_fast_on_10k(self):
        t0 = time.perf_counter()
        sigs = [emb._sig("power_w", 400 + i % 50, 400.0, 20.0, 30, "W", lambda v: f"{int(v)}W") for i in range(10_000)]
        strings = emb.evidence_strings({"signals": sigs})
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(strings), 10_000)
        self.assertTrue(all(abs(s["z"]) <= 4.0 for s in sigs))


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_nodes(self):
        calls = []

        def flaky(req, timeout=45):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("node down")
            return _Resp({"message": {"content": "The house feels calm."}})
        with mock.patch.object(emb.urllib.request, "urlopen", flaky):
            self.assertEqual(emb.llm("p", "s"), "The house feels calm.")
        self.assertEqual(calls, [n + "/api/chat" for n in emb.OLLAMA_NODES[:3]])

    def test_memory_write_has_no_retry_but_main_still_stores(self):
        # RETRY GAP: remember — one urlopen attempt; main() catches the failure and stores the state anyway
        with mock.patch.object(emb.urllib.request, "urlopen", side_effect=OSError("memsrv down")) as uo:
            with self.assertRaises(OSError):
                emb.remember("t", "embodiment", {})
        self.assertEqual(uo.call_count, 1)
        cur = _world(**OFF)

        def flaky(req, timeout=60):
            if req.full_url.endswith("/remember"):
                raise OSError("memsrv down")
            return _Resp({"message": {"content": ""}})
        rc, uo, out = _run_main(cur, urlopen=mock.MagicMock(side_effect=flaky))
        self.assertEqual(rc, 0)
        self.assertIn("memory write failed (state still stored)", out)
        (_, p), = cur.stmts("INSERT INTO embodiment_state")
        self.assertEqual(p[0], "off_rhythm")
        self.assertFalse(p[4].adapted["memory_written"])


class TestUnit(unittest.TestCase):
    def test_sig_degrades_and_clamps(self):
        self.assertFalse(emb._sig("lights_on", None, 3.0, 1.0, 10, "lamps", str)["usable"])
        s = emb._sig("lights_on", 4.0, None, None, 2, "lamps", lambda v: f"{int(v)} lit")
        self.assertFalse(s["usable"]); self.assertIn("no baseline yet (2 samples)", s["note"])
        s = emb._sig("lights_on", 40.0, 3.0, 0.1, 10, "lamps", lambda v: f"{int(v)} lit")
        self.assertEqual(s["z"], 4.0)                                      # floor 1.0 applied, clamped at ±4
        self.assertIn("above normal", s["note"])
        self.assertEqual(emb.clamp(-9, -4, 4), -4)

    def test_baseline_filters_by_hour_and_daytype(self):
        rows = [(2, 14, 1.0), (2, 14, 3.0), (2, 14, 1.0), (2, 14, 3.0), (6, 14, 99.0), (2, 9, 99.0)]
        cur = _Cur([("AS dow", rows)])
        self.assertEqual(emb._baseline(cur, "SELECT 1 AS h, 1 AS val", (), False, 14), (2.0, 1.0, 4))
        self.assertEqual(emb._baseline(cur, "SELECT 1 AS h, 1 AS val", (), True, 14), (None, None, 1))
        self.assertEqual(emb._baseline(_Cur(raise_on=("AS dow",)), "x", (), False, 14), (None, None, 0))

    def test_scalar_and_residents_fail_safe(self):
        self.assertIsNone(emb._scalar(_Cur(raise_on=("SELECT",)), "SELECT 1"))
        self.assertEqual(emb._scalar(_Cur([("SELECT 1", (7,))]), "SELECT 1"), 7)
        self.assertEqual(emb._residents(_Cur(raise_on=("device_owner",))), ["jordan", "amy"])
        self.assertEqual(emb._residents(_Cur([("device_owner", [("zed",), ("amy",)])])), ["amy", "zed"])

    def test_fleet_pulse(self):
        self.assertTrue(emb.fleet_pulse(_Cur([("service_registry", (5, 5, 0, 2))]))["healthy"])
        p = emb.fleet_pulse(_Cur([("service_registry", (5, 3, 1, 2))]))
        self.assertFalse(p["healthy"]); self.assertEqual(p["note"], "2 of 5 down; 1 stale heartbeat(s)")
        self.assertIsNone(emb.fleet_pulse(_Cur(raise_on=("service_registry",)))["healthy"])

    def test_evidence_strings_rank_by_deviation(self):
        st = {"signals": [{"usable": True, "z": 0.1, "note": "a"}, {"usable": True, "z": -3, "note": "b"}, {"usable": False, "z": 0, "note": "c"}]}
        self.assertEqual(emb.evidence_strings(st), ["b", "a"])
        self.assertEqual(emb.evidence_strings(st, top=1), ["b"])
        self.assertEqual(emb.evidence_strings({"signals": [{"usable": False, "z": 0, "note": "c"}]}), ["c"])


class TestIntegration(unittest.TestCase):
    def test_states_are_deterministic_from_telemetry(self):
        self.assertEqual(_compute(_world())["house_state"], "calm")
        self.assertEqual(_compute(_world(presence=(), any_indoor=0))["house_state"], "empty")
        night = datetime(2026, 10, 6, 3, 0)
        st = _compute(_world(when=night, lights=0, act30=1, means={"occupancy": 1.0, "indoor_activity": 2.0, "lights_on": 0.0, "av_on": 0.0, "power_w": 400.0}), when=night)
        self.assertEqual(st["house_state"], "asleep")
        self.assertEqual(st["occupancy"]["summary"], "jordan is home, asleep")
        st = _compute(_world(**OFF))
        self.assertEqual((st["house_state"], st["base_state"]), ("off_rhythm", "busy"))
        self.assertGreaterEqual(st["magnitude"], emb.OFF_RHYTHM_SIGMA)

    def test_thin_telemetry_never_claims_off_rhythm(self):
        st = _compute(_world(lights=None, av=None, power=None, act30=None, presence=(("jordan", "office"),)))
        self.assertEqual(st["usable_signals"], 1)
        self.assertEqual(st["house_state"], "calm")

    def test_store_then_accessor_round_trip(self):
        cur = _world()
        st = _compute(cur)
        with mock.patch.object(emb, "lineage_stamp", lambda **kw: STAMP):
            row = emb.store(cur, st, {"healthy": True}, "The house feels calm.", "deterministic", False)
        self.assertEqual(row, (3, _FakeDT.fixed))
        (sql, p), = cur.stmts("INSERT INTO embodiment_state")
        self.assertEqual(p[0], "calm")
        self.assertEqual(p[4].adapted["headline"], "The house feels calm.")
        self.assertIs(p[5].adapted, STAMP)
        latest = (p[0], p[1].adapted, p[4].adapted)
        with mock.patch.object(emb.psycopg2, "connect", return_value=_conn(_Cur([("FROM embodiment_state", latest)]))):
            line = emb.current_embodiment()
        self.assertTrue(line.startswith("The house right now: calm — jordan is home; "))
        with mock.patch.object(emb.psycopg2, "connect", side_effect=OSError("pg down")):
            self.assertEqual(emb.current_embodiment(), "")


class TestFunctional(unittest.TestCase):
    def test_off_rhythm_golden_path_writes_memory_and_state(self):
        latest = ("off_rhythm", {"summary": "jordan is home"}, {"summary": ["lights_on: 12 lit"], "headline": "h"})
        cur = _world(latest=latest, **OFF)
        rc, uo, out = _run_main(cur)
        self.assertEqual(rc, 0)
        mem = [c for c in uo.call_args_list if c[0][0].full_url.endswith("/remember")]
        self.assertEqual(len(mem), 1)
        body = json.loads(mem[0][0][0].data)
        self.assertTrue(body["text"].startswith("[Embodiment] The house feels off its usual rhythm right now — busier"))
        self.assertEqual(body["metadata"]["house_state"], "off_rhythm")
        (_, p), = cur.stmts("INSERT INTO embodiment_state")
        self.assertEqual(p[0], "off_rhythm")
        self.assertTrue(p[4].adapted["memory_written"])
        self.assertEqual(p[4].adapted["labelled_by"], "deterministic")
        self.assertIn("HOUSE_STATE     = 'off_rhythm'", out)
        self.assertIn("Accessor preview → The house right now: off_rhythm — jordan is home; lights_on: 12 lit", out)

    def test_calm_house_is_never_a_memory(self):
        cur = _world()
        rc, uo, out = _run_main(cur)
        self.assertEqual(rc, 0)
        self.assertFalse([c for c in uo.call_args_list if c[0][0].full_url.endswith("/remember")])
        self.assertEqual(cur.stmts("INSERT INTO embodiment_state")[0][1][0], "calm")

    def test_cooldown_and_no_write_flag(self):
        cur = _world(cooldown=(1,), **OFF)
        rc, uo, out = _run_main(cur)
        self.assertFalse([c for c in uo.call_args_list if c[0][0].full_url.endswith("/remember")])
        self.assertIn("within memory cooldown", out)
        cur = _world(**OFF)
        rc, uo, out = _run_main(cur, ["--no-write"])
        self.assertFalse([c for c in uo.call_args_list if c[0][0].full_url.endswith("/remember")])
        self.assertTrue(cur.stmts("INSERT INTO embodiment_state"))

    def test_demo_degraded_stores_nothing(self):
        cur = _world()
        rc, uo, out = _run_main(cur, ["--demo-degraded"])
        self.assertEqual(rc, 0)
        self.assertFalse(cur.stmts("INSERT INTO embodiment_state"))
        self.assertIn("SIMULATED source absent", out)
        uo.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        r = subprocess.run([sys.executable, "-c", "import nova_embodiment"], cwd=SCRIPTS,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        self.assertRegex(SRC, r'if __name__ == "__main__":\n\s+sys\.exit\(main\(\)\)')
        with mock.patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            _load("embodiment_frame_probe", SCRIPT)


if __name__ == "__main__":
    unittest.main()
