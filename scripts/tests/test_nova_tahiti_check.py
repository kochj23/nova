#!/usr/bin/env python3
"""Tests for nova_tahiti_check.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Pure extraction/verdicts on fixtures; DB through fake cursors.
Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import re
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_tahiti_check.py"
spec = importlib.util.spec_from_file_location("tahiti_check_under_test", SCRIPT)
tc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tc)
SRC = SCRIPT.read_text()

T0 = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


class _Cur:
    """Answers fetchone/fetchall by substring of the last SQL; records every execute."""
    def __init__(self, routes=None):
        self.routes = routes or []
        self.sql, self.params, self._last = [], [], ""
        self.connection = mock.Mock()

    def execute(self, sql, params=None):
        self.sql.append(sql); self.params.append(params); self._last = sql

    def _route(self, default):
        for needle, val in self.routes:
            if needle in self._last:
                return val(self) if callable(val) else val
        return default

    def fetchone(self):
        return self._route(None)

    def fetchall(self):
        return self._route([])


def _journal(tmp: Path, posts: dict) -> Path:
    for rel, (date, body) in posts.items():
        p = tmp / "content" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'---\ntitle: "x"\ndate: {date}\n---\n\n{body}\n')
    return tmp


def _fake_db(sched_failures=5, new_devices=0, incidents=0):
    return _Cur([
        ("FROM beliefs WHERE active", [(7, "printer poller", "The printer_poller never fails.", T0 - timedelta(days=3), T0),
                                       (8, "rain", "There has been no rain this week.", T0, T0)]),
        ("FROM house_facts", [("garage_door", "state", "no", "home_assistant", T0),
                              ("side_yard", "coverage", "no camera covers the side yard", "manual", T0)]),
        ("FROM agent_docs", [("printers", "Never fabricate numbers.\nThe old_queue_table is not used by anything.",
                              int(T0.timestamp() * 1000))]),
        ("SELECT DISTINCT task_id FROM scheduler_runs", [("printer_poller",), ("wan_monitor",)]),
        ("FROM research_log", []),
        ("FROM web_searches", []),
        ("FROM scheduler_runs WHERE task_id = ANY", (sched_failures, int(T0.timestamp() * 1000))),
        ("FROM scheduler_runs WHERE status IN", (sched_failures, int(T0.timestamp() * 1000))),
        ("FROM telemetry.net_inventory", (new_devices,)),
        ("FROM incidents", (incidents,)),
        ("FROM pg_class", (0, 0)),
    ])


class Security(unittest.TestCase):
    def test_no_hardcoded_credentials_ips_or_home_paths(self):
        self.assertNotRegex(SRC, r"(?i)(password|api_key|token)\s*=\s*['\"][^'\"]+['\"]")
        self.assertNotRegex(SRC, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
        self.assertNotRegex(SRC, r"/" + r"Users/|/home/")
        self.assertNotRegex(SRC, r"[\w.+-]+@[\w-]+\.[\w.]+")
        self.assertNotRegex(SRC, r"\b([0-9a-f]{2}:){5}[0-9a-f]{2}\b")

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r"execute\(f[\"']")
        self.assertNotRegex(SRC, r"execute\([^)]*%\s*\(")

    def test_never_edits_what_it_judges(self):
        for verb in (r"DELETE\s+FROM", r"\bDROP\s", r"TRUNCATE", r"subprocess", r"os\.system", r"\.unlink\(",
                     r"write_text\(", r"/forget"):
            self.assertNotRegex(SRC, verb)
        writes = re.findall(r"(INSERT INTO|(?<!DO )UPDATE)\s+([\w.]+)", SRC)
        self.assertTrue(writes)
        self.assertTrue(all(t == "tahiti_check" for _, t in writes), writes)

    def test_ddl_goes_through_safe_ddl(self):
        self.assertIn("nova_pg_schema.safe_ddl(cur, SCHEMA)", SRC)
        self.assertNotIn("cur.execute(SCHEMA)", SRC)


class Performance(unittest.TestCase):
    def test_extraction_scales(self):
        doc = " ".join(f"Task job_{i} never fails. Nothing new on the network. No doubt about it." for i in range(3000))
        t = time.monotonic()
        tc.extract("agent_doc:x", doc, T0, limit=10_000)
        self.assertLess(time.monotonic() - t, 3.0)

    def test_task_matching_is_precompiled(self):
        pats = tc.task_patterns({f"task_number_{i}" for i in range(1500)})
        t = time.monotonic()
        for _ in range(300):
            tc.tasks_in_claim("the task_number_42 never fails", pats)
        self.assertLess(time.monotonic() - t, 3.0)

    def test_budget_and_statement_timeout(self):
        self.assertLessEqual(tc.STATEMENT_TIMEOUT_MS, 30000)
        self.assertLess(tc.TIME_BUDGET_S, 600)
        self.assertIn("deadline=None if a.dry_run else t0 + TIME_BUDGET_S", SRC)


class Retry(unittest.TestCase):
    def test_failed_notify_leaves_month_undigested(self):
        cur = _Cur()
        claims = [{"claim_id": "a", "verdict": "refuted", "searched": True, "source": "belief:1", "claim": "x",
                   "domain": "house", "evidence": {}}]
        with mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=mock.Mock(return_value=False))}):
            self.assertFalse(tc.digest(cur, claims, "2026-10"))
        self.assertFalse(any("SET digested_month" in q for q in cur.sql))

    def test_digest_once_per_month(self):
        cur = _Cur([("WHERE digested_month = %s", (1,))])
        notify = mock.Mock(return_value=True)
        with mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=notify)}):
            self.assertFalse(tc.digest(cur, [], "2026-10"))
        notify.assert_not_called()

    def test_a_failing_direct_check_rolls_back_and_keeps_going(self):
        cur = _Cur()
        cur.execute = mock.Mock(side_effect=RuntimeError("canceling statement due to statement timeout"))
        c = tc.extract("belief:1", "The printer_poller never fails.", T0)[0]
        r = tc.direct_check(cur, None, c, tc.task_patterns({"printer_poller"}), T0)
        self.assertTrue(r["error"])
        cur.connection.rollback.assert_called()

    def test_rerun_upserts_by_claim_id(self):
        self.assertIn("ON CONFLICT (claim_id) DO UPDATE", SRC)
        self.assertEqual(tc.claim_id("journal:a/b", "No new devices."), tc.claim_id("journal:c/d", "no new devices"))


class Unit(unittest.TestCase):
    def test_classify_kinds(self):
        cases = {"The printer poller never fails.": "never",
                 "claude_actions is NOT used for this.": "unused",
                 "That table doesn't exist on the replica.": "nonexistent",
                 "Zero rogue access points flagged.": "zero",
                 "Nothing changed in the automation logs.": "nothing",
                 "No new devices joined the network.": "none"}
        for s, kind in cases.items():
            self.assertEqual(tc.classify(s)[0], kind, s)

    def test_non_claims_are_skipped(self):
        for s in ("No doubt it rained.", "A Chrome zero-day was patched.", "Never fabricate memory statistics.",
                  "You should never write stub files.", "I never sleep, I just idle.", "\"No way,\" she said."):
            self.assertIsNone(tc.classify(s), s)

    def test_window_and_cycle(self):
        self.assertEqual(tc.window_from_text("no failures in the last 14 days"), 14)
        self.assertEqual(tc.window_from_text("zero errors over the past two weeks"), 14)
        self.assertEqual(tc.window_from_text("nothing new this week"), 7)
        self.assertIsNone(tc.window_from_text("the poller never fails"))
        self.assertEqual(tc.natural_cycle("no rain this week")[0], 365)
        self.assertEqual(tc.natural_cycle("no certificate expired")[0], 90)
        self.assertEqual(tc.natural_cycle("the job never fails")[0], 1)
        self.assertEqual(tc.natural_cycle("the moon is pretty")[1], "weekly (default)")

    def test_verdicts(self):
        self.assertEqual(tc.verdict(False, 1, 7, None), "unsearched")
        self.assertEqual(tc.verdict(True, 1, 7, None), "too_short")
        self.assertEqual(tc.verdict(True, 30, 7, None), "confirmed")
        self.assertEqual(tc.verdict(False, 1, 7, {"found": True}), "refuted")
        self.assertEqual(tc.verdict(False, 1, 30, {"found": False, "window_days": 90}), "confirmed")
        self.assertEqual(tc.verdict(False, 1, 365, {"found": False, "window_days": 90}), "too_short")

    def test_instrument_readings_and_search_logs(self):
        self.assertTrue(tc.is_instrument_reading("no"))
        self.assertTrue(tc.is_instrument_reading(" Off "))
        self.assertFalse(tc.is_instrument_reading("no camera covers the side yard"))
        logs = [("research_log", "is there surf in tahiti in august"), ("web_searches", "tahiti weather")]
        self.assertEqual(len(tc.searched_in_logs(["tahiti", "surf"], logs)), 1)
        self.assertEqual(tc.searched_in_logs([], logs), [])

    def test_task_mentions_need_a_compound_name_near_the_negation(self):
        pats = tc.task_patterns({"printer_poller", "news", "wan_monitor"})
        self.assertEqual(tc.tasks_in_claim("The printer poller never fails", pats), ["printer_poller"])
        self.assertEqual(tc.tasks_in_claim("news never stops", pats), [])
        far = "wan_monitor was slow today, " + "x " * 60 + "and nothing failed"
        self.assertEqual(tc.tasks_in_claim(far, pats, near=80), [])

    def test_monthly_stats(self):
        st = tc.monthly_stats([("2026-10", 200, 20, 5, 170, 10, 15), ("2026-11", 0, 0, 0, 0, 0, 0)])
        self.assertEqual(st[0]["searched_pct"], 10.0)
        self.assertEqual(st[0]["refuted"], 5)
        self.assertIsNone(st[1]["searched_pct"])


class Integration(unittest.TestCase):
    def test_evaluate_reads_all_four_sources(self):
        with tempfile.TemporaryDirectory() as d:
            root = _journal(Path(d), {"operations/p.md": ("2026-10-14T09:00:00-07:00",
                                                         "The scheduler ran 100 tasks and reported zero failures."),
                                      "operations/old.md": ("2026-06-01T09:00:00-07:00",
                                                           "The scheduler ran 100 tasks with zero failures.")})
            with mock.patch.object(tc, "scheduler_tasks", return_value={"printer_poller", "wan_monitor"}):
                claims = tc.evaluate(_fake_db(), None, now=T0, journal=root)
        srcs = {c["source"].split(":")[0] for c in claims}
        self.assertEqual(srcs, {"belief", "house_fact", "agent_doc", "journal"})
        self.assertFalse(any("garage_door" in c["source"] for c in claims))       # instrument reading skipped
        self.assertFalse(any("old" in c["source"] for c in claims))               # outside 30 days
        self.assertFalse(any("fabricate" in c["claim"] for c in claims))          # an instruction, not a claim

    def test_evaluate_is_read_only(self):
        cur = _fake_db()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(tc, "scheduler_tasks", return_value={"printer_poller"}):
            tc.evaluate(cur, None, now=T0, journal=Path(d))
        self.assertTrue(all(not re.match(r"\s*(INSERT|UPDATE|DELETE|CREATE|ALTER)", q) for q in cur.sql))

    def test_scheduler_tasks_reads_yaml_and_recent_runs(self):
        cur = _Cur([("SELECT DISTINCT task_id FROM scheduler_runs", [("only_in_runs_task",)])])
        tasks = tc.scheduler_tasks(cur)
        self.assertIn("only_in_runs_task", tasks)
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not available")
        self.assertIn("comet_tail", tasks)


class Functional(unittest.TestCase):
    def _claims(self, **db):
        with tempfile.TemporaryDirectory() as d:
            root = _journal(Path(d), {"operations/p.md": ("2026-10-14T09:00:00-07:00",
                                                         "The scheduler ran 100 tasks and reported zero failures.")})
            with mock.patch.object(tc, "scheduler_tasks", return_value={"printer_poller"}):
                return {c["claim"]: c for c in tc.evaluate(_fake_db(**db), None, now=T0, journal=root)}

    def test_never_fails_refuted_by_scheduler_runs(self):
        c = self._claims(sched_failures=5)["The printer_poller never fails."]
        self.assertEqual(c["verdict"], "refuted")
        self.assertIn("5 failure/timeout run(s) of printer_poller", c["evidence"]["direct"]["detail"])

    def test_never_fails_confirmed_when_90_days_are_clean(self):
        c = self._claims(sched_failures=0)["The printer_poller never fails."]
        self.assertEqual(c["verdict"], "confirmed")
        self.assertTrue(c["searched"])

    def test_zero_failures_post_is_checked_against_that_day(self):
        c = self._claims(sched_failures=167)["The scheduler ran 100 tasks and reported zero failures."]
        self.assertEqual(c["verdict"], "refuted")
        self.assertIn("all scheduler tasks", c["evidence"]["direct"]["detail"])

    def test_unsearched_and_seasonal(self):
        cl = self._claims()
        self.assertEqual(cl["There has been no rain this week."]["verdict"], "unsearched")
        self.assertEqual(cl["There has been no rain this week."]["cycle_days"], 365)
        self.assertEqual(cl["no camera covers the side yard"]["verdict"], "unsearched")

    def test_digest_names_refuted_and_unsearched(self):
        claims = [{"claim_id": "a", "verdict": "refuted", "searched": True, "source": "belief:7", "domain": "house",
                   "claim": "The printer_poller never fails.", "evidence": {"direct": {"detail": "5 failures"}}},
                  {"claim_id": "b", "verdict": "unsearched", "searched": False, "source": "journal:x", "domain": "house",
                   "claim": "No one uses the guest VLAN.", "evidence": {}}]
        title, body = tc.render_digest("2026-10", claims)
        self.assertIn("1 absence claim(s) refuted, 1 never searched (50.0% of 2 ever searched)", title)
        for s in ("REFUTED", "UNSEARCHED", "5 failures", "Nothing was edited"):
            self.assertIn(s, body)


class Frame(unittest.TestCase):
    def test_help_and_no_mode(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            tc.main(["--help"])
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            tc.main([])

    def test_selftest(self):
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(tc.main(["--selftest"]), 0)
        self.assertIn("selftest ok", out.getvalue())

    def test_dry_run_writes_nothing_and_notifies_nothing(self):
        cur = _fake_db()
        conn = mock.Mock(cursor=mock.Mock(return_value=cur))
        notify = mock.Mock()
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(tc, "connect", return_value=conn), \
             mock.patch.object(tc, "journal_root", return_value=Path(d)), \
             mock.patch.object(tc, "scheduler_tasks", return_value={"printer_poller"}), \
             mock.patch.dict(sys.modules, {"nova_notify": mock.Mock(notify=notify)}):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(tc.main(["--dry-run"]), 0)
        self.assertIn("nothing written", out.getvalue())
        notify.assert_not_called()
        self.assertFalse(any(re.match(r"\s*(INSERT|UPDATE|CREATE|ALTER)", q) for q in cur.sql))

    def test_house_conventions(self):
        self.assertIn("Little Mister, 2026-10-15", SRC)
        self.assertIn("Written by Jordan Koch (via Claude).", SRC)
        self.assertIn("The Endless Summer", SRC)
        self.assertIn("nova_dsn.pg_dsn", SRC)
        self.assertIn("nova_journal.HUGO_ROOT", SRC)
        for col in ("claim_id text PRIMARY KEY", "source text", "claim text", "kind text", "searched boolean",
                    "window_days real", "cycle_days real", "verdict text", "evidence jsonb"):
            self.assertIn(col, SRC)
        for flag in ("--scheduled", "--dry-run", "--report", "--selftest"):
            self.assertIn(flag, SRC)


if __name__ == "__main__":
    unittest.main()
