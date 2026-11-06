#!/usr/bin/env python3
"""7-category tests for the 2026-11-06 military seasoning in nova_lexicon.py: the MILITARY shelf and
military_lines() (verbatim sentences from the military_doctrine* manuals, topic-ranked). PG is mocked.
Written by Jordan Koch (via Claude).

Run: NOVA_TEST_QUIET=1 python3 -m pytest -q tests/test_nova_lexicon_military_7cat.py
"""
import io
import sys
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_lexicon as lx  # noqa: E402

ROWS = [
    "[AFDD 6-0] RELIABILITY AND REDUNDANCY Redundant C2 systems provide the ability for alternative C2 systems "
    "to continue operations in the event of failure or damage to the primary system.",
    "[AFDD 2-8] Redundant C2 systems provide the ability for alternative C2 systems to continue operations in "
    "the event of failure or damage to the primary system.",
    "[FM 21-10 Field Hygiene] (1) Using an approved residual insecticide, spray the pit contents.",
    "no title prefix, a news item about backup failure that should never be quoted as a manual line.",
    "[TM 9-1904] The ideal agent should be stable in sto-  rage and also stable against the shock of explosion.",
]


def _conn(rows):
    cur = MagicMock(); cur.fetchall.return_value = [(r,) for r in rows]
    conn = MagicMock(); conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


class TestSecurity(unittest.TestCase):
    def test_topic_cannot_inject_tsquery_or_sql(self):
        conn, cur = _conn([])
        lx.military_lines("backup'); SELECT pg_sleep(9); -- | !& <-> :*", conn=conn)
        sql, params = cur.execute.call_args_list[-1].args
        self.assertNotIn("pg_sleep", sql)
        self.assertRegex(params[0], r"^[a-z]+( \| [a-z]+)*$")


class TestPerformance(unittest.TestCase):
    def test_statement_timeout_and_bounded_words(self):
        conn, cur = _conn([])
        lx.military_lines(" ".join(f"word{chr(97 + i % 26)}alpha" * 2 for i in range(200)), conn=conn)
        self.assertIn("statement_timeout", cur.execute.call_args_list[0].args[0])
        self.assertLessEqual(cur.execute.call_args_list[-1].args[1][0].count("|"), 11)

    def test_pick_fast(self):
        t = time.perf_counter(); lx._pick_military(ROWS * 200, ["backup", "failure"], 3)
        self.assertLess(time.perf_counter() - t, 1.0)


class TestRetry(unittest.TestCase):
    def test_unreachable_db_returns_empty_and_logs(self):
        import psycopg2
        err = io.StringIO()
        with patch.object(psycopg2, "connect", side_effect=psycopg2.OperationalError("down")), redirect_stderr(err):
            self.assertEqual(lx.military_lines("backup"), [])
        self.assertIn("military_lines unavailable", err.getvalue())


class TestUnit(unittest.TestCase):
    def test_heading_stripped_duplicates_and_debris_dropped(self):
        out = lx._pick_military(ROWS, ["redundant", "failure", "storage"], 5)
        sents = [s for _t, s in out]
        self.assertTrue(sents[0].startswith("Redundant C2"))
        self.assertEqual(len(sents), len(set(sents)))            # same sentence from AFDD 2-8 not repeated
        self.assertFalse(any("(1)" in s for s in sents))
        self.assertFalse(any("news item" in s for s in sents))     # no [title] prefix -> not a manual

    def test_ocr_hyphen_joined(self):
        out = lx._pick_military(ROWS[-1:], ["storage"], 1)
        self.assertIn("storage", out[0][1])

    def test_empty_topic_uses_fallback(self):
        conn, cur = _conn([])
        lx.military_lines("", conn=conn)
        self.assertIn("leadership", cur.execute.call_args_list[-1].args[1][0])


class TestIntegration(unittest.TestCase):
    def test_seasoning_carries_military_shelf_and_library(self):
        with patch.object(lx, "ferengi_rule", return_value=(1, "r")), \
             patch.object(lx, "military_lines", return_value=[("AFDD 6-0", "Redundancy matters here.")]):
            s = lx.seasoning("burbank-ops", "backup failure")
        self.assertIn("THE MILITARY SHELF", s)
        self.assertIn('[AFDD 6-0]: "Redundancy matters here."', s)

    def test_block_without_library_when_db_down(self):
        with patch.object(lx, "military_lines", return_value=[]):
            b = lx.military_block("x")
        self.assertIn("THE MILITARY SHELF", b)
        self.assertNotIn("MILITARY LIBRARY", b)


class TestFunctional(unittest.TestCase):
    def test_emergency_and_unknown_sections_stay_unseasoned(self):
        with patch.object(lx, "military_lines", side_effect=AssertionError("must not query")):
            for sec in ("", "breaking", "wat"):
                self.assertEqual(lx.seasoning(sec, "brush fire evacuation"), "")


class TestFrame(unittest.TestCase):
    def test_shelf_rules_forbid_invention(self):
        self.assertIn("never invent a manual or a quote", lx.MILITARY)
        self.assertIn("Semper Supra", lx.MILITARY)


if __name__ == "__main__":
    unittest.main()
