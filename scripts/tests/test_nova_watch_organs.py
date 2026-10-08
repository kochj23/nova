#!/usr/bin/env python3
"""Tests for the watch organs (nova_watch_common, nova_bodach_watch, nova_night_watch,
nova_buick8_log, nova_derry_clock, nova_shine) — the 7 house categories (Security, Performance,
Retry, Unit, Integration, Functional, Frame), centred on the scoring / threshold / escalation
logic. Offline: no PostgreSQL, no network, no Slack. Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_watch_organs.py
"""
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_watch_common as W  # noqa: E402
import nova_bodach_watch as B  # noqa: E402
import nova_night_watch as N  # noqa: E402
import nova_buick8_log as L  # noqa: E402
import nova_derry_clock as D  # noqa: E402
import nova_shine as S  # noqa: E402

FILES = ["nova_watch_common.py", "nova_bodach_watch.py", "nova_night_watch.py",
         "nova_buick8_log.py", "nova_derry_clock.py", "nova_shine.py"]
T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)


class FakeCursor:
    """Minimal cursor for the logbook: one unexplained_events row kept in memory."""

    def __init__(self):
        self.row = None
        self.calls = []
        self._result = None

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        s = " ".join(sql.split())
        if s.startswith("SELECT id, evidence FROM unexplained_events"):
            self._result = [(1, self.row["evidence"])] if self.row else []
        elif s.startswith("INSERT INTO unexplained_events"):
            kind, sig, desc, fs, ls, ev, src = params
            import json
            self.row = {"kind": kind, "signature": sig, "description": desc, "first_seen": fs,
                        "last_seen": ls, "occurrences": 1, "evidence": json.loads(ev), "cause": "unknown",
                        "status": "open", "hypotheses": []}
            self._result = [(1,)]
        elif s.startswith("UPDATE unexplained_events SET occurrences"):
            import json
            self.row["occurrences"] += 1
            self.row["evidence"] = json.loads(params[2])
            self._result = []
        elif s.startswith("SELECT description, occurrences"):
            r = self.row
            self._result = [(r["description"], r["occurrences"], r["first_seen"], r["last_seen"],
                             r["cause"], r["status"], r["hypotheses"])] if r else []
        else:
            self._result = []

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result or [])


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_secrets(self):
        pat = re.compile(r"(xox[bap]-[0-9A-Za-z-]{10,}|password\s*=\s*['\"][^'\"]+['\"]|api[_-]?key\s*=\s*['\"][A-Za-z0-9]{16,})", re.I)
        for f in FILES:
            self.assertIsNone(pat.search((SCRIPTS / f).read_text()), f)

    def test_sql_is_parameterized(self):
        for f in FILES:
            src = (SCRIPTS / f).read_text()
            self.assertNotRegex(src, r"execute\(\s*f[\"']", f"{f} builds SQL with an f-string")

    def test_home_location_never_in_source(self):
        coord = re.compile(r"34\.1\d{2,}\s*,\s*-118\.3\d{2,}")
        for f in FILES:
            self.assertIsNone(coord.search((SCRIPTS / f).read_text()), f"{f} hardcodes home coordinates")

    def test_journal_safe_strips_addresses_and_bearings(self):
        t = W.journal_safe("ambulance for 4917 Thompson Avenue (~0.8 mi E) and 480 Riverside Drive")
        self.assertNotIn("4917", t)
        self.assertNotIn("Riverside", t)
        self.assertNotRegex(t, r"mi\s+E\b")

    def test_no_literal_home_dir(self):
        for f in FILES + ["tests/test_nova_watch_organs.py"]:
            self.assertNotIn("/Users/" + "kochj", (SCRIPTS / f).read_text(), f)


class TestPerformance(unittest.TestCase):
    def test_calibrate_10k_windows_fast(self):
        wins = [(T0 + timedelta(minutes=15 * i), (i % 7) * 0.5, ["air", "chp"][: i % 3]) for i in range(10000)]
        t = time.time()
        res = B.calibrate(wins)
        self.assertLess(time.time() - t, 2.0)
        self.assertIn(res["threshold"], B.THRESHOLD_GRID)

    def test_episodes_10k_rows_fast(self):
        rows = [(T0 + timedelta(seconds=10 * i), ("cam", "person")) for i in range(10000)]
        t = time.time()
        eps = W.episodes(rows, gap_s=120)
        self.assertLess(time.time() - t, 1.0)
        self.assertEqual(len(eps), 1)


class TestRetry(unittest.TestCase):
    def test_connect_retries_then_succeeds(self):
        import psycopg2
        good = mock.MagicMock()
        with mock.patch.object(psycopg2, "connect", side_effect=[Exception("down"), Exception("down"), good]) as c:
            sleeps = []
            conn = W.connect("dbname=x", attempts=3, delay=0.01, _sleep=sleeps.append)
        self.assertIs(conn, good)
        self.assertEqual(c.call_count, 3)
        self.assertEqual(len(sleeps), 2)

    def test_connect_gives_up(self):
        import psycopg2
        with mock.patch.object(psycopg2, "connect", side_effect=Exception("down")):
            with self.assertRaises(Exception):
                W.connect("dbname=x", attempts=2, delay=0, _sleep=lambda s: None)

    def test_desk_input_fails_open(self):
        # RETRY GAP: desk_last_input — a single ioreg call; it must fail open (None), never raise.
        def boom(*a, **k):
            raise OSError("no ioreg")
        self.assertIsNone(S.desk_last_input(T0, _run=boom))


class TestUnit(unittest.TestCase):
    # ── Bodach scoring ──
    def test_single_type_never_fires(self):
        st = {"scanner": 1.0, "chp": 0, "air": 0, "network": 0, "motion": 0}
        score, present = B.combine(st)
        self.assertFalse(B.fires(score, present, 0.5))

    def test_two_strong_types_fire_at_threshold(self):
        score, present = B.combine({"scanner": 1.0, "chp": 0, "air": 1.0, "network": 0, "motion": 0})
        self.assertEqual(present, ["air", "scanner"])
        self.assertTrue(B.fires(score, present, 2.0))
        self.assertFalse(B.fires(score, present, 2.25))

    def test_weak_signals_not_present(self):
        score, present = B.combine({"scanner": 0, "chp": 0.4, "air": 0.0, "network": 0, "motion": 0.3})
        self.assertEqual((score, present), (0, []))

    def test_strength_curves(self):
        self.assertEqual(B.strength_scanner([]), 0)
        self.assertEqual(B.strength_scanner([0.4]), 0.75)
        self.assertEqual(B.strength_chp([("1125-Traffic Hazard", 1.0)] * 5), 0.5)
        self.assertEqual(B.strength_chp([("1183-Trfc Collision-Unkn Inj", 1.0)]), 0.8)
        self.assertEqual(B.strength_air([{"tight": False, "hits": 6}]), 0.5)
        self.assertEqual(B.strength_air([{"tight": True, "hits": 10}]), 0.75)
        self.assertEqual(B.strength_air([{"tight": True, "hits": B.AIR_SUSTAINED}]), 1.0)
        self.assertEqual(B.strength_motion(9, 0, night=False), 0)
        self.assertEqual(B.strength_motion(3, 5, night=True), 0)
        self.assertEqual(B.strength_motion(3, 0, night=True), 1.0)

    def test_merge_and_calibrate_picks_lowest_rare_threshold(self):
        we = [T0 + timedelta(minutes=15 * i) for i in range(10)]
        wins = [(we[0], 2.0, ["air", "motion"]), (we[1], 2.0, ["air", "motion"]),  # one episode
                (we[8], 1.75, ["air", "chp"]), (we[9], 3.0, ["air", "chp", "scanner"])]
        self.assertEqual(len(B.merge_episodes([we[0], we[1], we[8]])), 2)
        res = B.calibrate(wins, grid=(1.5, 2.0, 2.5, 3.0), target=1)
        self.assertEqual(res["by_threshold"][1.5], 2)
        self.assertEqual(res["threshold"], 2.5)

    # ── logbook ──
    def test_cause_needs_evidence(self):
        with self.assertRaises(L.CauseWithoutEvidence):
            L.validate_resolution("a neighbour's phone", None)
        with self.assertRaises(L.CauseWithoutEvidence):
            L.validate_resolution("unknown", {"x": 1})
        L.validate_resolution("claimed by Jordan", {"device_owner": "jordan"})

    def test_power_spike_rule(self):
        self.assertTrue(L.power_spike(1325, 0))
        self.assertFalse(L.power_spike(2400, 2300))     # dryer at its usual peak
        self.assertFalse(L.power_spike(250, 0))         # below the absolute floor

    def test_describe_never_states_unproven_cause(self):
        row = {"description": "x", "occurrences": 2, "first_seen": T0, "last_seen": T0, "cause": "unknown",
               "status": "open", "hypotheses": [{"label": "neighbour", "status": "hypothesis"}]}
        d = L.describe(row)
        self.assertIn("Cause unknown", d)
        self.assertNotIn("neighbour", d)

    # ── Derry ──
    def test_holidays_and_recurrence(self):
        h = D.holidays(2026)
        self.assertEqual(h[date(2026, 11, 26)], "Thanksgiving")
        self.assertEqual(h[date(2026, 9, 7)], "Labor Day")
        self.assertEqual(h[date(2026, 5, 25)], "Memorial Day")
        self.assertEqual(D.classify_recurrence([2025]), "last_year")
        self.assertEqual(D.classify_recurrence([2024, 2025]), "cycle")
        self.assertEqual(D.classify_recurrence([]), "no_history")
        self.assertIsNone(D.compare(10, 5, 30, 10))
        self.assertEqual(D.compare(15, 10, 30, 30), "15 vs 10 last year (+50%)")

    # ── Shine ──
    def test_phone_transitions_ignore_flicker(self):
        r = [(T0 + timedelta(minutes=i), room) for i, room in enumerate(
            ["office", "nearby", "office", "living_room", "office", "living_room", "living_room"])]
        self.assertEqual(S.phone_transitions(r), [T0 + timedelta(minutes=5)])

    def test_effective_gap_starts_at_waking(self):
        now = datetime(2026, 10, 8, 10, 0, tzinfo=W.TZ)
        last_night = now - timedelta(hours=12)
        self.assertAlmostEqual(S.effective_gap_h(now, last_night, 8), 2.0)
        self.assertEqual(S.effective_gap_h(now, now - timedelta(minutes=30), 8), 0.5)

    def test_threshold(self):
        self.assertEqual(S.threshold_hours(5.3, 1.5, 4.0), 7.95)
        self.assertEqual(S.threshold_hours(0.2, 1.5, 2.0), 2.0)


class TestIntegration(unittest.TestCase):
    def test_organs_share_common_loaders(self):
        for f in ("nova_bodach_watch.py", "nova_night_watch.py", "nova_shine.py"):
            src = (SCRIPTS / f).read_text()
            self.assertIn("import nova_watch_common as W", src)
            self.assertNotIn("def miles(", src)

    def test_night_watch_posts_slack_only_to_chat(self):
        src = (SCRIPTS / "nova_night_watch.py").read_text()
        self.assertIn("W.retry(W.post_slack, msg, nova_config.SLACK_CHAN", src)
        self.assertNotIn("post_both(", src)  # never Discord

    def test_logbook_counts_each_occurrence_once(self):
        cur = FakeCursor()
        L.log_unexplained("sensor_silence", "presence:mmwave", "silent", {}, occurrence_key="a", ts=T0, cur=cur)
        L.log_unexplained("sensor_silence", "presence:mmwave", "silent", {}, occurrence_key="a", ts=T0, cur=cur)
        L.log_unexplained("sensor_silence", "presence:mmwave", "silent", {}, occurrence_key="b", ts=T0, cur=cur)
        self.assertEqual(cur.row["occurrences"], 2)
        self.assertEqual(cur.row["cause"], "unknown")
        self.assertIn("Cause unknown", L.cause_statement("sensor_silence", "presence:mmwave", cur=cur))

    def test_night_watch_compose_shape(self):
        start, end = N.night_bounds(date(2026, 10, 8))
        g = {"by_class": {"person": 9, "vehicle": 30}, "person_zones": {"back_yard": 9}, "person_n": 9,
             "person_p95": 2, "deep_person": 3, "scanner": [(T0, 0.4, "medical aid 480 Riverside Drive", "x")],
             "loiters": [{"tight": True, "hits": 40}], "newdev": [(T0, "aa", "warning", "t")],
             "buick": [("network_device", "d")], "bodach_max": 2.0, "bodach_fired": True, "bodach_types": 2,
             "bed_phone": (start + timedelta(hours=1), end - timedelta(hours=1), 9), "bed_mmwave": (None, None, 0)}
        msg = N.compose(g, start, end)
        self.assertLessEqual(len(msg.splitlines()), 5)
        self.assertNotIn("Riverside", msg)
        self.assertIn("1 medical", msg)
        quiet = dict(g, person_n=0, deep_person=0, scanner=[], loiters=[], newdev=[], buick=[],
                     bodach_max=0, bodach_fired=False, bodach_types=0)
        self.assertEqual(len(N.compose(quiet, start, end).splitlines()), 1)


class TestFunctional(unittest.TestCase):
    def test_simulated_silence_escalates_to_contacts(self):
        steps = [r[2] for r in S.simulate("silence")]
        self.assertEqual(steps, ["ask_jordan", "voice", "contact"])

    def test_simulated_recovery_clears(self):
        acts = [r[2] for r in S.simulate("recovery")]
        self.assertEqual(acts[-1], "clear")
        self.assertNotIn("contact", acts)

    def test_household_activity_holds_silence_escalation(self):
        st = {"step": 1, "step_at": (T0 - timedelta(minutes=30)).isoformat(), "cause": "silence"}
        inp = {"home": True, "waking": True, "gap_h": 9, "thr_h": 8, "last_signal": T0 - timedelta(hours=9),
               "household_recent": True}
        self.assertEqual(S.decide(st, T0, inp, S.DEFAULTS)[1], "hold")
        st["cause"] = "medical"
        self.assertEqual(S.decide(st, T0, inp, S.DEFAULTS)[1], "voice")

    def test_silence_does_not_escalate_at_night(self):
        st = {"step": 1, "step_at": (T0 - timedelta(minutes=30)).isoformat(), "cause": "silence"}
        inp = {"home": True, "waking": False, "gap_h": 0, "thr_h": 8, "last_signal": T0 - timedelta(hours=9)}
        self.assertEqual(S.decide(st, T0, inp, S.DEFAULTS)[1], "hold")

    def test_disabled_shine_sends_nothing(self):
        fake_notify = mock.MagicMock()
        mod = mock.MagicMock(notify=fake_notify)
        cur = mock.MagicMock()
        cur.fetchall.return_value = [("Pat", "imessage", "+15550000000")]
        with mock.patch.dict(sys.modules, {"nova_notify": mod, "nova_imessage": mod}):
            for action in ("ask_jordan", "voice", "contact"):
                out = S.act(cur, action, "why", {"wait": 20, "gap_h": 9}, enabled=False, dry_run=False, n_contacts=1)
                self.assertNotEqual(out, "sent")
        fake_notify.assert_not_called()
        mod.send_imessage.assert_not_called()

    def test_contact_message_is_factual(self):
        m = S.contact_message(T0, 9)
        self.assertIn("I do not know that anything is wrong", m)
        self.assertTrue(S.contact_message(T0, 9, test=True).startswith("[TEST"))


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        env = dict(os.environ, NOVA_TEST_QUIET="1")
        for f in FILES[1:]:
            r = subprocess.run([sys.executable, str(SCRIPTS / f), "--help"], capture_output=True, timeout=30, env=env)
            self.assertEqual(r.returncode, 0, f"{f}: {r.stderr[-300:]}")

    def test_simulate_runs_offline(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_shine.py"), "--simulate", "all"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("contact", r.stdout)


if __name__ == "__main__":
    unittest.main()
