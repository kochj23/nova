#!/usr/bin/env python3
"""Tests for nova_coagency.py herd-reach send gate (_do_reach / _reach_send_gate /
_checked_send_gate: per-recipient advisory lock, idempotency on proposal id, per-recipient
spacing) — the 7 house categories (Security, Performance, Retry, Unit, Integration,
Functional, Frame). Written by Jordan Koch (via Claude).

Origin: 2026-10-08 self-justification audit, ledger #123/#124 — two approved reaches to
O.C. emailed 3.6s apart."""
import importlib.util
import io
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
SCRIPT = SCRIPTS / "nova_coagency.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


C = _load("coagency_reach_gate_under_test", SCRIPT)


def _src_of(fn_name):
    m = re.search(rf"^def {fn_name}\(.*?(?=^def |\Z)", SRC, re.S | re.M)
    return m.group(0)


class _Cur:
    """Cursor stub: first matching SQL substring wins; records statements + params."""
    def __init__(self, rules=(), raise_on=(), raise_times=None):
        self.rules, self.raise_on = list(rules), tuple(raise_on)
        self.raise_times = raise_times          # None = always raise on match
        self.sql, self.params, self._last = [], [], None

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = None
        for sub in self.raise_on:
            if sub in sql and (self.raise_times is None or self.raise_times > 0):
                if self.raise_times is not None:
                    self.raise_times -= 1
                raise RuntimeError(f"stub PG failure on {sub}")
        for sub, val in self.rules:
            if sub in sql:
                self._last = val(sql, params) if callable(val) else val
                return

    def fetchone(self):
        return self._last

    def fetchall(self):
        return [] if self._last is None else [self._last]

    def stmts(self, sub):
        return [(s, p) for s, p in zip(self.sql, self.params) if sub in s]


def _cursor(already_this_pid=None, recent_to_person=None, email="oc@example.invalid", **kw):
    return _Cur([("pg_advisory", (True,)),
                 ("FROM herd_correspondents", ("O.C.", email) if email else None),
                 ("action LIKE %s ORDER BY id", (already_this_pid,) if already_this_pid else None),
                 ("AND target=%s AND action LIKE", (recent_to_person, datetime(2026, 10, 6)) if recent_to_person else None),
                 ("INSERT INTO autonomy_ledger", (3,)),
                 ("INSERT INTO coagency_log", None)], **kw)


def _reach(cur, pid=135, action="send-to-O.C.: a thought about fishbowls", send_ok=True, send_exc=None):
    sm = types.SimpleNamespace(send_mail=mock.MagicMock(return_value=send_ok, side_effect=send_exc))
    note = mock.MagicMock()
    with mock.patch.dict(sys.modules, {"nova_send_mail": sm}), mock.patch.object(C, "notify", note), \
         mock.patch.object(C.time, "sleep", lambda s: None), redirect_stdout(io.StringIO()):
        rc = C._do_reach(cur, "live", pid, {"proposed_action": action}, source="coagency",
                         autonomy_level="rung2-supervised", vetoable=False)
    return rc, sm.send_mail, note


class TestSecurity(unittest.TestCase):
    def test_gate_sql_is_parameterized(self):
        body = _src_of("_reach_send_gate") + _src_of("_do_reach")
        self.assertNotRegex(body, r'execute\(\s*f["\']')            # no f-string SQL
        self.assertIn("make_interval(secs => %s)", body)

    def test_hostile_proposal_id_and_recipient_are_bound_not_interpolated(self):
        cur = _cursor(email="x'); DROP TABLE autonomy_ledger;--@evil.invalid")
        rc, send, _ = _reach(cur, pid="1; DELETE FROM coagency_proposals")
        for s, p in cur.stmts("pg_advisory") + cur.stmts("FROM autonomy_ledger"):
            self.assertNotIn("DROP TABLE", s); self.assertNotIn("DELETE FROM", s)
            self.assertIsNotNone(p)

    def test_no_credentials_in_source_and_mail_goes_through_keychain_wrapper(self):
        self.assertNotRegex(SRC, r"(?i)(smtp_pass|password\s*=\s*['\"][^'\"]+['\"])")
        self.assertNotIn("smtplib", SRC)
        self.assertIn("nova_send_mail.send_mail", _src_of("_do_reach_locked"))

    def test_direct_audiences_never_take_the_email_path(self):
        self.assertIsNone(C._reach_parts("send-to-Jordan: hi"))
        self.assertIsNone(C._reach_parts("send-to-claude: hi"))

    def test_ledger_never_stores_the_email_body_beyond_a_preview(self):
        cur = _cursor()
        _reach(cur, action="send-to-O.C.: " + "x" * 2000)
        (_, p), = cur.stmts("INSERT INTO autonomy_ledger")
        self.assertTrue(all(len(str(v)) < 600 for v in p if isinstance(v, str)))


class TestPerformance(unittest.TestCase):
    def test_gate_is_bounded_two_queries_and_fast(self):
        cur = _cursor()
        t = time.perf_counter()
        for i in range(10_000):
            C._reach_send_gate(cur, i, "herd:O.C.")
        self.assertLess(time.perf_counter() - t, 2.0)
        self.assertEqual(len(cur.sql), 20_000)                         # exactly 2 per call, no loop
        self.assertTrue(all("LIMIT 1" in s for s in cur.sql))

    def test_retry_loop_is_bounded(self):
        cur = _Cur(raise_on=("FROM autonomy_ledger",))
        with redirect_stdout(io.StringIO()):
            C._checked_send_gate(cur, 1, "herd:O.C.", sleep=lambda s: None)
        self.assertEqual(len(cur.stmts("FROM autonomy_ledger")), C.GATE_ATTEMPTS)


class TestRetry(unittest.TestCase):
    def test_gate_retries_with_exponential_backoff_then_succeeds(self):
        cur = _Cur([("FROM autonomy_ledger", None)], raise_on=("FROM autonomy_ledger",), raise_times=2)
        sleeps = []
        with redirect_stdout(io.StringIO()):
            r = C._checked_send_gate(cur, 1, "herd:O.C.", sleep=sleeps.append)
        self.assertIsNone(r)                                           # third attempt clean -> send
        self.assertEqual(sleeps, [C.GATE_BACKOFF_S, C.GATE_BACKOFF_S * 2])

    def test_gate_exhausted_fails_closed_and_loud(self):
        cur = _cursor(raise_on=("FROM autonomy_ledger WHERE executed",))
        rc, send, _ = _reach(cur)
        send.assert_not_called()                                       # never mail blind
        (_, p), = cur.stmts("INSERT INTO coagency_log")
        self.assertEqual(p[1], "execute_deferred")
        self.assertIn("fail closed", p[2])

    def test_failed_email_is_not_blind_resent_but_ledgered_and_left_for_batch(self):
        # RETRY: the send itself is retried by the 15-min execute_approved batch, never
        # in-process (a timed-out send may have gone out — a resend is the bug we fixed).
        cur = _cursor()
        rc, send, note = _reach(cur, send_ok=False)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(rc, 1)
        (_, lp), = cur.stmts("INSERT INTO autonomy_ledger")
        self.assertIn("send failed", str(lp))
        self.assertEqual(cur.stmts("SET status=%s, executed_at=now()")[0][1][0], "approved")
        self.assertIn("send failed", note.call_args[0][0])

    def test_email_exception_does_not_escape(self):
        rc, send, note = _reach(_cursor(), send_exc=RuntimeError("smtp down"))
        self.assertEqual(rc, 1)
        self.assertIn("failed", note.call_args[0][0])


class TestUnit(unittest.TestCase):
    def test_gate_returns_none_when_clear(self):
        self.assertIsNone(C._reach_send_gate(_cursor(), 135, "herd:O.C."))

    def test_gate_duplicate_beats_spacing(self):
        kind, why = C._reach_send_gate(_cursor(already_this_pid=124, recent_to_person=123), 135, "herd:O.C.")
        self.assertEqual(kind, "duplicate"); self.assertIn("ledger #124", why)

    def test_gate_spacing(self):
        kind, why = C._reach_send_gate(_cursor(recent_to_person=123), 135, "herd:O.C.")
        self.assertEqual(kind, "deferred"); self.assertIn("ledger #123", why)

    def test_idempotency_key_is_the_proposal_id(self):
        cur = _cursor()
        C._reach_send_gate(cur, 135, "herd:O.C.")
        self.assertEqual(cur.stmts("action LIKE %s ORDER BY id")[0][1], ("email reach to % (proposal #135):%",))

    def test_spacing_default_is_24h_and_env_driven(self):
        self.assertIn('NOVA_HERD_REACH_SPACING_H", "24"', SRC)
        cur = _cursor()
        C._reach_send_gate(cur, 1, "herd:O.C.")
        self.assertEqual(cur.stmts("AND target=%s")[0][1], ("herd:O.C.", C.HERD_REACH_SPACING_H * 3600))


class TestIntegration(unittest.TestCase):
    def test_ledger_format_written_is_the_one_the_gate_reads(self):
        cur = _cursor()
        _reach(cur, pid=777)
        (_, lp), = cur.stmts("INSERT INTO autonomy_ledger")
        action = next(v for v in lp if isinstance(v, str) and v.startswith("email reach to"))
        self.assertRegex(action, r"^email reach to .+ \(proposal #777\):")
        self.assertIn("herd:O.C.", lp)

    def test_lock_is_per_recipient_and_always_released(self):
        cur2 = _cursor()
        _reach(cur2)
        locks = cur2.stmts("pg_advisory")
        self.assertEqual([s.split("(")[0].split()[-1] for s, _ in locks], ["pg_advisory_lock", "pg_advisory_unlock"])
        self.assertEqual({p[0] for _, p in locks}, {"nova-reach:oc@example.invalid"})

    def test_unlock_runs_even_when_send_path_raises(self):
        cur = _cursor()
        with mock.patch.object(C, "_do_reach_locked", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                C._do_reach(cur, "live", 1, {"proposed_action": "send-to-O.C.: hi"}, source="coagency",
                            autonomy_level="rung2-supervised", vetoable=False)
        self.assertTrue(cur.stmts("pg_advisory_unlock"))

    def test_mode_execute_routes_reaches_through_the_gate(self):
        self.assertIn("_checked_send_gate(oc, pid, target)", _src_of("_do_reach_locked"))
        self.assertIn("return _do_reach(", _src_of("mode_execute"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_sends_once_and_records(self):
        cur = _cursor()
        rc, send, note = _reach(cur)
        self.assertEqual(rc, 0)
        send.assert_called_once()
        self.assertEqual(send.call_args[0][0], "oc@example.invalid")
        self.assertEqual(cur.stmts("SET status=%s, executed_at=now()")[0][1][0], "executed")
        self.assertIn("sent an approved reach #135", note.call_args[0][0])

    def test_audit_replay_second_oc_reach_is_deferred(self):
        # ledger #123/#124: #122 went out, then #135 to the same person 3.6s later.
        cur = _cursor(recent_to_person=123)
        rc, send, note = _reach(cur, pid=135)
        self.assertEqual(rc, 0)
        send.assert_not_called(); note.assert_not_called()
        self.assertFalse(cur.stmts("coagency_proposals SET"))         # stays approved for the batch
        self.assertEqual(cur.stmts("INSERT INTO coagency_log")[0][1][1], "execute_deferred")

    def test_rerun_of_sent_proposal_is_closed_not_resent(self):
        cur = _cursor(already_this_pid=124)
        rc, send, _ = _reach(cur, pid=135)
        send.assert_not_called()
        self.assertIn("already emailed", cur.stmts("coagency_proposals SET status='executed'")[0][1][0])

    def test_error_path_no_address_on_file(self):
        cur = _cursor(email=None)
        rc, send, note = _reach(cur)
        send.assert_not_called()
        self.assertEqual(rc, 1)
        self.assertIn("no herd address on file", note.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_exposes_gate_and_never_runs_main(self):
        with mock.patch.object(C, "main", side_effect=AssertionError("main ran")):
            m = _load("coagency_reimport", SCRIPT)
        for name in ("_reach_send_gate", "_checked_send_gate", "_do_reach", "_do_reach_locked"):
            self.assertTrue(callable(getattr(m, name)))


if __name__ == "__main__":
    unittest.main()
