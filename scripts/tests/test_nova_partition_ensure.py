#!/usr/bin/env python3
"""Tests for nova_partition_ensure.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import datetime
import importlib.util
import io
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
SCRIPT = SCRIPTS / "nova_partition_ensure.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_partition_ensure_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pe = _load()
import nova_config  # noqa: E402


class _Cur:
    def __init__(self, parents, existing=(), fail=()):
        self.parents, self.existing, self.fail = parents, set(existing), set(fail)
        self.sql, self._reg = [], None

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if sql.startswith("SELECT to_regclass"):
            name = params[0].split('"')[1]
            self._reg = (name if name in self.existing else None,)
        elif sql.startswith("CREATE TABLE"):
            part = sql.split('"')[1]
            if part in self.fail:
                raise RuntimeError("overlap")

    def fetchall(self):
        return [(p,) for p in self.parents]

    def fetchone(self):
        return self._reg


def _months(n=pe.MONTHS_AHEAD + 1):
    t = datetime.date.today()
    y, m, out = t.year, t.month, []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        y, m = pe._add_month(y, m)
    return out


def _run(cur):
    with patch.object(pe.psycopg2, "connect", return_value=MagicMock(cursor=MagicMock(return_value=cur))), \
         patch.object(nova_config, "post_both") as pb, redirect_stdout(io.StringIO()) as out:
        rc = pe.main()
    return rc, pb, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        self.assertNotRegex(SRC, r"(?i)(password|secret|token|api[_-]?key)\s*=\s*['\"][^'\"]{8,}")

    def test_bounds_parameterized_and_scope_telemetry_only(self):
        cur = _Cur(["presence"])
        _run(cur)
        creates = [(s, p) for s, p in cur.sql if s.startswith("CREATE TABLE")]
        self.assertTrue(all(s.startswith('CREATE TABLE telemetry."presence_') and "FROM (%s) TO (%s)" in s
                            for s, _ in creates))
        self.assertIn("n.nspname = 'telemetry' AND parent.relkind = 'p'", cur.sql[0][0])
        self.assertNotRegex(SRC, r"\b(DROP|TRUNCATE|DELETE FROM)\b")


class TestPerformance(unittest.TestCase):
    def test_many_parents(self):
        cur = _Cur([f"t{i}" for i in range(500)], existing={f"t{i}_{m}" for i in range(500) for m in _months()})
        t0 = time.perf_counter()
        rc, pb, _ = _run(cur)
        self.assertLess(time.perf_counter() - t0, 2.0)
        pb.assert_not_called()


class TestRetry(unittest.TestCase):
    def test_create_failure_reported_not_raised(self):
        # RETRY GAP: main/CREATE TABLE — one attempt per partition; failures collected and alerted
        first = f"soil_{_months()[0]}"
        cur = _Cur(["soil"], fail={first})
        rc, pb, out = _run(cur)
        self.assertEqual(rc, 0)
        self.assertIn("FAILED: " + first, out)
        self.assertIn(":x: 1 failed", pb.call_args[0][0])

    def test_alert_failure_swallowed(self):
        with patch.object(pe.psycopg2, "connect", return_value=MagicMock(cursor=MagicMock(return_value=_Cur(["a"])))), \
             patch.object(nova_config, "post_both", side_effect=RuntimeError("slack")), \
             redirect_stdout(io.StringIO()) as out:
            self.assertEqual(pe.main(), 0)
        self.assertIn("alert post failed", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_add_month(self):
        self.assertEqual(pe._add_month(2026, 12), (2027, 1))
        self.assertEqual(pe._add_month(2026, 1), (2026, 2))

    def test_bounds_cross_year(self):
        cur = _Cur(["x"])
        _run(cur)
        params = [p for s, p in cur.sql if s.startswith("CREATE")]
        for lo, hi in params:
            ly, lm = int(lo[:4]), int(lo[5:7])
            self.assertEqual(hi, "%d-%02d-01" % pe._add_month(ly, lm))


class TestIntegration(unittest.TestCase):
    def test_alert_goes_to_alerts_channel(self):
        rc, pb, _ = _run(_Cur(["presence"]))
        self.assertEqual(pb.call_args.kwargs["slack_channel"], nova_config.SLACK_ALERTS)
        self.assertIn("created 4 missing telemetry partition", pb.call_args[0][0])


class TestFunctional(unittest.TestCase):
    def test_creates_only_missing(self):
        months = _months()
        cur = _Cur(["presence"], existing={f"presence_{months[0]}", f"presence_{months[1]}"})
        rc, pb, out = _run(cur)
        self.assertEqual(rc, 0)
        created = [s.split('"')[1] for s, _ in cur.sql if s.startswith("CREATE")]
        self.assertEqual(created, [f"presence_{m}" for m in months[2:]])
        self.assertIn("created 2, failed 0", out)

    def test_all_present_is_silent(self):
        cur = _Cur(["presence"], existing={f"presence_{m}" for m in _months()})
        rc, pb, _ = _run(cur)
        pb.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(pe.psycopg2, "connect") as c:
            _load()
        c.assert_not_called()


if __name__ == "__main__":
    unittest.main()
