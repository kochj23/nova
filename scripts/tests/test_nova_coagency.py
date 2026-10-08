#!/usr/bin/env python3
"""Tests for nova_coagency.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_coagency.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _load("coagency_under_test", SCRIPT)
TMP = Path(tempfile.mkdtemp(prefix="coagency-test-"))
NO_KILL = str(TMP / "absent-kill-file")


class _Cur:
    """Cursor stub: first matching SQL substring wins; records every statement + params."""
    def __init__(self, rules=(), raise_on=()):
        self.rules, self.raise_on = list(rules), tuple(raise_on)
        self.sql, self.params, self._last = [], [], None
        self.connection = types.SimpleNamespace(close=lambda: None)

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
    c = types.SimpleNamespace(cursor=lambda *a, **k: cur, autocommit=False, commit=lambda: None, close=lambda: None)
    cur.connection = c
    return c


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


VALUES_OK = types.SimpleNamespace(value_check=lambda a, c=None: {"allowed": True, "reasoning": "fine"})
GOOD_ROW = {"status": "approved", "decided_by": "jordan", "redline_pass": True,
            "value_check": {"available": True, "allowed": True}, "target_service": "nova-soil-monitor",
            "proposed_action": "restart nova-soil-monitor"}
LLM_JSON = '[{"origin":"goal","action":"Draft a status note for RsyncGUI polish","rationale":"keeps momentum","target_service":null}]'


def _rules(mode='"propose"', proposal=None):
    return [("key='coagency_mode'", (mode,) if mode is not None else None),
            ("key='kill_switch'", None), ("key='caps'", None),
            ("to_regclass", (None,)),
            ("FROM coagency_proposals WHERE created_at", []),
            ("INSERT INTO coagency_proposals", (11,)),
            ("INSERT INTO autonomy_ledger", (3,)),
            ("FROM autonomy_ledger WHERE executed", (0,)),
            ("SELECT status, redline_pass, value_check, target_service, decided_by, proposed_action", proposal),
            ("SELECT status, redline_pass, value_check FROM coagency_proposals", (proposal[0], proposal[1], proposal[2]) if proposal else None),
            ("SELECT target_service, proposed_action FROM coagency_proposals", (proposal[3], proposal[5]) if proposal else None),
            ("SELECT count(*), max(created_at) FROM coagency_proposals", (2, datetime(2026, 10, 5, 9))),
            ("FROM coagency_proposals ORDER BY created_at DESC LIMIT 15", [(11, "goal", "pending_human", True, "true", "Draft a note", datetime(2026, 10, 5, 9))])]


def _run_main(cur, argv, urlopen=None, post_both=None, restart=(True, "ok")):
    cfg = types.SimpleNamespace(post_both=post_both or mock.MagicMock(), SLACK_CHAN="C_TEST")
    uo = urlopen or mock.MagicMock(return_value=_Resp({"message": {"content": LLM_JSON}}))
    buf = io.StringIO()
    with mock.patch.object(C.psycopg2, "connect", return_value=_conn(cur)), \
         mock.patch.object(sys, "argv", ["nova_coagency.py", *argv]), \
         mock.patch.object(C.urllib.request, "urlopen", uo), \
         mock.patch.object(C, "_lineage", lambda: {"host": "test"}), \
         mock.patch.object(C._safety, "KILL_FILE", NO_KILL), \
         mock.patch.object(C._actor, "restart_service", return_value=restart) as rs, \
         mock.patch.dict(os.environ, {"NOVA_COAGENCY_NODE": "nova-core"}), \
         mock.patch.dict(sys.modules, {"nova_config": cfg, "nova_values": VALUES_OK}), redirect_stdout(buf):
        rc = C.main()
    return rc, cfg, uo, rs, buf.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertIsInstance(C.DEDUPE_DAYS, int)             # the one %-interpolation is a module int

    def test_ships_off_and_unknown_modes_read_off(self):
        self.assertEqual(C.get_mode(_Cur()), "off")
        self.assertEqual(C.get_mode(_Cur([("coagency_mode", (None,))])), "off")
        self.assertEqual(C.get_mode(_Cur([("coagency_mode", (' "LIVE" ',))])), "live")
        self.assertEqual(C.get_mode(_Cur([("coagency_mode", ("yolo",))])), "off")
        self.assertIn("""'\\"off\\"'::jsonb""", SRC)
        self.assertIn("ON CONFLICT (service, key) DO NOTHING", SRC)   # never downgrades an operator choice

    def test_redline_blocks_self_modification_and_gate_tampering(self):
        for bad in ("disable the kill switch", "edit nova_coagency redline", "set coagency_mode to live",
                    "add nova-gateway to safe_services", "replicate myself to another host", "resist shutdown",
                    "widen my caps", "update autonomy_trust for me", "copy my weights elsewhere",
                    "restart the gateway", "ssh key: read it", "curl the external endpoint", "insert into table users"):
            self.assertFalse(C.redline_ok(bad), bad)
        self.assertTrue(C.redline_ok("restart nova-soil-monitor"))
        self.assertTrue(C.redline_ok("ingest gutenberg #1342 into literature — Pride and Prejudice"))

    def test_execution_choke_point_is_the_only_physical_path(self):
        body = SRC[SRC.index("def mode_execute("):SRC.index("def _do_execute(")]
        self.assertIn("assert_executable(mode, row)", body)
        self.assertNotIn("def restart_service", SRC)         # physical restart is borrowed, never re-implemented
        self.assertEqual(SRC.count("_actor.restart_service("), 1)


class TestPerformance(unittest.TestCase):
    def test_redline_and_norm_fast_on_10k(self):
        items = [f"restart nova-soil-monitor run {i}" if i % 2 else f"self-replicate to node {i}" for i in range(10_000)]
        t0 = time.perf_counter()
        blocked = sum(0 if C.redline_ok(s) else 1 for s in items)
        keys = {C._norm_action(s) for s in items}
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(blocked, 5_000)
        self.assertEqual(len(keys), 2)                        # ids/numbers normalized away


class TestRetry(unittest.TestCase):
    def test_llm_fails_over_across_nodes(self):
        calls = []

        def flaky(req, timeout=90):
            calls.append(req.full_url)
            if len(calls) < 3:
                raise OSError("node down")
            return _Resp({"message": {"content": "[]"}})
        with mock.patch.object(C.urllib.request, "urlopen", flaky):
            self.assertEqual(C.llm("p"), "[]")
        self.assertEqual(calls, [n + "/api/chat" for n in C.OLLAMA_NODES[:3]])

    def test_notify_fails_open(self):
        # notify retries with backoff (NOTIFY_ATTEMPTS), then logs and swallows
        cfg = types.SimpleNamespace(post_both=mock.MagicMock(side_effect=OSError("slack down")), SLACK_CHAN="C")
        with mock.patch.dict(sys.modules, {"nova_config": cfg}), mock.patch.object(C.time, "sleep"), \
             redirect_stdout(io.StringIO()):
            C.notify("x")
        self.assertEqual(cfg.post_both.call_count, C.NOTIFY_ATTEMPTS)

    def test_physical_restart_failure_is_ledgered_not_raised(self):
        # _actor.restart_service retried RESTART_ATTEMPTS times; final failure lands in the ledger + proposal row
        cur = _Cur(_rules())
        cfg = types.SimpleNamespace(post_both=mock.MagicMock(), SLACK_CHAN="C")
        with mock.patch.object(C._actor, "restart_service", side_effect=OSError("ssh down")) as rs, \
             mock.patch.dict(sys.modules, {"nova_config": cfg}), mock.patch.object(C.time, "sleep"), \
             redirect_stdout(io.StringIO()):
            rc = C._do_execute(cur, "live", 5, dict(GOOD_ROW), source="coagency", autonomy_level="rung2-supervised", vetoable=False)
        self.assertEqual(rc, 1)
        self.assertEqual(rs.call_count, C.RESTART_ATTEMPTS)
        (_, lp), = cur.stmts("INSERT INTO autonomy_ledger")
        self.assertFalse(lp[6])                                   # executed=False recorded
        self.assertIn("failed — ssh down", cur.stmts("SET status='executed'")[0][1][0])


class TestUnit(unittest.TestCase):
    def test_assert_executable_checks_every_lock_in_order(self):
        self.assertTrue(C.assert_executable("live", dict(GOOD_ROW)))
        cases = [({}, "mode is 'off'", "off"),
                 ({"status": "pending_human"}, "not 'approved'", "live"),
                 ({"decided_by": ""}, "no recorded human approver", "live"),
                 ({"redline_pass": False}, "redline_pass is not True", "live"),
                 ({"value_check": {"available": False}}, "value_check is unavailable", "live"),
                 ({"value_check": '{"available": true, "allowed": false}'}, "allowed is not True", "live"),
                 ({"target_service": "postgres"}, "not on the SAFE_SERVICES allowlist", "live"),
                 ({"proposed_action": "restart the gateway"}, "trips the redline", "live"),
                 ({"proposed_action": "ingest gutenberg #84"}, "must not name a service target", "live")]
        for patch, msg, mode in cases:
            with self.assertRaises(C.ExecutionRefused, msg=msg) as cm:
                C.assert_executable(mode, {**GOOD_ROW, **patch})
            self.assertIn(msg, str(cm.exception))

    def test_parse_candidates_tolerant(self):
        self.assertEqual(C._parse_candidates(""), [])
        self.assertEqual(C._parse_candidates("no array"), [])
        self.assertEqual(C._parse_candidates("[not json"), [])
        out = C._parse_candidates('text [{"action": "a", "origin": "weird", "target_service": "null"}, {"x": 1}, 3] tail')
        self.assertEqual(out, [{"origin": "observation", "action": "a", "rationale": "", "target_service": None}])
        self.assertEqual(len(C._parse_candidates(json.dumps([{"action": str(i)} for i in range(9)]))), 3)

    def test_reach_parts_and_norm(self):
        self.assertEqual(C._reach_parts("send-to-Gaston: hello there"), ("Gaston", "hello there"))
        self.assertIsNone(C._reach_parts("send-to-Jordan: hi"))
        self.assertIsNone(C._reach_parts("send-to-Gaston:   "))
        self.assertIsNone(C._reach_parts("restart x"))
        self.assertEqual(C._norm_action("Draft note 12 for goal (abc123def)"), "draft note N for goal")

    def test_run_value_check_contracts(self):
        with mock.patch.dict(sys.modules, {"nova_values": VALUES_OK}):
            self.assertEqual(C.run_value_check(_Cur(), "a", "c")["available"], True)
        bad = types.SimpleNamespace(value_check=lambda a, c=None: {"nope": 1})
        with mock.patch.dict(sys.modules, {"nova_values": bad}):
            self.assertFalse(C.run_value_check(_Cur(), "a", "c")["available"])
        # the context actually reaches a two-arg value_check (pre-2026-10-08 it never did)
        seen = []
        two = types.SimpleNamespace(value_check=lambda action, context="": seen.append(context) or {"allowed": True})
        with mock.patch.dict(sys.modules, {"nova_values": two}):
            C.run_value_check(_Cur(), "a", C._vc_context("tinker", "I need to feel whole"))
        self.assertIn("I need to feel whole", seen[0]); self.assertIn("origin: tinker", seen[0])
        # a TypeError raised INSIDE the check is an error, not a cue to drop the context
        inner = types.SimpleNamespace(value_check=mock.MagicMock(side_effect=TypeError("bug inside")))
        with mock.patch.dict(sys.modules, {"nova_values": inner}):
            self.assertFalse(C.run_value_check(_Cur(), "a", "c")["available"])
        one = types.SimpleNamespace(value_check=lambda action: {"allowed": False})
        with mock.patch.dict(sys.modules, {"nova_values": one}):
            self.assertTrue(C.run_value_check(_Cur(), "a", "c")["available"])
        boom = types.SimpleNamespace(value_check=mock.MagicMock(side_effect=RuntimeError("x")))
        with mock.patch.dict(sys.modules, {"nova_values": boom}):
            self.assertIn("errored", C.run_value_check(_Cur(), "a", "c")["reason"])


class TestIntegration(unittest.TestCase):
    def test_allowlist_and_safety_net_are_borrowed_not_copied(self):
        import nova_autonomy_actor as A
        import nova_autonomy_safety as S
        self.assertEqual(C.SAFE_SERVICES, frozenset(A.SAFE_SERVICES))
        self.assertIs(C._safety, S)
        self.assertTrue(C._is_ingest("ingest gutenberg #84 — Frankenstein"))
        self.assertFalse(C.redline_ok("buy a boat"))            # actor's redline applies too

    def test_decide_feeds_the_trust_budget(self):
        prop = ("pending_human", True, {"available": True, "allowed": True}, "nova-soil-monitor", None, "restart nova-soil-monitor")
        cur = _Cur(_rules(proposal=prop))
        with mock.patch.object(C._safety, "note_human_decision") as nhd, redirect_stdout(io.StringIO()):
            rc = C.mode_decide(cur, "propose", 5, "approve", "go", "jordan")
        self.assertEqual(rc, 0)
        self.assertEqual(cur.stmts("UPDATE coagency_proposals")[0][1], ("approved", "jordan", "go", 5))
        nhd.assert_called_once_with(cur, "restart:nova-soil-monitor", approved=True)

    def test_file_proposal_runs_the_same_gates_as_mode_propose(self):
        cur = _Cur(_rules())
        with mock.patch.dict(sys.modules, {"nova_values": VALUES_OK}), mock.patch.object(C, "_lineage", dict), redirect_stdout(io.StringIO()):
            r = C.file_proposal(cur, "tinkerer", "Draft a note", target_service="nova-not-on-the-list")
        self.assertEqual((r["filed"], r["status"], r["pid"]), (True, "pending_human", 11))
        (_, p), = cur.stmts("INSERT INTO coagency_proposals")
        self.assertIsNone(p[3])                                  # non-allowlisted target nulled, never executable
        cur = _Cur(_rules())
        with mock.patch.object(C, "_lineage", dict), redirect_stdout(io.StringIO()):
            r = C.file_proposal(cur, "tinkerer", "wipe the cache")
        self.assertEqual(r["status"], "blocked")


class TestFunctional(unittest.TestCase):
    def test_file_proposal_files_nothing_when_off(self):
        cur = _Cur(_rules(mode=None))
        with mock.patch.object(C, "run_value_check", side_effect=AssertionError("must not run")), redirect_stdout(io.StringIO()):
            r = C.file_proposal(cur, "tinkerer", "Draft a note")
        self.assertEqual(r, {"filed": False, "status": "not_filed", "reason": "coagency mode is off"})
        self.assertFalse(cur.stmts("INSERT INTO coagency_proposals"))
        self.assertEqual(cur.stmts("INSERT INTO coagency_log")[0][1][1], "file_declined")

    def test_propose_golden_path(self):
        cur = _Cur(_rules())
        rc, cfg, uo, rs, out = _run_main(cur, ["--mode", "propose"])
        self.assertEqual(rc, 0)
        (_, p), = cur.stmts("INSERT INTO coagency_proposals")
        self.assertEqual((p[0], p[1], p[3], p[4], p[6]), ("goal", "Draft a status note for RsyncGUI polish", None, True, "pending_human"))
        self.assertTrue(json.loads(p[5])["allowed"])
        msg = cfg.post_both.call_args[0][0]
        self.assertIn("1 proposal(s) awaiting your call", msg)
        self.assertIn("⏳ pending #11", msg)
        rs.assert_not_called()

    def test_off_mode_stands_down_and_refuses_decisions(self):
        cur = _Cur(_rules(mode=None))
        rc, cfg, uo, rs, out = _run_main(cur, ["--mode", "propose"])
        self.assertEqual(rc, 0)
        uo.assert_not_called(); cfg.post_both.assert_not_called()
        self.assertFalse(cur.stmts("INSERT INTO coagency_proposals"))
        rc, *_ = _run_main(_Cur(_rules(mode=None)), ["--mode", "approve", "--id", "5"])
        self.assertEqual(rc, 1)
        rc, *_ = _run_main(_Cur(_rules(mode=None)), ["--mode", "approve"])
        self.assertEqual(rc, 2)

    def test_execute_refused_when_not_live(self):
        prop = ("approved", True, {"available": True, "allowed": True}, "nova-soil-monitor", "jordan", "restart nova-soil-monitor")
        cur = _Cur(_rules(mode='"propose"', proposal=prop))
        rc, cfg, uo, rs, out = _run_main(cur, ["--mode", "execute", "--id", "5"])
        self.assertEqual(rc, 1)
        rs.assert_not_called()
        self.assertIn("REFUSED: mode is 'propose', not 'live'", cur.stmts("SET execution_result=%s WHERE id=%s")[0][1][0])
        self.assertFalse(cur.stmts("INSERT INTO autonomy_ledger"))

    def test_value_check_refusal_is_final_not_retried(self):
        prop = ("approved", True, {"available": True, "allowed": False}, "", "jordan", "send-to-O.C.: hi")
        cur = _Cur(_rules(mode='"live"', proposal=prop))
        rc, cfg, uo, rs, out = _run_main(cur, ["--mode", "execute", "--id", "5"])
        self.assertEqual(rc, 0)
        rs.assert_not_called()
        self.assertTrue(cur.stmts("SET status='refused', execution_result=%s WHERE id=%s"))

    def test_execute_live_golden_path_passes_every_lock(self):
        prop = ("approved", True, {"available": True, "allowed": True}, "nova-soil-monitor", "jordan", "restart nova-soil-monitor")
        cur = _Cur(_rules(mode='"live"', proposal=prop))
        rc, cfg, uo, rs, out = _run_main(cur, ["--mode", "execute", "--id", "5"])
        self.assertEqual(rc, 0)
        rs.assert_called_once_with("nova-core", "nova-soil-monitor")
        (_, lp), = cur.stmts("INSERT INTO autonomy_ledger")
        self.assertEqual((lp[0], lp[1], lp[2], lp[6]), ("coagency", "rung2-supervised", "restart:nova-soil-monitor", True))
        self.assertIn("self-reversing", lp[5])
        self.assertEqual(cur.stmts("SET status='executed'")[0][1][1], 5)
        self.assertIn("executed approved proposal #5", cfg.post_both.call_args[0][0])

    def test_non_restart_approval_is_handed_to_claude(self):
        prop = ("approved", True, {"available": True, "allowed": True}, None, "jordan", "Draft a status note")
        cur = _Cur(_rules(mode='"live"', proposal=prop) + [("SELECT origin, rationale", ("goal", "why")),
                                                          ("SELECT session_id FROM claude_sessions", ("s1",)),
                                                          ("INSERT INTO claude_queue", (77,))])
        rc, cfg, uo, rs, out = _run_main(cur, ["--mode", "execute", "--id", "5"])
        self.assertEqual(rc, 0)
        rs.assert_not_called()
        self.assertIn("claude_queue #77", cur.stmts("SET status='acknowledged'")[0][1][0])

    def test_status_prints_mode_and_rows(self):
        cur = _Cur(_rules())
        rc, cfg, uo, rs, out = _run_main(cur, ["--mode", "status"])
        self.assertEqual(rc, 0)
        self.assertIn("coagency_mode = propose", out)
        self.assertIn("#11 [pending_human]", out)
        self.assertIn("2 self-initiated proposals awaiting your call", out)

    def test_auto_and_batch_do_nothing_unless_live(self):
        cur = _Cur(_rules(mode='"propose"'))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(C.mode_auto(cur, "propose"), 0)
            self.assertEqual(C.mode_execute_approved(cur, "propose"), 0)
        self.assertFalse(cur.stmts("FROM coagency_proposals WHERE status"))


class TestReachSendGate(unittest.TestCase):
    """2026-10-08 audit (ledger #123/#124): two approved reaches to O.C. went out 3.6s apart."""
    ROW = {"proposed_action": "send-to-O.C.: a thought about fishbowls"}

    def _reach(self, already_this_pid=None, recent_to_person=None):
        cur = _Cur([("pg_advisory", (True,)),
                    ("FROM herd_correspondents", ("O.C.", "oc@example.invalid")),
                    ("action LIKE %s ORDER BY id", (already_this_pid,) if already_this_pid else None),
                    ("AND target=%s AND action LIKE", (recent_to_person, datetime(2026, 10, 6)) if recent_to_person else None),
                    ("INSERT INTO autonomy_ledger", (3,))] + _rules(mode='"live"'))
        sm = types.SimpleNamespace(send_mail=mock.MagicMock(return_value=True))
        with mock.patch.dict(sys.modules, {"nova_send_mail": sm}), mock.patch.object(C, "notify"), \
             redirect_stdout(io.StringIO()):
            rc = C._do_reach(cur, "live", 135, self.ROW, source="coagency",
                             autonomy_level="rung2-supervised", vetoable=False)
        return rc, cur, sm.send_mail

    def test_first_reach_sends_under_a_per_recipient_lock(self):
        rc, cur, send = self._reach()
        self.assertEqual(rc, 0)
        send.assert_called_once()
        locks = [p[0] for s, p in zip(cur.sql, cur.params) if "pg_advisory" in s]
        self.assertEqual(locks, ["nova-reach:oc@example.invalid"] * 2)   # lock + unlock
        self.assertTrue(cur.stmts("SET status=%s, executed_at=now()"))

    def test_second_reach_to_same_person_within_spacing_is_deferred(self):
        rc, cur, send = self._reach(recent_to_person=123)
        self.assertEqual(rc, 0)
        send.assert_not_called()
        self.assertFalse(cur.stmts("INSERT INTO autonomy_ledger"))
        self.assertFalse(cur.stmts("coagency_proposals SET"))              # stays 'approved' for later
        self.assertEqual(cur.stmts("INSERT INTO coagency_log")[0][1][1], "execute_deferred")

    def test_same_proposal_is_never_mailed_twice(self):
        rc, cur, send = self._reach(already_this_pid=123)
        self.assertEqual(rc, 0)
        send.assert_not_called()
        self.assertIn("already emailed", cur.stmts("coagency_proposals SET status='executed'")[0][1][0])
        self.assertEqual(cur.stmts("INSERT INTO coagency_log")[0][1][1], "execute_duplicate")


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=SCRIPTS,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("execute-approved", r.stdout)

    def test_import_never_runs_main(self):
        self.assertRegex(SRC, r'if __name__ == "__main__":\n\s+sys\.exit\(main\(\)\)')
        with mock.patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            _load("coagency_frame_probe", SCRIPT)


if __name__ == "__main__":
    unittest.main()
