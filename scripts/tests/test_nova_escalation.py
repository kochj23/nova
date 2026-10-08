#!/usr/bin/env python3
"""Tests for nova_escalation.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_escalation as E  # noqa: E402

SRC = (SCRIPTS / "nova_escalation.py").read_text()
CALM = {"depleted": False, "reasons": []}
TIRED = {"depleted": True, "reasons": ["late night (01:10)"]}
OK = {"degraded": False, "penalty": 0.0, "reasons": []}
BAD = {"degraded": True, "penalty": 0.25, "reasons": ["memory server unreachable"]}
TWO = {"claim": "x", "sources": [{"id": "camera:alley_north"}, {"id": "scanner:Burbank PD"}]}
ONE = {"claim": "x", "sources": [{"id": "camera:alley_north"}, {"id": "camera:front_door"}]}
KW = dict(life_safety=False, urgent=False, jordan_key=False, preconsent=False, molink=None)


class Cur:
    def __init__(self, rows=None, boom=False):
        self.rows, self.boom, self.sql = rows or {}, boom, []
        self.connection = mock.MagicMock()
        self._r = None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._r = None
        for k, v in self.rows.items():
            if k in sql:
                self._r = v
                break

    def fetchone(self):
        return self._r[0] if isinstance(self._r, list) and self._r else self._r

    def fetchall(self):
        return self._r if isinstance(self._r, list) else []


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')

    def test_no_ip_literals(self):
        self.assertNotRegex(SRC, r"192\.168\.\d+\.\d+")

    def test_nova_cannot_be_both_keys(self):
        # reasoning alone (no sensors, no Jordan, no pre-consent) never clears an alert
        self.assertFalse(E.decide("alert", ONE, jordan=CALM, nova=OK, **KW)["allowed"])

    def test_outward_needs_molink_first(self):
        d = E.decide("outward", TWO, jordan=CALM, nova=OK, **KW)
        self.assertFalse(d["allowed"])
        self.assertIn("MOLINK", d["reason"])

    def test_only_jordan_marks_feedback(self):
        with self.assertRaises(PermissionError):
            E.feedback(Cur(), 1, "unneeded", "nova")
        cur = Cur()
        cur.rowcount = 1
        self.assertTrue(E.feedback(cur, 1, "unneeded: routine", "jordan"))
        self.assertTrue(any("feedback_by=%s WHERE id=%s" in s for s, _ in cur.sql))

    def test_preconsent_only_counts_for_life_safety(self):
        self.assertFalse(E.decide("alert", ONE, jordan=CALM, nova=OK, **dict(KW, preconsent=True))["allowed"])


class TestPerformance(unittest.TestCase):
    def test_decide_10k(self):
        t = time.monotonic()
        for _ in range(10000):
            E.decide("alert", TWO, jordan=CALM, nova=OK, **KW)
        self.assertLess(time.monotonic() - t, 5.0)


class TestRetry(unittest.TestCase):
    def test_http_retries_with_backoff(self):
        calls, sleeps = {"n": 0}, []

        class R:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'

        def opener(url, timeout):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("refused")
            return R()
        ok, body, err = E.http_ok("http://x", attempts=3, _open=opener, _sleep=sleeps.append)
        self.assertTrue(ok)
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [0.5, 1.0])

    def test_http_gives_up_loudly(self):
        ok, _b, err = E.http_ok("http://x", attempts=2, _open=mock.Mock(side_effect=OSError("down")),
                                _sleep=lambda s: None)
        self.assertFalse(ok)
        self.assertIn("down", err)

    def test_authorize_fails_open_only_for_life_safety(self):
        with mock.patch.object(E, "settings", side_effect=RuntimeError("boom")):
            self.assertTrue(E.authorize(Cur(), source="t", kind="k", action_class="alert", life_safety=True,
                                        dry=True)["allowed"])
            self.assertFalse(E.authorize(Cur(), source="t", kind="k", action_class="alert", dry=True)["allowed"])


class TestUnit(unittest.TestCase):
    def test_two_types_clear_alert(self):
        d = E.decide("alert", TWO, jordan=CALM, nova=OK, **KW)
        self.assertTrue(d["allowed"])
        self.assertEqual(set(d["keys"]), {"reasoning", "sensors"})

    def test_jordan_key(self):
        self.assertTrue(E.decide("alert", ONE, jordan=CALM, nova=OK, **dict(KW, jordan_key=True))["allowed"])

    def test_fatigue_defers_non_urgent(self):
        d = E.decide("alert", TWO, jordan=TIRED, nova=OK, **KW)
        self.assertFalse(d["allowed"])
        self.assertTrue(d["deferred"])
        self.assertTrue(E.decide("alert", TWO, jordan=TIRED, nova=OK, **dict(KW, urgent=True))["allowed"])

    def test_degraded_blocks_non_life_safety(self):
        self.assertFalse(E.decide("alert", TWO, jordan=CALM, nova=BAD, **KW)["allowed"])
        ls = dict(KW, life_safety=True, preconsent=True, molink="unanswered")
        self.assertTrue(E.decide("outward", ONE, jordan=TIRED, nova=BAD, **ls)["allowed"])

    def test_uncorroborated_cannot_ask(self):
        self.assertFalse(E.decide("ask", {"sources": [{"id": "nova:reasoning"}]}, jordan=CALM, nova=OK, **KW)["allowed"])

    def test_note_always(self):
        self.assertTrue(E.decide("note", None, jordan=TIRED, nova=BAD, **KW)["allowed"])

    def test_late_night(self):
        self.assertTrue(E.is_late_night(datetime(2026, 1, 1, 9, 30, tzinfo=E.TZ).replace(hour=1)))
        self.assertFalse(E.is_late_night(datetime(2026, 1, 1, 14, 0, tzinfo=E.TZ)))

    def test_adjusted_confidence(self):
        self.assertEqual(E.adjusted_confidence(0.8, BAD), 0.6)
        self.assertEqual(E.adjusted_confidence(0.8, OK), 0.8)
        self.assertIsNone(E.adjusted_confidence(None, BAD))

    def test_selftest(self):
        self.assertEqual(E.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_nova_state_reads_boiler_and_gateway(self):
        cur = Cur({"gateway_traces": [(1000, 5, 0)], "boiler_state": [(120.0, 100.0)]})
        st = E.nova_state(cur, s=dict(E.DEFAULTS), _http=lambda url: (True, {"degraded": False,
                                                                              "backends": {"active": "openrouter"}}, None))
        self.assertTrue(st["degraded"])
        self.assertTrue(any("Boiler" in r for r in st["reasons"]))
        self.assertTrue(any("openrouter" in r for r in st["reasons"]))

    def test_preconsent_falls_back_to_shine_flag(self):
        with mock.patch.dict(sys.modules, {"nova_commanders_intent": None}):
            self.assertTrue(E.preconsent_active(Cur({"the_shine": [(True,)]})))
            self.assertFalse(E.preconsent_active(Cur({"the_shine": [(False,)]})))

    def test_held_decision_goes_to_restraint_ledger(self):
        with mock.patch("nova_restraint.record_restraint") as rr:
            d = E.authorize(Cur({"RETURNING id": [(7,)]}), source="bodach", kind="cluster", action_class="alert",
                            item=ONE, _jordan=CALM, _nova=OK)
        self.assertFalse(d["allowed"])
        self.assertEqual(rr.call_args.kwargs["channel"], "two-man")


class TestFunctional(unittest.TestCase):
    def test_authorize_logs_every_decision(self):
        cur = Cur({"RETURNING id": [(11,)]})
        d = E.authorize(cur, source="bodach", kind="cluster", action_class="alert", item=TWO, _jordan=CALM, _nova=OK)
        self.assertTrue(d["allowed"])
        self.assertEqual(d["log_id"], 11)
        self.assertTrue(any("INSERT INTO escalation_log" in s for s, _ in cur.sql))

    def test_molink_status_answered_by_chat(self):
        cur = Cur({"FROM slack_prompts": [("C1", "1.0", datetime.now(E.TZ), None)], "gateway_traces": [(1,)]})
        self.assertEqual(E.molink_status(cur, "r1", _read=lambda c, t: (None, None)), "answered")

    def test_molink_status_unanswered(self):
        cur = Cur({"FROM slack_prompts": [("C1", "1.0", datetime.now(E.TZ), None)], "gateway_traces": []})
        self.assertEqual(E.molink_status(cur, "r1", _read=lambda c, t: (None, None)), "unanswered")

    def test_molink_ask_dry_and_idempotent(self):
        self.assertEqual(E.molink_ask(Cur({"FROM slack_prompts": [("1.0",)]}), "r1", "ok?")["reason"], "already asked")
        r = E.molink_ask(Cur(), "r2", "Bodach saw something", dry=True)
        self.assertIn("reply *fine*", r["text"])


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_escalation.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_escalation.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
