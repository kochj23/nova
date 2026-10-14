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
        doc = pm.render(inc, "critical", [inc], ev, pm.action_items(inc, 1, None), pm.lessons(inc, 1, [], 0), 1, [], 0)
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
        acts = pm.action_items(inc, 4, ["restart_x"])
        doc = pm.render(inc, "critical+recurring", [inc], [], acts, pm.lessons(inc, 4, [], 5), 4, [], 5)
        for h in ("## Summary", "## Impact", "## Root causes", "## Trigger", "## Detection", "## Resolution",
                  "## Action items", "### What went well", "### What went wrong", "### Where we got lucky",
                  "## Timeline"):
            self.assertIn(h, doc)
        self.assertNotIn("SomePerson", doc)
        self.assertEqual({a["kind"] for a in acts}, set(pm.KINDS))
        self.assertIn("restart_x", acts[1]["description"])

    def test_queue_desc_stable_across_weeks(self):
        a = pm.action_items(_inc(), 4, None)
        b = pm.action_items(_inc(id=99, opened_at=datetime(2026, 11, 2, tzinfo=timezone.utc)), 9, None)
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


if __name__ == "__main__":
    unittest.main()
