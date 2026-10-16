#!/usr/bin/env python3
"""Tests for nova_restore_drill.py pure functions: dump-name parsing, RPO/RTO math, row-count ratio
checks, TOC filtering, settle detection and the pass/fail verdict. No docker, NAS or Postgres.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_restore_drill.py
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_restore_drill as d   # noqa: E402

H = 3600


class TestParseDumpName(unittest.TestCase):
    def test_parses_db_and_pacific_time(self):
        db, ts = d.parse_dump_name("nova_ops_20261013_045521")
        self.assertEqual(db, "nova_ops")
        self.assertEqual((ts.year, ts.month, ts.day, ts.hour, ts.minute), (2026, 10, 13, 4, 55))
        self.assertEqual(ts.utcoffset().total_seconds(), -7 * H)          # PDT

    def test_db_with_underscores(self):
        self.assertEqual(d.parse_dump_name("nova_memories_20261013_020001")[0], "nova_memories")

    def test_rejects_legacy_and_junk(self):
        for n in ("nova_memories_20260421_114930.sql.gz", "nova_ops", "nova_ops_2026_x", "nova_ops_20261399_000000"):
            self.assertIsNone(d.parse_dump_name(n), n)


class TestRpoRto(unittest.TestCase):
    def test_rpo(self):
        self.assertEqual(d.rpo_seconds(1000.0, 400.0), 600.0)

    def test_rpo_clamps_clock_skew(self):
        self.assertEqual(d.rpo_seconds(100.0, 160.0), 0.0)

    def test_rpo_unknown(self):
        self.assertIsNone(d.rpo_seconds(100.0, None))

    def test_rto_full_estimate_scales_by_bytes(self):
        self.assertEqual(d.rto_full_estimate(600, 1_000, 4_000), 2400.0)

    def test_rto_full_estimate_full_restore_is_elapsed(self):
        self.assertEqual(d.rto_full_estimate(600, 4_000, 4_000), 600.0)

    def test_rto_full_estimate_missing(self):
        self.assertIsNone(d.rto_full_estimate(600, 0, 4_000))
        self.assertIsNone(d.rto_full_estimate(None, 1, 1))


class TestRatio(unittest.TestCase):
    def test_within_band(self):
        self.assertTrue(d.ratio_ok(950_000, 1_000_000))

    def test_live_grew_too_much(self):
        self.assertFalse(d.ratio_ok(700_000, 1_000_000))

    def test_restored_bigger_than_live_beyond_band(self):
        self.assertFalse(d.ratio_ok(1_200_000, 1_000_000))

    def test_small_tables_use_absolute_slack(self):
        self.assertTrue(d.ratio_ok(150, 186))
        self.assertTrue(d.ratio_ok(0, 0))

    def test_empty_restore_of_nonempty_table_fails_even_if_small(self):
        self.assertFalse(d.ratio_ok(0, 21))

    def test_unknown_counts_fail(self):
        self.assertFalse(d.ratio_ok(None, 10))
        self.assertFalse(d.ratio_ok(10, None))


TOC = [
    ";",
    "; Archive created at 2026-10-13 04:55:21 PDT",
    "8001; 1259 16400 TABLE public syslog_events kochj",
    "8721; 0 16400 TABLE DATA public syslog_events kochj",
    "8722; 0 16401 TABLE DATA public claude_memories kochj",
    "8723; 0 16402 TABLE DATA public scheduler_runs kochj",
    "9001; 1259 16500 INDEX public syslog_events_ts kochj",
]
SIZES = {"8721": 1_800_000_000, "8722": 50_000, "8723": 300_000_000}


class TestSelectToc(unittest.TestCase):
    def test_full_mode_keeps_everything(self):
        kept, skipped, bk, bt = d.select_toc(TOC, SIZES, None, set())
        self.assertEqual(kept, TOC)
        self.assertEqual(skipped, [])
        self.assertEqual(bk, bt)
        self.assertEqual(bt, sum(SIZES.values()))

    def test_skip_big_keeps_schema_and_key_tables(self):
        kept, skipped, bk, bt = d.select_toc(TOC, SIZES, 150_000_000, {"public.scheduler_runs"})
        self.assertEqual(skipped, ["public.syslog_events"])
        self.assertIn(TOC[2], kept)                 # table DDL kept
        self.assertIn(TOC[6], kept)                 # index kept
        self.assertNotIn(TOC[3], kept)              # bulk data skipped
        self.assertIn(TOC[5], kept)                 # key table kept despite size
        self.assertEqual(bk, 300_050_000)

    def test_data_sizes_by_toc_id(self):
        self.assertEqual(d.data_sizes_by_toc_id({"8721.dat.gz": 5, "toc.dat": 9, "8722.dat": 3}),
                         {"8721": 5, "8722": 3})


class TestSettled(unittest.TestCase):
    def test_settle(self):
        self.assertTrue(d.is_settled(0, d.SETTLE_S))
        self.assertFalse(d.is_settled(10, d.SETTLE_S))
        self.assertFalse(d.is_settled(None, 1e12))


GOOD = {"public.agent_docs": {"exists": True, "ratio_ok": True, "restored": 86, "live": 86}}


class TestVerdict(unittest.TestCase):
    def test_pass(self):
        ok, reasons = d.verdict(True, GOOD, 14 * H, 1800)
        self.assertTrue(ok, reasons)
        self.assertEqual(reasons, [])

    def test_restore_failure(self):
        ok, reasons = d.verdict(False, GOOD, 14 * H, 1800)
        self.assertFalse(ok)
        self.assertIn("pg_restore failed", reasons)

    def test_rpo_over_26h(self):
        ok, reasons = d.verdict(True, GOOD, 27 * H, 1800)
        self.assertFalse(ok)
        self.assertTrue(any(r.startswith("RPO 27.0h") for r in reasons))

    def test_rpo_unknown_fails(self):
        self.assertFalse(d.verdict(True, GOOD, None, 1800)[0])

    def test_rto_over_budget(self):
        ok, reasons = d.verdict(True, GOOD, H, d.RTO_MAX_S + 1)
        self.assertFalse(ok)
        self.assertTrue(any("RTO" in r for r in reasons))

    def test_missing_and_drifted_tables(self):
        checks = {"a.x": {"exists": False}, "a.y": {"exists": True, "ratio_ok": False, "restored": 1, "live": 900}}
        ok, reasons = d.verdict(True, checks, H, 60)
        self.assertFalse(ok)
        self.assertIn("a.x missing from restore", reasons)
        self.assertTrue(any(r.startswith("a.y row count off") for r in reasons))

    def test_key_tables_cover_the_brief(self):
        names = {t for t, _ in d.KEY_TABLES}
        for t in ("public.claude_memories", "public.agent_docs", "public.scheduler_runs",
                  "telemetry.events", "public.incidents"):
            self.assertIn(t, names)
        self.assertTrue(set(d.FRESHNESS_TABLES) <= names)



class TestMemoriesDataOnly(unittest.TestCase):
    """#34 (Little Mister, 2026-10-15): the retired monthly test's data-only restore lives here now."""
    STREAM = ["SET statement_timeout = 0;\n", "COPY public.memories (id, text, embedding) FROM stdin;\n",
              "a\tx\\nmultiline-escaped\t[1,2]\n", "b\ty\t[3,4]\n", "\\.\n",
              "COPY public.memories_archive (id) FROM stdin;\n", "z\n", "\\.\n"]

    def test_counts_only_memories_rows(self):
        self.assertEqual(d.count_copy_rows(self.STREAM), 2)

    def test_empty_stream_is_zero(self):
        self.assertEqual(d.count_copy_rows([]), 0)

    def test_awk_twin_matches_python_counter(self):
        import shutil
        import subprocess
        if not shutil.which("awk"):
            self.skipTest("no awk")
        r = subprocess.run(["awk", d.COPY_COUNT_AWK], input="".join(self.STREAM),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.stdout.strip(), "2")

    def test_data_count_parses_and_fails_closed(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        with patch.object(d, "docker", return_value=SimpleNamespace(returncode=0, stdout="1650000\n", stderr="")):
            self.assertEqual(d.memories_data_count("nova_memories_x"), (1650000, None))
        with patch.object(d, "docker", return_value=SimpleNamespace(returncode=1, stdout="", stderr="boom")) as m:
            self.assertEqual(d.memories_data_count("nova_memories_x"), (None, "boom"))
        args = m.call_args[0]
        self.assertIn("--data-only", " ".join(args))
        self.assertIn(":ro", " ".join(args))          # dump mounted read-only

    def test_band_is_the_old_ten_percent(self):
        self.assertTrue(d.ratio_ok(1_000_000, 1_090_000, d.MEM_RATIO_LO, d.MEM_RATIO_HI))
        self.assertFalse(d.ratio_ok(800_000, 1_000_000, d.MEM_RATIO_LO, d.MEM_RATIO_HI))


class TestTierObjectives(unittest.TestCase):
    """#35: measured RTA/RPA against Tier-0 targets (RTO 1h, RPO 15m)."""

    def test_dump_only_misses_tier0_rpo(self):
        o = d.tier_objectives(900, 900, 13 * H, None, 3600, 900)
        self.assertEqual(o["rpa_source"], "dump")
        self.assertTrue(o["rto_met"])
        self.assertFalse(o["rpo_met"])

    def test_wal_chain_meets_rpo(self):
        o = d.tier_objectives(None, 1200, 13 * H, 120, 3600, 900)
        self.assertEqual((o["rpa_s"], o["rpa_source"], o["rpo_met"]), (120, "wal", True))
        self.assertEqual(o["rta_s"], 1200)     # no full estimate -> elapsed

    def test_full_estimate_beats_partial_elapsed(self):
        o = d.tier_objectives(7200, 600, 60, None, 3600, 900)
        self.assertEqual(o["rta_s"], 7200)
        self.assertFalse(o["rto_met"])

    def test_unknowns_are_none_not_false(self):
        o = d.tier_objectives(None, None, None, None, 3600, 900)
        self.assertIsNone(o["rto_met"]); self.assertIsNone(o["rpo_met"])

    def test_schema_adds_columns_lock_safely(self):
        self.assertIn("ADD COLUMN IF NOT EXISTS rpo_met", d.SCHEMA_SQL)
        import inspect
        self.assertIn("safe_ddl", inspect.getsource(d.record))


if __name__ == "__main__":
    unittest.main()
