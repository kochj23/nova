#!/usr/bin/env python3
"""Tests for nova_backup_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
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
SCRIPT = SCRIPTS / "nova_backup_monitor.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    notify_stub = types.ModuleType("nova_notify")
    notify_stub.notify = MagicMock()
    with patch.dict(sys.modules, {"nova_notify": notify_stub}):    # bound at import; sys.modules restored after
        spec.loader.exec_module(mod)
    return mod


bm = _load("backup_monitor_under_test", SCRIPT)
# Module-level stubs: PG and the notification bus must never be reached. Both are module attributes
# of the loaded copy (not the shared psycopg2 / nova_notify modules), so nothing leaks across files.
bm.psycopg2 = types.SimpleNamespace(connect=MagicMock(side_effect=AssertionError("unmocked psycopg2.connect")))


class _Cur:
    """Cursor stub: answers `max(ts)` and `SELECT rc, ok` per job prefix; records every statement."""
    def __init__(self, last=None, latest=None):
        self.last, self.latest = last or {}, latest or {}
        self.sql, self._row = [], None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        prefix = params[0].rstrip("%")
        if "max(ts)" in sql:
            self._row = (self.last.get(prefix),)
        else:
            self._row = self.latest.get(prefix, (0, True))

    def fetchone(self):
        return self._row


def _conn(cur):
    c = types.SimpleNamespace(cursor=lambda: cur, autocommit=False, closed=False)
    c.close = lambda: setattr(c, "closed", True)
    return c


def _ago(hours):
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _check(cur, notify=None):
    """Run check() against a cursor stub; returns (issues, notify mock, stdout)."""
    n = notify if notify is not None else MagicMock()
    conn = _conn(cur)
    buf = io.StringIO()
    with patch.object(bm.psycopg2, "connect", MagicMock(return_value=conn)) as connect, \
         patch.object(bm.nova_notify, "notify", n), redirect_stdout(buf):
        issues = bm.check()
    connect.assert_called_once_with(bm.DSN)
    return issues, n, buf.getvalue(), conn


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("password", bm.DSN)                       # pg auth via ~/.pgpass, never in source

    def test_sql_is_parameterized_and_read_only(self):
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        self.assertIsNone(re.search(r"execute\([^)]*%\s*\(", SRC))
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM|DROP|TRUNCATE)\b", SRC))
        cur = _Cur(last={p: _ago(1) for p in bm.JOBS})
        _check(cur)
        for sql, params in cur.sql:
            self.assertIn("%s", sql)
            self.assertEqual(len(params), 1)
            self.assertTrue(params[0].endswith("%"))                 # the LIKE pattern is a bound value, not spliced

    def test_job_names_never_reach_the_sql_text(self):
        with patch.dict(bm.JOBS, {"evil'; DROP TABLE telemetry.backup_runs; --": 1}, clear=True):
            cur = _Cur(last={"evil'; DROP TABLE telemetry.backup_runs; --": _ago(1)})
            _check(cur)
        self.assertTrue(all("DROP" not in sql for sql, _ in cur.sql))


class TestPerformance(unittest.TestCase):
    def test_check_fast_across_10k_jobs(self):
        jobs = {f"nova-backup:job{i}": 36 for i in range(10_000)}
        cur = _Cur(last={p: _ago(1) for p in jobs})
        with patch.dict(bm.JOBS, jobs, clear=True):
            t0 = time.perf_counter()
            issues, n, _, _ = _check(cur)
            dt = time.perf_counter() - t0
        self.assertLess(dt, 3.0)
        self.assertEqual(issues, [])
        self.assertEqual(len(cur.sql), 20_000)                        # exactly two bounded queries per job
        self.assertEqual(n.call_count, 1)                             # one digest, not 10k notifications


class TestRetry(unittest.TestCase):
    def test_pg_connect_failure_is_one_shot_and_raises(self):
        # RETRY GAP: check()/psycopg2.connect — a single attempt; the OSError escapes so launchd sees a
        # non-zero exit rather than a silent "healthy" (no safe default is possible without the table)
        connect = MagicMock(side_effect=OSError("pg-primary unreachable"))
        with patch.object(bm.psycopg2, "connect", connect):
            with self.assertRaises(OSError):
                bm.check()
        self.assertEqual(connect.call_count, 1)

    def test_notify_failure_fails_open_to_stdout(self):
        # RETRY GAP: emit() — nova_notify.notify is tried once; on failure the line is printed, never re-sent
        n = MagicMock(side_effect=RuntimeError("bus down"))
        buf = io.StringIO()
        with patch.object(bm.nova_notify, "notify", n), redirect_stdout(buf):
            bm.emit("Backups healthy", "nas: 1.0h ago", "info", "backup-digest")
        self.assertEqual(n.call_count, 1)
        self.assertEqual(buf.getvalue().strip(), "[info] Backups healthy: nas: 1.0h ago")

    def test_notify_failure_inside_check_still_returns_issues(self):
        cur = _Cur(last={p: None for p in bm.JOBS})
        issues, n, out, _ = _check(cur, notify=MagicMock(side_effect=RuntimeError("bus down")))
        self.assertEqual(len(issues), len(bm.JOBS))
        self.assertEqual(out.count("[warning]"), len(bm.JOBS))


class TestUnit(unittest.TestCase):
    def test_emit_routes_through_nova_notify_with_backup_category(self):
        n = MagicMock()
        with patch.object(bm.nova_notify, "notify", n), redirect_stdout(io.StringIO()) as buf:
            bm.emit("t", "b", "warning", "k")
        n.assert_called_once_with(title="t", body="b", level="warning", category="backup", source=bm.SOURCE, dedup_key="k")
        self.assertEqual(buf.getvalue(), "")

    def test_emit_without_nova_notify_prints(self):
        buf = io.StringIO()
        with patch.object(bm, "nova_notify", None), redirect_stdout(buf):
            bm.emit("t", "b", "critical", "k")
        self.assertEqual(buf.getvalue().strip(), "[critical] t: b")

    def test_jobs_table_shape(self):
        self.assertEqual(set(bm.JOBS), {"nova-backup:nas", "nova-backup:external"})
        self.assertTrue(all(isinstance(h, int) and h > 0 for h in bm.JOBS.values()))
        self.assertEqual(bm.SOURCE, "nova-backup-monitor")


class TestIntegration(unittest.TestCase):
    def test_reads_telemetry_backup_runs_with_prefix_like(self):
        cur = _Cur(last={p: _ago(1) for p in bm.JOBS})
        _check(cur)
        self.assertTrue(all("telemetry.backup_runs" in sql for sql, _ in cur.sql))
        self.assertEqual(cur.sql[0][1], ("nova-backup:nas%",))
        self.assertIn("ok = true", cur.sql[0][0])
        self.assertIn("ORDER BY ts DESC LIMIT 1", cur.sql[1][0])

    def test_staleness_thresholds_warning_then_critical(self):
        cur = _Cur(last={"nova-backup:nas": _ago(40), "nova-backup:external": _ago(80)})
        issues, n, _, _ = _check(cur)
        levels = {p: lvl for p, _, lvl in issues}
        self.assertEqual(levels, {"nova-backup:nas": "warning", "nova-backup:external": "critical"})
        self.assertIn("limit 36h", issues[0][1])
        self.assertEqual({c.kwargs["dedup_key"] for c in n.call_args_list},
                         {"backup-stale:nova-backup:nas", "backup-stale:nova-backup:external"})

    def test_connection_is_autocommit_and_closed(self):
        cur = _Cur(last={p: _ago(1) for p in bm.JOBS})
        _, _, _, conn = _check(cur)
        self.assertTrue(conn.autocommit)
        self.assertTrue(conn.closed)


class TestFunctional(unittest.TestCase):
    def test_golden_path_all_healthy_posts_a_single_digest(self):
        cur = _Cur(last={"nova-backup:nas": _ago(1), "nova-backup:external": _ago(2.5)})
        issues, n, out, _ = _check(cur)
        self.assertEqual(issues, [])
        n.assert_called_once()
        kw = n.call_args.kwargs
        self.assertEqual((kw["title"], kw["level"], kw["dedup_key"]), ("Backups healthy", "info", "backup-digest"))
        self.assertEqual(kw["body"], "nas: 1.0h ago, external: 2.5h ago")
        self.assertEqual(out, "")

    def test_never_succeeded_job_warns(self):
        cur = _Cur(last={"nova-backup:nas": _ago(1), "nova-backup:external": None})
        issues, n, _, _ = _check(cur)
        self.assertEqual(issues, [("nova-backup:external", "no successful run ever recorded", "warning")])
        n.assert_called_once()
        self.assertEqual(n.call_args.kwargs["title"], "Backup stale/failed: external")
        self.assertNotIn("Backups healthy", [c.kwargs["title"] for c in n.call_args_list])

    def test_recent_failed_run_is_surfaced_despite_older_success(self):
        cur = _Cur(last={p: _ago(1) for p in bm.JOBS}, latest={"nova-backup:nas": (23, False)})
        issues, n, _, _ = _check(cur)
        self.assertEqual(issues, [("nova-backup:nas", "most recent run FAILED (rc=23)", "warning")])
        self.assertEqual(n.call_args.kwargs["dedup_key"], "backup-stale:nova-backup:nas")

    def test_error_path_exit_code_contract(self):
        # __main__ maps a non-empty issue list to exit 1 — the same expression, evaluated here
        self.assertIn("sys.exit(1 if check() else 0)", SRC)
        cur = _Cur(last={p: None for p in bm.JOBS})
        issues, _, _, _ = _check(cur)
        self.assertEqual(1 if issues else 0, 1)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_check(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_backup_monitor"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")

    def test_module_surface(self):
        self.assertTrue(callable(bm.check) and callable(bm.emit))


if __name__ == "__main__":
    unittest.main()
