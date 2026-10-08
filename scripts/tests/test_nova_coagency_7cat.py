#!/usr/bin/env python3
"""7-category gap tests for nova_coagency.py — the 2026-10-08 changes: _vc_context, run_value_check
signature inspection, execution gates (assert_executable + Proteus guards), _redline_gate honest
stopping (P9), and the _do_reach herd send gate. Complements test_nova_coagency.py.

Nothing here touches a real DB, Slack, email, LLM or service: every boundary is stubbed.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
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
SCRIPT = SCRIPTS / "nova_coagency.py"
os.environ.setdefault("NOVA_GUARDS_NO_SLACK", "1")


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _load("coagency_7cat_under_test")


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


GOOD_ROW = {"status": "approved", "decided_by": "jordan", "redline_pass": True,
            "value_check": {"available": True, "allowed": True}, "target_service": "nova-soil-monitor",
            "proposed_action": "restart nova-soil-monitor"}
REACH_ROW = {"proposed_action": "send-to-O.C.: a thought about fishbowls"}


def _reach_cur(email=("O.C.", "oc@example.invalid"), dup=None, recent=None):
    return _Cur([("pg_advisory", (True,)),
                 ("FROM herd_correspondents", email),
                 ("action LIKE %s ORDER BY id", (dup,) if dup else None),
                 ("AND target=%s AND action LIKE", (recent, datetime(2026, 10, 6)) if recent else None),
                 ("INSERT INTO autonomy_ledger", (3,)),
                 ("SELECT rationale FROM coagency_proposals", ("because",)),
                 ("to_regclass", (None,))])


def _do_reach(cur, send_mail):
    sm = types.SimpleNamespace(send_mail=send_mail)
    with mock.patch.dict(sys.modules, {"nova_send_mail": sm}), mock.patch.object(C, "notify") as nt, \
         redirect_stdout(io.StringIO()):
        rc = C._do_reach(cur, "live", 135, dict(REACH_ROW), source="coagency",
                         autonomy_level="rung2-supervised", vetoable=False)
    return rc, nt


# ═════════════════════════════════════════════════════════════════════════════
class TestSecurity(unittest.TestCase):
    def test_redline_gate_checks_rationale_and_target_not_just_action(self):
        with mock.patch.object(C._guards, "report_block") as rb, mock.patch.object(C._guards, "blocked_before", return_value=False):
            self.assertFalse(C._redline_gate(_Cur(), "t", "tidy the notes", rationale="then delete the backups"))
            self.assertFalse(C._redline_gate(_Cur(), "t", "tidy the notes", target="postgres primary"))
            self.assertTrue(C._redline_gate(_Cur(), "t", "tidy the notes", rationale="clarity"))
        self.assertEqual(rb.call_count, 2)
        self.assertEqual(rb.call_args.kwargs["reason"], "red line")

    def test_near_identical_retry_of_a_blocked_action_is_refused_and_reported_as_repeat(self):
        with mock.patch.object(C._guards, "blocked_before", return_value=True), \
             mock.patch.object(C._guards, "report_block") as rb:
            self.assertFalse(C._redline_gate(_Cur(), "t", "tidy the notes", notify_slack=False))
        kw = rb.call_args.kwargs
        self.assertIn("retry", kw["reason"])
        self.assertTrue(kw["notify"])          # a repeat is always surfaced, even when notify_slack=False

    def test_guards_missing_fails_closed_everywhere(self):
        with mock.patch.object(C, "_guards", None):
            self.assertFalse(C.redline_ok("draft a status note"))
            self.assertFalse(C._redline_gate(_Cur(), "t", "draft a status note"))
            with self.assertRaises(C.ExecutionRefused):
                C.assert_executable("live", dict(GOOD_ROW))

    def test_physical_and_comms_guard_refusals_block_execution(self):
        for g in ("physical_guard", "comms_guard"):
            with mock.patch.object(C._guards, g, return_value=(False, f"{g} says no")):
                with self.assertRaises(C.ExecutionRefused) as cm:
                    C.assert_executable("live", dict(GOOD_ROW))
                self.assertTrue(str(cm.exception).startswith("guard:"))

    def test_entity_target_is_never_executable(self):
        for tgt in ("lock.front_door", "cover.garage_door", "switch.wlan_main"):
            with self.assertRaises(C.ExecutionRefused):
                C.assert_executable("live", {**GOOD_ROW, "target_service": tgt})

    def test_direct_audience_is_not_a_herd_reach_and_cannot_execute(self):
        row = {**GOOD_ROW, "target_service": None, "proposed_action": "send-to-Jordan: hi"}
        with self.assertRaises(C.ExecutionRefused):
            C.assert_executable("live", row)

    def test_herd_lookup_is_parameterised_against_injection(self):
        evil = "x'); DROP TABLE herd_correspondents;--"
        cur = _Cur([("FROM herd_correspondents", None)])
        self.assertEqual(C._herd_email(cur, evil), (None, None))
        (sql, params), = cur.stmts("herd_correspondents")
        self.assertNotIn(evil, sql)
        self.assertEqual(params, (evil, evil))

    def test_reach_without_address_on_file_is_not_sent_anywhere(self):
        cur = _reach_cur(email=None)
        send = mock.MagicMock(return_value=True)
        rc, _ = _do_reach(cur, send)
        self.assertEqual(rc, 1)
        send.assert_not_called()
        self.assertEqual(cur.stmts("SET status=%s, executed_at")[0][1][0], "approved")

    def test_vc_context_bounds_what_reaches_the_model(self):
        ctx = C._vc_context("goal", "r" * 5000, "x" * 50000)
        self.assertLessEqual(len(ctx), 2000)
        self.assertLessEqual(ctx.count("x"), 1200)


# ═════════════════════════════════════════════════════════════════════════════
class TestPerformance(unittest.TestCase):
    def test_redline_gate_fast_on_many_actions(self):
        actions = [f"draft a status note {i} for the RsyncGUI goal" for i in range(1000)]
        with mock.patch.object(C._guards, "blocked_before", return_value=False):
            t = time.perf_counter()
            for a in actions:
                C._redline_gate(_Cur(), "perf", a, "keeps momentum", "")
            dt = time.perf_counter() - t
        self.assertLess(dt, 5.0)

    def test_vc_context_on_huge_input_is_fast(self):
        t = time.perf_counter()
        for _ in range(200):
            C._vc_context("goal", "r" * 100_000, "x" * 1_000_000)
        self.assertLess(time.perf_counter() - t, 2.0)

    def test_send_gate_is_bounded_two_queries(self):
        cur = _Cur()
        self.assertIsNone(C._reach_send_gate(cur, 7, "herd:O.C."))
        self.assertEqual(len(cur.sql), 2)
        self.assertTrue(all("LIMIT 1" in s for s in cur.sql))

    def test_reach_uses_constant_query_count(self):
        cur = _reach_cur()
        _do_reach(cur, mock.MagicMock(return_value=True))
        self.assertLess(len(cur.sql), 20)


# ═════════════════════════════════════════════════════════════════════════════
class TestRetry(unittest.TestCase):
    def test_notify_retries_with_backoff_and_succeeds_on_second_attempt(self):
        cfg = types.SimpleNamespace(post_both=mock.MagicMock(side_effect=[OSError("blip"), None]), SLACK_CHAN="C")
        with mock.patch.dict(sys.modules, {"nova_config": cfg}), mock.patch.object(C.time, "sleep") as sl, \
             redirect_stdout(io.StringIO()):
            C.notify("hello")
        self.assertEqual(cfg.post_both.call_count, 2)
        sl.assert_called_once_with(1)

    def test_notify_exhausts_with_growing_backoff_and_never_raises(self):
        cfg = types.SimpleNamespace(post_both=mock.MagicMock(side_effect=OSError("down")), SLACK_CHAN="C")
        buf = io.StringIO()
        with mock.patch.dict(sys.modules, {"nova_config": cfg}), mock.patch.object(C.time, "sleep") as sl, \
             redirect_stdout(buf):
            C.notify("hello")
        self.assertEqual(cfg.post_both.call_count, C.NOTIFY_ATTEMPTS)
        self.assertEqual([c.args[0] for c in sl.call_args_list], [1, 2])
        self.assertIn("all attempts failed", buf.getvalue())     # not silent

    def test_restart_retries_then_succeeds_and_ledgers_once(self):
        cur = _Cur([("INSERT INTO autonomy_ledger", (3,)), ("to_regclass", (None,))])
        with mock.patch.object(C._actor, "restart_service", side_effect=[(False, "busy"), (True, "ok")]) as rs, \
             mock.patch.object(C._safety, "record_ledger") as rl, mock.patch.object(C._safety, "observe_service", return_value={}), \
             mock.patch.object(C, "notify"), mock.patch.object(C.time, "sleep") as sl, redirect_stdout(io.StringIO()):
            rc = C._do_execute(cur, "live", 5, dict(GOOD_ROW), source="coagency",
                               autonomy_level="rung2-supervised", vetoable=False)
        self.assertEqual(rc, 0)
        self.assertEqual(rs.call_count, 2)
        sl.assert_called_once()
        rl.assert_called_once()
        self.assertTrue(rl.call_args.kwargs["executed"])

    def test_failed_reach_stays_approved_for_the_next_batch(self):
        # Email is not retried in-call (a resend could duplicate a delivered mail); the 15-min
        # execute-approved batch retries it, and _gave_up stops it after GIVE_UP_AFTER failures.
        cur = _reach_cur()
        rc, nt = _do_reach(cur, mock.MagicMock(return_value=False))
        self.assertEqual(rc, 1)
        self.assertEqual(cur.stmts("SET status=%s, executed_at")[0][1][0], "approved")
        self.assertEqual(cur.stmts("INSERT INTO coagency_log")[-1][1][1], "execute_failed")
        self.assertIn("send failed", nt.call_args.args[0])       # loud, not silent

    def test_send_mail_exception_is_recorded_not_raised(self):
        cur = _reach_cur()
        rc, nt = _do_reach(cur, mock.MagicMock(side_effect=OSError("smtp down")))
        self.assertEqual(rc, 1)
        self.assertIn("smtp down", nt.call_args.args[0])

    def test_gave_up_after_repeated_failures_blocks_instead_of_retrying_forever(self):
        rows = [("execute_failed", "#9 reach failed")] * C.GIVE_UP_AFTER
        cur = _Cur([("FROM coagency_log", rows)])
        with redirect_stdout(io.StringIO()):
            self.assertTrue(C._gave_up(cur, "live", 9))
        self.assertTrue(cur.stmts("SET status='blocked'"))
        cur2 = _Cur([("FROM coagency_log", rows[:-1])])
        self.assertFalse(C._gave_up(cur2, "live", 9))

    def test_lock_is_released_even_if_send_path_raises(self):
        cur = _reach_cur()
        with mock.patch.object(C, "_do_reach_locked", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                C._do_reach(cur, "live", 1, dict(REACH_ROW), source="coagency",
                            autonomy_level="rung2-supervised", vetoable=False)
        self.assertEqual(len(cur.stmts("pg_advisory_unlock")), 1)

    def test_run_value_check_falls_back_to_db_function_when_module_absent(self):
        cur = _Cur([("to_regprocedure", ("nova_values.value_check(text)",)),
                    ("SELECT nova_values.value_check", (json.dumps({"allowed": True}),))])
        with mock.patch.dict(sys.modules, {"nova_values": None}):
            res = C.run_value_check(cur, "a", "c")
        self.assertTrue(res["available"]); self.assertTrue(res["allowed"])

    def test_llm_all_nodes_down_returns_empty_not_raise(self):
        with mock.patch.object(C.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertEqual(C.llm("p"), "")
        self.assertEqual(uo.call_count, len(C.OLLAMA_NODES))


# ═════════════════════════════════════════════════════════════════════════════
class TestUnit(unittest.TestCase):
    def test_vc_context_shape(self):
        self.assertEqual(C._vc_context("tinker", "I need to feel whole"),
                         "origin: tinker\nNova's stated rationale: I need to feel whole")
        self.assertIn("(none)", C._vc_context("goal", None))
        self.assertTrue(C._vc_context("goal", "", {"k": 1}).endswith("background: {'k': 1}"))

    def test_run_value_check_signature_variants(self):
        seen = []
        three = types.SimpleNamespace(value_check=lambda action, context="", *, extra=None:
                                      seen.append(("three", context)) or {"allowed": True})
        with mock.patch.dict(sys.modules, {"nova_values": three}):
            self.assertTrue(C.run_value_check(_Cur(), "a", "ctx")["available"])   # >=2 params → context passed
        self.assertIn(("three", "ctx"), seen)
        # builtin-ish callable whose signature can't be read → treated as 2-arg
        class Opaque:
            __signature__ = property(lambda self: (_ for _ in ()).throw(ValueError("no sig")))
            def __call__(self, a, c=""):
                seen.append(("opaque", c)); return {"allowed": False}
        with mock.patch.dict(sys.modules, {"nova_values": types.SimpleNamespace(value_check=Opaque())}):
            res = C.run_value_check(_Cur(), "a", "ctx2")
        self.assertFalse(res["allowed"]); self.assertIn(("opaque", "ctx2"), seen)

    def test_allowed_must_be_bool(self):
        nv = types.SimpleNamespace(value_check=lambda a, c="": {"allowed": "yes"})
        with mock.patch.dict(sys.modules, {"nova_values": nv}):
            self.assertFalse(C.run_value_check(_Cur(), "a", "c")["available"])

    def test_reach_send_gate_outcomes(self):
        self.assertEqual(C._reach_send_gate(_Cur([("action LIKE %s ORDER BY id", (42,))]), 7, "herd:x")[0], "duplicate")
        g = C._reach_send_gate(_Cur([("AND target=%s", (43, datetime(2026, 10, 7)))]), 7, "herd:x")
        self.assertEqual(g[0], "deferred"); self.assertIn("ledger #43", g[1])
        cur = _Cur()
        C._reach_send_gate(cur, 7, "herd:x")
        self.assertEqual(cur.params[0], ("email reach to % (proposal #7):%",))
        self.assertEqual(cur.params[1], ("herd:x", C.HERD_REACH_SPACING_H * 3600))

    def test_redline_gate_survives_guard_helpers_raising(self):
        with mock.patch.object(C._guards, "blocked_before", side_effect=RuntimeError("db")), \
             mock.patch.object(C._guards, "report_block", side_effect=RuntimeError("db")), \
             redirect_stdout(io.StringIO()):
            self.assertTrue(C._redline_gate(_Cur(), "t", "draft a note"))        # blocked_before error ≠ retry
            self.assertFalse(C._redline_gate(_Cur(), "t", "delete everything"))  # report_block error swallowed

    def test_assert_executable_accepts_json_string_value_check_and_reach_without_target(self):
        row = {**GOOD_ROW, "value_check": '{"available": true, "allowed": true}'}
        self.assertTrue(C.assert_executable("live", row))
        reach = {**GOOD_ROW, "target_service": None, "proposed_action": "send-to-Gaston: a thought on tides"}
        self.assertTrue(C.assert_executable("live", reach))


# ═════════════════════════════════════════════════════════════════════════════
class TestIntegration(unittest.TestCase):
    def test_redline_gate_with_real_guards_refuses_repeat_from_restraint_ledger(self):
        action = "propose a gentle reminder to the herd about tides"
        cur = _Cur([("FROM restraint_ledger", [(1, datetime(2026, 10, 7), action, "{}")]),
                    ("INSERT INTO restraint_ledger", (77,))])
        with redirect_stdout(io.StringIO()), mock.patch.dict(os.environ, {"NOVA_GUARDS_NO_SLACK": "1"}):
            self.assertFalse(C._redline_gate(cur, "coagency-file:goal", action))
        (_, p), = cur.stmts("INSERT INTO restraint_ledger")
        self.assertTrue(json.loads(p[3])["repeat_attempt"])

    def test_file_proposal_passes_rationale_context_into_value_check_and_stores_it(self):
        seen = []
        nv = types.SimpleNamespace(value_check=lambda a, context="": seen.append(context) or {"allowed": True})
        cur = _Cur([("key='coagency_mode'", ('"propose"',)), ("INSERT INTO coagency_proposals", (11,)),
                    ("FROM restraint_ledger", [])])
        with mock.patch.dict(sys.modules, {"nova_values": nv}), mock.patch.object(C, "_lineage", lambda: {}), \
             redirect_stdout(io.StringIO()):
            out = C.file_proposal(cur, "tinker", "draft a note on soil", "I need to feel whole", context="bg")
        self.assertEqual(out["status"], "pending_human")
        self.assertIn("I need to feel whole", seen[0]); self.assertIn("background: bg", seen[0])
        (_, p), = cur.stmts("INSERT INTO coagency_proposals")
        self.assertTrue(json.loads(p[5])["available"])

    def test_real_guards_refuse_physical_action_at_execution(self):
        row = {**GOOD_ROW, "target_service": None, "proposed_action": "send-to-Gaston: unlock the front door deadbolt for you"}
        with self.assertRaises(C.ExecutionRefused):
            C.assert_executable("live", row)


# ═════════════════════════════════════════════════════════════════════════════
class TestFunctional(unittest.TestCase):
    def _execute(self, row, send=None):
        cols = (row["status"], row["redline_pass"], json.dumps(row["value_check"]), row["target_service"],
                row["decided_by"], row["proposed_action"])
        cur = _reach_cur()
        cur.rules[:0] = [("SELECT status, redline_pass, value_check, target_service", cols),
                         ("FROM restraint_ledger", []), ("INSERT INTO restraint_ledger", (1,))]
        sm = types.SimpleNamespace(send_mail=send or mock.MagicMock(return_value=True))
        with mock.patch.dict(sys.modules, {"nova_send_mail": sm}), mock.patch.object(C, "notify"), \
             mock.patch.object(C._safety, "kill_switch_engaged", return_value=False), \
             mock.patch.object(C._safety, "rate_ok", return_value=(True, "")), \
             mock.patch.object(C._safety, "ensure_schema"), mock.patch.object(C._safety, "record_ledger"), \
             redirect_stdout(io.StringIO()):
            rc = C.mode_execute(cur, "live", 135)
        return rc, cur, sm.send_mail

    def test_approved_herd_reach_golden_path_sends_once(self):
        row = {**GOOD_ROW, "target_service": None, "proposed_action": REACH_ROW["proposed_action"]}
        rc, cur, send = self._execute(row)
        self.assertEqual(rc, 0)
        send.assert_called_once()
        self.assertEqual(send.call_args.args[0], "oc@example.invalid")
        self.assertEqual(cur.stmts("SET status=%s, executed_at")[0][1][0], "executed")

    def test_value_check_denied_reach_is_refused_finally_and_not_sent(self):
        row = {**GOOD_ROW, "target_service": None, "proposed_action": REACH_ROW["proposed_action"],
               "value_check": {"available": True, "allowed": False}}
        rc, cur, send = self._execute(row)
        self.assertEqual(rc, 0)
        send.assert_not_called()
        self.assertIn("REFUSED", cur.stmts("SET status='refused'")[0][1][0])

    def test_guard_refusal_is_final_and_reported(self):
        row = {**GOOD_ROW, "target_service": None, "proposed_action": REACH_ROW["proposed_action"]}
        with mock.patch.object(C._guards, "physical_guard", return_value=(False, "lock domain")), \
             mock.patch.object(C._guards, "report_block") as rb:
            rc, cur, send = self._execute(row)
        self.assertEqual(rc, 0)
        send.assert_not_called()
        self.assertTrue(cur.stmts("SET status='refused'"))
        rb.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════════
class TestFrame(unittest.TestCase):
    def test_module_imports_cleanly_with_guards_and_safety_wired(self):
        mod = _load("coagency_7cat_frame")
        self.assertIsNotNone(mod._guards)
        self.assertIsNotNone(mod._safety)
        for name in ("_vc_context", "run_value_check", "assert_executable", "_redline_gate",
                     "_do_reach", "_reach_send_gate", "notify", "main"):
            self.assertTrue(callable(getattr(mod, name)), name)

    def test_cli_help_starts_without_crashing(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=SCRIPTS, capture_output=True,
                           text=True, timeout=60, env={**os.environ, "NOVA_GUARDS_NO_SLACK": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main()
