#!/usr/bin/env python3
"""Tests for nova_homekit_battery_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_homekit_battery_monitor.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hb = _load("hb", SCRIPT)
hb.notify = MagicMock()                     # safety net: the real nova_notify.notify writes to PG


def _acc(name, room, level=None, low=None):
    chars = []
    if level is not None:
        chars.append({"type": "Battery Level", "value": level})
    if low is not None:
        chars.append({"type": "Status Low Battery", "value": low})
    return {"name": name, "room": room, "services": [{"characteristics": chars}]}


def _body(accessories):
    raw = json.dumps(accessories).encode()
    return raw + b" " * max(0, 1100 - len(raw))                   # the real payload is large; pad past the 1000-byte sanity floor


def _resp(raw):
    r = MagicMock(); r.read.return_value = raw
    r.__enter__ = lambda s: s; r.__exit__ = lambda s, *a: False
    return r


def _run(accessories=None, urlopen=None):
    """Run main() with HomeKit, PG, notify and sleep stubbed. Returns (rc, stdout, cursor, notify mock, connect mock)."""
    cur = MagicMock(); conn = MagicMock(); conn.cursor.return_value = cur
    u = urlopen or MagicMock(return_value=_resp(_body(accessories or [])))
    out = io.StringIO()
    with patch.object(hb.urllib.request, "urlopen", u), patch.object(hb.psycopg2, "connect", MagicMock(return_value=conn)) as connect, \
         patch.object(hb, "notify", MagicMock()) as notify, patch("time.sleep"), redirect_stdout(out):
        rc = hb.main()
    return rc, out.getvalue(), cur, notify, connect


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", hb.DSN)

    def test_sql_is_parameterized_and_scoped_to_telemetry_battery(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"telemetry.battery"})
        rc, _, cur, _, _ = _run([_acc("x'); SELECT pg_sleep(9); --", "hall", 50, 0)])
        sql, params = cur.execute.call_args[0]
        self.assertNotIn("pg_sleep", sql)
        self.assertEqual(params[0], "x'); SELECT pg_sleep(9); --")

    def test_homekit_endpoint_is_loopback_and_read_only(self):
        self.assertTrue(hb.HOMEKIT_URL.startswith("http://127.0.0.1:37433/"))
        self.assertNotIn("method=", SRC)                                # a plain GET, never a write to HomeKit
        self.assertIn("hk.auth_headers()", SRC)                        # NovaHomeKit 51e7a91 Bearer token


class TestPerformance(unittest.TestCase):
    def test_extract_batteries_10k_accessories_under_bound(self):
        accs = [_acc(f"d{i}", "r", i % 100, i % 2) for i in range(10_000)]
        t0 = time.perf_counter()
        rows = list(hb.extract_batteries(accs))
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(rows), 10_000)


class TestRetry(unittest.TestCase):
    def test_fetch_retries_with_backoff_then_succeeds(self):
        good = _resp(_body([_acc("a", "r", 90, 0)]))
        u = MagicMock(side_effect=[OSError("cold"), OSError("cold"), good])
        with patch.object(hb.urllib.request, "urlopen", u), patch("time.sleep") as sl, redirect_stdout(io.StringIO()):
            data = hb.fetch_accessories()
        self.assertEqual(u.call_count, 3)
        self.assertEqual(sl.call_args_list, [((4,),), ((4,),)])
        self.assertEqual(data[0]["name"], "a")

    def test_short_body_counts_as_a_failed_attempt(self):
        u = MagicMock(side_effect=[_resp(b"[]"), _resp(_body([_acc("a", "r", 1, 1)]))])
        with patch.object(hb.urllib.request, "urlopen", u), patch("time.sleep"), redirect_stdout(io.StringIO()):
            self.assertEqual(len(hb.fetch_accessories()), 1)
        self.assertEqual(u.call_count, 2)

    def test_exhausted_retries_fail_open_with_none(self):
        u = MagicMock(side_effect=OSError("down"))
        with patch.object(hb.urllib.request, "urlopen", u), patch("time.sleep") as sl, redirect_stdout(io.StringIO()) as out:
            self.assertIsNone(hb.fetch_accessories(retries=3))
        self.assertEqual((u.call_count, sl.call_count), (3, 3))
        self.assertEqual(out.getvalue().count("fetch attempt"), 3)

    def test_notify_failure_is_swallowed_per_device(self):
        # RETRY GAP: notify() — one attempt per low device; a failing bus is logged and the run still returns 0
        cur = MagicMock(); conn = MagicMock(); conn.cursor.return_value = cur
        with patch.object(hb.urllib.request, "urlopen", MagicMock(return_value=_resp(_body([_acc("a", "r", 5, 1)])))), \
             patch.object(hb.psycopg2, "connect", MagicMock(return_value=conn)), patch.object(hb, "notify", MagicMock(side_effect=RuntimeError("bus"))), \
             patch("time.sleep"), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(hb.main(), 0)
        self.assertIn("notify failed for a: bus", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_extract_edges(self):
        self.assertEqual(list(hb.extract_batteries([])), [])
        self.assertEqual(list(hb.extract_batteries([{"name": "n", "room": "r"}])), [])          # no services
        self.assertEqual(list(hb.extract_batteries([{"name": "n", "services": None}])), [])
        self.assertEqual(list(hb.extract_batteries([_acc("n", "r", "abc")])), [])               # bad level, no low flag
        self.assertEqual(list(hb.extract_batteries([_acc("n", "r", "42", 1)])), [("n", "r", 42, True)])
        self.assertEqual(list(hb.extract_batteries([_acc("n", "r", None, 7)])), [])                        # odd flag -> None -> no row
        self.assertEqual(list(hb.extract_batteries([_acc("n", "r", 50, 7)])), [("n", "r", 50, None)])
        self.assertEqual(list(hb.extract_batteries([_acc("n", None, 10, False)])), [("n", None, 10, False)])

    def test_thresholds(self):
        self.assertEqual((hb.LOW_THRESHOLD, hb.WARN_THRESHOLD), (20, 40))

    def test_ensure_table_creates_telemetry_battery(self):
        cur = MagicMock()
        hb.ensure_table(cur)
        self.assertIn("CREATE TABLE IF NOT EXISTS telemetry.battery", cur.execute.call_args[0][0])


class TestIntegration(unittest.TestCase):
    def test_low_devices_are_deduped_per_device_through_the_notify_bus(self):
        rc, _, cur, notify, _ = _run([_acc("Door", "porch", 20, 0), _acc("Hall", "hall", 21, 0), _acc("Flag", "attic", None, 1)])
        self.assertEqual([c[0][0] for c in notify.call_args_list], ["Low battery — Door", "Low battery — Flag"])
        kw = notify.call_args_list[0][1]
        self.assertEqual((kw["dedup_key"], kw["category"], kw["level"], kw["source"]), ("battery-low:Door", "battery", "warning", "nova_homekit_battery_monitor.py"))
        self.assertEqual(kw["meta"], {"room": "porch", "level": 20})
        self.assertIn("low-battery flag set", notify.call_args_list[1][1]["body"])

    def test_rows_are_inserted_in_order_before_alerting(self):
        rc, _, cur, notify, _ = _run([_acc("A", "r", 50, 0), _acc("B", "r", 10, 1)])
        inserts = [c[0][1] for c in cur.execute.call_args_list if c[0][0].startswith("INSERT")]
        self.assertEqual(inserts, [("A", "r", 50, False), ("B", "r", 10, True)])


class TestFunctional(unittest.TestCase):
    def test_golden_path(self):
        rc, out, cur, notify, connect = _run([_acc("Door", "porch", 80, 0), _acc("Leak", "bath", 12, 1)])
        self.assertEqual(rc, 0)
        self.assertEqual(connect.call_args[0][0], hb.DSN)
        self.assertEqual(sum(1 for c in cur.execute.call_args_list if c[0][0].startswith("INSERT")), 2)
        self.assertIn("bath/Leak: level=12 low=True  <-- LOW", out)
        self.assertIn("checked 2 battery devices, 1 low.", out)
        notify.assert_called_once()

    def test_error_path_unreachable_homekit_skips_pg(self):
        rc, out, cur, notify, connect = _run(urlopen=MagicMock(side_effect=OSError("down")))
        self.assertEqual(rc, 1)
        self.assertIn("HomeKit unreachable", out)
        connect.assert_not_called()
        notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_is_silent_and_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_homekit_battery_monitor"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
