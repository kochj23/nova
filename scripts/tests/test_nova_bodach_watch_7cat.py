#!/usr/bin/env python3
"""7-category tests for nova_bodach_watch (independent signals clustering near home).
Offline: no PostgreSQL, no notifier, no network. Written by Jordan Koch (via Claude).
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
import nova_bodach_watch as B  # noqa: E402

# The two-man gate is tested in test_nova_escalation and below; alert-path tests run with it open
# (offline: no HTTP probes, no PG). keys/spinnaker are what the alert body quotes.
_REAL_TWO_MAN = B.two_man
_GATE = mock.patch.object(B, "two_man", side_effect=lambda cur, w: {
    "allowed": True, "reason": "test", "keys": ["reasoning", "sensors"],
    "spinnaker": B.SP.assess(B.signal_item(w))})


def setUpModule():
    _GATE.start()


def tearDownModule():
    _GATE.stop()

T0 = datetime(2026, 10, 8, 9, 0, tzinfo=timezone.utc)   # 02:00 local (night)


def window(strengths, evidence=None):
    score, present = B.combine(strengths)
    ev = {"scanner": [{"ts": T0.isoformat(), "mi": 0.4, "text": "medical aid 480 Riverside Drive"}],
          "chp": [{"ts": T0.isoformat(), "type": "1183-Trfc Collision", "location": "Olive Ave / Main St", "mi": 0.7}],
          "air": [{"hex": "a1b2c3", "callsign": "AIR12", "hits": 40, "min_alt_ft": 900, "min_nm": 0.4, "tight": True}],
          "network": [{"ts": T0.isoformat(), "mac": "aa:bb:cc:dd:ee:ff", "level": "warning"}],
          "motion": {"person_episodes": 5, "baseline_p95": 1, "night": True}}
    ev.update(evidence or {})
    return {"ws": T0 - B.WINDOW, "we": T0, "score": score, "present": present, "strengths": strengths,
            "evidence": ev}


STRONG = {"scanner": 1.0, "chp": 0, "air": 1.0, "network": 0, "motion": 0}


class Cur:
    def __init__(self, recent_alert=False):
        self.recent_alert = recent_alert
        self.calls = []
        self._rows = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        self._rows = [(1,)] if ("alerted AND window_end" in sql and self.recent_alert) else []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)

    def inserts(self):
        return [p for s, p in self.calls if s.startswith("INSERT INTO bodach_scores")]


def run_live(w, cur, notify, *, dry_run=False, threshold=2.0):
    fake_feeds = mock.MagicMock()
    fake_feeds.return_value.window.return_value = w
    conn = mock.MagicMock()
    conn.cursor.return_value = cur
    with mock.patch.object(W, "connect", return_value=conn), mock.patch.object(W, "ensure_schema"), \
            mock.patch.object(W, "get_config", return_value=threshold), \
            mock.patch.object(W, "now_utc", return_value=T0), mock.patch.object(B, "Feeds", fake_feeds), \
            mock.patch.object(W.time, "sleep"), \
            mock.patch.dict(sys.modules, {"nova_notify": mock.MagicMock(notify=notify)}), \
            redirect_stdout(io.StringIO()) as out:
        rc = B.run_live(dry_run)
    return rc, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_safe_summary_has_no_places_macs_or_callsigns(self):
        w = window({"scanner": 1.0, "chp": 0.8, "air": 1.0, "network": 0.5, "motion": 1.0})
        safe = B.describe(w, safe=True)
        for leak in ("Riverside", "480", "Olive", "aa:bb", "AIR12", "a1b2c3", "0.4 mi"):
            self.assertNotIn(leak, safe)

    def test_stored_summary_is_the_safe_one(self):
        cur = Cur()
        run_live(window(STRONG), cur, mock.MagicMock(return_value=True))
        safe_summary = cur.inserts()[0][6]
        self.assertNotIn("Riverside", safe_summary)
        self.assertNotIn("AIR12", safe_summary)

    def test_home_coords_never_written(self):
        cur = Cur()
        with mock.patch.object(W, "home", return_value=(40.0, -100.0)):
            run_live(window(STRONG), cur, mock.MagicMock(return_value=True))
        self.assertNotIn("-100.0", json.dumps(cur.calls, default=str))

    def test_alert_body_says_no_cause_implied(self):
        notify = mock.MagicMock(return_value=True)
        run_live(window(STRONG), Cur(), notify)
        self.assertIn("no cause is implied", notify.call_args.kwargs["body"])
        self.assertEqual(notify.call_args.kwargs["category"], "local")


class TestPerformance(unittest.TestCase):
    def _feeds(self, n_heli):
        f = object.__new__(B.Feeds)
        f._keys = {}
        f.scanner, f.chp, f.net = [], [], []
        f.heli = [(T0 - timedelta(days=1) + timedelta(seconds=10 * i), "h1", "C", 1000, 0.5) for i in range(n_heli)]
        f.person_ts = sorted(T0 - timedelta(minutes=7 * i) for i in range(5000))
        return f

    def test_day_of_windows_fast(self):
        f = self._feeds(8640)
        t = time.time()
        we = T0 - timedelta(days=1) + B.WINDOW
        n = 0
        while we <= T0:
            f.window(we - B.WINDOW, we)
            we += B.STEP
            n += 1
        self.assertLess(time.time() - t, 3.0)
        self.assertEqual(n, 93)

    def test_slice_index_cached_once(self):
        f = self._feeds(1000)
        f.window(T0 - B.WINDOW, T0)
        f.window(T0 - 2 * B.WINDOW, T0 - B.WINDOW)
        self.assertEqual(len(f._keys), 4)   # one index per feed list, reused


class TestRetry(unittest.TestCase):
    def test_notify_retried_then_alerted(self):
        notify = mock.MagicMock(side_effect=[False, Exception("pg"), True])
        cur = Cur()
        run_live(window(STRONG), cur, notify)
        self.assertEqual(notify.call_count, 3)
        self.assertTrue(cur.inserts()[0][9])

    def test_notify_failure_recorded_as_not_alerted(self):
        notify = mock.MagicMock(return_value=False)
        cur = Cur()
        rc, out = run_live(window(STRONG), cur, notify)
        self.assertEqual(rc, 0)
        self.assertEqual(notify.call_count, 3)
        self.assertFalse(cur.inserts()[0][9])
        self.assertIn("attempt 3/3", out)

    def test_db_via_retrying_connect(self):
        self.assertIn("W.connect()", (SCRIPTS / "nova_bodach_watch.py").read_text())
        self.assertNotIn("psycopg2.connect", (SCRIPTS / "nova_bodach_watch.py").read_text())


class TestUnit(unittest.TestCase):
    def test_strength_network(self):
        self.assertEqual(B.strength_network(0), 0.0)
        self.assertEqual(B.strength_network(1), 0.5)
        self.assertEqual(B.strength_network(9), 1.0)

    def test_scanner_close_hit_bonus(self):
        self.assertEqual(B.strength_scanner([0.4, 1.0]), 1.0)
        self.assertEqual(B.strength_scanner([1.2]), 0.5)

    def test_calibrate_no_threshold_meets_target(self):
        wins = [(T0 + timedelta(hours=3 * i), 5.0, ["air", "chp"]) for i in range(10)]
        self.assertEqual(B.calibrate(wins, grid=(1.0, 2.0), target=1)["threshold"], 2.0)

    def test_motion_requires_two_episodes(self):
        self.assertEqual(B.strength_motion(1, 0, night=True), 0)


class TestIntegration(unittest.TestCase):
    def test_rate_limit_one_alert_per_two_hours(self):
        notify = mock.MagicMock(return_value=True)
        cur = Cur(recent_alert=True)
        run_live(window(STRONG), cur, notify)
        notify.assert_not_called()
        self.assertTrue(cur.inserts()[0][8])      # fired recorded
        self.assertFalse(cur.inserts()[0][9])     # but not alerted again

    def test_below_threshold_records_without_alert(self):
        notify = mock.MagicMock(return_value=True)
        cur = Cur()
        run_live(window({"scanner": 0.5, "chp": 0, "air": 0.5, "network": 0, "motion": 0}), cur, notify)
        notify.assert_not_called()
        self.assertEqual(len(cur.inserts()), 1)

    def test_feeds_use_shared_loaders(self):
        with mock.patch.object(W, "home", return_value=(40.0, -100.0)), \
                mock.patch.object(W, "load_scanner_near", return_value=[]) as sc, \
                mock.patch.object(W, "load_chp_near", return_value=[]) as ch, \
                mock.patch.object(W, "load_heli", return_value=[]), \
                mock.patch.object(W, "load_new_devices", return_value=[]), \
                mock.patch.object(W, "load_ext_detections", return_value=[]):
            w = B.Feeds(mock.MagicMock(), T0 - B.WINDOW, T0).window(T0 - B.WINDOW, T0)
        sc.assert_called_once()
        self.assertEqual(ch.call_args.args[3:5], (40.0, -100.0))
        self.assertEqual(w["score"], 0)


class TestFunctional(unittest.TestCase):
    def test_nothing_stirring_records_nothing(self):
        cur = Cur()
        run_live(window({k: 0 for k in STRONG}), cur, mock.MagicMock())
        self.assertEqual(cur.inserts(), [])

    def test_dry_run_prints_and_writes_nothing(self):
        notify = mock.MagicMock()
        cur = Cur()
        rc, out = run_live(window(STRONG), cur, notify, dry_run=True)
        self.assertEqual(rc, 0)
        notify.assert_not_called()
        self.assertEqual(cur.inserts(), [])
        self.assertIn('"score"', out)

    def test_calibrate_no_write_vs_write(self):
        wins = [window(STRONG)]
        conn = mock.MagicMock()
        with mock.patch.object(W, "connect", return_value=conn), mock.patch.object(B, "replay", return_value=wins), \
                mock.patch.object(W, "set_config") as sc, mock.patch.object(W, "ensure_schema"), \
                redirect_stdout(io.StringIO()):
            B.run_calibrate(30, write=False)
            sc.assert_not_called()
            B.run_calibrate(30, write=True)
        keys = [c.args[2] for c in sc.call_args_list]
        self.assertEqual(keys, ["threshold", "calibration"])


class TestFrame(unittest.TestCase):
    def test_main_routes(self):
        with mock.patch.object(B, "run_live", return_value=0) as rl, \
                mock.patch.object(B, "run_calibrate", return_value=0) as rc:
            self.assertEqual(B.main(["--dry-run"]), 0)
            self.assertEqual(B.main(["--calibrate", "--no-write", "--days", "7"]), 0)
        rl.assert_called_once_with(True)
        rc.assert_called_once_with(7, False)



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
        with mock.patch.object(B._privacy, "camera_use_ok", return_value=(False, "no")):
            self.assertFalse(B._camera_ok("safety", "internal"))

    def test_missing_guard_fails_closed(self):
        with mock.patch.object(B, "_privacy", None):
            self.assertFalse(B._camera_ok("safety", "internal"))

    def test_third_party_recipient_refused(self):
        self.assertFalse(B._camera_ok("safety", "a neighbour"))
        self.assertFalse(B._camera_ok("marketing", "internal"))

    def test_private_tag_shape(self):
        md = B._private({"n": 1}, "face")
        self.assertEqual(md["privacy"], "private")
        self.assertTrue(md["no_third_party"])


class TestPrivacyGatePerformance(unittest.TestCase):
    def test_gate_is_cheap(self):
        t = time.perf_counter()
        for _ in range(5000):
            B._camera_ok("presence", "internal")
        self.assertLess(time.perf_counter() - t, 1.0)


class TestPrivacyGateRetry(unittest.TestCase):
    def test_guard_exception_fails_closed_and_is_logged(self):
        logs = []
        with mock.patch.object(B._privacy, "camera_use_ok", side_effect=RuntimeError("boom")), \
                mock.patch.object(W, "log", side_effect=lambda tag, m: logs.append(m)):
            self.assertFalse(B._camera_ok("safety", "internal"))
        self.assertTrue(any("boom" in m for m in logs))   # never silent


class TestPrivacyGateUnit(unittest.TestCase):
    def test_allowed_purposes_pass(self):
        self.assertTrue(B._camera_ok("safety", "internal"))
        self.assertTrue(B._camera_ok("presence", "slack:jordan"))

    def test_private_without_guard_still_marks_private(self):
        with mock.patch.object(B, "_privacy", None):
            self.assertEqual(B._private({}, "face")["privacy"], "private")


class TestPrivacyGateIntegration(unittest.TestCase):
    def test_feeds_skip_camera_load_when_refused(self):
        with mock.patch.object(B, "_camera_ok", return_value=False), \
                mock.patch.object(W, "load_ext_detections") as led, \
                mock.patch.object(W, "home", return_value=(0.0, 0.0)), \
                mock.patch.object(W, "load_scanner_near", return_value=[]), \
                mock.patch.object(W, "load_chp_near", return_value=[]), \
                mock.patch.object(W, "load_heli", return_value=[]), \
                mock.patch.object(W, "load_new_devices", return_value=[]):
            f = B.Feeds(_GateCur(), T0 - B.WINDOW, T0)
        led.assert_not_called()
        self.assertEqual(f.person_ts, [])


class TestPrivacyGateFunctional(unittest.TestCase):
    def test_motion_evidence_tagged_private(self):
        with mock.patch.object(W, "home", return_value=(0.0, 0.0)), \
                mock.patch.object(W, "load_scanner_near", return_value=[]), \
                mock.patch.object(W, "load_chp_near", return_value=[]), \
                mock.patch.object(W, "load_heli", return_value=[]), \
                mock.patch.object(W, "load_new_devices", return_value=[]), \
                mock.patch.object(W, "load_ext_detections", return_value=[]):
            w = B.Feeds(_GateCur(), T0 - B.WINDOW, T0).window(T0 - B.WINDOW, T0)
        self.assertEqual(w["evidence"]["motion"]["privacy"], "private")
        self.assertEqual(w["evidence"]["motion"]["data_class"], "camera")
        B.describe(w, safe=True)   # describe still works on tagged evidence


class TestPrivacyGateFrame(unittest.TestCase):
    def test_module_wires_guard(self):
        src = (SCRIPTS / "nova_bodach_watch.py").read_text()
        self.assertIn("nova_privacy_guards", src)
        self.assertIn('_camera_ok("safety"', src)


class TestTwoManAdoption(unittest.TestCase):
    """2026-10-08: the Bodach alert goes through nova_escalation (SPINNAKER + two-man + fatigue)."""

    def test_signal_item_one_source_per_present_type(self):
        it = B.signal_item({"present": ["air", "motion", "bogus"]})
        self.assertEqual([x["id"] for x in it["sources"]], ["adsb:loiter", "camera:exterior"])

    def test_urgency(self):
        self.assertTrue(B.is_urgent({"present": ["air", "motion", "scanner"]}))
        self.assertTrue(B.is_urgent({"present": ["motion", "network"]}))
        self.assertFalse(B.is_urgent({"present": ["air", "motion"]}))

    def test_held_alert_is_not_sent(self):
        notify = mock.MagicMock(return_value=True)
        with mock.patch.object(B, "two_man", return_value={"allowed": False, "reason": "Jordan depleted"}):
            run_live(window(STRONG), Cur(), notify)
        notify.assert_not_called()

    def test_gate_fails_closed(self):
        with mock.patch("nova_escalation.authorize", side_effect=RuntimeError("boom")):
            self.assertFalse(_REAL_TWO_MAN(Cur(), {"present": ["air", "motion"]})["allowed"])


if __name__ == "__main__":
    unittest.main()
