#!/usr/bin/env python3
"""Tests for nova_ble_churn_report.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ble_churn_report.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="ble-churn-test-"))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock()
    with patch.dict(sys.modules, {"nova_notify": nn}), patch.object(Path, "home", classmethod(lambda c: TMP)):
        spec.loader.exec_module(mod)
    return mod


bc = _load("ble_churn_under_test", SCRIPT)
assert str(bc.LOG_FILE).startswith(str(TMP))
bc.notify = MagicMock()
bc.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=OSError("offline: pg stubbed")),
                                    extras=types.SimpleNamespace(RealDictCursor="RealDictCursor", Json=lambda d: ("JSON", d)))


class _Cur:
    def __init__(self, new=(), gone=(), anon=0, raise_on=()):
        self.new, self.gone, self.anon, self.raise_on = list(new), list(gone), anon, tuple(raise_on)
        self.sql, self.params, self.closed, self._last = [], [], False, ""

    def execute(self, sql, params=None):
        s = " ".join(sql.split()); self.sql.append(s); self.params.append(params); self._last = s
        for frag in self.raise_on:
            if frag in s:
                raise RuntimeError(f"stub failure on {frag}")

    def fetchall(self):
        return self.new if "day_rank = 2" in self._last else self.gone

    def fetchone(self): return {"count": self.anon}
    def close(self): self.closed = True
    def ran(self, frag): return [(s, p) for s, p in zip(self.sql, self.params) if frag in s]


class _Conn:
    def __init__(self, cur): self.cur = cur; self.commits = 0; self.closed = False
    def cursor(self, cursor_factory=None): self.cur.factory = cursor_factory; return self.cur
    def commit(self): self.commits += 1
    def close(self): self.closed = True


def _run(cur):
    conn = _Conn(cur); bc.notify = MagicMock()
    with patch.object(bc.psycopg2, "connect", MagicMock(return_value=conn)) as pg, redirect_stdout(io.StringIO()) as out:
        bc.run()
    return conn, pg, out.getvalue()


NEW = [{"device_mac": "AA:BB:CC:DD:EE:01", "device_name": "Car Alarm", "first_seen": date(2026, 10, 5)}]
GONE = [{"device_mac": "11:22:33:44:55:66", "device_name": "Fitbit", "days_seen": 12, "last_seen": datetime(2026, 10, 1, 8, 30)}]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC)); self.assertNotIn("password", bc.DSN)

    def test_sql_is_parameterized_and_writes_only_shared_observations(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        writes = {m.group(1) for m in re.finditer(r"\b(?:INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"shared_observations"})
        evil = "x'); DROP TABLE shared_observations; --"
        cur = _Cur(new=[{"device_mac": evil, "device_name": evil, "first_seen": date.today()}])
        _run(cur)
        sql, params = cur.ran("INSERT INTO shared_observations")[0]
        self.assertNotIn("DROP", sql); self.assertIn(evil, params[0]); self.assertEqual(params[1][1]["mac"], evil)

    def test_only_named_devices_are_tracked_individually(self):
        self.assertIn("device_name IS NOT NULL AND device_name != ''", SRC)
        cur = _Cur(anon=650); _run(cur)
        body = bc.notify.call_args[1]["body"]
        self.assertIn("650 anonymous/randomized-MAC devices seen today, not tracked individually", body)
        self.assertEqual(cur.ran("INSERT INTO"), [])


class TestPerformance(unittest.TestCase):
    def test_10k_churn_rows_processed_quickly_and_message_capped(self):
        new = [{"device_mac": f"AA:BB:CC:{i >> 8:02X}:{i & 255:02X}:00", "device_name": f"dev{i}", "first_seen": date.today()} for i in range(5_000)]
        gone = [{"device_mac": f"BB:BB:CC:{i >> 8:02X}:{i & 255:02X}:00", "device_name": f"old{i}", "days_seen": 11, "last_seen": datetime(2026, 10, 1)} for i in range(5_000)]
        cur = _Cur(new=new, gone=gone)
        t0 = time.perf_counter(); _run(cur)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(cur.ran("INSERT INTO")), 10_000)
        body = bc.notify.call_args[1]["body"]
        self.assertEqual(body.count("\n  + "), 15); self.assertEqual(body.count("\n  - "), 15)


class TestRetry(unittest.TestCase):
    def test_pg_connect_failure_propagates(self):
        # RETRY GAP: run/psycopg2.connect — one attempt; the exception escapes to the scheduler
        connect = MagicMock(side_effect=OSError("pg down")); bc.notify = MagicMock()
        with patch.object(bc.psycopg2, "connect", connect):
            with self.assertRaises(OSError):
                bc.run()
        self.assertEqual(connect.call_count, 1); bc.notify.assert_not_called()

    def test_notify_failure_is_swallowed_after_commit(self):
        # RETRY GAP: notify — one attempt; failure is logged, observations already committed, conn still closed
        cur = _Cur(new=NEW); conn = _Conn(cur)
        with patch.object(bc.psycopg2, "connect", MagicMock(return_value=conn)), patch.object(bc, "notify", MagicMock(side_effect=OSError("slack down"))), \
             redirect_stdout(io.StringIO()) as out:
            bc.run()
        self.assertEqual(conn.commits, 1); self.assertTrue(conn.closed and cur.closed)
        self.assertIn("Notify failed: slack down", out.getvalue()); self.assertIn("Report complete", out.getvalue())

    def test_insert_failure_propagates_without_commit(self):
        # RETRY GAP: shared_observations insert — no retry, no partial commit
        cur = _Cur(new=NEW, raise_on=("INSERT INTO",)); conn = _Conn(cur); bc.notify = MagicMock()
        with patch.object(bc.psycopg2, "connect", MagicMock(return_value=conn)), redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                bc.run()
        self.assertEqual(conn.commits, 0); bc.notify.assert_not_called()


class TestUnit(unittest.TestCase):
    def test_log_appends_to_tempdir_and_prints(self):
        bc.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)   # the tempdir's logs/ subdir is not created by import
        with redirect_stdout(io.StringIO()) as out:
            bc.log("unit-line")
        self.assertEqual(out.getvalue(), f"[ble_churn {date.today().isoformat()}] unit-line\n")
        self.assertIn("unit-line", bc.LOG_FILE.read_text())

    def test_log_survives_unwritable_file(self):
        with patch.object(bc, "LOG_FILE", TMP / "no-such-dir" / "x.log"), redirect_stdout(io.StringIO()) as out:
            bc.log("still prints")
        self.assertIn("still prints", out.getvalue())

    def test_queries_define_new_and_gone_as_documented(self):
        self.assertIn("WHERE day_rank = 2 AND d = current_date", SRC)                      # second distinct day = "new"
        self.assertIn("WHERE days_seen >= 10 AND last_seen < now() - interval '3 days'", SRC)  # gone


class TestIntegration(unittest.TestCase):
    def test_notify_is_the_shared_helper_and_cursor_is_realdict(self):
        self.assertIn("from nova_notify import notify", SRC); self.assertNotIn("def notify", SRC)
        cur = _Cur(); _run(cur)
        self.assertEqual(cur.factory, "RealDictCursor")

    def test_observations_feed_the_shared_context_with_metadata(self):
        cur = _Cur(new=NEW, gone=GONE); _run(cur)
        inserts = cur.ran("INSERT INTO shared_observations")
        self.assertEqual(len(inserts), 2)
        self.assertIn("'nova_ble_churn_report', 'network', 'ble-new-device'", inserts[0][0])
        self.assertEqual(inserts[0][1][0], "New Bluetooth device seen for the first time: Car Alarm (AA:BB:CC:DD:EE:01)")
        self.assertEqual(inserts[0][1][1], ("JSON", {"mac": "AA:BB:CC:DD:EE:01", "name": "Car Alarm"}))
        self.assertIn("'ble-device-gone'", inserts[1][0])
        self.assertIn("Fitbit (11:22:33:44:55:66), last seen 2026-10-01 08:30", inserts[1][1][0])
        self.assertEqual(inserts[1][1][1][1]["last_seen"], "2026-10-01T08:30:00")


class TestFunctional(unittest.TestCase):
    def test_golden_path_queries_writes_commits_and_notifies(self):
        cur = _Cur(new=NEW, gone=GONE, anon=42); conn, pg, out = _run(cur)
        pg.assert_called_once_with(bc.DSN)
        self.assertEqual(cur.sql[0].split()[0], "WITH"); self.assertEqual(len([s for s in cur.sql if s.startswith(("WITH", "SELECT"))]), 3)
        self.assertEqual(conn.commits, 1); self.assertTrue(conn.closed and cur.closed)
        title, kw = bc.notify.call_args[0][0], bc.notify.call_args[1]
        self.assertEqual(title, f"Bluetooth Churn Report ({date.today().isoformat()})")
        self.assertEqual((kw["level"], kw["category"], kw["dedup_key"]), ("info", "network", "ble-churn-daily"))
        self.assertTrue(kw["body"].startswith("Bluetooth churn — 1 new, 1 gone (named devices only; 42 anonymous"))
        self.assertIn("New:\n  + Car Alarm (AA:BB:CC…)", kw["body"]); self.assertIn("Gone:\n  - Fitbit (11:22:33…), last seen 10-01", kw["body"])
        self.assertIn("new=1 gone=1 anon_today=42", out); self.assertIn("Report complete", out)

    def test_quiet_day_still_notifies_without_sections(self):
        cur = _Cur(); conn, pg, out = _run(cur)
        body = bc.notify.call_args[1]["body"]
        self.assertNotIn("New:", body); self.assertNotIn("Gone:", body); self.assertEqual(cur.ran("INSERT INTO"), [])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_the_report(self):
        self.assertIn('if __name__ == "__main__":\n    run()', SRC)
        box = TMP / "frame-home"; box.mkdir(exist_ok=True)
        r = subprocess.run([sys.executable, "-c", "import nova_ble_churn_report as m; print('IMPORT-OK', m.DSN.split()[1])"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": str(box)})
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(r.stdout.strip(), "IMPORT-OK dbname=nova_ops")


if __name__ == "__main__":
    unittest.main()
