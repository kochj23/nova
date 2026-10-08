#!/usr/bin/env python3
"""Tests for nova_pendulum.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import os
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_pendulum as P  # noqa: E402

SRC = (SCRIPTS / "nova_pendulum.py").read_text()


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = next((list(v) for k, v in self.routes.items() if k in sql), [])

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def fake_conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    c.info.host = "pg-primary.example"
    return c


def routes():
    now = datetime.now(timezone.utc)
    disk = [("node_status", "studio", "root", now - timedelta(days=13 - d), 50.0 + d, 95.0, 40.0) for d in range(14)]
    disk += [("storage_metrics", "nas-ip", "pool", now - timedelta(days=13 - d), 70.0, 95.0, None) for d in range(14)]
    disk += [("node_status", "ignored-host", "root", now, 99.0, 95.0, 1.0)]
    return {
        "FROM service_config": [],
        "FROM telemetry.disk_forecast": disk,
        "FROM telemetry.net_inventory": [("nas-ip", "UNAS")],
        "FROM telemetry.cert_expiry": [("nova", 12.0), ("udm", 600.0)],
        "to_regclass": [("pendulum_blades",)],
        "FROM pendulum_blades WHERE kind='pg'": [(now - timedelta(days=d), 100e9 - d * 1e9) for d in range(6, 0, -1)],
        "FROM pg_database": [(101e9, "127.0.0.1")],
        "FROM capacity_snapshots": [("core", 150.0)],
    }


class TestSecurity(unittest.TestCase):
    def test_sql_parameterized_and_no_secrets(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_or_hardcoded_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def test_hostile_blade_name_stays_a_parameter(self):
        evil = "x'; DROP TABLE pendulum_blades; --"
        cur = FakeCur()
        P.write(cur, datetime.now(timezone.utc), [{"blade": evil, "kind": "disk", "value": 1.0, "unit": "pct_used",
                                                   "days": 3.0, "days_lo": None, "days_hi": None,
                                                   "accelerating": False, "inside_lead": True, "note": None}])
        ins = [(s, p) for s, p in cur.sql if "INSERT INTO pendulum_blades" in s][0]
        self.assertNotIn(evil, ins[0])
        self.assertIn(evil, ins[1])

    def test_never_pages(self):
        for w in ("post_slack", "post_both", "notify(", "claude_queue"):
            self.assertNotIn(w, SRC)


class TestPerformance(unittest.TestCase):
    def test_daily_and_fit_on_10k_points(self):
        t0 = datetime(2026, 9, 1, tzinfo=timezone.utc)
        pts = [(t0 + timedelta(minutes=2 * i), 50 + i * 0.001) for i in range(10000)]
        t = time.monotonic()
        d = P.daily(pts)
        b = P.blade(d, 40.0)
        self.assertLess(time.monotonic() - t, 3.0)
        self.assertIsNotNone(b["days"])


class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff(self):
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("connection refused")
            return mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=flaky):
            P.W.connect(_sleep=lambda s: None)
        self.assertEqual(calls["n"], 3)

    def test_query_failure_fails_open(self):
        # RETRY GAP: _q — a failed read is not retried; each blade degrades to "nothing known"
        with mock.patch("builtins.print"):
            cur = FakeCur(boom=True)
            self.assertEqual(P.disk_blades(cur, P.DISKS), [])
            self.assertEqual(P.cert_blades(cur), [])
            self.assertIsNone(P.pg_blade(cur, []))
            self.assertEqual(P.prior_pg(cur), [])
            self.assertEqual(P.config(cur), (P.DISKS, P.LEAD_DAYS))

    def test_unresolvable_dsn_host_fails_open(self):
        cur = FakeCur({"FROM pg_database": [(1e9, "127.0.0.1")], "FROM capacity_snapshots": []})
        with mock.patch("socket.gethostbyname", side_effect=OSError("nxdomain")), mock.patch("builtins.print"):
            b = P.pg_blade(cur, [], "nowhere.example")
        self.assertIsNone(b["days"])
        self.assertIn("unknown", b["note"])


class TestUnit(unittest.TestCase):
    def test_theil_sen_ignores_a_step(self):
        pts = [(i, float(i)) for i in range(10)]
        pts[5] = (5, 50.0)                         # one bulk-ingest spike
        self.assertAlmostEqual(P.theil_sen(pts)[0], 1.0, places=6)
        self.assertEqual(P.theil_sen([(0, 1.0)]), (None, None, None))

    def test_blade_edges(self):
        self.assertTrue(P.blade([], 10.0)["note"].startswith("learning"))
        self.assertIsNone(P.blade([(i, 5.0 - i) for i in range(6)], 10.0)["days"])
        self.assertEqual(P.blade([(i, float(i)) for i in range(6)], 0.0)["days"], 0.0)
        self.assertEqual(P.blade([(i, i * 1e-9) for i in range(6)], 1e6)["days"], P.MAX_DAYS)

    def test_lead_and_line(self):
        b = P.flag_lead({"blade": "cert:nova", "kind": "cert", "days": 12.0, "days_lo": None,
                         "accelerating": False}, P.LEAD_DAYS)
        self.assertTrue(b["inside_lead"])
        self.assertIn("inside its 21-day lead", P.watch_bill_line([b]))
        self.assertFalse(P.flag_lead({"kind": "disk", "days": None}, P.LEAD_DAYS)["inside_lead"])
        self.assertEqual(P._range({"days_lo": 5.0, "days_hi": None}), " (>= 5)")

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(P.selftest(), 0)


class TestIntegration(unittest.TestCase):
    def test_reuses_existing_collectors(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("telemetry.disk_forecast", SRC)
        self.assertIn("telemetry.cert_expiry", SRC)
        self.assertIn("capacity_snapshots", SRC)

    def test_config_keys(self):
        cur = FakeCur({"FROM service_config": []})
        P.config(cur)
        self.assertEqual([p for s, p in cur.sql if "service_config" in s],
                         [("pendulum", "disks"), ("pendulum", "lead_days")])

    def test_disk_blades_filter_and_label(self):
        bl = {b["blade"]: b for b in P.disk_blades(FakeCur(routes()), P.DISKS + [["node_status", "studio"]])}
        self.assertEqual(set(bl), {"studio:root", "UNAS:pool"})          # ignored-host not configured
        self.assertAlmostEqual(bl["studio:root"]["days"], 32.0, delta=1.5)  # 95 - 63 at 1 pct/day
        self.assertIn("OLS says 40 d", bl["studio:root"]["note"])
        self.assertIsNone(bl["UNAS:pool"]["days"])

    def test_pg_blade_resolves_loopback_to_dsn_host(self):
        with mock.patch("socket.gethostbyname", return_value="core-ip") as gh:
            b = P.pg_blade(FakeCur(routes()), [(datetime.now(timezone.utc) - timedelta(days=d), 100e9 - d * 1e9)
                                               for d in range(6, 0, -1)], "pg-primary.example")
        gh.assert_called_once_with("pg-primary.example")
        self.assertEqual(b["blade"], "pg:core")
        self.assertAlmostEqual(b["days"], 150.0, delta=10)


class TestFunctional(unittest.TestCase):
    def _run(self, dry):
        cur = FakeCur(routes())
        with mock.patch.object(P.W, "connect", return_value=fake_conn(cur)), \
                mock.patch("socket.gethostbyname", return_value="core-ip"), mock.patch("builtins.print") as pr:
            blades = P.run(dry=dry)
        return cur, blades, pr

    def test_run_writes_every_blade_nearest_first(self):
        cur, blades, pr = self._run(False)
        kinds = {b["kind"] for b in blades}
        self.assertEqual(kinds, {"disk", "cert", "pg"})
        self.assertEqual(blades[0]["blade"], "cert:nova")
        self.assertTrue(blades[0]["inside_lead"])
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS pendulum_blades" in s for s, _ in cur.sql))
        self.assertEqual(sum("INSERT INTO pendulum_blades" in s for s, _ in cur.sql), len(blades))
        self.assertTrue(any("Pendulum: nearest blade is cert:nova" in str(c) for c in pr.call_args_list))

    def test_dry_run_writes_nothing(self):
        cur, blades, _ = self._run(True)
        self.assertTrue(blades)
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE", "DELETE")))

    def test_line_from_empty_table(self):
        cur = FakeCur({"FROM pendulum_blades WHERE ts": []})
        with mock.patch.object(P.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print") as pr:
            self.assertEqual(P.show(line_only=True), 0)
        pr.assert_called_with("Pendulum: no blade is falling.")


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_pendulum.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_pendulum.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
