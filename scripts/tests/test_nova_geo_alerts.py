#!/usr/bin/env python3
"""Tests for nova_geo_alerts.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2          # noqa: F401
import psycopg2.extras   # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_geo_alerts.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_geo_alerts_test_"))


def _load():
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    spec = importlib.util.spec_from_file_location("ngeoalerts", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_notify": nn}), patch("psycopg2.connect", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    mod.STATE = str(TMP / "geo.last")
    mod.notify = MagicMock(return_value=True)
    return mod


ga = _load()
T = datetime(2026, 1, 1, 12, 0)


def _row(src, mi, text, d="NE"):
    return {"id": 1, "source": src, "text": text, "created_at": T, "mi": mi, "dir": d}


class _MemCur:
    def __init__(self, rows, close, report=None):
        self.rows, self.close, self.report = rows, close, report or []
        self.sql = []; self.params = []

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def fetchall(self):
        if "GROUP BY source" in self._last:
            return [{"source": k, "c": v} for k, v in self.close.items()]
        if "ORDER BY created_at" in self._last and "created_at > %s" in self._last:
            return self.rows
        return self.report

    def fetchone(self):
        return {"ts": "2026-01-01 11:50:00"}


def _main(rows, close, argv=(), report=None, heli=0, wind=None):
    mc = _MemCur(rows, close, report)
    mem = MagicMock(); mem.cursor.return_value = mc
    oc = MagicMock(); oc.fetchone.side_effect = lambda: (heli,)
    ops = MagicMock(); ops.cursor.return_value = oc
    ga.notify.reset_mock()
    with patch.object(ga.psycopg2, "connect", side_effect=lambda dsn: mem if "nova_memories" in dsn else ops), \
         patch.object(ga, "_wind_dir", return_value=wind), patch.object(sys, "argv", ["x", *argv]), \
         redirect_stdout(io.StringIO()) as out:
        ga.main()
    return mc, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_cursor_value_is_parameterized_and_formats_are_constants(self):
        Path(ga.STATE).write_text("2026-01-01'; DROP TABLE memories;--")
        mc, _ = _main([], {})
        first = mc.sql[0]
        self.assertNotIn("DROP", first)
        self.assertEqual(mc.params[0], ("2026-01-01'; DROP TABLE memories;--",))
        # the only %-formatted SQL interpolates module constants, never row data
        for m in re.finditer(r'"\s*%\s*\(([^)]*)\)', SRC):
            self.assertEqual(set(x.strip() for x in m.group(1).split(",")) - {"WINDOW_MIN", "CONV_MI"}, set())

    def test_dry_run_never_notifies_or_moves_cursor(self):
        Path(ga.STATE).unlink(missing_ok=True)
        _, out = _main([_row("fire", 0.5, "working fire structure")], {"fire": 1}, argv=["--dry"])
        ga.notify.assert_not_called()
        self.assertFalse(Path(ga.STATE).exists())
        self.assertIn("PROXIMITY:", out)


class TestPerformance(unittest.TestCase):
    def test_serious_regex_10k_lines(self):
        lines = ["unit responding to a routine traffic stop on glenoaks"] * 9_999 + ["shots fired near olive"]
        t0 = time.perf_counter()
        hits = sum(1 for l in lines if ga.SERIOUS.search(l))
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(hits, 1)


class TestRetry(unittest.TestCase):
    def test_wind_lookup_fails_open(self):
        # RETRY GAP: _wind_dir — one PG read; failure returns None (no UPWIND tag, alert still sent)
        with patch.object(ga.psycopg2, "connect", side_effect=psycopg2.OperationalError("down")) as c:
            self.assertIsNone(ga._wind_dir())
        self.assertEqual(c.call_count, 1)

    def test_helicopter_lookup_failure_is_swallowed(self):
        # RETRY GAP: main()/overhead_flights — one attempt; failure means heli=False, alerts still go out
        mc = _MemCur([_row("scanner", 1.0, "pursuit southbound")], {})
        mem = MagicMock(); mem.cursor.return_value = mc
        ga.notify.reset_mock()
        def connect(dsn):
            if "nova_memories" in dsn:
                return mem
            raise psycopg2.OperationalError("ops down")
        with patch.object(ga.psycopg2, "connect", side_effect=connect), patch.object(ga, "_wind_dir", return_value=None), \
             patch.object(sys, "argv", ["x"]), redirect_stdout(io.StringIO()) as out:
            ga.main()
        self.assertEqual(ga.notify.call_count, 1)
        self.assertIn("heli=False", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_upwind(self):
        self.assertTrue(ga._upwind("NE", 60))
        self.assertTrue(ga._upwind("N", 340))           # wraps across 0
        self.assertFalse(ga._upwind("S", 0))
        self.assertFalse(ga._upwind(None, 10))
        self.assertFalse(ga._upwind("N", None))

    def test_serious_patterns(self):
        for t in ("Structure fire, fully involved", "ADW suspect", "officer needs help", "hostage situation"):
            self.assertTrue(ga.SERIOUS.search(t), t)
        for t in ("traffic collision non-injury", "noise complaint", "armedforces day parade"):
            self.assertFalse(ga.SERIOUS.search(t), t)

    def test_last_ts_falls_back_to_ten_minutes(self):
        Path(ga.STATE).unlink(missing_ok=True)
        cur = _MemCur([], {})
        self.assertEqual(ga._last_ts(cur), "2026-01-01 11:50:00")
        self.assertIn("interval '10 min'", cur.sql[0])


class TestIntegration(unittest.TestCase):
    def test_proximity_dedupes_and_tags_upwind_fire(self):
        Path(ga.STATE).unlink(missing_ok=True)
        rows = [_row("fire", 1.2, "[BFD] structure fire Olive"), _row("fire", 1.4, "[BFD] structure fire update"),
                _row("scanner", 9.0, "shots fired far away"), _row("scanner", 0.3, "loud music")]
        _main(rows, {"fire": 2}, wind=50)
        self.assertEqual(ga.notify.call_count, 1)
        body = ga.notify.call_args.kwargs["body"]
        self.assertIn("UPWIND", body)
        self.assertNotIn("[BFD]", body)
        self.assertEqual(Path(ga.STATE).read_text(), T.isoformat())


class TestFunctional(unittest.TestCase):
    def test_convergence_posts_report_with_heli(self):
        rep = [{"source": "fire", "text": "[x] smoke showing", "created_at": T, "mi": 1.0, "dir": "N"},
               {"source": "scanner", "text": "perimeter set", "created_at": T, "mi": 1.1, "dir": "N"}]
        _main([], {"scanner": 2, "fire": 1}, report=rep, heli=1)
        kw = ga.notify.call_args.kwargs
        self.assertEqual(kw["dedup_key"], "geo-convergence")
        self.assertIn("LAPD helicopter overhead", kw["body"])
        self.assertIn("12:00 ~1.0mi N: smoke showing", kw["body"])

    def test_quiet_night_posts_nothing(self):
        _, out = _main([], {"scanner": 3})
        ga.notify.assert_not_called()
        self.assertIn("0 proximity alert(s), convergence=False", out)


class TestFrame(unittest.TestCase):
    def test_import_smoke(self):
        # no --help/--selftest: every run (even --dry) reads PG, so the frame check is the import
        code = "import sys; sys.path.insert(0, sys.argv[1]); import nova_geo_alerts as g; print(g.ALERT_MI)"
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "2.5")

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        with patch.object(psycopg2, "connect", side_effect=AssertionError("import must not connect")):
            _load()


if __name__ == "__main__":
    unittest.main()
