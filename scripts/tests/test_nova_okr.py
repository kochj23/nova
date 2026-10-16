#!/usr/bin/env python3
"""Tests for nova_okr.py (plan #58): KR grading, read-only metric contract, seed idempotence."""
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import nova_okr as o  # noqa: E402


class TestGrade(unittest.TestCase):
    def test_met(self):
        self.assertEqual(o.grade("<", 5, 82, 4), 1.0)
        self.assertEqual(o.grade(">", 0.5, 0.42, 0.6), 1.0)
        self.assertEqual(o.grade(">=", 10, 3, 10), 1.0)

    def test_progress_is_linear_and_below_one(self):
        self.assertAlmostEqual(o.grade("<", 5, 85, 45), 0.5)
        self.assertEqual(o.grade("<", 5, 85, 5.0001), 0.99)
        self.assertEqual(o.grade(">=", 10, 3, 1), 0.0)       # regressed -> clamped to 0

    def test_none_and_degenerate(self):
        self.assertIsNone(o.grade("<", 5, 82, None))
        self.assertEqual(o.grade("<", 5, None, 9), 0.0)


class TestContract(unittest.TestCase):
    def test_every_kr_metric_is_a_select(self):
        for kr in o.KRS:
            self.assertTrue(kr["metric_sql"].lstrip().upper().startswith("SELECT"), kr["kr_key"])
            self.assertIn(kr["comparator"], ("<", "<=", ">", ">="))

    def test_objective_and_three_krs(self):
        self.assertEqual(o.OBJECTIVE["title"], "Jordan is interrupted only when it matters")
        self.assertEqual({k["kr_key"] for k in o.KRS}, {"pages_per_day", "page_precision", "top10_recurrence_closed"})

    def test_measure_skips_non_select_and_survives_a_broken_metric(self):
        cur = MagicMock()
        rows = [(1, "g", "evil", "DELETE FROM goals", "<", 5, None),
                (2, "g", "broken", "SELECT x", "<", 5, None),
                (3, "g", "ok", "SELECT 3", "<", 5, 9)]
        executed = []

        def ex(sql, params=None):
            executed.append(sql)
            if sql == "SELECT x":
                raise RuntimeError("no column x")
        cur.execute.side_effect = ex
        cur.fetchall.return_value = rows
        cur.fetchone.return_value = (3,)
        out = o.measure(cur)
        self.assertNotIn("DELETE FROM goals", executed)
        self.assertIn("ROLLBACK TO SAVEPOINT kr", executed)
        self.assertEqual([r[0] for r in out], ["broken", "ok"])
        self.assertEqual(out[1][2], 1.0)

    def test_seed_is_idempotent_sql(self):
        cur = MagicMock()
        cur.fetchall.return_value = []
        with patch.object(o.nova_pg_schema, "safe_ddl"):
            o.seed(cur)
        inserts = [c.args[0] for c in cur.execute.call_args_list if "INSERT INTO goal" in c.args[0] and "goal_log" not in c.args[0]]
        self.assertTrue(inserts and all("ON CONFLICT" in s for s in inserts))


if __name__ == "__main__":
    unittest.main()
