#!/usr/bin/env python3
"""Tests for nova_goal_check.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gc = _load("nova_goal_check_t", SCRIPTS / "nova_goal_check.py")
gc.notify = MagicMock()          # notification bus stubbed at load
gc.log = MagicMock()             # structured logger stubbed at load (no log file writes)
SRC = (SCRIPTS / "nova_goal_check.py").read_text()

PG_FUNCS = ("ensure_goals_schema", "ensure_rules_schema", "detect_activity_from_git", "promote_corrections",
            "get_stale_goals", "get_overdue_goals", "get_active_goals", "goal_summary", "get_active_rules")


def _run(**ret):
    """Run main() with every goal/rule DB helper mocked; returns (rc, mocks)."""
    defaults = {"promote_corrections": 0, "get_stale_goals": [], "get_overdue_goals": [],
                "get_active_goals": [], "goal_summary": {}, "get_active_rules": []}
    defaults.update(ret)
    mocks = {}
    with ExitStack() as st:
        for f in PG_FUNCS:
            v = defaults.get(f)
            kw = {"side_effect": v} if isinstance(v, Exception) else {"return_value": v}
            mocks[f] = st.enter_context(patch.object(gc, f, MagicMock(**kw)))
        rc = gc.main()
    return rc, mocks


class _Base(unittest.TestCase):
    def setUp(self):
        gc.notify.reset_mock(); gc.log.reset_mock()


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_no_raw_sql_here(self):
        # all DB access is delegated to nova_goals / nova_rules
        self.assertNotRegex(SRC, r"\b(SELECT|INSERT|UPDATE|DELETE)\b\s")
        self.assertNotIn("psycopg2", SRC)


class TestPerformance(_Base):
    def test_message_built_fast_for_10k_goals(self):
        many = [{"title": f"g{i}", "deadline": "2026-01-01", "days_idle": i} for i in range(10_000)]
        t0 = time.perf_counter()
        _run(get_stale_goals=many, get_overdue_goals=many, get_active_goals=many)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertIn("g9999", gc.notify.call_args[1]["body"])


class TestRetry(_Base):
    def test_db_failure_propagates_without_posting(self):
        # RETRY GAP: main()/ensure_schema — no retry; the scheduler's next run is the retry, nothing is posted
        with self.assertRaises(RuntimeError):
            _run(get_stale_goals=RuntimeError("pg down"))
        gc.notify.assert_not_called()


class TestUnit(_Base):
    def test_on_track_is_silent(self):
        rc, _ = _run(get_active_goals=[{}] * 4)
        self.assertEqual(rc, 0)
        gc.notify.assert_not_called()

    def test_too_many_active_goals_warns(self):
        _run(get_active_goals=[{}] * 5)
        self.assertIn("5 active goals", gc.notify.call_args[1]["body"])

    def test_title_strips_markdown(self):
        _run(get_overdue_goals=[{"title": "Ship", "deadline": "2026-01-01"}])
        title = gc.notify.call_args[0][0]
        self.assertTrue(title.startswith("Goal Check — "))
        self.assertNotIn("*", title)


class TestIntegration(_Base):
    def test_uses_shared_goal_and_rule_modules(self):
        self.assertIn("from nova_goals import", SRC)
        self.assertIn("from nova_rules import", SRC)
        import nova_goals, nova_rules
        # compare by origin, not identity: another test file may have reloaded nova_goals/nova_rules
        self.assertEqual((gc.get_stale_goals.__module__, gc.get_stale_goals.__qualname__), ("nova_goals", "get_stale_goals"))
        self.assertEqual((gc.promote_corrections.__module__, gc.promote_corrections.__qualname__), ("nova_rules", "promote_corrections"))

    def test_schema_git_and_promotion_run_in_order(self):
        _, m = _run()
        for f in ("ensure_goals_schema", "ensure_rules_schema", "detect_activity_from_git", "promote_corrections"):
            self.assertEqual(m[f].call_count, 1, f)


class TestFunctional(_Base):
    def test_golden_path_posts_overdue_stale_and_rules(self):
        rc, _ = _run(promote_corrections=2,
                     get_overdue_goals=[{"title": "Taxes", "deadline": "2026-04-15"}],
                     get_stale_goals=[{"title": "NMAPScanner v2", "days_idle": 12}],
                     get_active_rules=[1, 2, 3])
        self.assertEqual(rc, 0)
        args, kw = gc.notify.call_args
        self.assertIn("Taxes — was due 2026-04-15", kw["body"])
        self.assertIn("NMAPScanner v2 — 12d", kw["body"])
        self.assertIn("2 new rule(s) from corrections. 3 total", kw["body"])
        self.assertEqual((kw["level"], kw["dedup_key"]), ("warning", "goal-check"))


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_goal_check"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
