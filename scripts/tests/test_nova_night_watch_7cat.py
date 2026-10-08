#!/usr/bin/env python3
"""7-category tests for nova_night_watch (the morning report on the night around the house).
Offline: no PostgreSQL, no Slack, no Discord. Written by Jordan Koch (via Claude).
"""
import io
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import nova_watch_common as W  # noqa: E402
import nova_night_watch as N  # noqa: E402

START, END = N.night_bounds(date(2026, 10, 8))


def g(**kw):
    base = {"by_class": {}, "person_zones": {}, "person_n": 0, "person_p95": 2, "deep_person": 0,
            "scanner": [], "loiters": [], "newdev": [], "buick": [], "bodach_max": 0.0, "bodach_fired": False,
            "bodach_types": 0, "bed_phone": (None, None, 0), "bed_mmwave": (None, None, 0)}
    base.update(kw)
    return base


class Cur:
    def __init__(self):
        self.calls = []
        self._rows = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if "FROM unexplained_events" in sql:
            self._rows = [("sensor_silence", "d")]
        elif "FROM bodach_scores" in sql:
            self._rows = [(1.5, False, 1)]
        elif "room='master_bedroom'" in sql:
            self._rows = [(START + timedelta(hours=2), END - timedelta(hours=1), 12)]
        else:
            self._rows = []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


def run_main(argv, post_results, quiet_msg="msg"):
    post = mock.MagicMock(side_effect=post_results)
    conn = mock.MagicMock()
    with mock.patch.object(W, "connect", return_value=conn), \
            mock.patch.object(N, "gather", return_value=g()), \
            mock.patch.object(W, "post_slack", post), mock.patch.object(W.time, "sleep"), \
            mock.patch("nova_config.post_both") as pb, mock.patch("nova_config.post_discord") as pd, \
            redirect_stdout(io.StringIO()) as out:
        rc = N.main(argv)
    return rc, post, pb, pd, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_compose_strips_addresses_and_bearings(self):
        msg = N.compose(g(scanner=[(START, 0.3, "ambulance to 1200 Olive Ave (~0.3 mi NE)", "c")],
                          buick=[("power_spike", "1200 Olive Ave plug")]), START, END)
        self.assertNotIn("Olive", msg)
        self.assertNotRegex(msg, r"mi\s+NE")

    def test_never_posts_to_discord(self):
        rc, post, pb, pd, _ = run_main([], [True])
        pb.assert_not_called()
        pd.assert_not_called()
        import nova_config
        self.assertEqual(post.call_args.args[1], nova_config.SLACK_CHAN)

    def test_no_scanner_text_in_report(self):
        msg = N.compose(g(scanner=[(START, 0.3, "suspect named Smith", "c")]), START, END)
        self.assertNotIn("Smith", msg)

    def test_sleep_is_evidence_not_diagnosis(self):
        line = N.sleep_line(g(bed_phone=(START, END, 4)))
        self.assertNotRegex(line.lower(), r"insomnia|slept badly|poor sleep|disorder")


class TestPerformance(unittest.TestCase):
    def test_gather_20k_detections(self):
        det = [(START - timedelta(days=13) + timedelta(seconds=60 * i), "driveway", "front",
                ("person", "car")[i % 2]) for i in range(20000)]
        with mock.patch.object(W, "load_ext_detections", return_value=det), \
                mock.patch.object(W, "load_scanner_near", return_value=[]), \
                mock.patch.object(W, "load_heli", return_value=[]), \
                mock.patch.object(W, "load_new_devices", return_value=[]):
            t = time.time()
            res = N.gather(Cur(), START, END)
        self.assertLess(time.time() - t, 2.0)
        self.assertIn("person_p95", res)

    def test_report_bounded_to_five_lines(self):
        big = g(by_class={f"c{i}": i for i in range(50)}, person_n=50, deep_person=9,
                scanner=[(START, 0.2, "x", "c")] * 100, loiters=[{"tight": True, "hits": 99}] * 10,
                newdev=[(START, "m", "w", "t")] * 30, buick=[("k", "d")] * 30, bodach_max=4.0, bodach_fired=True)
        self.assertLessEqual(len(N.compose(big, START, END).splitlines()), 5)


class TestRetry(unittest.TestCase):
    def test_post_retried_until_ok(self):
        rc, post, *_ = run_main([], [False, Exception("timeout"), True])
        self.assertEqual(rc, 0)
        self.assertEqual(post.call_count, 3)

    def test_post_failure_is_loud(self):
        rc, post, _pb, _pd, out = run_main([], [False, False, False])
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", out)

    def test_db_connect_retries(self):
        self.assertIn("W.connect()", (SCRIPTS / "nova_night_watch.py").read_text())


class TestUnit(unittest.TestCase):
    def test_night_bounds(self):
        self.assertEqual(START.astimezone(W.TZ).hour, 22)
        self.assertEqual(END.astimezone(W.TZ).hour, 7)
        self.assertEqual(END - START, timedelta(hours=9))

    def test_classify(self):
        self.assertEqual(N.classify("Truck"), "vehicle")
        self.assertEqual(N.classify(""), "unlabelled")
        self.assertEqual(N.classify("cat"), "cat")

    def test_notable(self):
        self.assertEqual(N.notable(g()), [])
        self.assertIn("never-seen network device", N.notable(g(newdev=[1])))
        self.assertIn("sustained helicopter orbit", N.notable(g(loiters=[{"tight": True, "hits": 30}])))
        self.assertNotIn("sustained helicopter orbit", N.notable(g(loiters=[{"tight": True, "hits": 29}])))

    def test_sleep_line_empty(self):
        self.assertEqual(N.sleep_line(g()), "no bedroom presence evidence recorded")


class TestIntegration(unittest.TestCase):
    def test_gather_reads_every_feed(self):
        det = [(START + timedelta(hours=4), "back_cam", "back_yard", "person"),
               (START + timedelta(hours=4, minutes=10), "drive", "front", "car")]
        with mock.patch.object(W, "load_ext_detections", return_value=det), \
                mock.patch.object(W, "load_scanner_near", return_value=[(START, 0.5, "x", "c")]) as sc, \
                mock.patch.object(W, "load_heli", return_value=[]), \
                mock.patch.object(W, "load_new_devices", return_value=[]):
            res = N.gather(Cur(), START, END)
        self.assertEqual(res["by_class"], {"person": 1, "vehicle": 1})
        self.assertEqual(res["deep_person"], 1)
        self.assertEqual(res["buick"], [("sensor_silence", "d")])
        self.assertEqual(res["bed_phone"][2], 12)
        self.assertEqual(sc.call_args.args[2], N.NEAR_MI)


class TestFunctional(unittest.TestCase):
    def test_dry_run_prints_never_posts(self):
        rc, post, pb, _pd, out = run_main(["--dry-run", "--date", "2026-10-08"], [True])
        self.assertEqual(rc, 0)
        post.assert_not_called()
        self.assertIn("Night Watch", out)

    def test_quiet_night_single_line(self):
        self.assertEqual(len(N.compose(g(), START, END).splitlines()), 1)

    def test_busy_night_golden(self):
        msg = N.compose(g(by_class={"person": 6}, person_n=6, person_zones={"back_yard": 6},
                          bodach_max=2.0, bodach_fired=True, bodach_types=2), START, END)
        self.assertIn("exterior person activity above baseline", msg)
        self.assertIn("(alerted)", msg)


class TestFrame(unittest.TestCase):
    def test_import_and_entrypoint(self):
        self.assertTrue(callable(N.main))
        self.assertEqual(N.TAG, "night-watch")

    def test_bad_date_rejected(self):
        with self.assertRaises(ValueError):
            N.main(["--dry-run", "--date", "not-a-date"])


if __name__ == "__main__":
    unittest.main()
