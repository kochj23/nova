#!/usr/bin/env python3
"""7-category tests for nova_watch_common (shared plumbing of the watch organs).

Security, Performance, Retry, Unit, Integration, Functional, Frame. Offline: no PostgreSQL,
no network, no Slack. Coordinates used here are synthetic (not the house).
Written by Jordan Koch (via Claude).
"""
import io
import json
import sys
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_watch_common as W  # noqa: E402

T0 = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
FAKE_HOME = (40.0, -100.0, "home")   # synthetic, deliberately nowhere near the house


class Cur:
    """Scripted cursor: first rule whose substring is in the SQL supplies the rows."""

    def __init__(self, rules=None):
        self.rules = rules or []
        self.calls = []
        self._rows = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        self._rows = []
        for sub, rows in self.rules:
            if sub in sql:
                self._rows = list(rows)
                break

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class TestSecurity(unittest.TestCase):
    def test_journal_safe_variants(self):
        for raw in ("medical at 12 N Main Street", "1200 Olive Ave.", "(~1.2 mi NE)", "0.4 miles SW",
                    "3 nm NNW"):
            out = W.journal_safe(raw)
            self.assertNotRegex(out, r"\d+\s+(N\s+)?(Main|Olive)", raw)
            self.assertNotRegex(out, r"\b(mi|miles|nm)\s+(NE|SW|NNW)\b", raw)

    def test_home_never_logged(self):
        buf = io.StringIO()
        with mock.patch("nova_geo_query.home_coords", side_effect=[Exception("pg down"), FAKE_HOME]), \
                redirect_stdout(buf):
            h = W.home(_sleep=lambda s: None)
        self.assertEqual(h, (40.0, -100.0))
        self.assertNotIn("40.0", buf.getvalue())
        self.assertNotIn("-100", buf.getvalue())

    def test_retry_log_never_contains_arguments(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            W.retry(lambda secret: False, "xoxb-SECRET-TOKEN", attempts=2, _sleep=lambda s: None)
        self.assertNotIn("SECRET", buf.getvalue())

    def test_config_sql_parameterized(self):
        cur = Cur()
        W.get_config(cur, "svc'; DROP TABLE x;--", "k")
        W.set_config(cur, "svc", "k", {"a": 1})
        for sql, params in cur.calls:
            self.assertNotIn("DROP", sql)
            self.assertTrue(params)

    def test_post_slack_without_token_never_touches_network(self):
        with mock.patch("nova_config.slack_bot_token", return_value=""), \
                mock.patch("urllib.request.urlopen") as uo:
            self.assertFalse(W.post_slack("x", "C1"))
        uo.assert_not_called()

    def test_post_slack_token_only_in_header(self):
        seen = {}

        def fake_urlopen(req, timeout=10):
            seen["body"] = req.data.decode()
            seen["auth"] = req.get_header("Authorization")
            return io.BytesIO(b'{"ok": true}')
        with mock.patch("nova_config.slack_bot_token", return_value="xoxb-test"), \
                mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
            self.assertTrue(W.post_slack("hello", "C1"))
        self.assertNotIn("xoxb", seen["body"])
        self.assertEqual(seen["auth"], "Bearer xoxb-test")


class TestPerformance(unittest.TestCase):
    def test_heli_loiters_50k_samples(self):
        s = [(T0 + timedelta(seconds=i), f"hx{i % 50}", "CALL", 1000, 0.5) for i in range(50000)]
        t = time.time()
        out = W.heli_loiters(s)
        self.assertLess(time.time() - t, 1.0)
        self.assertEqual(len(out), 50)

    def test_percentile_and_count_between_large(self):
        v = list(range(200000))
        t = time.time()
        self.assertAlmostEqual(W.percentile(v, 0.5), 99999.5)
        ts = [T0 + timedelta(seconds=i) for i in range(200000)]
        self.assertEqual(W.count_between(ts, T0, T0 + timedelta(seconds=100)), 100)
        self.assertLess(time.time() - t, 2.0)

    def test_retry_is_bounded(self):
        calls = []
        W.retry(lambda: calls.append(1), attempts=3, _sleep=lambda s: None)
        self.assertEqual(len(calls), 3)


class TestRetry(unittest.TestCase):
    def test_retry_backoff_then_success(self):
        sleeps, n = [], iter([Exception("a"), False, "ok"])

        def fn():
            r = next(n)
            if isinstance(r, Exception):
                raise r
            return r
        self.assertEqual(W.retry(fn, _sleep=sleeps.append), "ok")
        self.assertEqual(sleeps, [2.0, 4.0])

    def test_retry_gives_up_loudly(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            res = W.retry(mock.MagicMock(side_effect=OSError("x"), __name__="send"), attempts=3,
                          _sleep=lambda s: None, tag="t")
        self.assertFalse(res)
        self.assertEqual(buf.getvalue().count("failed"), 3)

    def test_retry_default_sleep_is_patchable(self):
        with mock.patch.object(W.time, "sleep") as sl:
            W.retry(lambda: False, attempts=2)
        sl.assert_called_once_with(2.0)

    def test_home_retries_then_raises_when_unset(self):
        with mock.patch("nova_geo_query.home_coords", return_value=None) as hc:
            with self.assertRaises(RuntimeError):
                W.home(_sleep=lambda s: None)
        self.assertEqual(hc.call_count, 3)

    def test_post_slack_under_retry(self):
        resp = [urllib.error.URLError("dns"), io.BytesIO(b'{"ok": false}'), io.BytesIO(b'{"ok": true}')]
        with mock.patch("nova_config.slack_bot_token", return_value="t"), \
                mock.patch("urllib.request.urlopen", side_effect=resp) as uo:
            self.assertTrue(W.retry(W.post_slack, "m", "C1", _sleep=lambda s: None))
        self.assertEqual(uo.call_count, 3)

    def test_load_scanner_uses_retrying_connect(self):
        conn = mock.MagicMock()
        conn.cursor.return_value = Cur([("FROM memories", [(T0, 0.3, "x", "c")])])
        with mock.patch.object(W, "connect", return_value=conn) as c:
            rows = W.load_scanner_near(T0, T0 + timedelta(hours=1))
        c.assert_called_once()
        conn.close.assert_called_once()
        self.assertEqual(len(rows), 1)


class TestUnit(unittest.TestCase):
    def test_miles(self):
        self.assertAlmostEqual(W.miles(40, -100, 40, -100), 0.0)
        self.assertAlmostEqual(W.miles(40, -100, 41, -100), 69.1, delta=0.2)

    def test_camera_and_medical(self):
        self.assertFalse(W.is_exterior_camera("interior_kitchen"))
        self.assertFalse(W.is_exterior_camera(None))
        self.assertTrue(W.is_exterior_camera("driveway"))
        self.assertTrue(W.is_medical("Person DOWN at the park"))
        self.assertFalse(W.is_medical(None))

    def test_in_night(self):
        self.assertTrue(W.in_night(datetime(2026, 10, 8, 23, tzinfo=W.TZ)))
        self.assertTrue(W.in_night(datetime(2026, 10, 8, 3, tzinfo=W.TZ)))
        self.assertFalse(W.in_night(datetime(2026, 10, 8, 12, tzinfo=W.TZ)))

    def test_episodes_split_on_gap(self):
        rows = [(T0, "k"), (T0 + timedelta(seconds=60), "k"), (T0 + timedelta(seconds=500), "k")]
        eps = W.episodes(rows, gap_s=120)
        self.assertEqual([e[3] for e in eps], [2, 1])

    def test_percentile_empty(self):
        self.assertEqual(W.percentile([], 0.9), 0.0)

    def test_get_config_decodes(self):
        self.assertEqual(W.get_config(Cur([("service_config", [('{"a": 1}',)])]), "s", "k"), {"a": 1})
        self.assertEqual(W.get_config(Cur([("service_config", [({"a": 2},)])]), "s", "k"), {"a": 2})
        self.assertEqual(W.get_config(Cur(), "s", "k", "dflt"), "dflt")


class TestIntegration(unittest.TestCase):
    def test_load_chp_near_filters_by_true_distance(self):
        rows = [(T0, "Traffic Hazard", "loc a", 40.001, -100.001),
                (T0, "Fire", "loc b", 40.02, -100.0)]   # ~1.4 mi
        cur = Cur([("chp_incidents", rows)])
        out = W.load_chp_near(cur, T0, T0, 40.0, -100.0, max_mi=0.5)
        self.assertEqual([r[1] for r in out], ["Traffic Hazard"])

    def test_load_ext_detections_drops_interior_and_filters_labels(self):
        rows = [(T0, "interior_hall", "hall", "person"), (T0, "driveway", "front", "person"),
                (T0, "driveway", "front", "car")]
        out = W.load_ext_detections(Cur([("telemetry.presence", rows)]), T0, T0, {"person"})
        self.assertEqual(out, [(T0, "driveway", "front", "person")])

    def test_new_devices_excludes_tests(self):
        cur = Cur()
        W.load_new_devices(cur, T0, T0)
        self.assertIn("[TEST]%", cur.calls[0][1])


class TestFunctional(unittest.TestCase):
    def test_feed_to_loiter_golden(self):
        samples = [(T0 + timedelta(seconds=i * 10), "abc", "N1 ", 1200, 0.8) for i in range(10)]
        lo = W.heli_loiters(W.load_heli(Cur([("overhead_flights", samples)]), T0, T0))
        self.assertEqual(len(lo), 1)
        self.assertTrue(lo[0]["tight"])
        self.assertEqual(lo[0]["callsign"], "N1")

    def test_few_samples_not_a_loiter(self):
        self.assertEqual(W.heli_loiters([(T0, "a", "", None, None)] * 4), [])


class TestFrame(unittest.TestCase):
    def test_exports(self):
        for n in ("retry", "post_slack", "connect", "home", "journal_safe", "timedelta", "ensure_schema"):
            self.assertIn(n, W.__all__)

    def test_schema_creates_every_table(self):
        for t in ("bodach_scores", "unexplained_events", "derry_monthly", "shine_contacts", "shine_log"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {t}", W.SCHEMA)
        cur = Cur()
        W.ensure_schema(cur)
        self.assertEqual(len(cur.calls), 1)


if __name__ == "__main__":
    unittest.main()
