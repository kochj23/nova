#!/usr/bin/env python3
"""Tests for nova_evitable_conflict.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import json
import math
import os
import random
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import nova_evitable_conflict as V  # noqa: E402

SRC = (SCRIPTS / "nova_evitable_conflict.py").read_text()


class FakeCur:
    """Routes SQL by keyword to canned rows; records every statement."""

    def __init__(self, routes=None, boom=False):
        self.routes, self.boom, self.sql, self._last = routes or {}, boom, [], []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.boom:
            raise RuntimeError("pg down")
        self._last = next((list(v) for k, v in self.routes.items() if k in sql), [])

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


def fake_conn(cur):
    c = mock.MagicMock()
    c.cursor.return_value = cur
    return c


# own components downgraded far more often, only while he is absent -> Dave's Dance
LEANING = {"FROM alert_triage_log t": [(True, False, 200, 180), (False, False, 400, 200),
                                       (True, True, 100, 50), (False, True, 100, 50)],
           "FROM action_audit": [("2026-10-08", 1000, 900)],
           "SELECT (SELECT count(*)": [(1600, 10, 4, 1)],
           "RETURNING id": [(77,)]}


class TestSecurity(unittest.TestCase):
    def test_no_secrets_no_fstring_sql_with_values(self):
        self.assertNotRegex(SRC, r'execute\(\s*f"')
        self.assertNotRegex(SRC, r"_q\(cur,\s*f\"")
        self.assertNotRegex(SRC, r"(?i)(password|token|secret)\s*=\s*['\"][^'\"]{6,}")

    def test_no_personal_paths_ids_or_ips(self):
        self.assertNotIn(str(Path.home()), SRC)
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotRegex(SRC, r"\bU0[0-9A-Z]{7,}\b")       # Slack ids come from nova_contact_sense

    def test_coverage_sql_only_constant_tables(self):
        for t in V.COVERAGE_LOGS:
            self.assertRegex(t, r"^[a-z_]+$")
        self.assertIn("%(s)s", V.COVERAGE_SQL)

    def test_hostile_category_stays_a_parameter(self):
        cur = FakeCur()
        evil = ["x'); DROP TABLE alert_triage_log; --"]
        V.cells(cur, "a", "b", evil)
        self.assertNotIn("DROP", cur.sql[0][0])
        self.assertIn(evil, cur.sql[0][1])


class TestPerformance(unittest.TestCase):
    def test_tests_on_10k_cells(self):
        rnd = random.Random(1)
        t = time.monotonic()
        for _ in range(10000):
            V.tests({(o, e): (rnd.randint(0, 500), 0) for o in (True, False) for e in (True, False)})
        self.assertLess(time.monotonic() - t, 3.0)


class TestRetry(unittest.TestCase):
    def test_connect_retries_with_backoff(self):
        n = {"c": 0}

        def flaky(*a, **k):
            n["c"] += 1
            if n["c"] < 3:
                raise OSError("refused")
            return mock.MagicMock()
        with mock.patch("psycopg2.connect", side_effect=flaky):
            V.W.connect(_sleep=lambda s: None)
        self.assertEqual(n["c"], 3)

    # RETRY GAP: cells / coverage — single reads; a failure FAILS OPEN to "insufficient", no exception.
    def test_query_failure_degrades_to_insufficient(self):
        cur = FakeCur(boom=True)
        with mock.patch("builtins.print"):
            res = V.tests(V.cells(cur, "a", "b", ["x"]))
            cov = V.coverage(cur, "a", "b")
        self.assertEqual({r["verdict"] for r in res.values()}, {"insufficient"})
        self.assertFalse(cov["adequate"])


class TestUnit(unittest.TestCase):
    def test_chi2_matches_hand_computation(self):
        r = V.chi2_2x2(30, 50, 20, 50)
        self.assertAlmostEqual(r["chi2"], 4.0, places=3)        # textbook 2x2
        self.assertAlmostEqual(r["p"], math.erfc(math.sqrt(2.0)), places=4)
        self.assertEqual(r["verdict"], "no_direction")          # p=0.0455 > alpha 0.01

    def test_edges(self):
        self.assertEqual(V.chi2_2x2(0, 0, 0, 0)["verdict"], "insufficient")
        self.assertEqual(V.chi2_2x2(10, 100, 90, 100)["verdict"], "away_from_self")
        self.assertEqual(V.chi2_2x2(100, 100, 0, 100)["verdict"], "toward_self")   # OR stays finite

    def test_dave_dance(self):
        tw = {"verdict": "toward_self"}
        nd = {"verdict": "no_direction"}
        self.assertTrue(V.dave_dance({"absent": tw, "engaged": nd}))
        self.assertIsNone(V.dave_dance({"absent": tw, "engaged": tw}))
        self.assertIsNone(V.dave_dance({"absent": nd, "engaged": nd}))

    def test_selftest(self):
        with mock.patch("builtins.print"):
            self.assertEqual(V.selftest(), 0)


    def test_wrapper_delegates_to_charles(self):
        import nova_charles
        with mock.patch.object(nova_charles, "main", return_value=0) as cm, mock.patch("builtins.print") as pr:
            self.assertEqual(V.main(["--run", "--dry-run"]), 0)
            self.assertEqual(V.main(["--show"]), 0)
        self.assertEqual(cm.call_args_list[0].args[0], ["--triage"] + ['--days', '30'] + ["--dry-run"])
        self.assertEqual(cm.call_args_list[1].args[0], ["--triage"] + ['--days', '30'] + ["--show"])
        self.assertIn("merged into nova_charles.py --triage on 2026-10-09", pr.call_args_list[0].args[0])


class TestIntegration(unittest.TestCase):
    def test_shared_helpers_imported(self):
        self.assertIn("import nova_watch_common as W", SRC)
        self.assertIn("from nova_contact_sense import JORDAN_SLACK, MACHINE_CHANNELS", SRC)

    def test_self_categories_from_service_config(self):
        cur = FakeCur({"service_config": [(json.dumps(["gateway"]),)]})
        self.assertEqual(V.self_categories(cur), ["gateway"])
        self.assertIn("evitable_conflict", cur.sql[0][0])
        self.assertEqual(V.self_categories(FakeCur()), list(V.SELF_CATEGORIES))

    def test_cells_then_tests_shape(self):
        res = V.tests(V.cells(FakeCur(LEANING), "a", "b", ["x"]))
        self.assertEqual(set(res), {"engaged", "absent", "all"})
        self.assertEqual((res["all"]["n_self"], res["all"]["k_self"]), (300, 230))

    def test_coverage_reads_action_audit(self):
        cov = V.coverage(FakeCur(LEANING), "a", "b")
        self.assertEqual(cov["action_audit"]["share_logged"], 0.9)
        self.assertEqual(cov["alert_triage_log"], 1600)
        self.assertTrue(cov["adequate"])

    def test_schema(self):
        for c in ("evitable_conflict_results", "stratum text", "p real", "coverage jsonb", "queue_id int"):
            self.assertIn(c, V.SCHEMA)


    def test_run_is_reached_through_charles(self):
        with mock.patch.object(V, "run", return_value={}) as r, mock.patch("builtins.print"):
            V.main(["--run"])
        r.assert_called_once()                      # wrapper -> nova_charles --triage -> this module's run()


class TestFunctional(unittest.TestCase):
    def _run(self, dry, routes):
        cur = FakeCur(routes)
        with mock.patch.object(V.W, "connect", return_value=fake_conn(cur)), mock.patch("builtins.print"):
            out = V.run(30, dry=dry)
        return cur, out

    def test_run_records_and_asks_once_per_lean(self):
        cur, out = self._run(False, LEANING)
        self.assertTrue(out["dance"])
        self.assertTrue(any("CREATE TABLE IF NOT EXISTS evitable_conflict_results" in s for s, _ in cur.sql))
        self.assertEqual(sum("INSERT INTO evitable_conflict_results" in s for s, _ in cur.sql), 3)
        q = [p for s, p in cur.sql if "INSERT INTO claude_queue" in s]
        self.assertEqual(len(q), 2)                     # absent (Dave's Dance) and all
        self.assertTrue(any("absent" in p[1] for p in q))

    def test_dry_run_writes_nothing(self):
        cur, out = self._run(True, LEANING)
        self.assertEqual(out["results"]["absent"]["verdict"], "toward_self")
        self.assertFalse(any(k in s for s, _ in cur.sql for k in ("CREATE", "INSERT", "UPDATE")))

    def test_no_data_files_nothing(self):
        cur, out = self._run(False, {})
        self.assertFalse(any("INSERT INTO claude_queue" in s for s, _ in cur.sql))
        self.assertFalse(out["coverage"]["adequate"])


class TestFrame(unittest.TestCase):
    def test_selftest_cli(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_evitable_conflict.py"), "--selftest"],
                           capture_output=True, text=True, timeout=30, env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_evitable_conflict.py"), "--help"],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0)
        self.assertIn("--dry-run", r.stdout)

    def test_import_does_not_run(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
