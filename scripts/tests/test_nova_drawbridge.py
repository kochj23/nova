#!/usr/bin/env python3
"""Tests for nova_drawbridge.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The Drawbridge is the occupancy interlock: Nova never moves a garage door, gate, cover, robot
vacuum/mower or fan/heater plug while a person, pet or car is in its zone. Offline only: every
cursor is fake, HA HTTP is mocked, Slack is off. No device is ever moved."""
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
os.environ["NOVA_GUARDS_NO_SLACK"] = "1"
os.environ.setdefault("NOVA_TEST_QUIET", "1")

import nova_drawbridge as D  # noqa: E402
import nova_safety_guards as G  # noqa: E402

SCRIPT = SCRIPTS / "nova_drawbridge.py"
SRC = SCRIPT.read_text()
NOW = 1_800_000_000.0
GARAGE_ZONE = {"pattern": "garage:*", "sensors": ["presence:mmwave:garage", "presence:camera_vision:carport"],
               "clear_seconds": 120, "mode": "interlock", "enabled": True}


def _clear(ago=None):
    return {"ok": True, "occupied": False, "last_occupied": None if ago is None else NOW - ago}


class FakeCursor:
    """Answers the Drawbridge's SQL by substring. presence: {(method, room): (occ_age, newest_age, conf)}."""

    def __init__(self, zones=None, presence=None, fail_on=None):
        self.sql, self.zones, self.presence, self.fail_on = [], zones or [], presence or {}, fail_on
        self.connection, self.rowcount, self._last = mock.MagicMock(), 1, None

    def execute(self, sql, args=None):
        self.sql.append((sql, args))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("pg down")
        self._last = (sql, args)

    def fetchall(self):
        sql = self._last[0]
        if "FROM drawbridge_zones" in sql:
            return [(i, z["pattern"], z.get("motion_class", z["pattern"].split(":")[0]), json.dumps(z["sensors"]),
                     z["clear_seconds"], z["mode"], z.get("enabled", True), "seed") for i, z in enumerate(self.zones)]
        return []

    def fetchone(self):
        sql, args = self._last
        if "telemetry.presence" in sql:
            return self.presence.get((args[1], args[2]), (None, None, None))
        if "count(*)" in sql:
            return (0,)
        return None

    def inserts(self, table):
        return [a for s, a in self.sql if f"INSERT INTO {table}" in s]


def _evaluate(actuator, cls, key, action, cur):
    with mock.patch.object(D, "ensure_schema"):
        return D.evaluate(actuator, cls, key, action, caller="test", cur=cur, now=time.time())


# ── Security ──────────────────────────────────────────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|token|api_key|secret)\s*=\s*['\"][A-Za-z0-9_\-]{12,}['\"]")
        self.assertIn("find-generic-password", SRC)          # Keychain, then nova_secrets
        self.assertIn("nova_secrets", SRC)

    def test_no_private_addresses_or_home_paths(self):
        self.assertNotRegex(SRC, r"192\.168\.|10\.\d+\.\d+\.\d+|/Users/|/home/")
        self.assertNotRegex(SRC, r"@(gmail|digitalnoise)\.")

    def test_sql_is_parameterised(self):
        self.assertNotRegex(SRC, r"execute\(\s*f[\"']")
        cur = FakeCursor()
        evil = "cover.x'); DROP TABLE drawbridge_log;--"
        D.log_decision(cur, evil, "open", False, "r", {}, "t")
        sql, args = cur.sql[-1]
        self.assertNotIn("DROP TABLE", sql)
        self.assertIn(evil, args)

    def test_sensor_method_allowlist(self):
        st = D.read_presence_sensor(FakeCursor(), "ble_rssi'; --", "garage", 300)
        self.assertFalse(st["ok"])

    def test_proteus_still_blocks_before_drawbridge(self):
        with mock.patch.object(D, "guard") as g:
            ok, why = G.physical_guard("close the garage door")
        self.assertFalse(ok)
        self.assertIn("PHYSICAL GUARD", why)
        g.assert_not_called()                                # no confirmation -> Proteus decides alone


# ── Performance ───────────────────────────────────────────────────────────────
class TestPerformance(unittest.TestCase):
    def test_pure_check_10k_under_a_second(self):
        states = {r: _clear(900) for r in GARAGE_ZONE["sensors"]}
        t = time.monotonic()
        for _ in range(10_000):
            D.check("cover.garage_door", "open", states, NOW, GARAGE_ZONE)
        self.assertLess(time.monotonic() - t, 1.0)

    def test_classifier_10k_under_two_seconds(self):
        t = time.monotonic()
        for i in range(10_000):
            D.extract_actuators(f"hue light {i} on", ["light.kitchen"])
        self.assertLess(time.monotonic() - t, 2.0)


# ── Retry ─────────────────────────────────────────────────────────────────────
class TestRetry(unittest.TestCase):
    def test_ha_http_retries_then_succeeds(self):
        good = mock.MagicMock()
        good.read.return_value = b'{"state": "off", "last_changed": "2026-10-15T10:00:00+00:00"}'
        with mock.patch.object(D.urllib.request, "urlopen", side_effect=[OSError("x"), OSError("y"), good]) as u, \
                mock.patch.object(D.time, "sleep"):
            out = D._http_json(mock.MagicMock())
        self.assertEqual(u.call_count, 3)
        self.assertEqual(out["state"], "off")

    def test_ha_http_gives_up_and_sensor_blocks(self):
        with mock.patch.object(D, "_ha_token", return_value="t"), \
                mock.patch.object(D.urllib.request, "urlopen", side_effect=OSError("down")), \
                mock.patch.object(D.time, "sleep"):
            st = D.read_sensors(FakeCursor(), ["ha:binary_sensor.yard_motion"], 120)
        self.assertFalse(st["ha:binary_sensor.yard_motion"]["ok"])
        ok, _ = D.check("lawn_mower.yard", "start", st, NOW,
                        {"pattern": "mower:*", "sensors": ["ha:binary_sensor.yard_motion"], "clear_seconds": 300,
                         "mode": "interlock"})
        self.assertFalse(ok)

    def test_pg_connect_retries_then_raises(self):
        import psycopg2
        with mock.patch.object(psycopg2, "connect", side_effect=psycopg2.OperationalError("no")) as c, \
                mock.patch.object(D.time, "sleep"):
            with self.assertRaises(psycopg2.OperationalError):
                D._cursor()
        self.assertEqual(c.call_count, 3)


# ── Unit ──────────────────────────────────────────────────────────────────────
class TestUnit(unittest.TestCase):
    def test_occupied_blocks(self):
        st = {"presence:mmwave:garage": {"ok": True, "occupied": True}, "presence:camera_vision:carport": _clear()}
        ok, why = D.check("cover.garage_door", "open", st, NOW, GARAGE_ZONE)
        self.assertFalse(ok)
        self.assertIn("someone is there now", why)

    def test_clear_for_less_than_clear_seconds_blocks(self):
        st = {"presence:mmwave:garage": _clear(30), "presence:camera_vision:carport": _clear()}
        ok, why = D.check("cover.garage_door", "open", st, NOW, GARAGE_ZONE)
        self.assertFalse(ok)
        self.assertIn("clear only 30s", why)

    def test_clear_long_enough_allows(self):
        st = {"presence:mmwave:garage": _clear(121), "presence:camera_vision:carport": _clear()}
        self.assertTrue(D.check("cover.garage_door", "open", st, NOW, GARAGE_ZONE)[0])

    def test_sensor_read_failure_blocks(self):
        self.assertFalse(D.check("cover.garage_door", "open", None, NOW, GARAGE_ZONE)[0])
        st = {"presence:mmwave:garage": {"ok": False, "error": "feed silent"},
              "presence:camera_vision:carport": _clear()}
        ok, why = D.check("cover.garage_door", "open", st, NOW, GARAGE_ZONE)
        self.assertFalse(ok)
        self.assertIn("unreadable", why)
        missing = {"presence:camera_vision:carport": _clear()}         # a mapped sensor with no reading
        self.assertFalse(D.check("cover.garage_door", "open", missing, NOW, GARAGE_ZONE)[0])

    def test_unmapped_allows(self):
        ok, why = D.check("vacuum.downstairs", "start", {}, NOW, None)
        self.assertTrue(ok)
        self.assertIn("UNMAPPED", why)
        ok, _ = D.check("switch.space_heater", "on", None, NOW,
                        {"pattern": "energy:*", "sensors": [], "clear_seconds": 60, "mode": "unmapped"})
        self.assertTrue(ok)

    def test_exception_inside_check_blocks(self):
        bad = dict(GARAGE_ZONE, clear_seconds="not-a-number")
        st = {r: _clear(999) for r in GARAGE_ZONE["sensors"]}
        ok, why = D.check("cover.garage_door", "open", st, NOW, bad)
        self.assertFalse(ok)
        self.assertIn("check error", why)

    def test_non_motion_actuator_unaffected(self):
        self.assertEqual(D.check("light.kitchen", "on", None, NOW)[0], True)
        self.assertEqual(D.extract_actuators("hue light 3 on", ["light.garage_light_2"]), [])
        for t in ("cover the monitoring gap", "fan out the work", "blind spot review", "run the gate checks",
                  "restart nova-gateway", "turn on garage light", "unlock the front door"):
            self.assertEqual(D.extract_actuators(t), [], t)

    def test_stop_and_off_never_blocked(self):
        self.assertTrue(D.check("cover.garage_door", "stop", None, NOW, GARAGE_ZONE)[0])
        self.assertTrue(D.check("switch.garage_heater", "turn off", None, NOW, motion_class="energy",
                                zone={"pattern": "energy:garage", "sensors": ["x"], "clear_seconds": 60,
                                      "mode": "interlock"})[0])
        self.assertFalse(D.is_safe_action("cover", "stop then close"))
        self.assertFalse(D.is_safe_action("energy", "turn on"))

    def test_classify_motion_classes(self):
        self.assertEqual(D.classify("cover.garage_door", "cover"), "garage")
        self.assertEqual(D.classify("cover.office_shade", "cover"), "cover")
        self.assertEqual(D.classify("vacuum.x", "vacuum"), "vacuum")
        self.assertEqual(D.classify("lawn_mower.x", "lawn_mower"), "mower")
        self.assertEqual(D.classify("switch.space_heater", "switch"), "energy")
        self.assertIsNone(D.classify("lock.front_door", "lock"))
        self.assertEqual(D.classify_text("open the gate"), "gate")
        self.assertEqual(D.classify_text("mow the lawn"), "mower")

    def test_room_inference_and_zone_specificity(self):
        self.assertEqual(D.infer_room("cover.office_shade", ["office", "garage"]), "office")
        self.assertEqual(D.infer_room("close the lr blinds", ["living_room"]), "living_room")
        zones = [{"pattern": "cover:*", "sensors": ["a"]}, {"pattern": "cover:office", "sensors": ["b"]},
                 {"pattern": "cover.office_shade_2", "sensors": ["c"]}]
        self.assertEqual(D.match_zone(zones, "cover.office_shade", "cover:office")["pattern"], "cover:office")
        self.assertEqual(D.match_zone(zones, "cover.office_shade_2", "cover:office")["pattern"], "cover.office_shade_2")
        self.assertEqual(D.match_zone(zones, "cover.x", "cover:unknown")["pattern"], "cover:*")
        self.assertIsNone(D.match_zone(zones, "vacuum.x", "vacuum:unknown"))

    def test_build_seed_is_conservative(self):
        rows = D.build_seed({"mmwave": {"office"}, "camera_vision": {"garage", "patio"},
                             "vehicle_vision": {"carport"}}, [("binary_sensor.yard_motion", True)])
        by = {r["pattern"]: r for r in rows}
        self.assertIn("presence:vehicle_vision:carport", by["garage:*"]["sensors"])   # cars count for the door
        self.assertIn("presence:mmwave:office", by["vacuum:*"]["sensors"])
        self.assertIn("ha:binary_sensor.yard_motion", by["mower:*"]["sensors"])
        self.assertEqual(by["energy:*"]["mode"], "unmapped")
        self.assertEqual(by["cover:office"]["mode"], "interlock")

    def test_selftest(self):
        with mock.patch("sys.stdout"):
            self.assertEqual(D.selftest(), 0)


# ── Integration ───────────────────────────────────────────────────────────────
class TestIntegration(unittest.TestCase):
    def test_uses_safe_ddl_and_named_tables(self):
        self.assertIn("nova_pg_schema", SRC)
        self.assertIn("safe_ddl", SRC)
        cur = FakeCursor()
        with mock.patch("nova_pg_schema.safe_ddl") as sd:
            D._schema_ok = False
            D.ensure_schema(cur)
            D._schema_ok = False
        self.assertIn("drawbridge_zones", sd.call_args[0][1])
        self.assertIn("drawbridge_log", sd.call_args[0][1])

    def test_presence_sensor_heartbeat_and_event(self):
        cur = FakeCursor(presence={("mmwave", "office"): (None, 5.0, 0.05),
                                   ("mmwave", "garage"): (None, 900.0, 0.05),
                                   ("mmwave", "patio"): (2.0, 2.0, 0.9),
                                   ("camera_vision", "carport"): (40.0, 40.0, 0.95)})
        self.assertEqual(D.read_presence_sensor(cur, "mmwave", "office", 700)["occupied"], False)
        self.assertFalse(D.read_presence_sensor(cur, "mmwave", "garage", 700)["ok"])     # dead feed
        self.assertTrue(D.read_presence_sensor(cur, "mmwave", "patio", 700)["occupied"])
        cam = D.read_presence_sensor(cur, "camera_vision", "carport", 700)
        self.assertTrue(cam["ok"])
        self.assertAlmostEqual(time.time() - cam["last_occupied"], 40, delta=2)
        none = D.read_presence_sensor(cur, "camera_vision", "alley", 700)
        self.assertTrue(none["ok"])
        self.assertIsNone(none["last_occupied"])

    def test_physical_guard_routes_motion_through_drawbridge(self):
        with mock.patch.object(D, "guard", return_value=(False, "DRAWBRIDGE: test")) as g:
            ok, why = G.physical_guard("turn on the space heater", source="gateway")
        self.assertFalse(ok)
        self.assertEqual(why, "DRAWBRIDGE: test")
        self.assertEqual(g.call_args.kwargs["caller"], "gateway")

    def test_drawbridge_block_does_not_spend_a_confirmation(self):
        with mock.patch.object(D, "guard", return_value=(False, "DRAWBRIDGE: occupied")), \
                mock.patch.object(G, "consume_confirmation") as cc:
            ok, _ = G.physical_guard("close the garage door", confirmation_id=9)
        self.assertFalse(ok)
        cc.assert_not_called()

    def test_exception_inside_drawbridge_blocks_not_crashes(self):
        with mock.patch.object(D, "guard", side_effect=RuntimeError("boom")):
            ok, why = G.physical_guard("start the vacuum")
        self.assertFalse(ok)
        self.assertIn("motion blocked", why)
        with mock.patch.object(D, "extract_actuators", side_effect=RuntimeError("boom")):
            ok, why = D.guard("start the vacuum")
        self.assertFalse(ok)

    def test_non_motion_physical_guard_does_no_io(self):
        with mock.patch.object(D, "_cursor", side_effect=AssertionError("no I/O for lights")):
            ok, why = G.physical_guard("hue light 4 on", entity_ids=["light.kitchen"])
        self.assertTrue(ok)
        self.assertEqual(why, "no protected physical domain touched")


# ── Functional ────────────────────────────────────────────────────────────────
class TestFunctional(unittest.TestCase):
    ZONES = [dict(GARAGE_ZONE, motion_class="garage"),
             {"pattern": "energy:*", "motion_class": "energy", "sensors": [], "clear_seconds": 60, "mode": "unmapped"}]

    def test_occupied_garage_blocks_logs_and_notifies(self):
        cur = FakeCursor(zones=self.ZONES, presence={("mmwave", "garage"): (1.0, 1.0, 0.9)})
        with mock.patch.object(D, "_notify_block") as n:
            ok, why = _evaluate("cover.garage_door", "garage", "garage:garage", "open", cur)
        self.assertFalse(ok)
        row = cur.inserts("drawbridge_log")[-1]
        self.assertEqual(row[0], "cover.garage_door")
        self.assertFalse(row[2])
        self.assertEqual(row[5], "test")
        n.assert_called_once()

    def test_clear_garage_allows_and_logs(self):
        cur = FakeCursor(zones=self.ZONES, presence={("mmwave", "garage"): (600.0, 3.0, 0.05)})
        ok, why = _evaluate("cover.garage_door", "garage", "garage:garage", "open", cur)
        self.assertTrue(ok, why)
        self.assertTrue(cur.inserts("drawbridge_log")[-1][2])

    def test_unmapped_allows_logs_and_registers(self):
        cur = FakeCursor(zones=self.ZONES)
        ok, why = _evaluate("vacuum.downstairs", "vacuum", "vacuum:unknown", "start", cur)
        self.assertTrue(ok)
        self.assertIn("UNMAPPED", why)
        self.assertEqual(cur.inserts("drawbridge_zones")[-1][0], "vacuum:unknown")
        self.assertTrue(cur.inserts("drawbridge_log"))

    def test_pg_failure_fails_closed(self):
        cur = FakeCursor(zones=self.ZONES, fail_on="FROM drawbridge_zones")
        with mock.patch.object(D, "_notify_block"):
            ok, why = _evaluate("cover.garage_door", "garage", "garage:garage", "open", cur)
        self.assertFalse(ok)
        self.assertIn("fail closed", why)
        with mock.patch.object(D, "_cursor", side_effect=OSError("pg down")):
            ok, why = D.guard("start the vacuum", caller="test")
        self.assertFalse(ok)

    def test_cli_check_prints_decision(self):
        with mock.patch.object(D, "_cursor", return_value=FakeCursor()), mock.patch.object(D, "ensure_schema"), \
                mock.patch.object(D, "guard", return_value=(False, "DRAWBRIDGE: x")) as g, \
                mock.patch("builtins.print") as p:
            rc = D.main(["--check", "cover.garage_door", "open"])
        self.assertEqual(rc, 3)
        self.assertEqual(g.call_args[0][1], ["cover.garage_door"])
        self.assertIn("BLOCK", p.call_args[0][0])

    def test_metrics_shape(self):
        cur = mock.MagicMock()
        cur.fetchone.side_effect = [(40, 3, 1, 5, 7), (2, 34)]
        m = D.metrics(cur, "2026-10")
        self.assertEqual((m["decisions"], m["blocks"], m["unmapped_actuators"]), (40, 3, 2))
        with self.assertRaises(ValueError):
            D.metrics(cur, "2026-10'; drop")


# ── Frame ─────────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_selftest_and_help_exit_zero(self):
        env = {**os.environ, "NOVA_TEST_QUIET": "1"}
        for arg in ("--selftest", "--help"):
            r = subprocess.run([sys.executable, str(SCRIPT), arg], capture_output=True, text=True, timeout=30, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertTrue(re.search(r"^def main\(", SRC, re.M))


if __name__ == "__main__":
    unittest.main()
