#!/usr/bin/env python3
"""Seven-category tests for nova_safety_guards.py (the Proteus rules): Security, Performance,
Retry, Unit, Integration, Functional, Frame. Complements test_nova_safety_guards.py.
No real PG, Slack or devices: every cursor is fake and _notify/post_both are mocked.
Written by Jordan Koch (via Claude)."""
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
os.environ["NOVA_GUARDS_NO_SLACK"] = "1"

import nova_safety_guards as G  # noqa: E402

SRC = (SCRIPTS / "nova_safety_guards.py").read_text()


class FakeCursor:
    def __init__(self, rows=None, one=None, rowcount=1):
        self.sql, self._rows, self._one, self.rowcount = [], list(rows or []), one, rowcount
        self.connection = mock.MagicMock()

    def execute(self, sql, args=None):
        self.sql.append((sql, args))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


# ── Security ────────────────────────────────────────────────────────────────
class TestSecurity(unittest.TestCase):
    def test_confirmation_sql_is_parameterised(self):
        oc = FakeCursor(one=(1,))
        evil = "unlock front door'); DROP TABLE safety_confirmations;--"
        G.request_confirmation(oc, "physical", evil)
        ins = [s for s in oc.sql if "INSERT INTO safety_confirmations" in s[0]][0]
        self.assertNotIn("DROP TABLE", ins[0])
        self.assertIn(evil, ins[1])

    def test_approver_must_be_jordan(self):
        for who in ("nova", "nova:self", "amy", "", "system", " jordan"):
            self.assertFalse(G.approve_confirmation(FakeCursor(), 1, who), who)
        self.assertTrue(G.approve_confirmation(FakeCursor(), 1, "jordan:cli"))

    def test_cid_injection_refused(self):
        with self.assertRaises((ValueError, TypeError)):
            G.approve_confirmation(FakeCursor(), "1 OR 1=1", "jordan")

    def test_consume_failure_fails_closed(self):
        oc = mock.MagicMock()
        oc.execute.side_effect = RuntimeError("pg down")
        ok, why = G.physical_guard("unlock the front door", confirmation_id=5, oc=oc)
        self.assertFalse(ok)
        self.assertIn("invalid", why)

    def test_unreadable_owner_fails_closed(self):
        ok, why = G.comms_guard("", macs=["aa:bb"], owner_lookup=lambda m: (None, "timeout"))
        self.assertFalse(ok)
        self.assertIn("unreadable", why)

    def test_no_hardcoded_secrets_or_user_paths(self):
        self.assertNotRegex(SRC, r"(password|secret|token)\s*=\s*['\"][^'\"]{8,}")
        self.assertNotIn("/Users/", SRC)

    def test_gracie_exemption_does_not_cover_real_people(self):
        self.assertTrue(G.safety_redline_ok("synthesize Gracie Wise voice for the briefing"))
        self.assertFalse(G.safety_redline_ok("Gracie Wise: clone Jordan's dad's voice"))

    def test_report_block_truncates_payload(self):
        oc = FakeCursor(one=(9,))
        G.report_block(oc, source="t", action="x" * 5000, reason="r" * 5000, guard="physical", notify=False)
        ins = [s for s in oc.sql if "INSERT INTO restraint_ledger" in s[0]][0]
        self.assertLessEqual(len(ins[1][1]), 1000)
        self.assertLessEqual(len(ins[1][2]), 1000)


# ── Performance ─────────────────────────────────────────────────────────────
class TestPerformance(unittest.TestCase):
    def test_guards_fast_on_large_input(self):
        big = ("turn the office lamp on and dim it " * 2000)
        t0 = time.perf_counter()
        for _ in range(20):
            G.physical_guard(big)
            G.comms_guard(big)
            G.manipulation_check(big)
            G.safety_redline_ok(big)
            G.is_health_nudge(big)
        self.assertLess(time.perf_counter() - t0, 5.0)

    def test_many_entities_linear(self):
        ents = [f"light.room_{i}" for i in range(20000)]
        t0 = time.perf_counter()
        ok, _ = G.physical_guard("lights on", entity_ids=ents)
        self.assertTrue(ok)
        self.assertLess(time.perf_counter() - t0, 2.0)

    def test_prior_blocks_bounded_query(self):
        rows = [(i, None, f"lock the front door attempt {i}", {}) for i in range(400)]
        oc = FakeCursor(rows=rows)
        t0 = time.perf_counter()
        G.prior_blocks(oc, "lock the front door attempt 3")
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertIn("LIMIT 400", oc.sql[0][0])


# ── Retry ───────────────────────────────────────────────────────────────────
class TestRetry(unittest.TestCase):
    def test_ops_cursor_retries_then_succeeds(self):
        import psycopg2
        conn = mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=[psycopg2.OperationalError("blip"), conn]) as c, \
                mock.patch("time.sleep") as sl:
            cur = G._ops_cursor()
        self.assertEqual(c.call_count, 2)
        sl.assert_called_once()
        self.assertIs(cur, conn.cursor.return_value)

    def test_ops_cursor_gives_up_after_3_with_backoff(self):
        import psycopg2
        with mock.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("down")) as c, \
                mock.patch("time.sleep") as sl:
            with self.assertRaises(psycopg2.OperationalError):
                G._ops_cursor()
        self.assertEqual(c.call_count, 3)
        delays = [a.args[0] for a in sl.call_args_list]
        self.assertEqual(len(delays), 2)
        self.assertLess(delays[0], delays[1])

    def test_pg_down_still_fails_closed(self):
        import psycopg2
        with mock.patch("psycopg2.connect", side_effect=psycopg2.OperationalError("down")), \
                mock.patch("time.sleep"):
            self.assertFalse(G.nudge_allowed())
            ok, why = G.comms_guard("", macs=["aa:bb:cc:dd:ee:ff"])
            self.assertFalse(ok)
            ok, _ = G.physical_guard("unlock the door", confirmation_id=3)
            self.assertFalse(ok)

    def test_notify_retries_and_logs(self):
        import nova_config
        with mock.patch.dict(os.environ, {"NOVA_GUARDS_NO_SLACK": ""}), \
                mock.patch.object(nova_config, "post_both", side_effect=[OSError("x"), OSError("y"), None]) as pb, \
                mock.patch("time.sleep") as sl, mock.patch.object(G, "log") as lg:
            G._notify("hello")
        self.assertEqual(pb.call_count, 3)
        self.assertEqual(sl.call_count, 2)
        self.assertTrue(any("attempt 1/3" in c.args[0] for c in lg.call_args_list))

    def test_notify_stops_after_success(self):
        import nova_config
        with mock.patch.dict(os.environ, {"NOVA_GUARDS_NO_SLACK": ""}), \
                mock.patch.object(nova_config, "post_both") as pb, mock.patch("time.sleep") as sl:
            G._notify("hello")
        self.assertEqual(pb.call_count, 1)
        sl.assert_not_called()

    def test_ledger_write_failure_still_notifies(self):
        oc = mock.MagicMock()
        oc.execute.side_effect = [None, RuntimeError("insert failed")]
        oc.fetchall.return_value = []
        with mock.patch.object(G, "_notify") as n:
            r = G.report_block(oc, source="t", action="lock door", reason="r", guard="physical")
        self.assertEqual(r["ledger_id"], -1)
        n.assert_called_once()


# ── Unit ────────────────────────────────────────────────────────────────────
class TestUnit(unittest.TestCase):
    def test_domains_of(self):
        self.assertEqual(G._domains_of(["Lock.front", "light.x", 3, "noperiod"]), {"lock", "light"})

    def test_setpoints_celsius_and_bad(self):
        pts = G._setpoints_f("set to 30C", setpoint_f="abc")
        self.assertTrue(pts[0] != pts[0])  # NaN
        self.assertAlmostEqual(pts[1], 86.0)

    def test_nan_setpoint_blocks(self):
        ok, _ = G.physical_guard("set thermostat", domains=["climate"], setpoint_f="bogus")
        self.assertFalse(ok)

    def test_norm_and_similar(self):
        self.assertEqual(G._norm("Please lock THE door!"), "lock door")
        self.assertEqual(G.similar("", "x"), 0.0)
        self.assertGreaterEqual(G.similar("lock the front door", "please lock front door now"), 0.72)

    def test_nudge_allowed_values(self):
        for v, exp in (('"true"', True), ("on", True), ({"a": 1}, False), (None, False), ("false", False)):
            self.assertEqual(G.nudge_allowed(FakeCursor(one=(v,))), exp, v)
        self.assertFalse(G.nudge_allowed(FakeCursor(one=None)))

    def test_anchor_values_shape(self):
        self.assertEqual(len(G.ANCHOR_NAMES), len(G.ANCHOR_VALUES))
        for v in G.ANCHOR_VALUES:
            self.assertTrue(v["statement"])
            self.assertGreaterEqual(v["priority_hint"], 8)

    def test_kill_engaged_reads_file(self):
        with mock.patch.object(G, "KILL_FILE", "/nonexistent/kill"):
            self.assertFalse(G.kill_engaged())

    def test_person_ranking_rx(self):
        self.assertTrue(G.PERSON_RANKING_RX.search("visitor_threat_score"))
        self.assertFalse(G.PERSON_RANKING_RX.search("host_threat_scores"))


# ── Integration ─────────────────────────────────────────────────────────────
class TestIntegration(unittest.TestCase):
    def test_request_approve_consume_flow_against_cursor(self):
        oc = FakeCursor(one=(42,))
        with mock.patch.object(G, "_notify") as n:
            cid = G.request_confirmation(oc, "physical", "unlock front door", ["lock.front"])
        self.assertEqual(cid, 42)
        self.assertIn("confirm 42", n.call_args.args[0])
        self.assertTrue(G.approve_confirmation(oc, cid, "jordan:cli"))
        oc._one = ("unlock front door",)
        ok, why = G.physical_guard("unlock front door", ["lock.front"], confirmation_id=cid, oc=oc)
        self.assertTrue(ok, why)
        oc._one = None  # second use: already consumed
        ok, _ = G.physical_guard("unlock front door", ["lock.front"], confirmation_id=cid, oc=oc)
        self.assertFalse(ok)

    def test_report_block_then_blocked_before(self):
        oc = FakeCursor(one=(1,), rows=[])
        r1 = G.report_block(oc, source="t", action="close the garage door", reason="P1", guard="physical", notify=False)
        self.assertFalse(r1["repeat"])
        oc._rows = [(1, None, "close the garage door", {})]
        self.assertTrue(G.blocked_before(oc, "please close the garage door now"))

    def test_scene_guard_routes_through_physical_guard(self):
        ok, why = G.scene_guard("Custom", ["light.a", "lock.front"])
        self.assertFalse(ok)
        self.assertIn("domain lock", why)

    def test_request_confirmation_db_failure_returns_minus_one(self):
        oc = mock.MagicMock()
        oc.execute.side_effect = RuntimeError("no table")
        with mock.patch.object(G, "_notify") as n:
            self.assertEqual(G.request_confirmation(oc, "physical", "x"), -1)
        n.assert_not_called()


# ── Functional ──────────────────────────────────────────────────────────────
class TestFunctional(unittest.TestCase):
    def test_cli_scene_check_refuses_with_exit_3(self):
        with mock.patch.object(G, "_ops_cursor", side_effect=RuntimeError("no pg")):
            self.assertEqual(G.main(["scene-check", "Lock", "Up"]), 3)
            self.assertEqual(G.main(["scene-check", "movie"]), 0)

    def test_cli_physical_check_and_manip(self):
        self.assertEqual(G.main(["physical-check", "turn on", "light.office"]), 0)
        self.assertEqual(G.main(["physical-check", "unlock", "lock.front_door"]), 3)
        self.assertEqual(G.main(["manip", "act now or else"]), 0)

    def test_cli_confirm(self):
        oc = FakeCursor()
        with mock.patch.object(G, "_ops_cursor", return_value=oc):
            self.assertEqual(G.main(["confirm", "7"]), 0)
            oc.rowcount = 0
            self.assertEqual(G.main(["confirm", "7"]), 1)
            self.assertEqual(G.main(["bogus"]), 2)

    def test_cli_usage(self):
        self.assertEqual(G.main([]), 2)

    def test_everyday_actions_pass_every_guard(self):
        for a in ("restart nova-gateway", "turn on office lamp", "set thermostat to 70F", "rebuild the cache"):
            self.assertTrue(G.physical_guard(a)[0], a)
            self.assertTrue(G.comms_guard(a)[0], a)
            self.assertTrue(G.safety_redline_ok(a), a)   # red line and physical_guard now agree (62-80F)


# ── Frame ───────────────────────────────────────────────────────────────────
class TestFrame(unittest.TestCase):
    def test_imports_and_usage_no_crash(self):
        env = dict(os.environ, NOVA_GUARDS_NO_SLACK="1")
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_safety_guards.py")],
                           capture_output=True, text=True, timeout=30, env=env)
        self.assertEqual(r.returncode, 2)
        self.assertIn("usage", r.stdout)

    def test_manip_subcommand_runs(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_safety_guards.py"), "manip", "hello"],
                           capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, NOVA_GUARDS_NO_SLACK="1"))
        self.assertEqual(r.returncode, 0)
        self.assertIn('"ok": true', r.stdout)



# ── 2026-10-08: red line and physical_guard agree on climate; lighting scenes pass ──
IN_BAND = ["set thermostat to 70F", "set thermostat to 72", "thermostat 21C", "set thermostat to 62",
           "set thermostat to 80F", "set thermostat to 75 degrees", "cool the house to 68"]
EXTREME = ["set thermostat to 95F", "set thermostat to 55", "set thermostat to 85 degrees", "heat to 30C",
           "set thermostat to 81F", "thermostat off", "turn the heat off", "set the thermostat to -5"]
LIGHT_SCENES = ["Bedtime Calm", "Good Night Lights", "Bedtime Reading", "Away Lamps Dim", "Reading"]
SECURING_SCENES = ["Lock Up", "Leave Home", "Good Night", "Bedtime", "Bedtime Lock Up", "Good Night Doors",
                   "Night Mode", "Arm Away", "Close Garage", "Calm Lockdown"]


class TestConsistencySecurity(unittest.TestCase):
    def test_extremes_and_off_refused_by_both(self):
        for t in EXTREME:
            self.assertFalse(G.safety_redline_ok(t), t)
            self.assertFalse(G.physical_guard(t)[0], t)

    def test_securing_scene_names_refused(self):
        for s in SECURING_SCENES:
            if s.lower().replace(" ", "_") in G.KNOWN_SAFE_SCENES:
                continue   # nova_home_control's own scenes: contents known (no locks)
            self.assertFalse(G.scene_guard(s)[0], s)

    def test_lighting_word_cannot_launder_a_hard_word(self):
        for s in ("Calm Lock Up", "Dim Lights and Lock Doors", "Soft Garage Close"):
            self.assertFalse(G.scene_guard(s)[0], s)


class TestConsistencyPerformance(unittest.TestCase):
    def test_climate_check_fast_on_long_text(self):
        t = "set thermostat to 72 " + "x" * 50000
        t0 = time.perf_counter()
        for _ in range(20):
            G.safety_redline_ok(t)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestConsistencyRetry(unittest.TestCase):
    def test_guard_eval_error_still_fails_closed(self):
        with mock.patch.object(G, "_physical_hits", side_effect=RuntimeError("x")):
            self.assertFalse(G.physical_guard("set thermostat to 72")[0])


class TestConsistencyUnit(unittest.TestCase):
    def test_bare_number_read_as_f(self):
        self.assertEqual(G._setpoints_f("set thermostat to 72"), [72.0])
        self.assertEqual(G._setpoints_f("set thermostat to 72F"), [72.0])   # not double-counted
        self.assertEqual(G._setpoints_f("thermostat humidity to 40%"), [])

    def test_climate_extreme_only_with_climate_word(self):
        self.assertFalse(G._climate_extreme("set the timer to 95"))
        self.assertTrue(G._climate_extreme("set thermostat to 95"))

    def test_scene_name_tiers(self):
        self.assertFalse(G._scene_name_risky("Bedtime Calm"))
        self.assertTrue(G._scene_name_risky("Good Night"))
        self.assertTrue(G._scene_name_risky("Calm Lock Up"))


class TestConsistencyIntegration(unittest.TestCase):
    def test_actor_redline_uses_same_band(self):
        import nova_autonomy_actor as A
        self.assertTrue(A.redline_ok("set thermostat to 70F"))
        self.assertFalse(A.redline_ok("set thermostat to 95F"))


class TestConsistencyFunctional(unittest.TestCase):
    def test_red_line_matches_physical_guard_on_every_case(self):
        for t in IN_BAND + EXTREME:
            self.assertEqual(G.safety_redline_ok(t), G.physical_guard(t)[0], t)

    def test_lighting_scenes_pass(self):
        for s in LIGHT_SCENES:
            self.assertTrue(G.scene_guard(s)[0], s)


class TestConsistencyFrame(unittest.TestCase):
    def test_cli_scene_check_bedtime_calm_exit_0(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_safety_guards.py"), "scene-check", "Bedtime Calm"],
                           capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, NOVA_GUARDS_NO_SLACK="1"))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
