#!/usr/bin/env python3
"""Tests for nova_incident_lifecycle.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import datetime
import importlib.util
import io
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lc = _load("lc", SCRIPTS / "nova_incident_lifecycle.py")
SRC = (SCRIPTS / "nova_incident_lifecycle.py").read_text()
PRE_SELFTEST = SRC[:SRC.index("def _selftest")]
NOW = datetime.datetime(2026, 10, 5, 12, 0, tzinfo=datetime.timezone.utc)
TODAY = datetime.date.today().isoformat()


class _Cur:
    """A context-manager cursor: fetchone()/fetchall() answer from one queue; every execute is recorded."""
    def __init__(self, answers=(), fail=None, rowcount=1):
        self.answers = list(answers); self.sql = []; self.params = []; self.fail = fail; self.rowcount = rowcount

    def __enter__(self): return self

    def __exit__(self, *a): return False

    def execute(self, sql, params=None):
        if self.fail and (self.fail is True or self.fail in sql):
            raise RuntimeError("db down")
        self.sql.append(" ".join(sql.split())); self.params.append(params)

    def fetchone(self):
        return self.answers.pop(0) if self.answers else None

    def fetchall(self):
        return self.answers.pop(0) if self.answers else []

    def executed(self, frag):
        return [s for s in self.sql if frag in s]


class _Conn:
    def __init__(self, cur): self.cur = cur; self.commits = 0; self.rollbacks = 0; self.closed = False

    def cursor(self, **kw): return self.cur

    def commit(self): self.commits += 1

    def rollback(self): self.rollbacks += 1

    def close(self): self.closed = True


def _notify(calls, raise_=False):
    """A nova_notify stub in sys.modules; `calls` collects (title, kwargs)."""
    mod = types.ModuleType("nova_notify")

    def notify(title, **kw):
        if raise_:
            raise RuntimeError("bus down")
        calls.append((title, kw)); return True
    mod.notify = notify
    return patch.dict(sys.modules, {"nova_notify": mod})


def _quiet():
    return redirect_stderr(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertNotIn('execute(f"', SRC)
        self.assertIsNone(re.search(r'"""\s*%\s*\(', SRC))             # no %-formatted SQL either
        cur = _Cur([(30,)]); conn = _Conn(cur)
        with _notify([]):
            lc.acknowledge(conn, 5, "x'; DROP TABLE telemetry.incidents; --")
        self.assertNotIn("DROP", cur.sql[0])
        self.assertEqual(cur.params[0], ("x'; DROP TABLE telemetry.incidents; --", 5))

    def test_writes_are_bounded_and_row_scoped_outside_the_selftest(self):
        writes = re.findall(r"\b(INSERT INTO|UPDATE|DELETE FROM)\s+([\w.]+)", PRE_SELFTEST)
        self.assertEqual(set(writes), {("UPDATE", "telemetry.incidents"), ("UPDATE", "public.incidents")})
        for m in re.finditer(r"UPDATE (telemetry|public)\.incidents", PRE_SELFTEST):
            self.assertIn("WHERE", PRE_SELFTEST[m.start():m.start() + 500])
        self.assertIn("ADD COLUMN IF NOT EXISTS", SRC)                 # schema change is idempotent

    def test_notifications_go_through_the_central_bus_not_slack(self):
        self.assertNotIn("slack", SRC.lower().replace("never hardcodes slack", ""))
        self.assertIn("from nova_notify import notify", SRC)


class TestPerformance(unittest.TestCase):
    def test_auto_close_fast_on_10k_stale_incidents(self):
        stale = [(i, "nas", f"disk {i}", NOW, 5400) for i in range(10_000)]
        cur = _Cur([stale] + [(5400,)] * 10_000); conn = _Conn(cur)
        calls = []
        t0 = time.perf_counter()
        with _notify(calls):
            closed = lc.auto_close_resolved(conn, 30)
        self.assertLess(time.perf_counter() - t0, 2.5)
        self.assertEqual(closed, 10_000)
        self.assertEqual(len(calls), 10_000)
        self.assertEqual(len({kw["dedup_key"] for _, kw in calls}), 10_000)


class TestRetry(unittest.TestCase):
    def test_connect_is_one_shot_and_returns_none(self):
        # RETRY GAP: _connect()/psycopg2.connect — a single attempt, None on failure (never raises)
        attempts = []

        def boom(*a, **k):
            attempts.append(1); raise OSError("pg down")
        with patch.object(psycopg2, "connect", boom), _quiet():
            self.assertIsNone(lc._connect())
        self.assertEqual(len(attempts), 1)

    def test_notify_fails_open(self):
        # RETRY GAP: _notify()/nova_notify.notify — one call; a raising or missing bus returns False
        with _notify([], raise_=True):
            self.assertFalse(lc._notify("t", body="b"))
        with patch.dict(sys.modules, {"nova_notify": None}):
            self.assertFalse(lc._notify("t", body="b"))

    def test_every_db_operation_fails_open_and_rolls_back(self):
        # RETRY GAP: auto_close_resolved / acknowledge / detect_recurrence / stats / auto_close_public / sweep —
        # each is one attempt; a failure degrades to a safe default and rolls back
        conn = _Conn(_Cur(fail=True))
        with _quiet(), _notify([]):
            self.assertEqual(lc.auto_close_resolved(conn), 0)
            self.assertFalse(lc.acknowledge(conn, 1, "x"))
            self.assertIsNone(lc.detect_recurrence(conn, 1))
            self.assertEqual(lc.stats(conn), {})
            self.assertEqual(lc.auto_close_public(conn), 0)
            self.assertEqual(lc.sweep(conn), {"closed": 0, "recurrence_scanned": 0, "public_closed": 0})
            self.assertFalse(lc.migrate(conn))
        self.assertEqual(conn.rollbacks, 5)
        self.assertEqual(conn.commits, 0)


class TestUnit(unittest.TestCase):
    def test_migrate(self):
        conn = _Conn(_Cur())
        self.assertTrue(lc.migrate(conn))
        self.assertIn("ALTER TABLE telemetry.incidents", conn.cur.sql[0])
        self.assertEqual(conn.commits, 1)

    def test_acknowledge_is_idempotent_on_first_ack(self):
        calls = []
        with _notify(calls):
            self.assertTrue(lc.acknowledge(_Conn(_Cur([(90,)])), 5, "jordan"))
            self.assertFalse(lc.acknowledge(_Conn(_Cur([None])), 5, "jordan"))     # already acked: no-op
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "Incident #5 acknowledged by jordan")
        self.assertEqual(calls[0][1]["dedup_key"], "incident-acked-5")
        self.assertEqual(calls[0][1]["body"], "MTTA 1.5m.")

    def test_detect_recurrence_stamps_key_and_warns_once_per_day_on_a_pattern(self):
        calls = []
        with _notify(calls):
            self.assertIsNone(lc.detect_recurrence(_Conn(_Cur([None])), 9))                  # no such incident
            cur = _Cur([("nas", "disk"), (2,)])
            self.assertEqual(lc.detect_recurrence(_Conn(cur), 9), "nas:disk")             # below threshold
            self.assertEqual(len(cur.executed("SET recurrence_key = %s")), 1)
            self.assertEqual(calls, [])
            self.assertEqual(lc.detect_recurrence(_Conn(_Cur([(None, None), (1,)])), 9), "unknown:uncategorized")
            cur = _Cur([("nas", "disk"), (lc.RECURRENCE_THRESHOLD,)])
            self.assertEqual(lc.detect_recurrence(_Conn(cur), 9), "nas:disk")             # the recurring-pattern warning
        self.assertEqual(len(calls), 1)
        title, kw = calls[0]
        self.assertEqual(title, "Recurring incident pattern: nas:disk")
        self.assertEqual((kw["level"], kw["category"]), ("warning", "incident_recurring"))
        self.assertEqual(kw["dedup_key"], f"recurring-nas:disk-{TODAY}")
        self.assertIn("recurred 3 times in 7d — needs a permanent fix", kw["body"])
        self.assertEqual(kw["meta"]["count_7d"], 3)

    def test_detect_recurrence_never_keys_on_its_own_notifications(self):
        calls = []
        for cat in ("incident_recurring", "incident"):
            cur = _Cur([("nas", cat), (99,)])
            with _notify(calls):
                self.assertIsNone(lc.detect_recurrence(_Conn(cur), 9))
            self.assertEqual(cur.executed("SET recurrence_key"), [])         # no stamp, no loop
        self.assertEqual(calls, [])

    def test_auto_close_resolved_edges(self):
        calls = []
        stale = [(1, "nas", "disk full", NOW, 5400), (2, None, "gpu wedge", NOW, 60)]
        cur = _Cur([stale, None, (None,)]); conn = _Conn(cur)
        with _notify(calls):
            closed = lc.auto_close_resolved(conn, 45)
        self.assertEqual(closed, 1)                                            # #1 lost the race -> skipped
        self.assertEqual(len(calls), 1)
        title, kw = calls[0]
        self.assertEqual(title, "Incident #2 resolved after 1.0m")             # NULL mttr -> SELECT's estimate
        self.assertIn("no new events in 45m", kw["body"])
        self.assertNotIn("(host", kw["body"])
        self.assertEqual(kw["dedup_key"], "incident-resolved-2")
        self.assertEqual(cur.params[0], (45,))

    def test_auto_close_public_reports_rowcount(self):
        conn = _Conn(_Cur(rowcount=4))
        self.assertEqual(lc.auto_close_public(conn), 4)
        self.assertIn("title ILIKE '%security%' AND started_at < now() - interval '7 days'", conn.cur.sql[0])
        self.assertEqual(conn.commits, 1)

    def test_stats_shape(self):
        row = {"open_now": 1, "resolved_24h": 2, "opened_24h": 3, "avg_mtta_min_7d": 1.5, "avg_mttr_min_7d": 9.0,
               "distinct_patterns_7d": 1}
        s = lc.stats(_Conn(_Cur([row, [{"recurrence_key": "nas:disk", "n": 4}]])))
        self.assertEqual(s["top_recurring"], [{"key": "nas:disk", "count": 4}])
        self.assertEqual(s["open_now"], 1)


class TestIntegration(unittest.TestCase):
    def test_sweep_chains_recurrence_then_close_then_public(self):
        cur = _Cur([[(1,)], ("nas", "disk"), (3,), [(1, "nas", "disk full", NOW, 5400)], (5400,)], rowcount=2)
        conn = _Conn(cur); calls = []
        with _notify(calls):
            s = lc.sweep(conn, 30)
        self.assertEqual(s, {"closed": 1, "recurrence_scanned": 1, "public_closed": 2})
        self.assertEqual([c[1]["category"] for c in calls], ["incident_recurring", "incident"])
        order = [i for i, q in enumerate(cur.sql) if "recurrence_key = %s" in q or "SET status = 'resolved'" in q or "UPDATE public.incidents" in q]
        self.assertEqual(order, sorted(order))                                 # stamp, then close, then public

    def test_recurrence_key_and_category_match_the_escalation_detector(self):
        esc = (SCRIPTS / "nova_incident_escalation.py").read_text()
        self.assertIn('":incident_recurring"', esc)                            # escalation excludes OUR category
        self.assertIn('category="incident_recurring"', SRC)
        self.assertIn('key = f"{host or \'unknown\'}:{root_cat or \'uncategorized\'}"', SRC)

    def test_notify_delegates_to_the_bus_with_this_source(self):
        calls = []
        with _notify(calls):
            lc.acknowledge(_Conn(_Cur([(10,)])), 3, "cli")
        self.assertEqual(calls[0][1]["source"], "nova_incident_lifecycle.py")


class TestFunctional(unittest.TestCase):
    def _main(self, argv, conn):
        real_argv = sys.argv; sys.argv = ["nova_incident_lifecycle.py", *argv]
        buf = io.StringIO(); calls = []
        connect = (lambda *a, **k: conn) if conn is not None else (lambda *a, **k: (_ for _ in ()).throw(OSError("pg down")))
        try:
            with patch.object(psycopg2, "connect", connect), _notify(calls), redirect_stdout(buf), _quiet():
                with self.assertRaises(SystemExit) as cm:
                    lc.main()
        finally:
            sys.argv = real_argv
        return cm.exception.code, buf.getvalue(), calls

    def test_ack_golden_path(self):
        conn = _Conn(_Cur([(30,)]))
        code, out, calls = self._main(["--ack", "5", "--who", "jordan"], conn)
        self.assertEqual(code, 0)
        self.assertIn("acked", out)
        self.assertIn("ALTER TABLE telemetry.incidents", conn.cur.sql[0])      # schema first, always
        self.assertEqual(calls[0][0], "Incident #5 acknowledged by jordan")
        self.assertTrue(conn.closed)

    def test_sweep_golden_path(self):
        conn = _Conn(_Cur([[(1,)], ("nas", "disk"), (3,), [(1, "nas", "disk full", NOW, 5400)], (5400,)], rowcount=2))
        code, out, calls = self._main(["--sweep", "--minutes", "45"], conn)
        self.assertEqual(code, 0)
        self.assertIn("sweep: closed 1, recurrence-scanned 1, public-closed 2", out)
        self.assertEqual(conn.cur.params[conn.cur.sql.index(conn.cur.executed("make_interval(mins => %s)")[0])], (45,))

    def test_pg_down_exits_one_without_raising(self):
        code, out, calls = self._main(["--sweep"], None)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])

    def test_migration_failure_stops_everything(self):
        conn = _Conn(_Cur(fail="ALTER TABLE"))
        code, out, calls = self._main(["--ack", "5"], conn)
        self.assertEqual(code, 1)
        self.assertEqual(conn.cur.executed("acked_at = now()"), [])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_without_touching_pg(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_incident_lifecycle.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--sweep", r.stdout)
        self.assertNotIn("DB connect", r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_incident_lifecycle"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
