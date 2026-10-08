#!/usr/bin/env python3
"""7-category tests for nova_shine (The Shine). Centred on: it is BUILT DISABLED and must stay
that way by default — while disabled it observes only and nobody is notified, spoken to or
messaged. Offline: no PostgreSQL, no Slack, no iMessage, no voice. Contacts here are synthetic.
Written by Jordan Koch (via Claude).
"""
import io
import json
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_watch_common as W  # noqa: E402
import nova_shine as S  # noqa: E402

# The two-man gate (nova_escalation) is tested on its own and below; the send-path tests here run
# with the gate open so they stay offline (no HTTP probes, no PG).
_REAL_TWO_MAN = S.two_man
mock.patch.object(S, "two_man", return_value={"allowed": True, "reason": "test"}).start()

T0 = datetime(2026, 10, 8, 23, 0, tzinfo=timezone.utc)   # 16:00 local, waking hours
CONTACT = ("Pat", "imessage", "+15550000000")


class Cur:
    def __init__(self, contacts=()):
        self.contacts = list(contacts)
        self.calls = []
        self._rows = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "count(*) FROM shine_contacts" in sql:
            self._rows = [(len(self.contacts),)]
        elif "relationship" in sql and "FROM shine_contacts" in sql:
            self._rows = [(c[0], "friend", c[1], 1, True) for c in self.contacts]
        elif "FROM shine_contacts" in sql:
            self._rows = [c if "channel, address" in sql else (c[0], c[2]) for c in self.contacts]
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def inserts(self):
        return [p for s, p in self.calls if s.startswith("INSERT INTO shine_log")]


def _mods(notify_rv=True, imsg_rv=True):
    notify = mock.MagicMock(return_value=notify_rv) if not isinstance(notify_rv, list) \
        else mock.MagicMock(side_effect=notify_rv)
    send = mock.MagicMock(return_value=imsg_rv) if not isinstance(imsg_rv, list) \
        else mock.MagicMock(side_effect=imsg_rv)
    return notify, send, {"nova_notify": mock.MagicMock(notify=notify),
                          "nova_imessage": mock.MagicMock(send_imessage=send)}


def run_evaluate(config, cur, *, home=True, alone=False, last_h=10.0, dry_run=False, mods=None,
                 medical=(), now=T0):
    """Drive evaluate() with every feed patched. config: {key: value} from service_config."""
    stored = {}

    def get_config(_c, svc, key, default=None):
        return config.get(key, default)

    def set_config(_c, svc, key, value, by=""):
        stored[key] = value
    js = {"phone": [now - timedelta(hours=last_h)], "chat": [], "face": []}
    conn = mock.MagicMock()
    conn.cursor.return_value = cur
    with mock.patch.object(W, "connect", return_value=conn), \
            mock.patch.object(W, "ensure_schema"), \
            mock.patch.object(W, "get_config", side_effect=get_config), \
            mock.patch.object(W, "set_config", side_effect=set_config), \
            mock.patch.object(W, "now_utc", return_value=now), \
            mock.patch.object(S, "presence_now", return_value=(home, alone)), \
            mock.patch.object(S, "jordan_signals", return_value=js), \
            mock.patch.object(S, "household_signals", return_value=[]), \
            mock.patch.object(S, "desk_last_input", return_value=None), \
            mock.patch.object(S, "medical_near", return_value=list(medical)), \
            mock.patch.object(S, "fall_detected", return_value=[]), \
            mock.patch.object(W.time, "sleep"), \
            mock.patch.dict(sys.modules, mods or {}), redirect_stdout(io.StringIO()):
        rc = S.evaluate(dry_run)
    return rc, stored


BASELINE = {"jordan_p99": 2.0, "alone_p99": 1.0, "at": T0.isoformat()}


class TestSecurity(unittest.TestCase):
    """The Shine must be disabled by default and must never contact anyone while disabled."""

    def test_disabled_by_default_when_no_config_row(self):
        notify, send, mods = _mods()
        cur = Cur([CONTACT])
        rc, stored = run_evaluate({"baseline": BASELINE}, cur, mods=mods)   # no 'enabled' row at all
        self.assertEqual(rc, 0)
        notify.assert_not_called()
        send.assert_not_called()
        ins = cur.inserts()
        self.assertEqual(len(ins), 1)
        step, action, reason, _ev, dry_run, enabled = ins[0]
        self.assertEqual((step, action, dry_run, enabled), (1, "ask_jordan", True, False))
        self.assertIn("observe-only", reason)
        self.assertEqual(stored["state"]["step"], 1)

    def test_disabled_full_escalation_contacts_nobody(self):
        notify, send, mods = _mods()
        cur = Cur([CONTACT])
        st = {"step": 2, "step_at": (T0 - timedelta(minutes=30)).isoformat(), "cause": "silence"}
        run_evaluate({"baseline": BASELINE, "state": st, "enabled": False}, cur, mods=mods)
        notify.assert_not_called()
        send.assert_not_called()
        self.assertIn("would contact 1", cur.inserts()[0][2])

    def test_explicit_false_and_garbage_stay_disabled(self):
        for val in (False, 0, None, ""):
            notify, send, mods = _mods()
            run_evaluate({"baseline": BASELINE, "enabled": val}, Cur([CONTACT]), mods=mods)
            notify.assert_not_called()
            send.assert_not_called()

    def test_live_test_refusals(self):
        with mock.patch.object(W, "connect") as c, redirect_stdout(io.StringIO()):
            self.assertEqual(S.live_test(""), 2)
            self.assertEqual(S.live_test("yes"), 2)
        c.assert_not_called()   # no confirm -> never even opens the DB
        notify, send, mods = _mods()
        conn = mock.MagicMock()
        conn.cursor.return_value = Cur([CONTACT])
        with mock.patch.object(W, "connect", return_value=conn), \
                mock.patch.object(W, "get_config", return_value=False), \
                mock.patch.dict(sys.modules, mods), redirect_stdout(io.StringIO()):
            self.assertEqual(S.live_test("SEND-TEST"), 2)          # disabled
        conn.cursor.return_value = Cur([])
        with mock.patch.object(W, "connect", return_value=conn), \
                mock.patch.object(W, "get_config", return_value=True), \
                mock.patch.dict(sys.modules, mods), redirect_stdout(io.StringIO()):
            self.assertEqual(S.live_test("SEND-TEST"), 2)          # no contacts
        send.assert_not_called()

    def test_test_contacts_never_sends(self):
        notify, send, mods = _mods()
        conn = mock.MagicMock()
        conn.cursor.return_value = Cur([CONTACT])
        out = io.StringIO()
        with mock.patch.object(W, "connect", return_value=conn), mock.patch.object(W, "ensure_schema"), \
                mock.patch.object(W, "get_config", return_value=False), \
                mock.patch.dict(sys.modules, mods), redirect_stdout(out):
            self.assertEqual(S.test_contacts(), 0)
        send.assert_not_called()
        self.assertNotIn("+1555", out.getvalue())   # addresses are not printed

    def test_contact_message_has_no_location_or_diagnosis(self):
        m = S.contact_message(T0, 9)
        self.assertNotRegex(m, r"\d+\.\d{3,}")
        self.assertNotRegex(m, r"(?i)\b(street|avenue|ave|drive)\b")
        self.assertNotRegex(m, r"(?i)(emergency|is hurt|has fallen|is dead)")

    def test_desk_history_reads_timestamps_only(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / ".claude"
            p.mkdir()
            ms = int(T0.timestamp() * 1000)
            (p / "history.jsonl").write_text(json.dumps({"timestamp": ms, "display": "private prompt"}) + "\nnot json\n")
            with mock.patch.object(Path, "home", return_value=Path(d)):
                out = S.desk_history_proxy(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
        self.assertEqual(out, [T0])
        self.assertTrue(all(isinstance(t, datetime) for t in out))


class TestPerformance(unittest.TestCase):
    def test_decide_100k(self):
        inp = {"home": True, "waking": True, "gap_h": 1, "thr_h": 8, "last_signal": T0}
        t = time.time()
        for _ in range(100000):
            S.decide({"step": 0}, T0, inp, S.DEFAULTS)
        self.assertLess(time.time() - t, 3.0)

    def test_phone_transitions_and_gaps_50k(self):
        rows = [(T0 + timedelta(minutes=i), ("office", "kitchen")[(i // 3) % 2]) for i in range(50000)]
        t = time.time()
        tr = S.phone_transitions(rows)
        g = S.waking_gaps(tr, 8, 22)
        self.assertLess(time.time() - t, 2.0)
        self.assertTrue(tr and g)


class TestRetry(unittest.TestCase):
    def _act(self, action, notify_rv=True, imsg_rv=True, contacts=(CONTACT,)):
        notify, send, mods = _mods(notify_rv, imsg_rv)
        with mock.patch.dict(sys.modules, mods), mock.patch.object(W.time, "sleep"), \
                redirect_stdout(io.StringIO()):
            out = S.act(Cur(contacts), action, "why", {"wait": 20, "gap_h": 9, "last_signal_dt": T0},
                        enabled=True, dry_run=False, n_contacts=len(contacts))
        return out, notify, send

    def test_ask_retries_notify(self):
        out, notify, _ = self._act("ask_jordan", notify_rv=[False, Exception("pg"), True])
        self.assertEqual(out, "sent")
        self.assertEqual(notify.call_count, 3)

    def test_voice_failure_is_reported_not_silent(self):
        out, notify, _ = self._act("voice", notify_rv=False)
        self.assertEqual(out, "send FAILED after retries")
        self.assertEqual(notify.call_count, 3)

    def test_contact_imessage_retried(self):
        out, _, send = self._act("contact", imsg_rv=[False, True])
        self.assertEqual(out, "contacted 1/1")
        self.assertEqual(send.call_count, 2)

    def test_contact_gives_up_after_three(self):
        out, notify, send = self._act("contact", imsg_rv=False)
        self.assertEqual(out, "contacted 0/1")
        self.assertEqual(send.call_count, 3)
        self.assertIn("nobody (send failed)", notify.call_args.kwargs["body"])

    def test_desk_input_retries_once(self):
        calls = []

        def run(*a, **k):
            calls.append(1)
            if len(calls) == 1:
                raise OSError("busy")
            return mock.MagicMock(stdout='"HIDIdleTime" = 60000000000')
        self.assertEqual(S.desk_last_input(T0, _run=run), T0 - timedelta(seconds=60))
        self.assertEqual(len(calls), 2)


class TestUnit(unittest.TestCase):
    def test_trigger_cause(self):
        self.assertEqual(S.trigger_cause({"fall": True, "medical": True}), "fall")
        self.assertEqual(S.trigger_cause({"medical": True}), "medical")
        self.assertEqual(S.trigger_cause({}), "silence")

    def test_decide_not_home_none(self):
        inp = {"home": False, "waking": True, "gap_h": 20, "thr_h": 8, "last_signal": None}
        self.assertEqual(S.decide({"step": 0}, T0, inp, S.DEFAULTS)[1], "none")

    def test_decide_leaving_home_clears(self):
        st = {"step": 2, "step_at": T0.isoformat(), "cause": "silence"}
        inp = {"home": False, "waking": True, "gap_h": 20, "thr_h": 8, "last_signal": None}
        self.assertEqual(S.decide(st, T0, inp, S.DEFAULTS)[:2], (0, "clear"))

    def test_decide_caps_at_step3(self):
        st = {"step": 3, "step_at": (T0 - timedelta(hours=2)).isoformat(), "cause": "medical"}
        inp = {"home": True, "waking": True, "gap_h": 20, "thr_h": 8, "last_signal": None}
        self.assertEqual(S.decide(st, T0, inp, S.DEFAULTS)[1], "hold")

    def test_medical_at_night_starts_step1(self):
        inp = {"home": True, "waking": False, "gap_h": 0, "thr_h": 8, "medical": True, "last_signal": None}
        self.assertEqual(S.decide({"step": 0}, T0, inp, S.DEFAULTS)[:2], (1, "ask_jordan"))

    def test_waking_gaps_cross_day_ignored(self):
        a = datetime(2026, 10, 8, 21, tzinfo=W.TZ)
        self.assertEqual(S.waking_gaps([a, a + timedelta(hours=12)], 8, 22), [])

    def test_defaults_have_no_enable_switch(self):
        self.assertNotIn("enabled", S.DEFAULTS)


class TestIntegration(unittest.TestCase):
    def test_quiet_jordan_writes_nothing(self):
        cur = Cur([CONTACT])
        rc, stored = run_evaluate({"baseline": BASELINE}, cur, last_h=0.5)
        self.assertEqual(rc, 0)
        self.assertEqual(cur.inserts(), [])
        self.assertNotIn("state", stored)

    def test_dry_run_writes_nothing(self):
        cur = Cur([CONTACT])
        _, stored = run_evaluate({"baseline": BASELINE, "enabled": True}, cur, dry_run=True)
        self.assertEqual(cur.inserts(), [])
        self.assertEqual(stored, {})

    def test_stale_baseline_recomputed(self):
        old = dict(BASELINE, at=(T0 - timedelta(days=2)).isoformat())
        with mock.patch.object(S, "baseline", return_value={"jordan_p99": 2.0, "alone_p99": 1.0}) as b:
            _, stored = run_evaluate({"baseline": old}, Cur(), last_h=0.5)
        b.assert_called_once()
        self.assertIn("baseline", stored)

    def test_evidence_row_excludes_raw_datetime(self):
        cur = Cur([CONTACT])
        run_evaluate({"baseline": BASELINE}, cur)
        ev = json.loads(cur.inserts()[0][3])
        self.assertNotIn("last_signal_dt", ev)


class TestFunctional(unittest.TestCase):
    def test_enabled_golden_path_asks_jordan(self):
        notify, send, mods = _mods()
        cur = Cur([CONTACT])
        run_evaluate({"baseline": BASELINE, "enabled": True}, cur, mods=mods)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs["category"], "shine")
        send.assert_not_called()
        self.assertEqual(cur.inserts()[0][4:], (False, True))

    def test_enabled_step3_contacts_once(self):
        notify, send, mods = _mods()
        cur = Cur([CONTACT])
        st = {"step": 2, "step_at": (T0 - timedelta(minutes=30)).isoformat(), "cause": "medical"}
        _, stored = run_evaluate({"baseline": BASELINE, "state": st, "enabled": True}, cur, mods=mods)
        send.assert_called_once()
        self.assertEqual(stored["state"]["step"], 3)

    def test_simulations(self):
        for sc in ("medical", "fall"):
            self.assertEqual([r[2] for r in S.simulate(sc)][-1], "contact", sc)

    def test_live_test_sends_test_prefixed_message(self):
        notify, send, mods = _mods()
        conn = mock.MagicMock()
        cur = Cur([CONTACT])
        conn.cursor.return_value = cur
        with mock.patch.object(W, "connect", return_value=conn), \
                mock.patch.object(W, "get_config", return_value=True), \
                mock.patch.dict(sys.modules, mods), redirect_stdout(io.StringIO()):
            self.assertEqual(S.live_test("SEND-TEST"), 0)
        self.assertTrue(send.call_args.args[1].startswith("[TEST"))
        self.assertEqual(len(cur.inserts()), 1)


class TestFrame(unittest.TestCase):
    def test_main_simulate(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(S.main(["--simulate", "all"]), 0)
        self.assertIn("== recovery", out.getvalue())

    def test_entrypoints_exist(self):
        for n in ("evaluate", "replay", "test_contacts", "live_test", "main"):
            self.assertTrue(callable(getattr(S, n)))



# ── P3 privacy gate (camera_use_ok before camera/face data; outputs tagged private) ──
class _GateCur:
    """Records SQL; returns no rows."""
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append(sql)

    def fetchall(self):
        return []

    def fetchone(self):
        return (None, None, 0)


class TestPrivacyGateSecurity(unittest.TestCase):
    def test_refused_guard_means_no_camera_query(self):
        with mock.patch.object(S._privacy, "camera_use_ok", return_value=(False, "no")):
            self.assertFalse(S._camera_ok("safety", "internal"))

    def test_missing_guard_fails_closed(self):
        with mock.patch.object(S, "_privacy", None):
            self.assertFalse(S._camera_ok("safety", "internal"))

    def test_third_party_recipient_refused(self):
        self.assertFalse(S._camera_ok("safety", "a neighbour"))
        self.assertFalse(S._camera_ok("marketing", "internal"))

    def test_private_tag_shape(self):
        md = S._private({"n": 1}, "face")
        self.assertEqual(md["privacy"], "private")
        self.assertTrue(md["no_third_party"])


class TestPrivacyGatePerformance(unittest.TestCase):
    def test_gate_is_cheap(self):
        t = time.perf_counter()
        for _ in range(5000):
            S._camera_ok("presence", "internal")
        self.assertLess(time.perf_counter() - t, 1.0)


class TestPrivacyGateRetry(unittest.TestCase):
    def test_guard_exception_fails_closed_and_is_logged(self):
        logs = []
        with mock.patch.object(S._privacy, "camera_use_ok", side_effect=RuntimeError("boom")), \
                mock.patch.object(W, "log", side_effect=lambda tag, m: logs.append(m)):
            self.assertFalse(S._camera_ok("safety", "internal"))
        self.assertTrue(any("boom" in m for m in logs))   # never silent


class TestPrivacyGateUnit(unittest.TestCase):
    def test_allowed_purposes_pass(self):
        self.assertTrue(S._camera_ok("safety", "internal"))
        self.assertTrue(S._camera_ok("presence", "slack:jordan"))

    def test_private_without_guard_still_marks_private(self):
        with mock.patch.object(S, "_privacy", None):
            self.assertEqual(S._private({}, "face")["privacy"], "private")


class TestPrivacyGateIntegration(unittest.TestCase):
    def test_face_query_skipped_when_refused(self):
        cur = _GateCur()
        with mock.patch.object(S, "_camera_ok", return_value=False):
            js = S.jordan_signals(cur, T0 - timedelta(days=1), T0)
            S.household_signals(cur, T0 - timedelta(days=1), T0)
        self.assertEqual(js["face"], [])
        self.assertFalse(any("face_presence" in q for q in cur.sql))
        self.assertFalse(any("frigate" in q for q in cur.sql))

    def test_face_query_runs_when_allowed(self):
        cur = _GateCur()
        S.jordan_signals(cur, T0 - timedelta(days=1), T0)
        self.assertTrue(any("face_presence" in q for q in cur.sql))


class TestPrivacyGateFunctional(unittest.TestCase):
    def test_evaluate_tags_evidence_private_when_face_used(self):
        src = (SCRIPTS / "nova_shine.py").read_text()
        self.assertIn('ev = _private(ev, "face")', src)
        ev = S._private({"gap_h": 1}, "face")
        self.assertTrue(ev["no_content_generation"])

    def test_contact_message_has_no_face_or_camera_detail(self):
        msg = S.contact_message(T0, 9.0)
        self.assertNotRegex(msg.lower(), r"face|camera")


class TestPrivacyGateFrame(unittest.TestCase):
    def test_module_wires_guard(self):
        src = (SCRIPTS / "nova_shine.py").read_text()
        self.assertIn("nova_privacy_guards", src)
        self.assertIn('_camera_ok("presence"', src)


class TestTwoManAdoption(unittest.TestCase):
    """2026-10-08: every Shine step goes through nova_escalation as life-safety."""

    def test_item_collapses_phone_upstream(self):
        import nova_spinnaker as SP
        a = SP.assess(S.shine_item({}))
        self.assertEqual(a["independent"], 1)           # silence + "home" both read his phone
        self.assertEqual(SP.assess(S.shine_item({"medical": 1}))["independent"], 2)

    def test_held_step_contacts_nobody(self):
        with mock.patch.object(S, "two_man", return_value={"allowed": False, "reason": "no pre-consent"}), \
             mock.patch("nova_imessage.send_imessage") as im:
            out = S.act(Cur([CONTACT]), "contact", "r", {"gap_h": 9}, True, False, 1)
        self.assertIn("held by the two-man rule", out)
        im.assert_not_called()

    def test_gate_is_life_safety_and_fails_open(self):
        with mock.patch("nova_escalation.authorize", side_effect=RuntimeError("boom")):
            self.assertTrue(_REAL_TWO_MAN(Cur(), "voice", {}, "r")["allowed"])
        with mock.patch("nova_escalation.authorize", return_value={"allowed": True}) as au:
            _REAL_TWO_MAN(Cur(), "contact", {"medical": 1}, "r")
        kw = au.call_args.kwargs
        self.assertTrue(kw["life_safety"])
        self.assertEqual(kw["action_class"], "outward")
        self.assertEqual(kw["molink"], "unanswered")


if __name__ == "__main__":
    unittest.main()
