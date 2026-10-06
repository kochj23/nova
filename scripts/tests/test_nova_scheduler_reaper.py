#!/usr/bin/env python3
"""Tests for nova_scheduler_reaper — the scheduler_runs zombie reaper.

Seven categories, matching the guarantees the reaper must hold:
 1. threshold-floor         — no/degenerate history falls back to the 24h floor
 2. threshold-3x            — 3x observed max wins when it exceeds the floor
 3. threshold-real          — the real ~12h observed max yields ~36h (documented)
 4. reaps-stale             — only 'running' rows older than the cutoff are flipped
 5. leaves-others           — fresh 'running' + all terminal rows are untouched
 6. idempotent              — a second reap on the just-reaped state touches nothing
 7. empty/degrade           — empty table and null-max are safe (0 reaped, no crash)

Pure logic against an in-memory fake connection — no live DB.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import nova_scheduler_reaper as r

HOUR_MS = 3_600_000
NOW = 1_000_000_000_000  # fixed clock (ms)


class FakeCursor:
    """Minimal cursor understanding the two SQL shapes the reaper issues."""

    def __init__(self, conn):
        self.conn = conn
        self.rowcount = 0
        self._fetch = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        s = " ".join(sql.split())
        if s.startswith("SELECT max(duration_ms)"):
            oks = [row["duration_ms"] for row in self.conn.rows
                   if row["status"] == "success" and row["duration_ms"] is not None]
            self._fetch = (max(oks) if oks else None,)
        elif s.startswith("UPDATE scheduler_runs SET status='orphaned'"):
            cutoff = params[0]
            n = 0
            for row in self.conn.rows:
                if row["status"] == "running" and row["started_at"] < cutoff:
                    row["status"] = "orphaned"
                    n += 1
            self.rowcount = n
        elif s.startswith("INSERT INTO claude_sessions"):
            pass
        elif "SAVEPOINT" in s or s.startswith("INSERT INTO claude_actions") or "RELEASE" in s:
            pass
        else:
            raise AssertionError(f"unexpected SQL: {s}")

    def fetchone(self):
        return self._fetch


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.committed = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass


def _row(status, age_ms, duration_ms=None):
    return {"status": status, "started_at": NOW - age_ms, "duration_ms": duration_ms}


class TestThreshold(unittest.TestCase):

    def test_1_floor_when_no_history(self):
        self.assertEqual(r.compute_threshold_ms(None), 24 * HOUR_MS)
        self.assertEqual(r.compute_threshold_ms(0), 24 * HOUR_MS)
        self.assertEqual(r.compute_threshold_ms(-5), 24 * HOUR_MS)
        # a small max (3x still under the floor) also pins to the floor
        self.assertEqual(r.compute_threshold_ms(1 * HOUR_MS), 24 * HOUR_MS)

    def test_2_three_x_when_above_floor(self):
        # 10h max -> 30h > 24h floor
        self.assertEqual(r.compute_threshold_ms(10 * HOUR_MS), 30 * HOUR_MS)

    def test_3_real_world_12h_max_gives_36h(self):
        # documented: observed max legit success ~12.08h -> ~36h threshold
        thr = r.compute_threshold_ms(int(12.08 * HOUR_MS))
        self.assertAlmostEqual(thr / HOUR_MS, 36.24, delta=0.1)


class TestReap(unittest.TestCase):

    def test_4_reaps_only_stale_running(self):
        rows = [
            _row("running", 40 * HOUR_MS),   # stale -> reaped (>36h)
            _row("running", 50 * HOUR_MS),   # stale -> reaped
        ]
        conn = FakeConn(rows + [_row("success", HOUR_MS, duration_ms=12 * HOUR_MS)])
        out = r.reap(conn, now_ms=NOW)
        self.assertEqual(out["reaped"], 2)
        self.assertTrue(all(x["status"] == "orphaned" for x in rows))

    def test_5_leaves_fresh_and_terminal_rows(self):
        fresh = _row("running", 1 * HOUR_MS)            # a real long-runner, young
        done = _row("success", 40 * HOUR_MS, 12 * HOUR_MS)
        failed = _row("failure", 40 * HOUR_MS)
        already = _row("orphaned", 99 * HOUR_MS)
        conn = FakeConn([fresh, done, failed, already])
        out = r.reap(conn, now_ms=NOW)
        self.assertEqual(out["reaped"], 0)
        self.assertEqual(fresh["status"], "running")
        self.assertEqual(done["status"], "success")
        self.assertEqual(failed["status"], "failure")
        self.assertEqual(already["status"], "orphaned")

    def test_6_idempotent(self):
        rows = [_row("running", 100 * HOUR_MS),
                _row("success", HOUR_MS, duration_ms=12 * HOUR_MS)]
        conn = FakeConn(rows)
        first = r.reap(conn, now_ms=NOW)
        self.assertEqual(first["reaped"], 1)
        second = r.reap(conn, now_ms=NOW)
        self.assertEqual(second["reaped"], 0)  # nothing left to reap

    def test_7_empty_table_is_safe(self):
        conn = FakeConn([])
        out = r.reap(conn, now_ms=NOW)
        self.assertEqual(out["reaped"], 0)
        # with no success history the threshold falls back to the 24h floor
        self.assertEqual(out["threshold_ms"], 24 * HOUR_MS)


# ── house categories added 2026-10-05 — the 7 house categories (Security, Performance, Retry, Unit,
# Integration, Functional, Frame). Written by Jordan Koch (via Claude). psycopg2.connect is mocked.

import re  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
from unittest.mock import MagicMock, patch  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "nova_scheduler_reaper.py")
SRC = open(SCRIPT).read()


class TestSecurity(unittest.TestCase):
    def test_no_credentials_and_parameterized_cutoff(self):
        self.assertIsNone(re.search(r"(password|token|secret)\s*=\s*['\"]", SRC, re.I))
        self.assertIsNone(re.search(r"execute\(\s*f[\"']", SRC))
        self.assertIn("started_at < %s", SRC)

    def test_only_writes_are_orphan_flip_and_audit(self):
        writes = sorted(set(m.group(0).split()[-1] for m in
                            re.finditer(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+\w+", SRC)))
        self.assertEqual(writes, ["claude_actions", "claude_sessions", "scheduler_runs"])
        self.assertNotIn("DELETE", SRC)


class TestPerformance(unittest.TestCase):
    def test_reap_10k_rows_fast(self):
        rows = [_row("running" if i % 2 else "success", (i % 100) * HOUR_MS, duration_ms=HOUR_MS)
                for i in range(10_000)]
        t0 = time.perf_counter()
        out = r.reap(FakeConn(rows), now_ms=NOW)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertGreater(out["reaped"], 0)


class TestRetry(unittest.TestCase):
    def test_connect_failure_returns_1_single_attempt(self):
        # RETRY GAP: main()/psycopg2.connect — one attempt; hourly launchd run is the retry
        with patch("psycopg2.connect", side_effect=RuntimeError("pg down")) as c, patch("builtins.print"):
            self.assertEqual(r.main(), 1)
        self.assertEqual(c.call_count, 1)

    def test_audit_insert_failure_rolls_back_to_savepoint(self):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.execute.side_effect = lambda sql, *a: (_ for _ in ()).throw(RuntimeError("fk")) if "claude_actions" in sql else None
        r._log_action(conn, {"reaped": 1, "threshold_h": 24.0})
        self.assertEqual(cur.execute.call_args_list[-1][0][0], "ROLLBACK TO SAVEPOINT sp_act")
        conn.commit.assert_called_once()


class TestUnit(unittest.TestCase):
    def test_threshold_custom_floor_factor(self):
        self.assertEqual(r.compute_threshold_ms(None, floor_h=1), HOUR_MS)
        self.assertEqual(r.compute_threshold_ms(2 * HOUR_MS, floor_h=1, factor=2), 4 * HOUR_MS)

    def test_fetch_max_success_null(self):
        self.assertEqual(r.fetch_max_success_ms(FakeConn([])), 0)
        self.assertEqual(r.fetch_max_success_ms(FakeConn([_row("success", 0, 5)])), 5)

    def test_ensure_session_rollback_on_error(self):
        conn = MagicMock()
        conn.cursor.return_value.__enter__.return_value.execute.side_effect = RuntimeError("x")
        r.ensure_session(conn)
        conn.rollback.assert_called_once()


class TestIntegration(unittest.TestCase):
    def test_run_once_chains_session_reap_and_audit(self):
        conn = FakeConn([_row("running", 100 * HOUR_MS)])
        seen = []
        orig = FakeCursor.execute

        def spy(self, sql, params=()):
            seen.append(" ".join(sql.split())[:30])
            return orig(self, sql, params)
        with patch.object(FakeCursor, "execute", spy):
            out = r.run_once(conn)
        self.assertEqual(out["reaped"], 1)
        self.assertTrue(seen[0].startswith("INSERT INTO claude_sessions"))
        self.assertTrue(any(s.startswith("INSERT INTO claude_actions") for s in seen))
        self.assertEqual(r.DSN, "host=localhost dbname=nova_ops user=kochj")


class TestFunctional(unittest.TestCase):
    def test_main_golden_path(self):
        conn = FakeConn([_row("running", 100 * HOUR_MS)])
        conn.close = MagicMock()
        with patch("psycopg2.connect", return_value=conn), patch("builtins.print") as p:
            self.assertEqual(r.main(), 0)
        conn.close.assert_called_once()
        self.assertIn("reaped 1 orphaned run(s)", p.call_args[0][0])

    def test_main_reap_error_closes_and_returns_1(self):
        conn = MagicMock()
        with patch("psycopg2.connect", return_value=conn), patch.object(r, "run_once", side_effect=RuntimeError("x")), \
             patch("builtins.print"):
            self.assertEqual(r.main(), 1)
        conn.close.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        res = subprocess.run([sys.executable, "-c", "import nova_scheduler_reaper"], cwd=os.path.dirname(SCRIPT),
                             capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((res.returncode, res.stdout.strip()), (0, ""), res.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
