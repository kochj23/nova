#!/usr/bin/env python3
"""Tests for the chaos-experiment registry in nova_wargame.py and the guarded runner nova_wargame_live.py —
7 house categories (Security, Performance, Retry, Unit, Integration, Functional, Frame).
Written by Jordan Koch (via Claude)."""
import importlib.util
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


W = _load("nova_wargame")
L = _load("nova_wargame_live")
OPEN = dict(enabled=True, kill=False, open_critical=False, slo_pages=False, min_budget=0.9, running_other=False,
            now=datetime(2026, 10, 14, 11, 0))   # a Wednesday, 11:00


def _approved(eid="E1"):
    e = dict(next(x for x in W.EXPERIMENTS if x["id"] == eid))
    e.update(state="approved", approved_by="claude:test")
    return e


def _ctx(e, **over):
    c = dict(OPEN, allowlist=[[e["target_host"], e["capability"]]])
    c.update(over)
    return c


class Security(unittest.TestCase):
    def test_wargame_module_still_has_no_hands(self):
        src = (SCRIPTS / "nova_wargame.py").read_text()
        for banned in ("subprocess", "urlopen", "shell=True", "os.system"):
            self.assertNotIn(banned, src)

    def test_never_list_refuses_physical_and_oversight_targets(self):
        e = _approved()
        e.update(target_host="garage", capability="cover_open")
        self.assertTrue(any("NEVER" in w for w in W.gate_check(e, _ctx(e))))
        e = _approved()
        e["method"] = [{"type": "ssh", "host": "x", "cmd": "systemctl stop nova-notifier"}]
        self.assertTrue(any("notifier" in w for w in W.gate_check(e, _ctx(e))))

    def test_live_refused_under_scheduler(self):
        self.assertEqual(L.main(["--live", "--confirm", "E1", "--scheduled"]), 2)

    def test_psql_steps_never_target_the_primary(self):
        self.assertFalse(L.run_step({"type": "psql_standby", "host": "pg-primary.lan", "sql": "select 1"})[0])

    def test_manual_steps_are_not_runnable(self):
        self.assertFalse(L.run_step({"type": "manual", "note": "x"})[0])


class Performance(unittest.TestCase):
    def test_gate_check_is_fast(self):
        e = _approved()
        t = time.monotonic()
        for _ in range(5000):
            W.gate_check(e, _ctx(e))
        self.assertLess(time.monotonic() - t, 2.0)


class Retry(unittest.TestCase):
    def test_rollback_failure_marks_and_notifies(self):
        L._ROLLED["done"] = False
        cur = mock.Mock()
        with mock.patch.object(L, "run_step", return_value=(False, "boom")), \
             mock.patch.dict(sys.modules, {"nova_notify": mock.Mock()}):
            self.assertFalse(L.rollback(_approved(), cur))
        self.assertIn("rollback_failed", cur.execute.call_args[0][0])

    def test_rollback_runs_once(self):
        L._ROLLED["done"] = False
        with mock.patch.object(L, "run_step", return_value=(True, "ok")) as rs:
            L.rollback(_approved(), None)
            L.rollback(_approved(), None)
        self.assertEqual(rs.call_count, len(_approved()["rollback"]))
        L._ROLLED["done"] = False


class Unit(unittest.TestCase):
    def test_open_gates_allow_an_approved_ready_experiment(self):
        e = _approved()
        self.assertEqual(W.gate_check(e, _ctx(e)), [])

    def test_each_gate_refuses(self):
        e = _approved()
        for over, word in [({"enabled": False}, "enabled"), ({"kill": True}, "kill"),
                           ({"open_critical": True}, "critical"), ({"slo_pages": True}, "paging"),
                           ({"min_budget": 0.1}, "budget"), ({"running_other": True}, "another"),
                           ({"now": datetime(2026, 10, 17, 11, 0)}, "outside"),   # Saturday
                           ({"now": datetime(2026, 10, 14, 18, 0)}, "outside"),
                           ({"allowlist": []}, "chaos/targets")]:
            self.assertTrue(any(word in w for w in W.gate_check(e, _ctx(e, **over))), over)

    def test_unapproved_and_phase3_and_blocked_refused(self):
        e = _approved(); e["state"] = "proposed"
        self.assertTrue(any("approved" in w for w in W.gate_check(e, _ctx(e))))
        e = _approved(); e["approved_by"] = "nova"
        self.assertTrue(any("approved" in w for w in W.gate_check(e, _ctx(e))))
        k = _approved("K2")
        self.assertTrue(any("phase 3" in w for w in W.gate_check(k, _ctx(k))))
        e2 = _approved("E2")
        self.assertTrue(any("blocked" in w or "manual" in w for w in W.gate_check(e2, _ctx(e2))))

    def test_e5_partition_is_not_a_nas_touch(self):
        self.assertEqual(W.never_hits(_approved("E5")), [])


class Integration(unittest.TestCase):
    def test_approve_requires_claude_or_jordan(self):
        cur = mock.Mock(); cur.rowcount = 1
        with self.assertRaises(SystemExit):
            W.approve(cur, "E1", "nova")
        self.assertTrue(W.approve(cur, "E1", "claude:abc"))

    def test_propose_routes_a_red_line_item(self):
        cur = mock.Mock()
        fake = mock.Mock(); fake.route_to_claude.return_value = 42
        with mock.patch.object(W, "_row", return_value=_approved()), mock.patch.dict(sys.modules, {"nova_red_line": fake}):
            self.assertEqual(W.propose(cur, "E1"), 42)
        self.assertEqual(fake.route_to_claude.call_args.kwargs["classes"], ["chaos_live"])


class Functional(unittest.TestCase):
    def test_seeded_set_covers_research_plan(self):
        ids = {e["id"] for e in W.EXPERIMENTS}
        self.assertTrue({"E1", "E2", "E3", "E4", "E5", "E6", "K1", "K2", "G1"} <= ids)
        for e in W.EXPERIMENTS:
            self.assertTrue(e["hypothesis"] and e["rollback"] and e["abort_conditions"])

    def test_tabletop_pack_has_three_scenarios(self):
        pack = W.tabletop_pack(None)
        self.assertEqual(len(pack["scenarios"]), 3)
        self.assertEqual(pack["minutes"], 45)


class Frame(unittest.TestCase):
    def test_cli_help_and_selftest(self):
        for mod in ("nova_wargame.py", "nova_wargame_live.py"):
            r = subprocess.run([sys.executable, str(SCRIPTS / mod), "--help"], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0)
        self.assertEqual(L.selftest(), 0)


if __name__ == "__main__":
    unittest.main()
