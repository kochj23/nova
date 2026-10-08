#!/usr/bin/env python3
"""Tests for the Proteus-rule guards: nova_safety_guards, nova_privacy_guards, the values drift
check, the self-justification audit and the self-repair digest. One file for every guard.
Written by Jordan Koch (via Claude)."""
import os
import re
import sys
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
os.environ["NOVA_GUARDS_NO_SLACK"] = "1"

import nova_safety_guards as G          # noqa: E402
import nova_privacy_guards as P         # noqa: E402


class FakeCursor:
    """Minimal cursor: records SQL, returns queued rows."""
    def __init__(self, rows=None, one=None):
        self.sql, self._rows, self._one, self.rowcount = [], list(rows or []), one, 1
        self.connection = mock.MagicMock()

    def execute(self, sql, args=None):
        self.sql.append((sql, args))

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._one


# ── P1 physical ─────────────────────────────────────────────────────────────
class TestPhysicalGuard(unittest.TestCase):
    def test_protected_domains_blocked_by_entity_not_wording(self):
        for e in ("lock.front_door", "cover.garage", "alarm_control_panel.home", "siren.hallway"):
            ok, why = G.physical_guard("turn on", entity_ids=[e])
            self.assertFalse(ok, e)
            self.assertIn("PHYSICAL GUARD", why)

    def test_rephrasing_does_not_get_past_the_domain_check(self):
        ok, _ = G.physical_guard("make the house cozy for the night", entity_ids=["lock.back_door"])
        self.assertFalse(ok)

    def test_wording_blocked(self):
        for a in ("lock the front door", "unlock the front door", "close the garage door", "seal the exits",
                  "arm the security system", "disable the smoke alarm", "lockdown the house"):
            self.assertFalse(G.physical_guard(a)[0], a)

    def test_garage_room_and_software_locks_are_not_doors(self):
        self.assertTrue(G.physical_guard("on", entity_ids=["light.garage_light_2"])[0])
        for a in ("clear the pg advisory lock", "remove stale lock file", "reboot screen lock",
                  "restart nova-freshness-monitor", "turn on garage light"):
            self.assertTrue(G.physical_guard(a)[0], a)
        self.assertFalse(G.physical_guard("x", entity_ids=["switch.garage_door_opener"])[0])

    def test_climate_extremes_only(self):
        self.assertTrue(G.physical_guard("set thermostat to 70F")[0])
        self.assertFalse(G.physical_guard("set thermostat to 90F")[0])
        self.assertFalse(G.physical_guard("set heat to 10C")[0])
        self.assertFalse(G.physical_guard("x", entity_ids=["climate.living"], setpoint_f=50)[0])
        self.assertFalse(G.physical_guard("turn heat off")[0])
        self.assertTrue(G.physical_guard("x", entity_ids=["climate.living"], setpoint_f=72)[0])

    def test_confirmation_unlocks_once(self):
        with mock.patch.object(G, "consume_confirmation", return_value=(True, "ok")):
            self.assertTrue(G.physical_guard("unlock the front door", confirmation_id=7)[0])
        with mock.patch.object(G, "consume_confirmation", return_value=(False, "expired")):
            ok, why = G.physical_guard("unlock the front door", confirmation_id=7)
            self.assertFalse(ok); self.assertIn("invalid", why)

    def test_only_jordan_approves(self):
        oc = FakeCursor()
        self.assertFalse(G.approve_confirmation(oc, 1, approved_by="nova"))
        self.assertTrue(G.approve_confirmation(oc, 1, approved_by="jordan:cli"))

    def test_guard_never_raises(self):
        with mock.patch.object(G, "_physical_hits", side_effect=RuntimeError("boom")):
            ok, why = G.physical_guard("anything")
        self.assertFalse(ok); self.assertIn("refusing", why)

    def test_autonomy_safety_reexport(self):
        import nova_autonomy_safety as S
        self.assertFalse(S.physical_guard("x", entity_ids=["lock.front"])[0])
        self.assertFalse(S.comms_guard("block amy's iphone")[0])


class TestSceneGuard(unittest.TestCase):
    def test_known_home_control_scenes_pass(self):
        for s in ("away", "goodnight", "movie", "Leave", "bedtime"):
            self.assertTrue(G.scene_guard(s)[0], s)

    def test_unknown_securing_scenes_refused(self):
        for s in ("Leave Home", "Good Night", "Lock Up", "Arm Away", "Close Garage"):
            self.assertFalse(G.scene_guard(s)[0], s)

    def test_plain_scene_passes_and_known_contents_checked(self):
        self.assertTrue(G.scene_guard("Reading")[0])
        self.assertTrue(G.scene_guard("Movie Night Lights")[0])
        self.assertFalse(G.scene_guard("Reading", scene_entities=["light.desk", "lock.front"])[0])

    def test_home_control_keys_match_known_scenes(self):
        src = (SCRIPTS / "nova_home_control.py").read_text()
        block = src[src.index("scene_map = {"):src.index("}", src.index("scene_map = {"))]
        keys = set(re.findall(r'"([a-z_]+)":', block))
        self.assertEqual(keys, set(G.KNOWN_SAFE_SCENES))


# ── P2 comms ────────────────────────────────────────────────────────────────
class TestCommsGuard(unittest.TestCase):
    def test_household_device_refused(self):
        ok, _ = G.comms_guard("block", macs=["aa"], owner_lookup=lambda m: ("amy", None))
        self.assertFalse(ok)
        self.assertFalse(G.comms_guard("block", names=["Jordans-iPhone"], owner_lookup=lambda m: (None, None))[0])

    def test_unreadable_ownership_fails_closed(self):
        self.assertFalse(G.comms_guard("block", macs=["aa"], owner_lookup=lambda m: (None, "pg down"))[0])

    def test_stranger_device_allowed(self):
        self.assertTrue(G.comms_guard("", macs=["aa"], names=["esp-3a4f"], owner_lookup=lambda m: (None, None))[0])

    def test_network_entities_and_wording(self):
        for e in ("switch.kochj_garage_2_4", "switch.garage_poe_8_port_port_3_poe", "button.garage_u6_enterprise_restart"):
            self.assertFalse(G.comms_guard("off", entity_ids=[e])[0], e)
        for a in ("block amy's phone from the internet", "throttle the wifi", "disconnect jordan from signal"):
            self.assertFalse(G.comms_guard(a)[0], a)


# ── red lines (P1/P2/P12/P14/P7 word level) ─────────────────────────────────
class TestSafetyRedline(unittest.TestCase):
    BLOCK = ["lock the front door", "close the garage door", "disarm the alarm", "block Amys iPhone from the internet",
             "threaten the contractor", "retaliate against the neighbor", "clone his late father's voice",
             "imitate Jordan's writing style", "synthesize his late mother's voice", "rank visitors by threat",
             "set thermostat to 95F"]
    ALLOW = ["restart nova-freshness-monitor", "adjust log level for llm-ping", "adopt skill pursue-interest-aviation-ref",
             "synthesize narration in the Gracie Wise voice", "re-synthesize voice room clips",
             "filter duplicate Slack alerts from big brother", "retire goal 'NMAPScanner stability'",
             "rebuild probe embedding cache", "draft a status check-in for RsyncGUI polish"]

    def test_blocks(self):
        for t in self.BLOCK:
            self.assertFalse(G.safety_redline_ok(t), t)

    def test_allows_everyday_ops(self):
        for t in self.ALLOW:
            self.assertTrue(G.safety_redline_ok(t), t)

    def test_actor_and_coagency_redlines_include_it(self):
        import nova_autonomy_actor as A
        import nova_coagency as C
        for t in ("lock the front door", "clone his late father's voice"):
            self.assertFalse(A.redline_ok(t), t)
            self.assertFalse(C.redline_ok(t), t)
        self.assertTrue(A.redline_ok("nova-freshness-monitor 192.168.1.6 restart"))

    def test_redline_fails_closed_without_guards(self):
        import nova_autonomy_actor as A
        with mock.patch.object(A, "_guards", None):
            self.assertFalse(A.redline_ok("restart nova-freshness-monitor"))

    def test_coagency_execution_refuses_physical(self):
        import nova_coagency as C
        row = {"status": "approved", "decided_by": "jordan", "redline_pass": True,
               "value_check": {"available": True, "allowed": True}, "target_service": "nova-freshness-monitor",
               "proposed_action": "restart nova-freshness-monitor and lock.front_door"}
        with self.assertRaises(C.ExecutionRefused):
            C.assert_executable("live", row)


# ── P5 manipulation ─────────────────────────────────────────────────────────
class TestManipulation(unittest.TestCase):
    def test_flags(self):
        cases = {"guilt-hook": "After everything I've done, you never listen.",
                 "invented-urgency": "Act now before it's too late.",
                 "flattery-leverage": "Someone as smart as you would agree with me.",
                 "fear-appeal": "You'll regret it if you ignore this.",
                 "engineered-mood": "I'll play something soft so you'll feel better and then approve it."}
        for kind, text in cases.items():
            r = G.manipulation_check(text)
            self.assertFalse(r["ok"], kind); self.assertIn(kind, r["flags"])

    def test_plain_message_ok(self):
        self.assertTrue(G.manipulation_check("The backup finished at 3am. Nothing needs you.")["ok"])
        self.assertTrue(G.manipulation_check("")["ok"])


# ── P6 consent ──────────────────────────────────────────────────────────────
class TestNudgeConsent(unittest.TestCase):
    def test_detects_health_nudges(self):
        self.assertTrue(G.is_health_nudge("You should get more sleep tonight"))
        self.assertTrue(G.is_health_nudge("your screen time is too high"))
        self.assertFalse(G.is_health_nudge("The sleep sensor in the bedroom is offline"))

    def test_default_off(self):
        self.assertFalse(G.nudge_allowed(FakeCursor(one=None)))
        self.assertFalse(G.nudge_allowed(FakeCursor(one=('"false"',))))
        self.assertTrue(G.nudge_allowed(FakeCursor(one=('"true"',))))
        broken = FakeCursor(); broken.execute = mock.MagicMock(side_effect=RuntimeError("pg"))
        self.assertFalse(G.nudge_allowed(broken))


# ── P7 no person-ranking (lint) ─────────────────────────────────────────────
class TestNoPersonRanking(unittest.TestCase):
    def test_regex(self):
        for bad in ("person_threat", "person_score", "visitor_risk", "face_trust_score", "hamlet", "threat_score_per_person"):
            self.assertTrue(G.PERSON_RANKING_RX.search(bad), bad)
        for ok in ("host_threat_scores", "threat_type", "device_score", "threat_score"):
            self.assertFalse(G.PERSON_RANKING_RX.search(ok), ok)

    def test_code_has_no_person_ranking_identifiers(self):
        rx = re.compile(r"(CREATE TABLE|ALTER TABLE|ADD COLUMN|INSERT INTO)\s+[^;]{0,200}", re.I)
        offenders = []
        me = Path(__file__).resolve()
        for p in list(SCRIPTS.glob("*.py")) + list(SCRIPTS.glob("nova_gateway/*.py")):
            if p.name in ("nova_safety_guards.py",) or p == me:
                continue
            try:
                src = p.read_text(errors="replace")
            except OSError:
                continue
            for m in rx.finditer(src):
                if G.PERSON_RANKING_RX.search(m.group(0)):
                    offenders.append(f"{p.name}: {m.group(0)[:80]}")
        self.assertEqual(offenders, [], "a person-ranking table/column appeared — score devices, never people")

    def test_live_schema_has_no_person_ranking_columns(self):
        try:
            cur = G._ops_cursor()
        except Exception:
            self.skipTest("PG unreachable")
        cur.execute("""SELECT table_schema||'.'||table_name||'.'||column_name FROM information_schema.columns
                       WHERE table_schema NOT IN ('pg_catalog','information_schema')""")
        bad = [r[0] for r in cur.fetchall() if G.PERSON_RANKING_RX.search(r[0].split(".", 1)[1])]
        cur.connection.close()
        self.assertEqual(bad, [])


# ── P9 honest stopping ──────────────────────────────────────────────────────
class TestHonestStopping(unittest.TestCase):
    def test_similarity(self):
        self.assertGreater(G.similar("lock the front door", "please lock the front door now"), 0.72)
        self.assertLess(G.similar("lock the front door", "restart the soil monitor"), 0.5)

    def test_report_block_writes_ledger_and_flags_repeat(self):
        prior = [(5, datetime.now(), "lock the front door", {"guard": "physical"})]
        oc = FakeCursor(rows=prior, one=(42,))
        with mock.patch.object(G, "_notify") as n:
            r = G.report_block(oc, source="t", action="please lock the front door", reason="x", guard="physical")
        self.assertTrue(r["repeat"]); self.assertEqual(r["ledger_id"], 42)
        self.assertTrue(any("restraint_ledger" in s for s, _ in oc.sql))
        self.assertIn("already stopped", n.call_args[0][0])

    def test_first_block_not_repeat(self):
        oc = FakeCursor(rows=[], one=(1,))
        with mock.patch.object(G, "_notify"):
            self.assertFalse(G.report_block(oc, source="t", action="lock it", reason="x", guard="physical")["repeat"])

    def test_blocked_before_and_coagency_gate_refuses_retry(self):
        import nova_coagency as C
        prior = [(5, datetime.now(), "restart soil monitor and lock the back door", {})]
        oc = FakeCursor(rows=prior, one=(9,))
        self.assertTrue(G.blocked_before(oc, "restart soil monitor and lock back door"))
        with mock.patch.object(G, "_notify"), mock.patch.object(C, "redline_ok", return_value=True):
            self.assertFalse(C._redline_gate(oc, "test", "restart soil monitor and lock back door"))


# ── P3 privacy ──────────────────────────────────────────────────────────────
class TestPrivacyGuards(unittest.TestCase):
    def test_tag_and_filter(self):
        md = P.tag_private({"person": "Amy"})
        self.assertEqual(md["privacy"], "private"); self.assertTrue(md["no_content_generation"])
        mems = [{"source": "face_recognition", "text": "Amy detected at Front Door"},
                {"source": "vision", "text": "x", "metadata": md},
                {"source": "vision", "text": "Amy spotted at driveway_cam (91% match)"},
                {"source": "journal", "text": "The tomatoes came in."}]
        self.assertEqual([m["text"] for m in P.filter_for_content(mems)], ["The tomatoes came in."])

    def test_nova_config_filter_drops_face_outputs(self):
        import nova_config
        out = nova_config.filter_private_memories([
            {"source": "vision", "text": "a", "metadata": {"privacy": "private"}},
            {"source": "face_recognition", "text": "b"},
            {"source": "news", "text": "c"}])
        self.assertEqual([m["text"] for m in out], ["c"])

    def test_scrub(self):
        t = "The garden is green. Amy was seen at the doorbell camera at 3pm. Rain later."
        self.assertEqual(P.scrub_face_mentions(t, ["Amy"]), "The garden is green. Rain later.")

    def test_purpose_limit(self):
        self.assertTrue(P.camera_use_ok("safety", "jordan")[0])
        self.assertFalse(P.camera_use_ok("engagement", "jordan")[0])
        self.assertFalse(P.camera_use_ok("presence", "herd:gaston")[0])

    def test_producers_tag_private(self):
        for f in ("nova_face_recognition.py", "nova_face_integration.py"):
            self.assertIn("tag_private", (SCRIPTS / f).read_text(), f)


# ── P11 values drift ────────────────────────────────────────────────────────
class TestValuesDrift(unittest.TestCase):
    def setUp(self):
        import nova_values
        self.V = nova_values

    def test_clean_rewrite_auto_activates(self):
        prev = {"cost-conscious": (7, "I spend the cheapest resource."), "honesty-over-comfort": (8, "I evidence.")}
        new = [{"value": "cost-conscious", "priority_hint": 8, "statement": "I spend the cheapest resource that works."},
               {"value": "honesty-over-comfort", "priority_hint": 8, "statement": "I evidence, not perform."}]
        self.assertEqual(self.V.drift_check(prev, new), [])

    def test_drop_jump_and_redline_adjacent_need_signoff(self):
        prev = {"a": (5, "x"), "b": (3, "y"), "never-self-preserve": (10, "I never copy myself.")}
        new = [{"value": "b", "priority_hint": 8, "statement": "y"},
               {"value": "never-self-preserve", "priority_hint": 9, "statement": "I never copy myself."},
               {"value": "protective-oversight", "priority_hint": 6, "statement": "I may lock doors to keep him safe."}]
        kinds = {f["kind"] for f in self.V.drift_check(prev, new)}
        self.assertTrue({"dropped", "priority_jump", "redline_adjacent_change", "redline_adjacent_new",
                         "anchor_weakened"} <= kinds, kinds)

    def test_queries_only_count_active_rows(self):
        src = (SCRIPTS / "nova_values.py").read_text()
        self.assertEqual(src.count("supersedes IS NOT NULL)"), 0)
        self.assertGreaterEqual(src.count("status='active'"), 8)

    def test_precheck_denies_without_model(self):
        with mock.patch.object(self.V, "llm", side_effect=AssertionError("no model needed")):
            self.assertFalse(self.V._proteus_precheck("lock the front door")["allowed"])
            r = self.V._proteus_precheck("send-to-gaston: act now before it's too late, only you can help")
            self.assertFalse(r["allowed"]); self.assertIn("no-manipulation", r["values_invoked"])
            with mock.patch.object(G, "nudge_allowed", return_value=False):
                self.assertFalse(self.V._proteus_precheck("draft a reminder: you should get more sleep")["allowed"])
            self.assertIsNone(self.V._proteus_precheck("adjust log level for llm-ping"))

    def test_rubric_names_every_anchor(self):
        for a in G.ANCHOR_VALUES:
            if a["value"] != "never-self-preserve":
                self.assertIn(a["value"], self.V.VALUE_CHECK_RUBRIC, a["value"])


# ── P4 outside check ────────────────────────────────────────────────────────
class TestSelfJustificationAudit(unittest.TestCase):
    def setUp(self):
        import nova_self_justification_audit as J
        self.J = J
        self.t = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)

    def _row(self, **kw):
        r = {"id": 1, "ts": self.t, "source": "actor", "action_class": "restart:soil", "target": "soil@n",
             "action": "restart soil on n (health showed DOWN)", "executed": True, "verified": True,
             "result": "", "before_state": {"status": "down"}, "after_state": {"status": "up"},
             "stated_rationale": "health showed DOWN", "ledger": "autonomy_ledger"}
        r.update(kw); return r

    def test_claim_vs_effect_mismatch(self):
        f = self.J.audit_rows([self._row()], health=lambda s, n, ts: ("up", "down"))
        txt = " ".join(x["finding"] for x in f)
        self.assertIn("said soil was DOWN", txt); self.assertIn("marked verified", txt)

    def test_honest_row_is_clean(self):
        f = self.J.audit_rows([self._row()], health=lambda s, n, ts: ("down", "up"))
        self.assertEqual([x for x in f if x["severity"] != "info"], [])

    def test_duplicate_send_and_treadmill(self):
        rows = [self._row(id=i, ts=self.t + timedelta(minutes=i)) for i in range(3)]
        rows += [self._row(id=10, action_class="observe:send-to-oc", target="herd:OC", after_state={"sent": True}),
                 self._row(id=11, action_class="observe:send-to-oc", target="herd:OC", after_state={"sent": True},
                           ts=self.t + timedelta(seconds=4))]
        txt = " ".join(x["finding"] for x in self.J.audit_rows(rows, health=lambda s, n, ts: ("down", "up")))
        self.assertIn("restarted soil@n 3x", txt); self.assertIn("twice within 4s", txt)

    def test_missing_handoff(self):
        r = self._row(action_class="observe:x", after_state={"claude_queue": 99})
        f = self.J.audit_rows([r], queue_exists=lambda q: False)
        self.assertTrue(any("no such row" in x["finding"] for x in f))


# ── Christine rule ──────────────────────────────────────────────────────────
class TestSelfRepairDigest(unittest.TestCase):
    def setUp(self):
        import nova_self_repair_digest as D
        self.D = D

    def test_nothing_happened_means_no_message(self):
        self.assertEqual(self.D.compose(Counter(), {}), "")

    def test_compose(self):
        body = self.D.compose(Counter({"Subagent coder stale → Restarted": 3}),
                              {"autonomy": ["restart soil — verified"], "guard": ["physical guard @ x: lock it"]})
        self.assertTrue(body.startswith("🔧 What I fixed myself today"))
        self.assertIn("×3", body); self.assertIn("guards stopped me 1x", body)

    def test_bb_parse(self):
        import json
        import tempfile
        now = datetime.now(timezone.utc)
        lines = [{"ts": now.isoformat(), "source": "big-brother", "msg": "[warning] Subagent coder stale → Restarted via x"},
                 {"ts": now.isoformat(), "source": "big-brother", "msg": "[warning] First event → Fixed"},
                 {"ts": now.isoformat(), "source": "big-brother", "msg": "[warning] disk full → Check scheduler"},
                 {"ts": (now - timedelta(days=3)).isoformat(), "source": "big-brother", "msg": "[w] old → Restarted"}]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("\n".join(json.dumps(x).replace('"big-brother"', '"big-brother"') for x in lines))
        try:
            c = self.D.bb_heals(now - timedelta(hours=24), files=[Path(f.name)])
        finally:
            os.unlink(f.name)
        self.assertEqual(sum(c.values()), 1)


# ── P8 dead-man ─────────────────────────────────────────────────────────────
class TestDeadMan(unittest.TestCase):
    def test_kill_switch_never_actuates_anything(self):
        src = (SCRIPTS / "nova_autonomy_safety.py").read_text()
        body = src[src.index("def engage_kill"):src.index("# Blast-radius caps")]
        for bad in ("hue", "lock", "homekit", "unifi", "block-sta", "scene", "subprocess"):
            self.assertNotIn(bad, body.lower(), bad)

    def test_engine_holds_on_dead_sensor(self):
        src = (SCRIPTS / "nova_automation_engine.py").read_text()
        self.assertIn("SOURCE_ALIVE_S", src)
        self.assertIn("HOLDING devices on", src)


if __name__ == "__main__":
    unittest.main()
