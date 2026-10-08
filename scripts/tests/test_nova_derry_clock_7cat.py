#!/usr/bin/env python3
"""7-category tests for nova_derry_clock (annual cycles, honest about thin history).
Offline: no PostgreSQL, no notifier. Written by Jordan Koch (via Claude).
"""
import io
import json
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
import nova_derry_clock as D  # noqa: E402

TODAY = date(2026, 11, 1)


class Cur:
    def __init__(self, rules):
        self.rules = rules
        self.calls = []
        self._r = []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        self._r = []
        for sub, rows in self.rules:
            if sub in sql:
                self._r = list(rows)
                if isinstance(rows, Exception):
                    raise rows
                break

    def fetchone(self):
        return self._r[0] if self._r else None

    def fetchall(self):
        return list(self._r)


class Boom(Exception):
    pass


class TestSecurity(unittest.TestCase):
    def test_memory_metrics_are_counts_only(self):
        mc = Cur([("source='scanner'", [(2026, 9, 10, 9)]),
                  ("extracted_date", [("imessage", 2019, 11, 40)])])
        mconn = mock.MagicMock()
        mconn.cursor.return_value = mc
        with mock.patch.object(W, "connect", return_value=mconn), \
                mock.patch.object(W, "home", return_value=(40.0, -100.0)):
            rows = D.compute_metrics(Cur([]))
        for sql, _ in mc.calls:
            self.assertNotRegex(sql.split("FROM")[0], r"\btext\b")
        self.assertTrue(all(isinstance(r[3], float) for r in rows))
        mconn.close.assert_called_once()

    def test_home_coords_never_in_rows_or_note(self):
        mconn = mock.MagicMock()
        mconn.cursor.return_value = Cur([])
        cur = Cur([("chp_incidents WHERE lat", [(2026, 9, 3, 25)])])
        with mock.patch.object(W, "connect", return_value=mconn), \
                mock.patch.object(W, "home", return_value=(40.0, -100.0)):
            rows = D.compute_metrics(cur)
        self.assertNotIn("-100", json.dumps(rows))

    def test_sql_parameterized(self):
        self.assertNotRegex((SCRIPTS / "nova_derry_clock.py").read_text(), r"execute\(\s*f[\"']")


class TestPerformance(unittest.TestCase):
    def test_holidays_century(self):
        t = time.time()
        for y in range(2000, 2100):
            D.holidays(y)
        self.assertLess(time.time() - t, 1.0)

    def test_upcoming_with_5k_prior_rows(self):
        rows = [(f"incident:k{i}", 2020 + i % 6, float(i % 3 + 1), 30) for i in range(5000)]
        cur = Cur([("FROM derry_monthly", rows), ("telemetry.incidents", [])])
        t = time.time()
        out = D.upcoming_cycles(30, cur=cur, today=TODAY)
        self.assertLess(time.time() - t, 2.0)
        self.assertTrue(out)


class TestRetry(unittest.TestCase):
    def test_note_notify_retried(self):
        notify = mock.MagicMock(side_effect=[False, Exception("pg"), True])
        conn = mock.MagicMock()
        with mock.patch.object(W, "connect", return_value=conn), mock.patch.object(D, "refresh", return_value=0), \
                mock.patch.object(D, "compose_note", return_value="*Derry*\nbody"), \
                mock.patch.object(W.time, "sleep"), \
                mock.patch.dict(sys.modules, {"nova_notify": mock.MagicMock(notify=notify)}), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(D.main(["--note", "--date", "2026-11-01"]), 0)
        self.assertEqual(notify.call_count, 3)
        self.assertEqual(notify.call_args.kwargs["dedup_key"], "derry-2026-11")

    def test_note_failure_logged(self):
        notify = mock.MagicMock(return_value=False)
        with mock.patch.object(W, "connect"), mock.patch.object(D, "refresh", return_value=0), \
                mock.patch.object(D, "compose_note", return_value="t\nb"), mock.patch.object(W.time, "sleep"), \
                mock.patch.dict(sys.modules, {"nova_notify": mock.MagicMock(notify=notify)}), \
                redirect_stdout(io.StringIO()) as out:
            D.main(["--note"])
        self.assertIn("attempt 3/3", out.getvalue())

    def test_missing_unexplained_table_tolerated(self):
        mconn = mock.MagicMock()
        mconn.cursor.return_value = Cur([])
        cur = Cur([("FROM unexplained_events", Boom("relation does not exist"))])
        with mock.patch.object(W, "connect", return_value=mconn), \
                mock.patch.object(W, "home", return_value=(40.0, -100.0)):
            self.assertEqual(D.compute_metrics(cur), [])


class TestUnit(unittest.TestCase):
    def test_nth_weekday(self):
        self.assertEqual(D.nth_weekday(2026, 1, 0, 3), date(2026, 1, 19))
        self.assertEqual(D.nth_weekday(2026, 5, 0, -1), date(2026, 5, 25))

    def test_compare(self):
        self.assertEqual(D.compare(5, 0, 30, 30), "5 vs 0 last year")
        self.assertIsNone(D.compare(5, None, 30, 30))
        self.assertIsNone(D.compare(5, 4, 30, 19))

    def test_fixed_holidays(self):
        self.assertEqual(D.holidays(2027)[date(2027, 10, 31)], "Halloween")


class TestIntegration(unittest.TestCase):
    def test_cycle_vs_last_year(self):
        rows = [("incident:ups-battery", 2024, 2.0, 30), ("incident:ups-battery", 2025, 3.0, 30),
                ("memories:imessage", 2019, 40.0, 0), ("chp_incidents", 2025, 9.0, 30)]
        cur = Cur([("FROM derry_monthly", rows), ("telemetry.incidents", [])])
        out = D.upcoming_cycles(10, cur=cur, today=TODAY)
        by = {c["label"].split(" ")[0]: c for c in out if c["kind"] != "holiday"}
        self.assertEqual(by["ups-battery"]["strength"], "cycle")
        self.assertEqual(by["imessage"]["strength"], "last_year")
        self.assertNotIn("chp_incidents", by)   # plain metrics are not cycles by themselves

    def test_critical_incident_anniversary(self):
        opened = datetime(2025, 11, 5, 20, tzinfo=timezone.utc)
        cur = Cur([("FROM derry_monthly", []), ("telemetry.incidents", [(opened, "NAS offline")])])
        out = D.upcoming_cycles(10, cur=cur, today=TODAY)
        ann = [c for c in out if c["kind"] == "anniversary"]
        self.assertEqual(ann[0]["date"], date(2026, 11, 5))
        self.assertIn("1 year(s) since critical incident", ann[0]["label"])

    def test_refresh_upserts(self):
        cur = Cur([])
        with mock.patch.object(D, "compute_metrics", return_value=[("m", 2026, 9, 1.0, 30, "e")]), \
                mock.patch.object(W, "ensure_schema"):
            self.assertEqual(D.refresh(cur), 1)
        self.assertIn("ON CONFLICT (metric, year, month) DO UPDATE", cur.calls[0][0])


class TestFunctional(unittest.TestCase):
    def test_note_no_history_is_honest(self):
        cur = Cur([("LEFT JOIN derry_monthly", []), ("min(make_date", [(date(2026, 7, 1),)]),
                   ("FROM derry_monthly WHERE month", []), ("telemetry.incidents", [])])
        note = D.compose_note(cur, TODAY)
        self.assertIn("No telemetry exists for November 2025", note)
        self.assertIn("First like-for-like comparison: July 2027", note)

    def test_note_with_last_year(self):
        cur = Cur([("LEFT JOIN derry_monthly", [("scanner_transmissions", None, None, 1200.0, 30),
                                                ("chp_incidents", None, None, 3.0, 5)]),
                   ("FROM derry_monthly WHERE month", []), ("telemetry.incidents", [])])
        note = D.compose_note(cur, TODAY)
        self.assertIn("scanner_transmissions = 1200 over 30 days", note)
        self.assertNotIn("chp_incidents", note)   # too few days to compare
        self.assertIn("Thanksgiving", note)

    def test_dry_run_note_never_notifies(self):
        notify = mock.MagicMock()
        with mock.patch.object(W, "connect"), mock.patch.object(D, "refresh", return_value=0), \
                mock.patch.object(D, "compose_note", return_value="t\nb"), \
                mock.patch.dict(sys.modules, {"nova_notify": mock.MagicMock(notify=notify)}), \
                redirect_stdout(io.StringIO()):
            D.main(["--note", "--dry-run"])
        notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_main_upcoming(self):
        with mock.patch.object(W, "connect"), \
                mock.patch.object(D, "upcoming_cycles", return_value=[
                    {"date": None, "kind": "holiday", "label": "x", "strength": "calendar", "evidence": "e"}]), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(D.main(["--upcoming", "5"]), 0)
        self.assertIn("(month)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
