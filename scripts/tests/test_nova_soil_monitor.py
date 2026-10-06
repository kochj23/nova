#!/usr/bin/env python3
"""Tests for nova_soil_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


so = _load("nova_soil_monitor_t", SCRIPTS / "nova_soil_monitor.py")
so.nova_notify = types.SimpleNamespace(notify=MagicMock())       # notification bus stubbed at load
so.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=RuntimeError("offline")))
SRC = (SCRIPTS / "nova_soil_monitor.py").read_text()


class _DB:
    """One fake PG serving both the telemetry read and the alert-state table (kept across runs)."""
    def __init__(self, readings):
        self.readings = readings; self.state = {}; self.sql = []

    def connect(self, dsn):
        db = self
        conn = MagicMock()
        cur = MagicMock()
        rows = {"v": []}

        def execute(sql, params=None):
            db.sql.append((sql, params))
            if "telemetry.soil" in sql:
                rows["v"] = [(s, pct, ts) for s, (pct, ts) in db.readings.items()]
            elif sql.startswith("SELECT sensor, state"):
                rows["v"] = [(s, st, ts) for s, (st, ts) in db.state.items()]
            elif sql.startswith("INSERT INTO public.soil_alert_state"):
                sensor, state, alert_ts, pct, alerted = params
                prev_ts = db.state.get(sensor, (None, None))[1]
                db.state[sensor] = (state, alert_ts if alerted else prev_ts)
        cur.execute.side_effect = execute
        cur.fetchall.side_effect = lambda: rows["v"]
        conn.cursor.return_value = cur
        conn.cursor.return_value.__enter__.return_value = cur
        return conn


def _main(db, now):
    so.nova_notify.notify.reset_mock()
    db.readings = {k: (pct, now) for k, (pct, _) in db.readings.items()}      # fresh readings each run
    with patch.object(so.psycopg2, "connect", side_effect=db.connect), redirect_stdout(io.StringIO()) as out:
        so.main(now)
    return out.getvalue()


def _now():
    return datetime.now(timezone.utc)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", so.DSN)

    def test_state_upsert_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))
        now = _now()
        db = _DB({"soil1": (50, now), "soil2": (50, now), "soil3": (50, now)})
        _main(db, now)
        ins = [p for s, p in db.sql if s.startswith("INSERT")]
        self.assertEqual(len(ins), 3)
        self.assertTrue(all(len(p) == 5 for p in ins))


class TestPerformance(unittest.TestCase):
    def test_should_alert_10k(self):
        t0 = _now()
        st = ("ok", "warning", "critical", "stale")
        start = time.perf_counter()
        for i in range(10_000):
            so._should_alert(st[i % 4], t0, st[(i + 1) % 4], t0 + timedelta(hours=i % 30))
        self.assertLess(time.perf_counter() - start, 2.0)


class TestRetry(unittest.TestCase):
    def test_pg_down_raises_before_any_alert(self):
        # RETRY GAP: check()/psycopg2.connect — one attempt, no retry; the 30-min launchd cadence is the
        # retry, and nothing is sent on a run that could not read the sensors.
        so.nova_notify.notify.reset_mock()
        with patch.object(so.psycopg2, "connect", side_effect=RuntimeError("pg down")) as c:
            with self.assertRaises(RuntimeError):
                so.main(_now())
        self.assertEqual(c.call_count, 1)
        so.nova_notify.notify.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_selfcheck(self):
        with redirect_stdout(io.StringIO()) as out:
            so._selfcheck()
        self.assertIn("selfcheck ok", out.getvalue())

    def test_should_alert_transitions(self):
        t0 = _now()
        self.assertEqual(so._should_alert(None, None, "ok", t0), (False, "ok"))
        self.assertEqual(so._should_alert("critical", t0, "warning", t0), (False, "same-problem"))
        self.assertEqual(so._should_alert("critical", None, "critical", t0), (True, "daily-reminder"))
        self.assertEqual(so._should_alert("stale", t0, "stale", t0 + timedelta(days=3)), (False, "debounced"))

    def test_check_classifies_each_sensor(self):
        now = _now()
        db = _DB({"soil1": (20, now), "soil2": (33, now), "soil3": (60, now - timedelta(hours=5))})
        with patch.object(so.psycopg2, "connect", side_effect=db.connect):
            res = {s: st for s, st, *_ in so.check(now)}
        self.assertEqual(res, {"soil1": "critical", "soil2": "warning", "soil3": "stale"})
        db2 = _DB({})
        with patch.object(so.psycopg2, "connect", side_effect=db2.connect):
            self.assertEqual({st for _, st, *_ in so.check(now)}, {"missing"})


class TestIntegration(unittest.TestCase):
    def test_notification_shape(self):
        now = _now()
        db = _DB({"soil1": (10, now), "soil2": (50, now), "soil3": (50, now)})
        _main(db, now)
        kw = so.nova_notify.notify.call_args[1]
        self.assertEqual((kw["title"], kw["level"], kw["category"], kw["dedup_key"]),
                         ("Soil moisture: First raised bed", "critical", "garden", "soil-low:soil1"))
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS public.soil_alert_state" in s for s, _ in db.sql))


class TestFunctional(unittest.TestCase):
    def test_debounce_across_runs(self):
        now = _now()
        db = _DB({"soil1": (30, now), "soil2": (50, now), "soil3": (50, now)})
        _main(db, now)                                             # ok -> warning: alert
        self.assertEqual(so.nova_notify.notify.call_count, 1)
        out = _main(db, now + timedelta(minutes=30))              # still warning: quiet
        self.assertEqual(so.nova_notify.notify.call_count, 0)
        self.assertIn("no new soil alerts", out)
        db.readings["soil1"] = (20, now)
        _main(db, now + timedelta(hours=1))                        # escalation: alert
        self.assertEqual(so.nova_notify.notify.call_count, 1)
        db.readings["soil1"] = (55, now)
        _main(db, now + timedelta(hours=2))                        # recovery: quiet, state reset
        self.assertEqual(db.state["soil1"][0], "ok")
        db.readings["soil1"] = (30, now)
        _main(db, now + timedelta(hours=3))                        # new dry spell: alert again
        self.assertEqual(so.nova_notify.notify.call_count, 1)


class TestFrame(unittest.TestCase):
    def test_selfcheck_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_soil_monitor.py"), "--selfcheck"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selfcheck ok", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_soil_monitor"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
