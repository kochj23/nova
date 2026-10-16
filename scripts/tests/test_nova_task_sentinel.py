#!/usr/bin/env python3
"""Tests for nova_task_sentinel.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The schedule-memorization regression lives in tests/test_nova_task_sentinel_schedule.py."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_task_sentinel.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ts = _load("task_sentinel_t", SCRIPT)
SRC = SCRIPT.read_text()
NOTIFY = MagicMock()
FAKE_NOTIFY = types.SimpleNamespace(notify=NOTIFY)
H = 3_600_000


def _runs(statuses, now, gap_h=1):
    return [{"started_at": now - i * gap_h * H, "status": s, "exit_code": 0 if s == "success" else 1}
            for i, s in enumerate(statuses)]


class _Cur:
    def __init__(self, rows=(), fail=()):
        self.rows = list(rows); self.fail = fail; self.stmts = []

    def execute(self, sql, params=None):
        self.stmts.append((sql, params))
        if any(f in sql for f in self.fail) and "SAVEPOINT" not in sql:
            raise RuntimeError("constraint violation")

    def fetchall(self):
        return self.rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _Conn:
    def __init__(self, rows=(), fail=()):
        self.cur = _Cur(rows, fail); self.commits = 0; self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


def _sweep(rows, configured=None, fail=()):
    conn = _Conn(rows, fail)
    NOTIFY.reset_mock()
    with patch.object(ts, "load_configured_tasks", return_value=configured or {}), \
            patch.dict(sys.modules, {"nova_notify": FAKE_NOTIFY}):
        out = ts.run_once(conn)
    return out, conn


class _DailyCur:
    """Answers run_daily()'s queries by substring; records every execute."""
    def __init__(self, failing=(), queued=False, sessions=("sess-1",)):
        from datetime import date, timedelta
        t = date.today()
        self.rows = [(task, t - timedelta(days=i), 9) for task in failing for i in (1, 2, 3)]
        self.queued, self.sessions, self.sql, self._last = queued, sessions, [], ""

    def execute(self, sql, params=None):
        self.sql.append((sql, params)); self._last = sql

    def fetchall(self):
        return self.rows

    def fetchone(self):
        if "FROM claude_queue" in self._last:
            return (1,) if self.queued else None
        if "FROM scheduler_runs" in self._last:
            return ("failure", "Traceback: boom")
        if "FROM claude_sessions" in self._last:
            return self.sessions
        return None


def _daily(cur, dry_run=False):
    conn = SimpleNamespace(cursor=lambda: cur, autocommit=False, close=lambda: None)
    with patch("psycopg2.connect", return_value=conn) as c, redirect_stdout(io.StringIO()) as out:
        made = ts.run_daily(dry_run=dry_run)
    return made, c, out.getvalue()


def _rows(task, statuses, gap_h=1):
    now = ts._now_ms()
    return [(task, r["started_at"], r["status"], r["exit_code"]) for r in _runs(statuses, now, gap_h)]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", ts.DSN)

    def test_only_fstring_sql_interpolates_an_int_window(self):
        # the one f-string query interpolates only WINDOW_DAYS arithmetic, and the sole caller passes no
        # window (module constant int) — no external value can reach it
        self.assertIn("runs_by_task = fetch_task_runs(conn)\n", SRC)
        conn = _Conn()
        ts.fetch_task_runs(conn, window_days=2)
        self.assertIn(f"{2 * 86400}", conn.cur.stmts[-1][0])
        self.assertEqual(len(re.findall(r'execute\(\s*\n?\s*f"', SRC)), 1)

    def test_queue_insert_is_parameterized_and_deduped(self):
        conn = _Conn()
        with patch.dict(sys.modules, {"nova_notify": FAKE_NOTIFY}):
            ts._page(conn, "t'x", {"state": "critical", "reason": "r"})
        sql, params = [s for s in conn.cur.stmts if "INSERT INTO claude_queue" in s[0]][0]
        self.assertNotIn("t'x", sql)
        self.assertIn("WHERE NOT EXISTS", sql)
        self.assertEqual(params[0], params[2])

    def test_daily_values_ride_as_parameters(self):
        hostile = "x' OR '1'='1"
        cur = _DailyCur(failing=[hostile])
        _daily(cur)
        self.assertTrue(all(hostile not in sql for sql, _ in cur.sql))
        ins = [p for sql, p in cur.sql if sql.startswith("INSERT INTO claude_queue")][0]
        self.assertIn(hostile, ins[1])


class TestPerformance(unittest.TestCase):
    def test_classify_10k_runs(self):
        now = ts._now_ms()
        runs = _runs(["success"] * 10_000, now)
        t0 = time.perf_counter()
        h = ts.classify_task(runs, now_ms=now)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(h["state"], "healthy")

    def test_daily_with_many_chronic_tasks(self):
        cur = _DailyCur(failing=[f"t{i}" for i in range(2_000)], queued=True)
        t0 = time.perf_counter()
        made, _, _ = _daily(cur)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(made, 0)                                  # all already queued: nothing new


class TestRetry(unittest.TestCase):
    def test_config_load_fails_safe_to_judge_everything(self):
        # RETRY GAP: load_configured_tasks — one local read + one ssh attempt; total failure returns {}
        with patch("builtins.open", side_effect=OSError("no yaml")), \
                patch("subprocess.run", side_effect=OSError("ssh down")) as r:
            self.assertEqual(ts.load_configured_tasks(), {})
        self.assertEqual(r.call_count, 1)
        out, _ = _sweep(_rows("ghost", ["failure"] * 4))
        self.assertEqual([d[0] for d in out], ["ghost"])

    def test_main_db_failure_returns_1(self):
        # RETRY GAP: main()/psycopg2.connect — one attempt, rc 1 (next 15-min tick retries)
        with patch("psycopg2.connect", side_effect=OSError("pg down")), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(ts.main(), 1)
        self.assertIn("sweep failed", err.getvalue())

    def test_daily_connect_is_one_shot(self):
        # RETRY GAP: run_daily/psycopg2.connect — one attempt (connect_timeout=8); tomorrow's run retries
        with patch("psycopg2.connect", side_effect=OSError("pg down")) as c:
            with self.assertRaises(OSError):
                ts.run_daily()
        self.assertEqual(c.call_count, 1)
        self.assertEqual(c.call_args.kwargs, {"connect_timeout": 8})


class TestUnit(unittest.TestCase):
    def test_classify_states(self):
        now = ts._now_ms()
        self.assertEqual(ts.classify_task(_runs(["success"] * 2, now), now)["state"], "unknown")
        self.assertEqual(ts.classify_task(_runs(["failure"] * 3 + ["success"] * 3, now), now)["state"], "failing")
        self.assertEqual(ts.classify_task(_runs(["error"] * 6 + ["success"], now), now)["state"], "critical")
        self.assertEqual(ts.classify_task(_runs(["running", "success", "success", "success"], now), now)["state"], "healthy")
        stale = [dict(r, started_at=r["started_at"] - 24 * H) for r in _runs(["success"] * 4, now)]
        self.assertEqual(ts.classify_task(stale, now)["state"], "stale")

    def test_schedule_interval_parsing(self):
        self.assertEqual(ts.schedule_interval_s("every 15m"), 900)
        self.assertEqual(ts.schedule_interval_s("daily 03:00"), 86400.0)
        self.assertEqual(ts.schedule_interval_s("cron 0 9 * * 1"), 604800.0)
        self.assertEqual(ts.schedule_interval_s("cron */30 * * * *"), 604800.0 / (7 * 24 * 2))
        self.assertIsNone(ts.schedule_interval_s(""))
        self.assertIsNone(ts.schedule_interval_s("whenever"))

    def test_main_routes_daily_mode(self):
        with patch.object(ts, "run_daily", return_value=0) as rd, patch("psycopg2.connect") as c:
            self.assertEqual(ts.main(["--daily", "--dry-run"]), 0)
            self.assertEqual(ts.main(["--daily"]), 0)
        self.assertEqual([k.kwargs for k in rd.call_args_list], [{"dry_run": True}, {"dry_run": False}])
        c.assert_not_called()                                        # the 15-min sweep did not run


class TestIntegration(unittest.TestCase):
    def test_reads_scheduler_runs_and_pages_through_shared_bus(self):
        conn = _Conn(_rows("t1", ["failure"] * 3 + ["success"] * 2))
        self.assertEqual(len(ts.fetch_task_runs(conn)["t1"]), 5)
        self.assertIn("FROM scheduler_runs", conn.cur.stmts[0][0])
        out, conn = _sweep(_rows("t1", ["failure"] * 3 + ["success"] * 2), configured={"t1": "every 1h"})
        kw = NOTIFY.call_args.kwargs
        self.assertEqual((kw["level"], kw["dedup_key"], kw["meta"]), ("warning", "task-sentinel:t1", {"dedup_window_s": 21600}))
        hb = [p for s, p in conn.cur.stmts if "INSERT INTO health_checks" in s][0]
        self.assertEqual(hb, ("up", "0 healthy, 1 degraded: t1"))

    def test_daily_reuses_chronic_failures_thresholds_and_dsn(self):
        import nova_chronic_failures as cf
        daily = SRC.split("def run_daily(")[1].split("\ndef ")[0]
        for use in ("cf.chronic(", "cf.DSN", "cf.PREFIX", "cf.OPEN", "cf.DAYS"):
            self.assertIn(use, daily)
        self.assertNotIn("FAIL_PER_DAY =", SRC)                     # thresholds imported, not copied
        cur = _DailyCur(failing=["prober"], queued=True)
        _, c, _ = _daily(cur)
        self.assertEqual(c.call_args.args[0], cf.DSN)
        self.assertEqual(cur.sql[0][1], (cf.DAYS + 1,))
        self.assertEqual([p for s, p in cur.sql if "FROM claude_queue" in s][0], ("Chronic failure: prober %", cf.OPEN))


class TestFunctional(unittest.TestCase):
    def test_sweep_pages_critical_queues_and_skips_retired(self):
        rows = _rows("dead", ["failure"] * 7) + _rows("ok", ["success"] * 5) + _rows("retired", ["failure"] * 7)
        out, conn = _sweep(rows, configured={"dead": "every 1h", "ok": "every 1h"})
        self.assertEqual([(d[0], d[1]["state"]) for d in out], [("dead", "critical")])
        self.assertEqual(NOTIFY.call_count, 1)
        self.assertTrue(any("INSERT INTO claude_queue" in s for s, _ in conn.cur.stmts))
        self.assertGreaterEqual(conn.commits, 1)

    def test_queue_insert_failure_rolls_back_savepoint_only(self):
        out, conn = _sweep(_rows("dead", ["failure"] * 7), fail=("INSERT INTO claude_queue",))
        sqls = [s for s, _ in conn.cur.stmts]
        self.assertIn("ROLLBACK TO SAVEPOINT sp_queue", sqls)
        self.assertTrue(any("INSERT INTO health_checks" in s for s in sqls))   # heartbeat still lands

    def test_main_all_healthy(self):
        with patch("psycopg2.connect", return_value=_Conn()), patch.object(ts, "load_configured_tasks", return_value={}), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(ts.main(), 0)
        self.assertIn("all tasks healthy", out.getvalue())

    def test_daily_queues_one_item_after_registering_the_session(self):
        cur = _DailyCur(failing=["prober"], sessions=None)        # no session rows at all -> 'chronic-failures'
        made, _, out = _daily(cur)
        self.assertEqual(made, 1)
        inserts = [(s.split("(")[0].strip(), p) for s, p in cur.sql if s.startswith("INSERT")]
        self.assertEqual(inserts[0], ("INSERT INTO claude_sessions", ("chronic-failures",)))
        name, (sid, desc, ctx) = inserts[1]
        self.assertEqual((name, sid), ("INSERT INTO claude_queue", "chronic-failures"))
        self.assertTrue(desc.startswith("Chronic failure: prober — 9 non-success runs/day for 3 days: fix or retire"))
        self.assertIn("Traceback: boom", ctx)

    def test_daily_dry_run_writes_nothing(self):
        cur = _DailyCur(failing=["prober"])
        made, _, out = _daily(cur, dry_run=True)
        self.assertEqual(made, 0)
        self.assertFalse(any(s.startswith("INSERT") for s, _ in cur.sql))
        self.assertIn("would queue: Chronic failure: prober", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_task_sentinel"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)

    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--daily", r.stdout)



class TestMergedModeFlaky(unittest.TestCase):
    """Consolidation item #12 (2026-10-15): Take Four runs as `nova_task_sentinel.py --flaky`; logic stays in nova_take_four."""

    def _with_fake(self, rc=0):
        fake = types.SimpleNamespace(main=MagicMock(return_value=rc))
        had, prev = "nova_take_four" in sys.modules, sys.modules.get("nova_take_four")
        sys.modules["nova_take_four"] = fake
        self.addCleanup(lambda: sys.modules.__setitem__("nova_take_four", prev) if had else sys.modules.pop("nova_take_four", None))
        return fake

    def test_mode_forwards_remaining_args(self):
        fake = self._with_fake(rc=0)
        with redirect_stdout(io.StringIO()):
            rc = ts.main(["--flaky", "--scheduled"])
        self.assertEqual(rc, 0)
        fake.main.assert_called_once_with(["--scheduled"])

    def test_mode_propagates_exit_code(self):
        fake = self._with_fake(rc=2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ts.main(["--dry-run", "--flaky"]), 2)
        fake.main.assert_called_once_with(["--dry-run"])

    def test_mode_selftest_subprocess(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_task_sentinel.py"), "--flaky", "--selftest"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest ok", r.stdout)
        self.assertIn("Take Four", r.stdout + r.stderr)

    def test_old_script_still_runnable(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_take_four.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("MERGED 2026-10-15", (SCRIPTS / "nova_take_four.py").read_text())

if __name__ == "__main__":
    unittest.main()


# ── #46(a) Chanos filter: the sentinel's heartbeat is earned, never a literal 'up' ──
def _hb(conn):
    return [p for s, p in conn.cur.stmts if "INSERT INTO health_checks" in s][0]


class TestHeartbeatEarned(unittest.TestCase):
    def test_unit_status_table(self):
        self.assertEqual(ts.heartbeat_status(5, 60, True, 0), "up")
        self.assertEqual(ts.heartbeat_status(5, 60, False, 0), "degraded")
        self.assertEqual(ts.heartbeat_status(0, None, True, 0), "down")
        self.assertEqual(ts.heartbeat_status(5, ts.BLIND_S + 1, True, 0), "down")
        self.assertEqual(ts.heartbeat_status(5, 60, True, 1), "down")

    def test_security_status_is_a_parameter(self):
        hb = SRC.split("def _heartbeat(")[1].split("\ndef ")[0]
        self.assertNotIn('("up",', hb)

    def test_functional_empty_scheduler_runs_is_down(self):
        _, conn = _sweep([], configured={"t1": "every 1h"})
        st, note = _hb(conn)
        self.assertEqual(st, "down"); self.assertIn("no scheduler_runs", note)

    def test_functional_stale_runs_are_down(self):
        rows = [(t, s - 3 * H, st, e) for t, s, st, e in _rows("t1", ["success"] * 5)]
        _, conn = _sweep(rows, configured={"t1": "every 1h"})
        self.assertEqual(_hb(conn)[0], "down")

    def test_retry_undelivered_page_is_down(self):
        conn = _Conn(_rows("dead", ["failure"] * 7))
        with patch.object(ts, "load_configured_tasks", return_value={"dead": "every 1h"}), \
                patch.dict(sys.modules, {"nova_notify": types.SimpleNamespace(notify=MagicMock(return_value=False))}):
            ts.run_once(conn)
        st, note = _hb(conn)
        self.assertEqual(st, "down"); self.assertIn("not delivered", note)

    def test_integration_five_straight_failures_escalate_and_sentinel_stays_up(self):
        # the Chanos injection: a scratch task with straight failures -> escalation; the sentinel itself is fine
        out, conn = _sweep(_rows("scratch", ["failure"] * 6), configured={"scratch": "every 1h"})
        self.assertEqual(out[0][1]["state"], "critical")
        self.assertEqual(NOTIFY.call_args.kwargs["level"], "critical")
        self.assertEqual(_hb(conn)[0], "up")

    def test_unit_config_load_failure_is_degraded(self):
        _, conn = _sweep(_rows("t1", ["success"] * 5), configured={})
        self.assertEqual(_hb(conn)[0], "degraded")

    def test_performance_status_fast(self):
        t = time.perf_counter()
        for i in range(100000):
            ts.heartbeat_status(i, i, True, 0)
        self.assertLess(time.perf_counter() - t, 1.0)

    def test_frame_docstring_mentions_earned_heartbeat(self):
        self.assertIn("heartbeat_status()", SRC.split('"""')[1])
