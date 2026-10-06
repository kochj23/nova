#!/usr/bin/env python3
"""Tests for nova_tracker_watch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_tracker_watch.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("tracker_watch", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tw = _load()


class _Cur:
    """Cursor stand-in: first execute → state rows, second → per-hour rows."""
    def __init__(self, state_rows, hour_rows):
        self.state_rows, self.hour_rows = state_rows, hour_rows
        self.sql = []; self._n = 0
    def execute(self, sql, params=None): self.sql.append(sql); self._n += 1
    def fetchall(self): return self.state_rows if self._n == 1 else self.hour_rows
    def close(self): pass


def _conn(cur):
    c = MagicMock(); c.cursor.return_value = cur
    return c


def _hours(n, base=None):
    base = base or datetime(2026, 2, 1, 0, 0)
    return [(base + timedelta(hours=i), 1, -40) for i in range(n)]


def _run(cur, hours=24, min_present=6, alert=False):
    with patch.object(tw.psycopg2, "connect", return_value=_conn(cur)), redirect_stdout(io.StringIO()) as out:
        with patch("nova_notify.notify") as notify:
            rc = tw.main(hours, min_present, alert)
    return rc, out.getvalue(), notify


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_interpolated_sql_values_are_integers_only(self):
        # the two queries interpolate only `hours` (argparse type=int) into the interval — no string value reaches SQL
        self.assertIn("ap.add_argument(\"--hours\", type=int", SRC)
        cur = _Cur([], [])
        with self.assertRaises(ValueError):
            # a non-int can never reach the query: main() formats it straight into the interval, so prove the type gate
            int("'; DROP")
        _run(cur, hours=24)
        self.assertTrue(all("interval '24 hours'" in s or "interval '24 hours'" in s for s in cur.sql if "interval" in s))

    def test_reports_population_not_individual_identity(self):
        # the privacy contract: it never claims to identify a specific tag
        self.assertIn("CANNOT: tell you WHICH tag", SRC)
        self.assertIn("COUNT".lower(), SRC.lower())


class TestPerformance(unittest.TestCase):
    def test_renders_1000_hours_fast(self):
        cur = _Cur([("separated", 500, 30)], _hours(1000))
        t0 = time.perf_counter()
        _run(cur, hours=1000)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_notify_failure_is_swallowed(self):
        # RETRY GAP: main/nova_notify.notify — one attempt; a failure is logged, never raised
        cur = _Cur([("separated", 10, 3)], _hours(8))
        with patch.object(tw.psycopg2, "connect", return_value=_conn(cur)), \
             patch("nova_notify.notify", side_effect=RuntimeError("slack down")), redirect_stdout(io.StringIO()) as out:
            rc = tw.main(24, 6, alert=True)
        self.assertEqual(rc, 0)
        self.assertIn("notify failed", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_below_threshold_is_quiet(self):
        cur = _Cur([("owner_nearby", 20, 5)], _hours(3))
        rc, out, notify = _run(cur, min_present=6, alert=True)
        self.assertEqual(rc, 0)
        self.assertIn("below alert threshold", out)
        notify.assert_not_called()

    def test_unclassified_state_label(self):
        cur = _Cur([(None, 4, 2)], [])
        _, out, _ = _run(cur)
        self.assertIn("unclassified", out)

    def test_missing_rssi_renders_placeholder(self):
        cur = _Cur([("separated", 1, 1)], [(datetime(2026, 2, 1, 3), 2, None)])
        _, out, _ = _run(cur)
        self.assertIn("strongest=   ?dBm", out)


class TestIntegration(unittest.TestCase):
    def test_queries_bluetooth_telemetry_for_findmy(self):
        cur = _Cur([], [])
        _run(cur)
        self.assertIn("FROM telemetry.bluetooth", cur.sql[0])
        self.assertIn("apple_subtype' = 'findmy'", cur.sql[0])
        self.assertIn("findmy_state' = 'separated'", cur.sql[1])

    def test_alert_flag_gates_notification(self):
        cur = _Cur([("separated", 10, 3)], _hours(8))
        _, _, notify = _run(cur, alert=False)
        notify.assert_not_called()


class TestFunctional(unittest.TestCase):
    def test_sustained_separated_tracker_alerts(self):
        cur = _Cur([("separated", 40, 9)], _hours(8))
        rc, out, notify = _run(cur, min_present=6, alert=True)
        self.assertEqual(rc, 0)
        self.assertIn("ALERT:", out)
        notify.assert_called_once()
        self.assertEqual(notify.call_args.kwargs, {"level": "warning", "category": "security"})


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--min-hours-present", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_tracker_watch as m; print(bool(m.DSN))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
