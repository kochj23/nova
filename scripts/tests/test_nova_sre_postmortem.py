#!/usr/bin/env python3
"""Tests for nova_sre_postmortem.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_sre_postmortem.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


pm = _load("sre_postmortem_under_test", SCRIPT)
pm.log = lambda m: None
SRC = SCRIPT.read_text()
T0 = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)


def _inc(**kw):
    d = {"id": 7, "opened_at": T0, "resolved_at": T0, "acked_at": None, "severity": "critical", "host": "h",
         "title": "x down", "summary": "disk full", "member_count": 3, "recurrence_key": "h:probe", "mtta_s": None,
         "mttr_s": 600, "resolution": None, "root_cat": "probe", "root_title": "PROBE FAIL x", "root_body": "b",
         "first_source": "nova_prober.py", "first_ts": T0, "first_level": "critical", "acked_by": "SomePerson"}
    d.update(kw)
    return d


class _Cur:
    def __init__(self, answers=None):
        self.answers = answers or {}
        self.sql = []
        self._rows = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._rows = []
        for k, v in self.answers.items():
            if k in sql:
                self._rows = list(v(sql, params) if callable(v) else v)
                break

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class TestSecurity(unittest.TestCase):
    def test_no_credentials(self):
        self.assertIsNone(re.search(r"(password|token|secret)\s*=\s*['\"]\w", SRC, re.I))

    def test_sql_is_parameterized(self):
        self.assertIsNone(re.search(r'execute\(\s*f["\']', SRC))

    def test_llm_is_local_only(self):
        self.assertTrue(pm.OLLAMA_URL.startswith("http://127.0.0.1"))


class TestPerformance(unittest.TestCase):
    def test_render_big_timeline_is_bounded(self):
        inc = _inc()
        ev = [{"ts": T0, "level": "warning", "source": "s", "title": f"e{i}", "channel": None} for i in range(10000)]
        t = time.time()
        doc = pm.render(inc, "critical", [inc], ev, pm.specific_actions(inc, {}), pm.lessons(inc, 1, [], 0), 1, [], 0)
        self.assertLess(time.time() - t, 2.0)
        self.assertLess(doc.count("\n- "), pm.TIMELINE_MAX + 40)


class TestRetry(unittest.TestCase):
    def test_llm_narrative_fails_open(self):
        # RETRY GAP: narrative_llm — one attempt by design; it must fail open, never raise
        def boom(*a, **k):
            raise OSError("ollama down")
        self.assertIsNone(pm.narrative_llm("doc", _urlopen=boom))


class TestUnit(unittest.TestCase):
    def test_selftest(self):
        self.assertEqual(pm.selftest(), 0)

    def test_trigger_reason(self):
        self.assertEqual(pm.trigger_reason("critical", "probe", 0, 3), "critical")
        self.assertEqual(pm.trigger_reason("warning", "probe", 3, 3), "recurring")
        self.assertEqual(pm.trigger_reason("critical", "probe", 5, 3), "critical+recurring")
        self.assertIsNone(pm.trigger_reason("critical", "incident_escalation", 5, 3))
        self.assertIsNone(pm.trigger_reason("warning", None, 0, 3))

    def test_idempotency_key(self):
        self.assertEqual(pm.pm_key(7, "critical", "h:probe", T0), "inc:7")
        self.assertEqual(pm.pm_key(7, "recurring", "h:probe", T0), "rk:h:probe:2026-W42")
        self.assertEqual(pm.pm_key(8, "critical+recurring", "h:probe", T0), "rk:h:probe:2026-W42")

    def test_template_sections_and_blameless(self):
        inc = _inc(acked_at=T0, mtta_s=120)
        acts = pm.specific_actions(inc, {"failed_runbooks": ["restart_x"]})
        doc = pm.render(inc, "critical+recurring", [inc], [], acts, pm.lessons(inc, 4, [], 5), 4, [], 5,
                        extra={"factors": ["f1"], "budget": None, "detect_s": None, "halved": None,
                               "near_miss": False, "due_at": pm.due_at(T0, "critical"), "problem": None})
        for h in ("## Summary", "## Impact", "## Root causes", "## Trigger", "## Detection", "## Contributing factors",
                  "## Resolution", "## Action items", "### What went well", "### What went wrong",
                  "### Where we got lucky", "## Timeline", "**Due:** 2026-10-16"):
            self.assertIn(h, doc)
        self.assertNotIn("SomePerson", doc)
        self.assertEqual([a["kind"] for a in acts], ["mitigate"])
        self.assertIn("restart_x", acts[0]["description"])

    def test_queue_desc_stable_across_weeks(self):
        f = {"problem": {"id": 3, "key": "h:probe", "state": "candidate"}}
        a = pm.specific_actions(_inc(), f)
        b = pm.specific_actions(_inc(id=99, opened_at=datetime(2026, 11, 2, tzinfo=timezone.utc)), f)
        self.assertEqual(pm.queue_desc("h:probe", "prevent", a[0]["description"]),
                         pm.queue_desc("h:probe", "prevent", b[0]["description"]))

    def test_sync_status(self):
        self.assertEqual(pm.sync_status("done"), "done")
        self.assertEqual(pm.sync_status("aged_out"), "cancelled")
        self.assertIsNone(pm.sync_status("queued"))

    def test_pages_ignore_triage_suppressed(self):
        self.assertEqual(pm.count_pages([{"channel": "C1"}, {"channel": "triage-suppressed"}, {"channel": None}]), 1)


class TestIntegration(unittest.TestCase):
    def test_reuses_lifecycle_threshold_and_remediation_runbooks(self):
        self.assertIn("nova_incident_lifecycle", SRC)
        self.assertIn("nova_remediation.RUNBOOKS", SRC)
        self.assertEqual(pm._threshold()[0] >= 2, True)

    def test_file_action_links_open_item_instead_of_duplicating(self):
        cur = _Cur({"SELECT id FROM claude_queue": [(42,)]})
        qid = pm.file_action(cur, "t", {"kind": "prevent", "owner": pm.OWNER, "queue_desc": "d"})
        self.assertEqual(qid, 42)
        self.assertFalse(any("INSERT INTO claude_queue" in s for s, _ in cur.sql))

    def test_file_action_registers_session_then_inserts(self):
        cur = _Cur({"INSERT INTO claude_queue": [(5,)]})
        self.assertEqual(pm.file_action(cur, "t", {"kind": "detect", "owner": pm.OWNER, "queue_desc": "d"}), 5)
        sqls = [s for s, _ in cur.sql]
        self.assertTrue(any("claude_sessions" in s for s in sqls))


class TestFunctional(unittest.TestCase):
    def test_sync_closes_done_actions_and_finalizes(self):
        cur = _Cur({"to_regclass": [("postmortems",)], "JOIN claude_queue": [(1, "done"), (2, "queued")]})
        cur.rowcount = 1
        out = pm.sync(cur)
        self.assertEqual(out["closed"], 1)
        self.assertTrue(any("status='final'" in s for s, _ in cur.sql))

    def test_sync_without_table_is_noop(self):
        self.assertEqual(pm.sync(_Cur({"to_regclass": [(None,)]})), {"closed": 0, "finalized": 0})


class TestFrame(unittest.TestCase):
    def test_help_and_selftest_exit_zero(self):
        env = dict(os.environ, NOVA_TEST_QUIET="1")
        for arg in ("--help", "--selftest"):
            r = subprocess.run([sys.executable, str(SCRIPT), arg], capture_output=True, timeout=30, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_does_not_run_main(self):
        self.assertTrue(hasattr(pm, "main"))


class TestPostmortemRework(unittest.TestCase):
    """#40 (Little Mister, 2026-10-15): specific actions, due dates, factors, review gate, one per problem."""
    def test_no_fact_no_action_and_never_the_templated_triple(self):
        self.assertEqual(pm.specific_actions(_inc(), {}), [])
        acts = pm.specific_actions(_inc(), {"problem": {"id": 1, "key": "h:probe", "state": "candidate"},
                                            "failed_runbooks": ["r1"], "noisy": ("det.py", 9, 10), "detect_s": 9999})
        self.assertEqual([a["kind"] for a in acts], ["prevent", "mitigate", "detect"])
        self.assertLessEqual(len(acts), pm.MAX_ACTIONS)
        self.assertFalse(any(pm.is_templated(a["description"]) for a in acts))
        self.assertEqual(acts[2]["subject"], "detector det.py")       # shared across problems
        self.assertTrue(pm.is_templated("Find and fix the underlying cause of h:probe (it keeps reopening)"))

    def test_known_error_problem_needs_no_prevent_action(self):
        self.assertEqual(pm.specific_actions(_inc(), {"problem": {"id": 1, "key": "k", "state": "known_error"}}), [])

    def test_late_detection_action_only_without_noisy_detector(self):
        acts = pm.specific_actions(_inc(), {"detect_s": pm.LATE_DETECT_S + 1})
        self.assertEqual([a["kind"] for a in acts], ["detect"])
        self.assertEqual(pm.specific_actions(_inc(), {"detect_s": 60}), [])

    def test_due_dates(self):
        self.assertEqual((pm.due_at(T0, "critical") - T0).days, 3)
        self.assertEqual((pm.due_at(T0, "warning") - T0).days, 5)
        self.assertEqual((pm.due_at(T0, None) - T0).days, 5)

    def test_detection_halved_and_near_miss(self):
        self.assertIsNone(pm.detection_halved(100, None))
        self.assertTrue(pm.detection_halved(50, 100))
        self.assertFalse(pm.detection_halved(60, 100))
        self.assertTrue(pm.near_miss(0, None))
        self.assertFalse(pm.near_miss(0, 0.01))
        self.assertFalse(pm.near_miss(2, None))

    def test_time_to_detect_ignores_suppressed_channels(self):
        from datetime import timedelta
        ev = [{"incident_id": 1, "ts": T0, "channel": None, "sent_at": None},
              {"incident_id": 1, "ts": T0, "channel": "triage-suppressed", "sent_at": T0 + timedelta(seconds=5)},
              {"incident_id": 1, "ts": T0 + timedelta(seconds=30), "channel": "C1", "sent_at": T0 + timedelta(seconds=90)}]
        self.assertEqual(pm.time_to_detect(ev), 90)
        self.assertIsNone(pm.time_to_detect(ev[:2]))

    def test_contributing_factors_from_data(self):
        f = pm.contributing_factors(_inc(), 4, [{"action": "r", "status": "failed"}], 5,
                                    {"noisy": ("d", 9, 10), "detect_s": 4000, "change": "proposal #3"})
        joined = " | ".join(f)
        for frag in ("recurred 4x", "remediation failed: r", "noisy", "late detection", "5 notifications",
                     "proposal #3"):
            self.assertIn(frag, joined)

    def test_review_gate(self):
        good = {"contributing_factors": ["x"], "detect_s": 60, "pages": 1, "actions": [], "recurring": True,
                "problem_id": 4, "problem_state": "known_error"}
        self.assertEqual(pm.review_gaps(good), [])
        bad = dict(good, contributing_factors=[], detect_s=None, problem_id=None, problem_state="candidate",
                   actions=[{"description": "Find and fix the underlying cause of x"}] * 4)
        gaps = " | ".join(pm.review_gaps(bad))
        for frag in ("no contributing factors", "detection time unknown", "4 actions", "templated",
                     "not linked to a problem"):
            self.assertIn(frag, gaps)
        self.assertIn("no action although", " ".join(pm.review_gaps(dict(good, problem_state="candidate"))))

    def test_review_promotes_passing_and_overdue_drafts_and_holds_the_rest(self):
        from datetime import timedelta
        future, past = T0 + timedelta(days=9), T0 - timedelta(days=1)
        rows = [(1, ["f"], None, False, 60, "recurring", 7, future, "known_error", [1]),
                (2, [], None, False, None, "critical", None, future, None, [2]),
                (3, [], None, False, None, "critical", None, past, None, [3])]
        cur = _Cur({"FROM postmortems p WHERE p.status='draft'": rows,
                    "FROM postmortem_actions WHERE postmortem_id": [],
                    "FROM telemetry.events WHERE incident_id": [(1,)]})
        out = pm.review(cur, now=T0)
        self.assertEqual(out, {"reviewed": 1, "overdue_accepted": 1, "held": 1})
        notes = [p[0] for s, p in cur.sql if s.startswith("UPDATE postmortems SET")]
        self.assertTrue(any("passed all gates" in n for n in notes))
        self.assertTrue(any("accepted overdue with gaps" in n for n in notes))
        self.assertTrue(any(n.startswith("Nova review: held") for n in notes))

    def test_run_reuses_the_live_postmortem_of_a_key(self):
        cur = _Cur({"SELECT pm_key FROM postmortems WHERE recurrence_key": [("rk:h:probe:2026-W40",)]})
        self.assertEqual(pm.live_key_for(cur, "h:probe"), "rk:h:probe:2026-W40")
        self.assertIsNone(pm.live_key_for(cur, None))

    def test_file_action_links_any_open_item_of_the_same_problem(self):
        calls = {"n": 0}

        def by_desc(sql, params):
            calls["n"] += 1
            return [] if calls["n"] == 1 else [(77,)]
        cur = _Cur({"SELECT id FROM claude_queue": by_desc})
        qid = pm.file_action(cur, "t", {"kind": "detect", "owner": pm.OWNER, "queue_desc": "new text",
                                        "subject": "h:probe"})
        self.assertEqual(qid, 77)
        self.assertFalse(any("INSERT INTO claude_queue" in s for s, _ in cur.sql))

    def test_retire_templated_records_a_reason(self):
        cur = _Cur({"SELECT a.id, a.queue_id, a.description": [(1, 10, "Find and fix the underlying cause of k"),
                                                               (2, None, "Runbook r failed on h/c")]})
        out = pm.retire_templated(cur)
        self.assertEqual(out["actions_retired"], 1)
        upd = [p for s, p in cur.sql if "SET status='cancelled'" in s]
        self.assertEqual(upd, [(pm.RETIRED_REASON, 1)])
        self.assertTrue(any("status='superseded'" in s for s, _ in cur.sql))

    def test_max_file_is_capped(self):
        self.assertLessEqual(pm.MAX_FILE, 5)


if __name__ == "__main__":
    unittest.main()
