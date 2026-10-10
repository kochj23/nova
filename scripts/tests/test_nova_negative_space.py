#!/usr/bin/env python3
"""Tests for nova_negative_space.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import datetime as _dt
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_negative_space.py"
SRC = PATH.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_negative_space_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ns = _load()


class _Cur:
    """Answers the detector's queries in source order: arrivals, owned BLE, wifi, stale devices,
    humans, observers. (The quiet-sensor query moved to nova_freshness_monitor on 2026-10-09.)"""
    def __init__(self, arrivals=0, owned=1, wifi=1, stale=(), humans=5, observers=()):
        self.answers = [[(arrivals,)], [(owned,)], [(wifi,)], list(stale), [(humans,)], list(observers)]
        self.sql, self.params = [], []

    def execute(self, sql, args=()):
        self.sql.append(sql); self.params.append(args)

    def fetchall(self):
        return self.answers.pop(0)


def _run(cur, alert=True):
    conn = mock.Mock(); conn.cursor.return_value = cur
    with mock.patch.object(ns.psycopg2, "connect", return_value=conn) as connect, \
            mock.patch("nova_notify.notify") as notify, \
            mock.patch("sys.stdout", new_callable=io.StringIO) as out:
        rc = ns.main(alert)
    return rc, notify, out.getvalue(), conn, connect


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password=", ns.DSN)

    def test_read_only_static_sql(self):
        self.assertNotRegex(SRC, r"\b(INSERT INTO|DELETE FROM|UPDATE \w+ SET|DROP |TRUNCATE)\b")
        self.assertNotRegex(SRC, r"q\(cur,\s*f[\"']")
        cur = _Cur()
        _run(cur)
        self.assertTrue(all(p == () for p in cur.params))


class TestPerformance(unittest.TestCase):
    def test_many_quiet_observers_and_dedup_keys(self):
        observers = [("busy", 10_000)] + [(f"o{i}", 1) for i in range(5_000)]
        t0 = time.perf_counter()
        _, notify, _, _, _ = _run(_Cur(observers=observers))
        self.assertEqual(notify.call_count, 5_000)
        keys = [ns.negspace_dedup_key("sensor_quiet", f"Presence method 'm{i}' has reported nothing for 9:00:0{i % 10}")
                for i in range(10_000)]
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertTrue(all(len(k) <= len("negspace:sensor_quiet:") + 80 for k in keys))


class TestRetry(unittest.TestCase):
    def test_notify_failure_fails_open(self):
        # RETRY GAP: main() notify — one attempt per finding; an exception is logged, the run still exits 0
        conn = mock.Mock(); conn.cursor.return_value = _Cur(arrivals=5, owned=0, wifi=0)
        with mock.patch.object(ns.psycopg2, "connect", return_value=conn), \
                mock.patch("nova_notify.notify", side_effect=RuntimeError("bus down")) as n, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            self.assertEqual(ns.main(True), 0)
        self.assertEqual(n.call_count, 1)
        self.assertIn("notify failed: bus down", out.getvalue())
        conn.close.assert_called_once()

    def test_pg_down_raises_once(self):
        # RETRY GAP: psycopg2.connect — no retry; launchd re-runs the detector on its own schedule
        with mock.patch.object(ns.psycopg2, "connect", side_effect=ns.psycopg2.OperationalError("down")) as c:
            with self.assertRaises(ns.psycopg2.OperationalError):
                ns.main(False)
        self.assertEqual(c.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_arrival_rule_thresholds(self):
        self.assertIn("arrival_no_device", _run(_Cur(arrivals=3, owned=0, wifi=0))[2])
        self.assertNotIn("arrival_no_device", _run(_Cur(arrivals=2, owned=0, wifi=0))[2])
        self.assertNotIn("arrival_no_device", _run(_Cur(arrivals=9, owned=0, wifi=1))[2])

    def test_observer_rule_needs_busy_peer(self):
        self.assertIn("observer_quiet", _run(_Cur(observers=[("a", 500), ("b", 5)]))[2])
        self.assertNotIn("observer_quiet", _run(_Cur(observers=[("a", 90), ("b", 0)]))[2])
        self.assertNotIn("observer_quiet", _run(_Cur(observers=[("a", 500)]))[2])

    def test_q_helper(self):
        cur = _Cur(); cur.answers = [[(1,)]]
        self.assertEqual(ns.q(cur, "SELECT 1", (2,)), [(1,)])
        self.assertEqual(cur.params, [(2,)])


class TestIntegration(unittest.TestCase):
    def test_reads_expected_tables_and_uses_central_bus(self):
        cur = _Cur()
        _run(cur)
        joined = " ".join(cur.sql)
        for t in ("telemetry.presence", "telemetry.bluetooth", "telemetry.device_owner"):
            self.assertIn(t, joined)
        self.assertIn("from nova_notify import notify", SRC)

    def test_sensor_quiet_moved_to_freshness_monitor_which_reuses_alert_findings(self):
        # merge M1 (2026-10-09): one silence detector. The check is gone from main() here ...
        main_src = SRC.split("def main(alert):")[1]
        self.assertNotIn("PARTITION BY method", main_src)
        self.assertNotIn("sensor_quiet", main_src)
        # ... and the freshness monitor raises it through THIS module's alert path
        fsrc = (SCRIPTS / "nova_freshness_monitor.py").read_text()
        self.assertIn("import nova_negative_space as ns", fsrc)
        self.assertIn("ns.alert_findings(due, notify=notify_fn)", fsrc)


class TestFunctional(unittest.TestCase):
    def test_findings_alert_with_stable_dedup_keys(self):
        last = _dt.datetime.now() - _dt.timedelta(hours=9)
        cur = _Cur(stale=[("Alex", last)], humans=0)
        rc, notify, out, conn, _ = _run(cur)
        self.assertEqual(rc, 0)
        kinds = [c[1]["dedup_key"].split(":")[1] for c in notify.call_args_list]
        self.assertEqual(kinds, ["device_no_human"])
        self.assertTrue(all(c[1]["meta"] == {"dedup_window_s": 28800} for c in notify.call_args_list))
        conn.close.assert_called_once()

    def test_alert_findings_is_the_shared_bus_path(self):
        # what nova_freshness_monitor calls for sensor_quiet: same source/category/key/window
        sent = []
        msg = lambda gap: f"Presence method 'mmwave' has reported nothing for {gap} (last: 2026-10-09 01:00)."
        with mock.patch("sys.stdout", new_callable=io.StringIO):
            n = ns.alert_findings([("sensor_quiet", msg("9:00:05"))], notify=lambda *a, **k: sent.append((a, k)))
            ns.alert_findings([("sensor_quiet", msg("13:07:00"))], notify=lambda *a, **k: sent.append((a, k)))
        self.assertEqual(n, 1)
        (a1, k1), (_, k2) = sent
        self.assertTrue(a1[0].startswith("Negative-space: Presence method 'mmwave'"))
        self.assertEqual((k1["source"], k1["category"], k1["level"]), ("nova_negative_space.py", "security", "warning"))
        self.assertEqual(k1["meta"], {"dedup_window_s": 28800})
        self.assertEqual(k1["dedup_key"], k2["dedup_key"])          # ticking gap -> same key
        self.assertTrue(k1["dedup_key"].startswith("negspace:sensor_quiet:"))
        self.assertEqual(ns.alert_findings([]), 0)

    def test_quiet_world_and_no_alert_flag(self):
        rc, notify, out, _, _ = _run(_Cur(), alert=True)
        self.assertIn("expected correlations all held", out)
        notify.assert_not_called()
        _, notify2, out2, _, _ = _run(_Cur(arrivals=5, owned=0, wifi=0), alert=False)
        self.assertIn("arrival_no_device", out2)
        notify2.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(PATH), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--alert", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_negative_space"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()


def test_sensor_quiet_judges_each_method_against_its_own_rhythm():
    """2026-10-06: the outdoor motion sensor (64 events/week, multi-day gaps) was paged as broken
    and Nova proposed disabling it. Quiet must mean 'gap > max(6h, 1.5x its longest recent gap)'.
    The query lives in nova_freshness_monitor since 2026-10-09 (merge M1)."""
    import pathlib, re
    src = pathlib.Path(__file__).resolve().parent.parent.joinpath("nova_freshness_monitor.py").read_text()
    block = src.split("PRESENCE_QUIET_SQL = ")[1].split('"""', 2)[1]
    assert "lag(ts) OVER (PARTITION BY method ORDER BY ts)" in block
    assert re.search(r"greatest\(interval '6 hours', longest \* 1\.5\)", block)
