#!/usr/bin/env python3
"""Tests for nova_contact_sense.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_contact_sense.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cs = _load("contact_sense_under_test", SCRIPT)
T0 = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)
H = timedelta(hours=1)


class _Cur:
    """Cursor stub: first matching SQL substring wins; records statements; rollback is counted."""
    def __init__(self, rules=(), raise_on=()):
        self.rules, self.raise_on = list(rules), tuple(raise_on)
        self.sql, self.params, self._last, self.rollbacks = [], [], None, 0
        self.connection = types.SimpleNamespace(rollback=self._rb, close=lambda: None)

    def _rb(self): self.rollbacks += 1

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = None
        for sub in self.raise_on:
            if sub in sql:
                raise RuntimeError(f"stub failure on {sub}")
        for sub, val in self.rules:
            if sub in sql:
                self._last = val
                return

    def fetchone(self):
        v = self._last
        return (v[0] if v else None) if isinstance(v, list) else v

    def fetchall(self):
        v = self._last
        return [] if v is None else (v if isinstance(v, list) else [v])

    def stmts(self, sub):
        return [(s, p) for s, p in zip(self.sql, self.params) if sub in s]


def _conn(cur):
    return types.SimpleNamespace(cursor=lambda *a, **k: cur, autocommit=False, close=lambda: None)


def _world(raise_on=()):
    oc = _Cur([("FROM gateway_traces", (T0 + 3 * H, 4)),
               ("FROM claude_messages", (T0 + 1 * H, 2)),
               ("FROM claude_actions", (T0 + 5 * H, 5))], raise_on=raise_on)
    mc = _Cur([("source='imessage'", (T0, 1)),
               ("source='email'", (None, 0))])
    return oc, mc


def _run_main(oc, mc, argv=()):
    buf = io.StringIO()
    with mock.patch.object(cs.psycopg2, "connect", side_effect=[_conn(oc), _conn(mc)]) as pc, \
         mock.patch.object(sys, "argv", ["nova_contact_sense.py", *argv]), redirect_stdout(buf):
        cs.main()
    return pc, buf.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('os.environ.get("NOVA_OPS_DSN"', SRC)

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertIn("VALUES (%s,%s,%s,%s,now())", cs.UPSERT)
        oc, mc = _world()
        cs.mouths(oc, mc)
        self.assertEqual(oc.params[0], (cs.MACHINE_CHANNELS,))          # channel list bound, not interpolated
        self.assertEqual(oc.params[1], (cs.JORDAN_SLACK,))

    def test_writes_only_its_own_table(self):
        writes = {m.group(1) for m in re.finditer(r"(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+([\w.]+)", SRC)}
        self.assertEqual(writes, {"contact_sense"})


class TestPerformance(unittest.TestCase):
    def test_merge_fast_on_10k_parts(self):
        parts = [(T0 + i * H, i % 7) if i % 3 else None for i in range(10_000)]
        t0 = time.perf_counter()
        last, cnt = cs.merge(parts)
        self.assertLess(time.perf_counter() - t0, 0.1)
        self.assertEqual(last, T0 + 9_998 * H)
        self.assertEqual(cnt, sum(i % 7 for i in range(10_000) if i % 3))


class TestRetry(unittest.TestCase):
    def test_one_fails_open_and_rolls_back(self):
        # RETRY GAP: one() — a single execute; a failed query rolls back, logs, and the mouth is silent
        cur = _Cur(raise_on=("FROM nowhere",))
        with redirect_stdout(io.StringIO()):
            self.assertIsNone(cs.one(cur, "SELECT 1 FROM nowhere"))
        self.assertEqual(cur.rollbacks, 1)
        self.assertEqual(cs.one(_Cur([("SELECT 1", (1,))]), "SELECT 1"), (1,))


class TestUnit(unittest.TestCase):
    def test_merge_edges(self):
        self.assertEqual(cs.merge([]), (None, 0))
        self.assertEqual(cs.merge([None, (None, 0)]), (None, 0))
        self.assertEqual(cs.merge([(T0, None)]), (T0, 0))
        self.assertEqual(cs.merge([(T0, 3), (T0 + 2 * H, 5)]), (T0 + 2 * H, 8))
        self.assertEqual(cs.merge([(T0 + 2 * H, 1), (T0, 1)]), (T0 + 2 * H, 2))

    def test_selftest_passes(self):
        with redirect_stdout(io.StringIO()):
            cs.selftest()


class TestIntegration(unittest.TestCase):
    def test_mouths_shape_matches_the_readers(self):
        # nova_time_sense takes max(last_at); nova_affect sums count_24h — both need these four mouths
        oc, mc = _world()
        rows = cs.mouths(oc, mc)
        self.assertEqual(set(rows), {"gateway", "imessage", "email", "claude"})
        self.assertEqual(rows["claude"][:2], (T0 + 5 * H, 7))                 # merge(messages, active hours)
        self.assertEqual(rows["email"][:2], (None, 0))
        self.assertEqual(max(v[0] for v in rows.values() if v[0]), T0 + 5 * H)

    def test_upsert_targets_contact_sense_by_mouth(self):
        self.assertTrue(cs.UPSERT.startswith("INSERT INTO contact_sense (mouth, last_at, count_24h, detail, updated_at)"))
        self.assertIn("ON CONFLICT (mouth) DO UPDATE", cs.UPSERT)
        self.assertIn("CREATE TABLE IF NOT EXISTS contact_sense", cs.DDL)


class TestFunctional(unittest.TestCase):
    def test_golden_path_upserts_every_mouth(self):
        oc, mc = _world()
        pc, out = _run_main(oc, mc)
        self.assertEqual(pc.call_count, 2)
        self.assertEqual(oc.sql[0], cs.DDL)
        ups = {p[0]: p[1:] for _, p in oc.stmts("INSERT INTO contact_sense")}
        self.assertEqual(set(ups), {"gateway", "imessage", "email", "claude"})
        self.assertEqual(ups["gateway"][:2], (T0 + 3 * H, 4))
        self.assertEqual(ups["claude"][:2], (T0 + 5 * H, 7))
        self.assertEqual(ups["email"][:2], (None, 0))
        self.assertIn("latest mouth: claude", out)

    def test_dry_run_writes_nothing(self):
        oc, mc = _world()
        pc, out = _run_main(oc, mc, ["--dry-run"])
        self.assertFalse(oc.stmts("INSERT INTO contact_sense"))
        self.assertIn("gateway   last=", out)

    def test_missing_table_silences_one_mouth_not_the_run(self):
        oc, mc = _world(raise_on=("FROM gateway_traces",))
        pc, out = _run_main(oc, mc)
        ups = {p[0] for _, p in oc.stmts("INSERT INTO contact_sense")}
        self.assertEqual(ups, {"imessage", "email", "claude"})
        self.assertEqual(oc.rollbacks, 1)
        self.assertIn("query failed (RuntimeError)", out)


class TestFrame(unittest.TestCase):
    def test_selftest_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], cwd=SCRIPTS,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"}, capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("selftest ok", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with mock.patch("psycopg2.connect", side_effect=AssertionError("import must not connect")):
            _load("contact_sense_frame_probe", SCRIPT)


if __name__ == "__main__":
    unittest.main()
