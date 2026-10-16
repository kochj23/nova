#!/usr/bin/env python3
"""Tests for nova_service_tiers.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Little Mister, 2026-10-15 (#35). No Postgres: cursor stubs only.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_service_tiers.py
"""
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_service_tiers as t  # noqa: E402

SRC = (SCRIPTS / "nova_service_tiers.py").read_text()


def rows(*pairs):
    return [{"component": c, "tier": k, "rto_s": t.TIERS[k][0], "rpo_s": t.TIERS[k][1], "note": None}
            for c, k in pairs]


class _Cur:
    def __init__(self, exists=True, data=()):
        self.exists, self.data, self.sql = exists, list(data), []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._last = sql

    def fetchone(self):
        return (self.exists,)

    def fetchall(self):
        return self.data


class TestSecurity(unittest.TestCase):
    def test_sql_is_parameterized(self):
        cur = _Cur()
        t.set_tier(cur, "x'; DROP TABLE service_tiers; --", 1, "n")
        sql, params = cur.sql[0]
        self.assertNotIn("DROP", sql)
        self.assertEqual(params[0], "x'; DROP TABLE service_tiers; --")

    def test_no_secrets_or_hosts(self):
        self.assertIsNone(re.search(r"\b192\.168\.", SRC))
        self.assertNotIn("password", SRC.lower())


class TestPerformance(unittest.TestCase):
    def test_lookup_fast_over_large_table(self):
        big = rows(*[(f"task_{i}", 2) for i in range(5000)], *[(f"fam{i}_*", 1) for i in range(500)])
        t0 = time.perf_counter()
        for i in range(200):
            t.tier_for([f"task_{i}"], big)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_load_returns_none_when_table_absent(self):
        # no retry: an absent table is a state (PRR reports n/a), not an error
        self.assertIsNone(t.load(_Cur(exists=False)))

    def test_ensure_is_idempotent_on_conflict(self):
        cur = _Cur()
        import nova_pg_schema
        orig = nova_pg_schema.safe_ddl
        nova_pg_schema.safe_ddl = MagicMock(return_value=0)
        try:
            t.ensure(cur)
            t.ensure(cur)
        finally:
            nova_pg_schema.safe_ddl = orig
        inserts = [s for s, _ in cur.sql if s.startswith("INSERT INTO service_tiers")]
        self.assertTrue(all("ON CONFLICT (component) DO NOTHING" in s for s in inserts))
        self.assertEqual(len(inserts), 2 * len(t.SEED))


class TestUnit(unittest.TestCase):
    def test_targets_match_the_brief(self):
        self.assertEqual(t.targets(0), (3600, 900))
        self.assertEqual(t.targets(1), (4 * 3600, 24 * 3600))
        self.assertEqual(t.targets(2), (72 * 3600, 7 * 86400))

    def test_exact_beats_glob_and_longest_glob_wins(self):
        r = rows(("backup_*", 1), ("*_ingest", 2), ("backup_ingest", 2), ("journal_*", 2), ("journal_emergency_*", 1))
        self.assertEqual(t.tier_for(["backup_ingest"], r)["component"], "backup_ingest")
        self.assertEqual(t.tier_for(["journal_emergency_daily"], r)["tier"], 1)
        self.assertEqual(t.tier_for(["journal_digest"], r)["tier"], 2)

    def test_tie_goes_to_stricter_tier(self):
        r = rows(("*_ingest", 2), ("backup_*", 1))
        self.assertEqual(t.tier_for(["backup_ingest"], r)["tier"], 1)

    def test_variants_strip_nova_prefix_and_suffix(self):
        r = rows(("memory_server", 0))
        self.assertEqual(t.tier_for(["nova_memory_server.py"], r)["tier"], 0)
        self.assertIsNone(t.tier_for(["self_model"], r))

    def test_seed_covers_the_tier0_brief(self):
        r = rows(*t.SEED)
        for name in ("postgresql", "nova_ops", "memory_server", "gateway", "chat"):
            self.assertEqual(t.tier_for([name], r)["tier"], 0, name)
        for name in ("scheduler", "scheduler-core", "nova_notifier", "novahomekit", "home_watchdog"):
            self.assertEqual(t.tier_for([name], r)["tier"], 1, name)
        for name in ("plex", "journal_digest", "analytics_aggregate"):
            self.assertEqual(t.tier_for([name], r)["tier"], 2, name)
        names = [c for c, _ in t.SEED]
        self.assertEqual(len(names), len(set(names)))


class TestIntegration(unittest.TestCase):
    def test_prr_check_uses_this_module(self):
        import nova_prr
        self.assertIn("tier", nova_prr.CHECKS)
        self.assertTrue(nova_prr.check_tier(rows(("gateway", 0)), ["gateway"])[0])

    def test_restore_drill_reads_targets_from_here(self):
        import inspect
        import nova_restore_drill
        self.assertIn("tier_for", inspect.getsource(nova_restore_drill.tier_targets))


class TestFunctional(unittest.TestCase):
    def test_set_upserts_with_derived_targets(self):
        cur = _Cur()
        t.set_tier(cur, "self_model", 2, "organ")
        sql, params = cur.sql[0]
        self.assertIn("ON CONFLICT (component) DO UPDATE", sql)
        self.assertEqual(params[:4], ("self_model", 2, 259200, 604800))

    def test_fmt(self):
        self.assertEqual((t._fmt_s(900), t._fmt_s(3600), t._fmt_s(86400 * 7)), ("15m", "1h", "7d"))


class TestFrame(unittest.TestCase):
    def test_header_and_guard(self):
        self.assertIn("Written by Jordan Koch (via Claude)", SRC)
        self.assertIn("Little Mister, 2026-10-15", SRC)
        self.assertIn('if __name__ == "__main__":', SRC)

    def test_import_has_no_side_effects(self):
        r = subprocess.run([sys.executable, "-c", "import nova_service_tiers"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
