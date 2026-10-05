#!/usr/bin/env python3
"""Tests for nova_incident_escalation.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The original pytest-style classify() checks are kept below, then the 7 house classes follow.

Categories:
  1. below threshold          -> not escalated
  2. chronic + recent         -> escalated
  3. single-day burst         -> not escalated (span < MIN_SPAN_DAYS)
  4. stale (last_open old)     -> not escalated
  5. meta-key exclusion        -> :incident_recurring / :incident_escalation ignored
  6. NULL recurrence_key        -> ignored, and reason string is well-formed
  7. ordering + malformed-row robustness
"""
import datetime
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
m = importlib.import_module("nova_incident_escalation")

NOW = datetime.datetime(2026, 9, 8, 12, 0, tzinfo=datetime.timezone.utc)


def _row(key, pages, days_span, last_age_days, resolved=0, acked=0, open_now=0):
    return {
        "recurrence_key": key,
        "pages": pages,
        "resolved": resolved,
        "acked": acked,
        "open_now": open_now,
        "distinct_days": days_span,
        "first_open": NOW - datetime.timedelta(days=6),
        "last_open": NOW - datetime.timedelta(days=last_age_days),
    }


# 1 ──────────────────────────────────────────────────────────────────────────
def test_below_threshold_not_escalated():
    rows = [_row("host:cat", pages=m.ESCALATE_THRESHOLD - 1, days_span=4, last_age_days=0)]
    assert m.classify(rows, now=NOW) == []


# 2 ──────────────────────────────────────────────────────────────────────────
def test_chronic_recent_escalated():
    rows = [_row("host:cat", pages=17, days_span=5, last_age_days=0,
                 resolved=17, acked=17)]
    hits = m.classify(rows, now=NOW)
    assert len(hits) == 1 and hits[0]["recurrence_key"] == "host:cat"
    assert int(hits[0]["pages"]) == 17


# 3 ──────────────────────────────────────────────────────────────────────────
def test_single_day_burst_not_escalated():
    # lots of pages but all on one calendar day -> a burst, not chronic
    rows = [_row("host:cat", pages=50, days_span=1, last_age_days=0)]
    assert m.classify(rows, now=NOW) == []


# 4 ──────────────────────────────────────────────────────────────────────────
def test_stale_not_escalated():
    # crossed count + span, but nothing recent -> problem went away
    rows = [_row("host:cat", pages=20, days_span=4,
                 last_age_days=m.RECENT_DAYS + 2)]
    assert m.classify(rows, now=NOW) == []


# 5 ──────────────────────────────────────────────────────────────────────────
def test_meta_keys_excluded():
    rows = [
        _row("Mac-mini:incident_recurring", pages=30, days_span=5, last_age_days=0),
        _row("Mac-mini:incident_escalation", pages=30, days_span=5, last_age_days=0),
    ]
    assert m.classify(rows, now=NOW) == []


# 6 ──────────────────────────────────────────────────────────────────────────
def test_null_key_ignored_and_reason_wellformed():
    rows = [
        {"recurrence_key": None, "pages": 99, "distinct_days": 9,
         "last_open": NOW, "resolved": 0, "acked": 0, "open_now": 0},
        _row("host:cat", pages=9, days_span=3, last_age_days=0,
             resolved=8, acked=8, open_now=1),
    ]
    hits = m.classify(rows, now=NOW)
    assert [h["recurrence_key"] for h in hits] == ["host:cat"]
    reason = m._reason(hits[0])
    assert "paged 9x" in reason and "ack'd 8x" in reason and "still open" in reason


# 7 ──────────────────────────────────────────────────────────────────────────
def test_ordering_and_malformed_rows():
    rows = [
        _row("low:cat", pages=8, days_span=2, last_age_days=0),
        _row("high:cat", pages=45, days_span=6, last_age_days=0),
        {"garbage": True},                       # malformed -> skipped, no raise
        {"recurrence_key": "bad", "pages": "NaN",  # bad types -> skipped
         "distinct_days": None, "last_open": None},
    ]
    hits = m.classify(rows, now=NOW)
    assert [h["recurrence_key"] for h in hits] == ["high:cat", "low:cat"]


# ── the 7 house categories (added 2026-10-05) ────────────────────────────────
import io
import os
import re
import subprocess
import time
import types
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

import psycopg2

SCRIPTS = Path(__file__).resolve().parents[1]
SRC = (SCRIPTS / "nova_incident_escalation.py").read_text()
PRE_SELFTEST = SRC[:SRC.index("def _selftest")]
# scan()/main() classify against the real clock, so DB rows are stamped relative to it; the pure
# decision tests keep the fixed NOW above.
REAL_NOW = datetime.datetime.now(datetime.timezone.utc)


class _Cur:
    def __init__(self, answers=(), fail=None):
        self.answers = list(answers); self.sql = []; self.params = []; self.fail = fail

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
    mod = types.ModuleType("nova_notify")

    def notify(title, **kw):
        if raise_:
            raise RuntimeError("bus down")
        calls.append((title, kw)); return True
    mod.notify = notify
    return patch.dict(sys.modules, {"nova_notify": mod})


def _quiet():
    return redirect_stderr(io.StringIO())


def _db_row(key="dsm:disk", pages=17, days=5, age_days=0, host="nas", acked_at=None):
    return {"recurrence_key": key, "pages": pages, "resolved": pages, "acked": 0, "open_now": 1, "distinct_days": days,
            "first_open": REAL_NOW - datetime.timedelta(days=6), "last_open": REAL_NOW - datetime.timedelta(days=age_days),
            "last_acked_at": acked_at, "severity": "warning", "host": host}


def _state(count=1, minutes_ago=10, last_pages=17, acked=False, first=None):
    return {"recurrence_key": "dsm:disk", "escalate_count": count, "last_pages": last_pages, "acked": acked,
            "last_escalated_ts": NOW - datetime.timedelta(minutes=minutes_ago),
            "first_escalated_ts": first or NOW - datetime.timedelta(days=1)}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_fstring_sql_only_interpolates_the_state_table_constant(self):
        blocks = re.findall(r'execute\(\s*f"""(.*?)"""', SRC, re.S) + re.findall(r'execute\(\s*f"(?!")([^"]*)"', SRC)
        self.assertGreaterEqual(len(blocks), 4)
        for b in blocks:
            self.assertEqual(set(re.findall(r"\{(\w+)\}", b)), {"STATE_TABLE"}, b)
        self.assertEqual(m.STATE_TABLE, "public.escalation_state")

    def test_only_write_outside_the_selftest_is_its_own_throttle_table(self):
        writes = re.findall(r"\b(?<!DO )(?:INSERT INTO|UPDATE|DELETE FROM)\s+(\{STATE_TABLE\}|[\w.]+)", PRE_SELFTEST)
        self.assertTrue(writes)
        self.assertEqual(set(writes), {"{STATE_TABLE}"})
        self.assertNotIn("UPDATE telemetry.incidents", PRE_SELFTEST)

    def test_meta_keys_can_never_escalate_themselves(self):
        rows = [_db_row(key=f"nas{s}") for s in m._META_SUFFIXES]
        self.assertEqual(m.classify(rows, now=REAL_NOW), [])


class TestPerformance(unittest.TestCase):
    def test_classify_and_decide_fast_on_10k(self):
        rows = [_db_row(key=f"h{i}:cat", pages=(i % 30), days=(i % 6), age_days=(i % 5)) for i in range(10_000)]
        t0 = time.perf_counter()
        hits = m.classify(rows, now=REAL_NOW)
        for h in hits:
            m.escalation_decision(_state(), h, now=NOW)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertTrue(hits)
        self.assertEqual([int(h["pages"]) for h in hits], sorted((int(h["pages"]) for h in hits), reverse=True))


class TestRetry(unittest.TestCase):
    def test_connect_is_one_shot_and_returns_none(self):
        # RETRY GAP: _connect()/psycopg2.connect — one attempt, None on failure
        attempts = []

        def boom(*a, **k):
            attempts.append(1); raise OSError("pg down")
        with patch.object(psycopg2, "connect", boom), _quiet():
            self.assertIsNone(m._connect())
        self.assertEqual(len(attempts), 1)

    def test_notify_fails_open(self):
        # RETRY GAP: _notify()/nova_notify.notify — one call; raising or missing bus returns False
        with _notify([], raise_=True):
            self.assertFalse(m._notify("t"))
        with patch.dict(sys.modules, {"nova_notify": None}):
            self.assertFalse(m._notify("t"))

    def test_db_reads_and_writes_fail_open(self):
        # RETRY GAP: find_recurring / _load_states / _record_escalation / _forget_absent — one attempt each
        conn = _Conn(_Cur(fail=True))
        with _quiet():
            self.assertEqual(m.find_recurring(conn), [])
            self.assertEqual(m._load_states(conn, ["k"]), {})
            m._record_escalation(conn, "k", NOW, 1, 9, False)
            m._forget_absent(conn, ["k"])
            m._ensure_state_table(conn)
        self.assertEqual(conn.rollbacks, 4)
        self.assertEqual(m._load_states(conn, []), {})                    # no keys: no query at all


class TestUnit(unittest.TestCase):
    def test_required_gap_hours(self):
        self.assertEqual([m._required_gap_hours(n) for n in (0, 1, 2, 3, 9)], [1, 1, 4, 24, 24])

    def test_escalation_decision_kinds(self):
        hit = _db_row(pages=17)
        self.assertEqual(m.escalation_decision(None, hit, NOW)["kind"], "first")
        self.assertEqual(m.escalation_decision(_state(count=1, minutes_ago=10), hit, NOW)["kind"], "backoff-hold")
        d = m.escalation_decision(_state(count=1, minutes_ago=61), hit, NOW)
        self.assertEqual((d["kind"], d["new_count"], d["emit"]), ("backoff-due", 2, True))
        self.assertEqual(m.escalation_decision(_state(count=2, minutes_ago=180), hit, NOW)["kind"], "backoff-hold")
        self.assertEqual(m.escalation_decision(_state(count=2, minutes_ago=241), hit, NOW)["kind"], "backoff-due")
        self.assertEqual(m.escalation_decision(_state(acked=True, minutes_ago=60), hit, NOW)["kind"], "ack-hold")
        self.assertEqual(m.escalation_decision(_state(acked=True, minutes_ago=25 * 60), hit, NOW)["kind"], "ack-daily-reminder")
        d = m.escalation_decision(_state(acked=True, minutes_ago=5, last_pages=5), _db_row(pages=17), NOW)
        self.assertEqual((d["kind"], d["clear_ack"]), ("worsened-despite-ack", True))
        # an ack on the incident itself, at/after our first page, counts as ours
        inc_acked = _db_row(pages=17, acked_at=NOW - datetime.timedelta(hours=2))
        self.assertEqual(m.escalation_decision(_state(minutes_ago=30), inc_acked, NOW)["kind"], "ack-hold")
        stale_ack = _db_row(pages=17, acked_at=NOW - datetime.timedelta(days=3))
        self.assertEqual(m.escalation_decision(_state(minutes_ago=30), stale_ack, NOW)["kind"], "backoff-hold")

    def test_reason_variants(self):
        self.assertEqual(m._reason({"pages": 8, "distinct_days": 2}), "paged 8x over 2 day(s)")
        r = m._reason({"pages": 9, "distinct_days": 3, "resolved": 8, "acked": 8, "open_now": 1})
        self.assertIn("auto-resolved 8x but recurred anyway", r)
        self.assertIn("1 still open", r)

    def test_emit_titles_follow_the_decision_kind(self):
        calls = []
        with _notify(calls):
            m._emit_escalation(_db_row(), {"kind": "first", "new_count": 1}, NOW)
            m._emit_escalation(_db_row(), {"kind": "ack-daily-reminder", "new_count": 2}, NOW)
            m._emit_escalation(_db_row(host=None), {"kind": "worsened-despite-ack", "new_count": 3}, NOW)
        self.assertEqual([t.split(" ")[0] for t, _ in calls], ["UNRESOLVED", "STILL", "WORSENING"])
        self.assertEqual({kw["level"] for _, kw in calls}, {"critical"})
        self.assertEqual({kw["category"] for _, kw in calls}, {"incident_escalation"})
        self.assertEqual([kw["dedup_key"] for _, kw in calls],
                         [f"escalation-dsm:disk-{NOW.date().isoformat()}-{n}" for n in (1, 2, 3)])
        self.assertIn(" on nas", calls[0][1]["body"])
        self.assertNotIn(" on ", calls[2][1]["body"].split("WORSENED")[0])


class TestIntegration(unittest.TestCase):
    def test_scan_first_page_records_state_and_forgets_absent(self):
        cur = _Cur([[_db_row()], []]); conn = _Conn(cur); calls = []
        with _notify(calls):
            hits = m.scan(conn, emit=True)
        self.assertEqual(hits[0]["_decision"]["kind"], "first")
        self.assertTrue(hits[0]["_emitted"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(cur.executed("INSERT INTO public.escalation_state")), 1)
        self.assertEqual(cur.params[cur.sql.index(cur.executed("INSERT INTO public.escalation_state")[0])]["cnt"], 1)
        self.assertEqual(len(cur.executed("DELETE FROM public.escalation_state WHERE NOT (recurrence_key = ANY(%s))")), 1)
        self.assertEqual(conn.commits, 2)                                    # ensure_state_table + end of scan

    def test_scan_throttles_a_repeat_page(self):
        recent = {**_state(count=1), "last_escalated_ts": REAL_NOW - datetime.timedelta(minutes=10)}
        cur = _Cur([[_db_row()], [recent]]); conn = _Conn(cur); calls = []
        with _notify(calls):
            hits = m.scan(conn, emit=True)
        self.assertEqual(hits[0]["_decision"]["kind"], "backoff-hold")
        self.assertFalse(hits[0]["_emitted"])
        self.assertEqual(calls, [])
        self.assertEqual(cur.executed("INSERT INTO"), [])

    def test_dry_run_scan_reads_only(self):
        cur = _Cur([[_db_row()], []]); conn = _Conn(cur); calls = []
        with _notify(calls):
            hits = m.scan(conn, emit=False)
        self.assertEqual(len(hits), 1)
        self.assertEqual(calls, [])
        self.assertEqual([s for s in cur.sql if not s.startswith("SELECT")], [])
        self.assertEqual(conn.commits, 0)

    def test_lifecycle_warning_category_is_what_we_exclude(self):
        lc = (SCRIPTS / "nova_incident_lifecycle.py").read_text()
        self.assertIn('category="incident_recurring"', lc)
        self.assertIn(":incident_recurring", m._META_SUFFIXES)


class TestFunctional(unittest.TestCase):
    def _main(self, argv, conn):
        real_argv = sys.argv; sys.argv = ["nova_incident_escalation.py", *argv]
        buf = io.StringIO(); calls = []
        connect = (lambda *a, **k: conn) if conn is not None else (lambda *a, **k: (_ for _ in ()).throw(OSError("pg down")))
        try:
            with patch.object(psycopg2, "connect", connect), _notify(calls), redirect_stdout(buf), _quiet():
                with self.assertRaises(SystemExit) as cm:
                    m.main()
        finally:
            sys.argv = real_argv
        return cm.exception.code, buf.getvalue(), calls

    def test_scan_golden_path_pages_once(self):
        conn = _Conn(_Cur([[_db_row()], []]))
        code, out, calls = self._main(["--scan"], conn)
        self.assertEqual(code, 0)
        self.assertIn("1 chronic-unresolved pattern(s): PAGED 1, throttled 0", out)
        self.assertIn("[PAGE] x17", out)
        self.assertEqual(calls[0][0], "UNRESOLVED x17: dsm:disk needs a PERMANENT fix")
        self.assertTrue(conn.closed)

    def test_dry_run_prints_would_page_and_emits_nothing(self):
        conn = _Conn(_Cur([[_db_row()], []]))
        code, out, calls = self._main(["--dry-run"], conn)
        self.assertEqual(code, 0)
        self.assertIn("WOULD page 1", out)
        self.assertEqual(calls, [])

    def test_nothing_crossing_the_bar(self):
        conn = _Conn(_Cur([[_db_row(pages=3)], []]))
        code, out, calls = self._main(["--scan"], conn)
        self.assertEqual(code, 0)
        self.assertIn("no chronic-unresolved incident patterns crossed the escalation bar", out)

    def test_pg_down_exits_one(self):
        code, out, calls = self._main(["--scan"], None)
        self.assertEqual(code, 1)
        self.assertEqual(calls, [])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_without_touching_pg(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_incident_escalation.py"), "--help"], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--dry-run", r.stdout)
        self.assertNotIn("DB connect", r.stderr)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_incident_escalation"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
