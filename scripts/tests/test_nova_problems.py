#!/usr/bin/env python3
"""Tests for nova_problems.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Little Mister, 2026-10-15 (#38). PG is mocked throughout.
Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_problems.py"
spec = importlib.util.spec_from_file_location("problems_under_test", SCRIPT)
pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pr)
pr.log = lambda m: None
SRC = SCRIPT.read_text()
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


class _Cur:
    """Answers by SQL substring; records every statement."""
    def __init__(self, answers=None, fail_on=None):
        self.answers, self.fail_on, self.sql = answers or {}, fail_on, []
        self._rows = []

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("relation does not exist")
        self._rows = []
        for k, v in self.answers.items():
            if k in sql:
                self._rows = list(v(sql, params) if callable(v) else v)
                break

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


def _p(**kw):
    d = {"id": 1, "recurrence_key": "h:probe", "state": "candidate", "fix_ref": None, "fix_at": None,
         "last_seen": NOW - timedelta(hours=2), "state_changed_at": NOW - timedelta(days=1),
         "first_seen": NOW - timedelta(days=30), "suppress_paging": True}
    d.update(kw)
    return d


class TestSecurity(unittest.TestCase):
    def test_no_credentials_or_hardcoded_hosts(self):
        self.assertIsNone(re.search(r"(password|token|secret)\s*=\s*['\"]\w", SRC, re.I))
        self.assertIsNone(re.search(r"\b192\.168\.\d+\.\d+\b", SRC))
        self.assertNotIn("/Users/", SRC)

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\'][^"\']*%s?\{', SRC))
        for s, _ in [(x, None) for x in re.findall(r'execute\(f"([^"]+)"', SRC)]:
            self.assertNotIn("{key", s)

    def test_a_real_page_is_never_folded_into_a_problem(self):
        self.assertFalse(pr.link_allowed({"state": "known_error", "suppress_paging": True}, "page"))
        self.assertFalse(pr.link_allowed({"state": "candidate", "suppress_paging": True}, "ticket"))
        self.assertFalse(pr.link_allowed({"state": "known_error", "suppress_paging": False}, "ticket"))
        self.assertTrue(pr.link_allowed({"state": "fix_in_change", "suppress_paging": True}, "ticket"))


class TestPerformance(unittest.TestCase):
    def test_pure_rules_fast_on_10k(self):
        t0 = time.perf_counter()
        for i in range(10000):
            pr.next_state(_p(), {"last_seen": NOW, "workaround": None, "fix": None}, NOW)
            pr.mentions(f"fix for host{i}:probe", "h:probe", 5)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_known_error_lookup_fails_open_inside_a_savepoint(self):
        # RETRY GAP: known_error_for — one read; a missing table returns None and rolls back to the savepoint
        cur = _Cur(fail_on="FROM problems")
        self.assertIsNone(pr.known_error_for(cur, "h", "probe"))
        self.assertTrue(cur.ran("ROLLBACK TO SAVEPOINT problems_ke"))

    def test_link_event_never_raises(self):
        self.assertIsNone(pr.link_event(_Cur(fail_on="SAVEPOINT"), {"id": 1, "category": "probe"}, "h"))

    def test_git_unavailable_is_empty(self):
        with patch.dict(sys.modules, {"nova_change_class": None}):
            self.assertEqual(pr.git_commits(3), [])


class TestUnit(unittest.TestCase):
    def test_split_key_handles_colons_in_hosts(self):
        self.assertEqual(pr.split_key("https://x:5001:storage"), ("https://x:5001", "storage"))
        self.assertEqual(pr.split_key("h:probe"), ("h", "probe"))

    def test_workaround_from_fresh_resolution_runbook_or_self_healing(self):
        self.assertEqual(pr.derive_workaround({"resolutions": ["", "repointed to .77"]})[1], "incident_resolution")
        self.assertEqual(pr.derive_workaround({"remediations_ok": ["restart_x"]})[1], "runbook")
        heal = {"n": 6, "auto_closed": 6, "mttr_minutes": [30, 40, 35, 31, 33, 36]}
        self.assertEqual(pr.derive_workaround(heal)[1], "self_healing")
        self.assertIsNone(pr.derive_workaround(dict(heal, mttr_minutes=[500] * 6)))
        self.assertIsNone(pr.derive_workaround(dict(heal, auto_closed=5)))
        self.assertIsNone(pr.derive_workaround({"n": 2}))

    def test_mentions(self):
        self.assertTrue(pr.mentions("fix: 192.168.1.6:scheduler storm", "192.168.1.6:scheduler"))
        self.assertTrue(pr.mentions("closes problem #12", "x:y", 12))
        self.assertFalse(pr.mentions("problem #120", "x:y", 12))
        self.assertFalse(pr.mentions("", "x:y", 1))

    def test_lifecycle_steps(self):
        f = {"last_seen": NOW - timedelta(hours=1), "workaround": ("self heals", "self_healing"), "fix": None}
        self.assertEqual(pr.next_state(_p(), f, NOW)[0], "known_error")
        fix = {"ref": "commit abc", "at": NOW - timedelta(hours=3)}
        st = pr.next_state(_p(state="known_error"), dict(f, fix=fix), NOW)
        self.assertEqual((st[0], st[2]["fix_ref"]), ("fix_in_change", "commit abc"))
        quiet = {"last_seen": NOW - timedelta(days=pr.CLOSE_QUIET_DAYS + 1)}
        self.assertEqual(pr.next_state(_p(state="fix_in_change"), quiet, NOW)[0], "closed")
        self.assertIsNone(pr.next_state(_p(), {"last_seen": NOW, "workaround": None, "fix": None}, NOW))

    def test_fix_that_did_not_hold_and_reopen_after_close(self):
        p = _p(state="fix_in_change", fix_ref="commit abc", fix_at=NOW - timedelta(days=3))
        st = pr.next_state(p, {"last_seen": NOW - timedelta(hours=1)}, NOW)
        self.assertEqual(st[0], "known_error")
        self.assertIn("did not hold", st[1])
        c = _p(state="closed", state_changed_at=NOW - timedelta(days=2), fix_ref="commit abc")
        st = pr.next_state(c, {"last_seen": NOW - timedelta(hours=1)}, NOW)
        self.assertEqual(st[0], "candidate")
        self.assertIn("did not hold", st[1])
        self.assertIsNone(pr.next_state(_p(state="closed", state_changed_at=NOW),
                                         {"last_seen": NOW - timedelta(days=20)}, NOW))


class TestIntegration(unittest.TestCase):
    def test_link_event_folds_into_the_anchor_incident(self):
        cur = _Cur({"FROM problems": [(4, "known_error", 900, True, "self heals")]})
        ev = {"id": 55, "title": "Crash Storm — studio", "level": "warning", "category": "crash_storm", "meta": None}
        out = pr.link_event(cur, ev, "studio")
        self.assertEqual(out, {"action": "attached", "incident_id": 900, "role": "problem", "suppress": True,
                               "problem_id": 4})
        self.assertEqual(cur.ran("UPDATE telemetry.events")[0][1], (900, 55))
        self.assertEqual(cur.ran("WHERE recurrence_key=%s")[0][1], ("studio:crash_storm",))

    def test_tier0_event_of_a_known_error_still_opens_an_incident(self):
        cur = _Cur({"FROM problems": [(4, "known_error", 900, True, "w")]})
        ev = {"id": 56, "title": "Keystone DOWN: Memory server", "level": "critical", "category": "core-liveness"}
        self.assertIsNone(pr.link_event(cur, ev, "192.168.1.2"))

    def test_correlator_calls_the_problem_hook_before_opening(self):
        csrc = (SCRIPTS / "nova_correlator.py").read_text()
        self.assertLess(csrc.index("nova_problems.link_event"), csrc.index("INSERT INTO telemetry.incidents"))

    def test_find_fix_from_executed_proposal_then_commit(self):
        p = _p()
        cur = _Cur({"FROM coagency_proposals": [(31, NOW, "repoint h:probe at the new box")]})
        self.assertEqual(pr.find_fix(cur, p, [])["ref"], "coagency_proposal #31")
        commits = [{"sha": "abcdef1234567", "at": NOW, "subject": "fix problem #1: cache", "files": []}]
        self.assertEqual(pr.find_fix(_Cur(), p, commits)["ref"], "commit abcdef1234")


class TestFunctional(unittest.TestCase):
    def test_upsert_skips_self_referential_keys_and_logs_new(self):
        rows = [("h:probe", 5, NOW, NOW, 9, "PROBE FAIL"), ("h:incident_recurring", 9, NOW, NOW, 3, "x")]
        cur = _Cur({"GROUP BY 1 HAVING": rows, "RETURNING (xmax = 0)": [(True,)], "SELECT id FROM problems": [(1,)]})
        self.assertEqual(pr.upsert_candidates(cur), 1)
        inserts = cur.ran("INSERT INTO problems (")
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0][1][0], "h:probe")
        self.assertTrue(cur.ran("INSERT INTO problem_log"))

    def test_transition_writes_the_log_with_reason(self):
        cur = _Cur()
        pr._transition(cur, 3, "candidate", "known_error", "workaround known (self_healing)",
                       {"workaround": "w", "workaround_source": "self_healing"})
        upd = cur.ran("UPDATE problems SET state=%s")[0]
        self.assertEqual(upd[1][0], "known_error")
        self.assertEqual(cur.ran("INSERT INTO problem_log")[0][1][1:4],
                         ("candidate", "known_error", "workaround known (self_healing)"))

    def test_nova_sets_known_error_and_fix_herself(self):
        cur = _Cur({"SELECT id, state FROM problems": [(2, "candidate")]})
        with patch.object(pr, "ensure_schema", lambda c: None):
            self.assertTrue(pr.set_known_error(cur, "h:probe", "restart the prober"))
            self.assertTrue(pr.set_fix(cur, "h:probe", "commit 05f4620"))
            self.assertFalse(pr.set_fix(cur, "h:probe", "  "))
        self.assertEqual(len(cur.ran("INSERT INTO problem_log")), 2)


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, NOVA_TEST_QUIET="1"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--known-error", r.stdout)

    def test_import_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":\n    sys.exit(main())', SRC)
        self.assertEqual(pr.STATES, ("candidate", "known_error", "fix_in_change", "closed"))


if __name__ == "__main__":
    unittest.main()
