#!/usr/bin/env python3
"""Tests for nova_gateway/identity.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_gateway" / "identity.py"
SRC = SCRIPT.read_text()


def _load():
    # by path, so the package __init__ (which pulls in the whole gateway) never runs
    spec = importlib.util.spec_from_file_location("ngw_identity", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


idn = _load()


class _Pool:
    """asyncpg-pool stand-in: records SQL+args; optional rows / failure."""
    def __init__(self, row=None, rows=(), fail_times=0):
        self.executed = []; self.row = row; self.rows = list(rows); self.fail_times = fail_times; self.calls = 0

    def _maybe_fail(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("pg down")

    async def execute(self, sql, *args):
        self._maybe_fail(); self.executed.append((sql, args))

    async def fetchrow(self, sql, *args):
        return self.row

    async def fetch(self, sql, *args):
        self.executed.append((sql, args)); return self.rows


def run(c):
    return asyncio.run(c)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r"\.(execute|fetch|fetchrow)\(\s*f[\"']", SRC))
        pool = _Pool()
        run(idn.update_active_channel(pool, "slack", "C1'); --injected"))
        sql, args = pool.executed[0]
        self.assertNotIn("--injected", sql)
        self.assertEqual(args, ("slack:C1'); --injected", "desktop"))

    def test_saved_summary_capped(self):
        pool = _Pool()
        run(idn.save_cross_context(pool, "slack", "s", "S" * 5000))
        self.assertEqual(len(pool.executed[0][1][0]), 500)


class TestPerformance(unittest.TestCase):
    def test_style_lookup_10k_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            idn.get_response_style(("mobile", "desktop", "tv", None)[i % 4])
        self.assertLess(time.perf_counter() - t0, 0.5)

    def test_cross_context_limited_to_three_rows(self):
        self.assertIn("LIMIT 3", SRC)


class TestRetry(unittest.TestCase):
    def test_resolve_identity_fails_open(self):
        # RETRY GAP: resolve_identity/pool.execute — one attempt, default identity on failure
        pool = _Pool(fail_times=5)
        r = run(idn.resolve_identity(pool, "signal", "+1"))
        self.assertEqual(r, {"user_id": "jordan", "display_name": "Jordan", "device_mode": "mobile", "preferences": {}})
        self.assertEqual(pool.calls, 1)

    def test_other_writers_fail_open(self):
        # RETRY GAP: get_cross_context / save_cross_context / update_active_channel — one shot, swallowed
        self.assertEqual(run(idn.get_cross_context(_Pool(fail_times=1), "slack")), "")
        self.assertIsNone(run(idn.save_cross_context(_Pool(fail_times=1), "slack", "s", "x" * 40)))
        self.assertIsNone(run(idn.update_active_channel(_Pool(fail_times=1), "slack", "C")))


class TestUnit(unittest.TestCase):
    def test_short_summary_skipped(self):
        pool = _Pool()
        run(idn.save_cross_context(pool, "slack", "s", "too short"))
        run(idn.save_cross_context(pool, "slack", "s", ""))
        self.assertEqual(pool.executed, [])

    def test_response_style_default(self):
        self.assertEqual(idn.get_response_style("unknown"), idn.RESPONSE_STYLE["desktop"])
        self.assertEqual(idn.DEVICE_MODE_MAP["signal"], "mobile")

    def test_empty_cross_context(self):
        self.assertEqual(run(idn.get_cross_context(_Pool(rows=[]), "slack")), "")


class TestIntegration(unittest.TestCase):
    def test_save_then_read_roundtrip_shape(self):
        pool = _Pool()
        run(idn.save_cross_context(pool, "discord", "gw2:discord:1", "We talked about the HVAC filter.", ["hvac"]))
        _, args = pool.executed[0]
        row = {"summary": args[0], "source_channel": args[1], "topics": args[3], "created_at": None}
        out = run(idn.get_cross_context(_Pool(rows=[row]), "slack"))
        self.assertEqual(out, "Recent context from other channels:\n[discord] We talked about the HVAC filter.")

    def test_expired_rows_cleaned_before_read(self):
        pool = _Pool(rows=[])
        run(idn.get_cross_context(pool, "slack"))
        self.assertIn("DELETE FROM cross_channel_context WHERE expires_at < now()", pool.executed[0][0])
        self.assertEqual(pool.executed[1][1], ("slack",))


class TestFunctional(unittest.TestCase):
    def test_resolve_identity_golden_path(self):
        row = {"user_id": "jordan", "display_name": "Little Mister", "active_channel": "slack:C1",
               "device_mode": None, "preferences": None}
        pool = _Pool(row=row)
        r = run(idn.resolve_identity(pool, "signal", "+1"))
        self.assertEqual((r["display_name"], r["device_mode"], r["preferences"]), ("Little Mister", "mobile", {}))
        self.assertIn("ON CONFLICT (channel_type, channel_id)", pool.executed[0][0])
        self.assertEqual(pool.executed[0][1], ("signal", "+1", "mobile"))

    def test_resolve_identity_no_row_returns_default(self):
        r = run(idn.resolve_identity(_Pool(row=None), "discord", "9"))
        self.assertEqual(r["device_mode"], "desktop")


class TestFrame(unittest.TestCase):
    def test_import_smoke(self):
        code = (f"import importlib.util as u; s=u.spec_from_file_location('i', {str(SCRIPT)!r}); "
                "m=u.module_from_spec(s); s.loader.exec_module(m); print(m.get_response_style('mobile')['max_tokens'])")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "1024")
        self.assertNotIn("__main__", SRC)


if __name__ == "__main__":
    unittest.main()
