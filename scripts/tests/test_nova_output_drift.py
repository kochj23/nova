#!/usr/bin/env python3
"""Tests for nova_output_drift.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from datetime import date
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_output_drift.py"
SRC = SCRIPT.read_text()


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


od = _load("od", SCRIPT)


class _Cur:
    """Routes each query to a canned answer by SQL substring (first match wins); records every statement."""
    def __init__(self, routes=()):
        self.routes = list(routes); self.sql = []; self._last = ""

    def execute(self, sql, params=None):
        self._last = " ".join(sql.split()); self.sql.append((self._last, params))
        for key, val in self.routes:
            if key in self._last and isinstance(val, Exception):
                raise val

    def fetchall(self):
        for key, val in self.routes:
            if key in self._last:
                return list(val)
        return []

    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None

    def ran(self, frag):
        return [(s, p) for s, p in self.sql if frag in s]


class _Conn:
    def __init__(self, cur):
        self._cur = cur; self.autocommit = False

    def cursor(self):
        return self._cur


NOTHING = ["[proactive-digest 08:00:13] **Nothing**"] * 5
TASK_ROWS = ([("proactive_digest", "nova_proactive_digest.py", t) for t in NOTHING]
             + [("weekly_report", "nova_weekly.py", "")] * 5
             + [("nova_peace", "nova_peace.py", "[nova_peace 11:36:43] Checking Jordan's state...")] * 5     # not a communicator
             + [("daily_brief", "nova_brief.py", f"[brief 08:00:00] posted {i} items") for i in range(5)]     # changing: fine
             + [("karr_report", "nova_karr.py", "[karr_report 10:00:00] all good, 3 detections")] * 5)       # constant but not zero-ish
DELIVERY_ROWS = [("morning_brief", True, 3), ("mail_deliver_pm", False, 3)]      # pm missed; am retired (no runs)
DELIVERY_ALREADY = "dedup_key='dead-mans-switch-recovery'"
COAG_ROWS = [("execute_failed", "#116 failed: ")] * 5 + [("executed", "#117 ok")]
HEALTH_ROWS = [("nova-core6", "ollama", ["down"] * 8), ("nova-core2", "ollama", ["down"] * 3 + ["up"] + ["down"] * 4), ("nova-core3", "x", ["down"] * 2)]


def _routes(seen=(), **over):
    r = {"AT TIME ZONE %s)::int": [(20, date(2026, 10, 8))], "FROM scheduler_runs WHERE task_id = ANY": DELIVERY_ROWS,
         DELIVERY_ALREADY: [], "FROM scheduler_runs WHERE status IN": TASK_ROWS, "FROM proactive_digest_log": [],
         "FROM coagency_log": COAG_ROWS, "FROM health_checks": HEALTH_ROWS, "FROM telemetry.events": [(k,) for k in seen]}
    r.update(over)
    return list(r.items())


def _main(argv=("nova_output_drift.py",), routes=None, notify=None):
    cur = _Cur(routes or _routes())
    stub = types.ModuleType("nova_notify"); stub.notify = notify or MagicMock(return_value=True)
    out = io.StringIO()
    with patch.object(od.psycopg2, "connect", return_value=_Conn(cur)), patch.object(sys, "argv", list(argv)), \
         patch.dict(sys.modules, {"nova_notify": stub}), redirect_stdout(out):
        rc = od.main()
    return rc, cur, stub.notify, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_probes_are_read_only_and_parameterized(self):
        for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP "):
            self.assertNotIn(verb, SRC)
        self.assertIsNone(re.search(r'execute\(\s*f"', SRC))
        rc, cur, _, _ = _main()
        for sql, params in cur.sql:
            self.assertNotIn(f"'{od.WINDOW_DAYS} days'", sql)                                # window travels as a parameter
        self.assertEqual(cur.ran("FROM scheduler_runs WHERE status IN")[0][1], (str(od.WINDOW_DAYS),))

    def test_findings_truncate_samples(self):
        tails = ["x" * 5000 + " nothing to report"] * 5
        f = od.probe_tasks(_Cur([("FROM scheduler_runs", [("daily_report", "r.py", t) for t in tails])]))
        self.assertLessEqual(len(f[0]["sample"]), 160)


class TestPerformance(unittest.TestCase):
    def test_classifiers_fast_on_10k(self):
        tails = [f"[job {i % 24:02d}:00:00] **Nothing** at all, 0 items" for i in range(10_000)]
        t0 = time.perf_counter()
        for i in range(0, 10_000, 5):
            od.classify(tails[i:i + 5]); od.stuck(tails[i:i + 5], ["failure"] * 5)
        od.chronic(["down"] * 10_000)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_pg_down_fails_open(self):
        # RETRY GAP: main (psycopg2.connect) — no retry; returns 0 and reports nothing rather than crashing the hour
        with patch.object(od.psycopg2, "connect", side_effect=OSError("no pg")), patch.object(sys, "argv", ["x"]), redirect_stdout(io.StringIO()):
            self.assertEqual(od.main(), 0)

    def test_one_broken_probe_does_not_stop_the_others(self):
        rc, cur, notify, out = _main(routes=_routes(**{"FROM scheduler_runs WHERE status IN": RuntimeError("relation missing")}))
        self.assertEqual(rc, 0)
        self.assertIn("probe_tasks failed (relation missing)", out)
        self.assertTrue(any("Chronic down" in c[0][0] for c in notify.call_args_list))     # chronic probe still ran

    def test_delivery_probe_failure_does_not_stop_the_others(self):
        # RETRY GAP: run_deliveries — one attempt per run; the next hourly run checks again
        rc, cur, notify, out = _main(routes=_routes(**{"FROM scheduler_runs WHERE task_id = ANY": RuntimeError("timeout")}))
        self.assertEqual(rc, 0)
        self.assertIn("probe_deliveries failed (timeout)", out)
        self.assertTrue(any("Chronic down" in c[0][0] for c in notify.call_args_list))


class TestUnit(unittest.TestCase):
    def test_selftest_passes(self):
        with redirect_stdout(io.StringIO()):
            od.demo()

    def test_normalise_and_classify_edges(self):
        self.assertEqual(od.normalise(None), "")
        self.assertEqual(od.normalise("2026-09-28 08:00:13 posted 12 items"), "posted 12 items")
        self.assertIsNone(od.classify([]))
        self.assertIsNone(od.classify(["**Nothing**"] * 5 + ["real"], n=6))
        self.assertEqual(od.classify(["", None, " "], n=3), "empty")
        self.assertEqual(od.classify(["same"] * 2, n=2), "unchanged")

    def test_stuck_and_chronic_edges(self):
        self.assertFalse(od.stuck(["e"] * 5, ["failure"] * 4))                          # statuses too short
        self.assertTrue(od.stuck([None] * 5, ["failure"] * 5))                          # five empty errors are one wall
        self.assertFalse(od.chronic([]))
        self.assertFalse(od.chronic(["down"] * 5 + ["healthy"]))
        self.assertTrue(od.chronic(["down", "error", "timeout"] * 2))

    def test_delivered_edges(self):
        rows = [("morning_brief", True, 3), ("mail_deliver_am", False, 2), ("mail_deliver_pm", False, 0)]
        missed, skipped = od.delivered(rows, 10, None)
        self.assertEqual(missed, [("mail_deliver_am", "Morning Mail Summary (8am)")])
        self.assertEqual(skipped, [("mail_deliver_pm", "too early (now=10h, check after 19h)")])
        missed, skipped = od.delivered(rows, 23, None)
        self.assertIn(("mail_deliver_pm", "no runs in 14d (retired?)"), skipped)     # a retired delivery never alarms
        self.assertEqual(od.delivered([], 3, None), ([], [(t, f"too early (now=3h, check after {h}h)") for t, h, _ in od.DELIVERIES]))
        self.assertEqual([d[:2] for d in od.DELIVERIES], [("morning_brief", 9), ("mail_deliver_am", 9), ("mail_deliver_pm", 19)])


class TestIntegration(unittest.TestCase):
    def test_probe_tasks_filters_by_communicator_and_zeroish(self):
        found = {f["task"]: f["kind"] for f in od.probe_tasks(_Cur([("FROM scheduler_runs", TASK_ROWS)]))}
        self.assertEqual(found, {"proactive_digest": "unchanged", "weekly_report": "empty"})

    def test_probe_digest_and_chronic(self):
        f = od.probe_digest(_Cur([("FROM proactive_digest_log", [(t,) for t in NOTHING])]))
        self.assertEqual((f[0]["kind"], f[0]["n"], f[0]["script"]), ("unchanged", 5, "nova_proactive_digest.py"))
        self.assertEqual(od.probe_digest(_Cur([("FROM proactive_digest_log", [("posted 12 items",)] * 5)])), [])
        cur = _Cur([("FROM health_checks", HEALTH_ROWS)])
        self.assertEqual(od.probe_chronic(cur), [{"node": "nova-core6", "service": "ollama", "checks": 8}])
        self.assertEqual(cur.sql[0][1], (str(od.CHRONIC_HOURS),))

    def test_probe_stuck_is_coagency_only_after_merge(self):
        # merge M2 (2026-10-09): failing scheduler tasks are nova_task_sentinel's; this probe no longer reads them
        cur = _Cur([("FROM coagency_log", COAG_ROWS)])
        out = {f["task"]: f for f in od.probe_stuck(cur)}
        self.assertEqual(set(out), {"coagency#116"})
        self.assertEqual(out["coagency#116"]["script"], "nova_coagency.py --mode execute-approved")
        self.assertEqual(cur.ran("scheduler_runs"), [])
        self.assertFalse(hasattr(od, "_host_of_task"))

    def test_delivery_check_reads_shared_scheduler_runs_with_local_day(self):
        cur = _Cur(_routes())
        missed, skipped, hour, today = od.probe_deliveries(cur)
        self.assertEqual((missed, hour), ([("mail_deliver_pm", "Evening Mail Summary (6pm)")], 20))
        sql, params = cur.ran("FROM scheduler_runs WHERE task_id = ANY")[0]
        self.assertEqual(params, ("America/Los_Angeles", date(2026, 10, 8),
                                  ["morning_brief", "mail_deliver_am", "mail_deliver_pm"], 14))
        self.assertNotIn("37460", SRC)                     # never the Studio scheduler's HTTP port again


class TestFunctional(unittest.TestCase):
    def test_golden_path_raises_one_warning_per_finding(self):
        rc, cur, notify, out = _main()
        self.assertEqual(rc, 0)
        self.assertIn("4 finding(s)", out)
        keys = sorted(c[1]["dedup_key"] for c in notify.call_args_list)
        self.assertEqual(keys, ["dead-mans-switch-recovery", "output_drift:chronic:nova-core6:ollama",
                                "output_drift:proactive_digest:unchanged", "output_drift:stuck:coagency#116",
                                "output_drift:weekly_report:empty"])
        drift = [c for c in notify.call_args_list if c[1]["dedup_key"] != "dead-mans-switch-recovery"]
        for c in drift:
            self.assertEqual((c[1]["level"], c[1]["category"], c[1]["source"]), ("warning", "output_drift", "nova_output_drift"))
        stuck = next(c for c in notify.call_args_list if c[1]["dedup_key"] == "output_drift:stuck:coagency#116")
        self.assertEqual(stuck[1]["meta"], {"host": "nova-core"})                       # host -> the correlator opens an incident
        self.assertIn("**Nothing**", next(c for c in notify.call_args_list if "proactive" in c[1]["dedup_key"])[0][1])

    def test_self_dedup_skips_keys_already_on_the_bus_today(self):
        rc, cur, notify, out = _main(routes=_routes(seen=["output_drift:proactive_digest:unchanged", "output_drift:chronic:nova-core6:ollama"]))
        self.assertEqual(notify.call_count, 3)                                           # 2 drift + 1 delivery
        self.assertIn("source='nova_output_drift'", cur.ran("FROM telemetry.events WHERE source")[0][0])

    def test_dry_run_prints_and_notifies_nothing(self):
        rc, cur, notify, out = _main(["x", "--dry-run"])
        self.assertEqual(rc, 0)
        notify.assert_not_called()
        self.assertIn("• [warning] Constant output: proactive_digest (unchanged for 5 runs) (?)", out)
        self.assertEqual(cur.ran("FROM telemetry.events"), [])
        self.assertIn("Dead Man's Switch — Missed Deliveries", out)

    def test_deliveries_mode_is_the_dead_mans_switch(self):
        rc, cur, notify, out = _main(["x", "--deliveries"])
        self.assertEqual(rc, 0)
        notify.assert_called_once()
        (title,), kw = notify.call_args
        self.assertEqual(title, "Dead Man's Switch — Missed Deliveries")
        self.assertEqual((kw["level"], kw["category"], kw["dedup_key"]), ("warning", "scheduler", "dead-mans-switch-recovery"))
        self.assertEqual(kw["meta"], {"missing": "mail_deliver_pm", "day": "2026-10-08"})
        self.assertIn("Evening Mail Summary (6pm)", kw["body"])
        self.assertEqual(cur.ran("FROM health_checks") + cur.ran("FROM coagency_log"), [])   # only this check ran
        # the same set of misses already raised today -> silent
        rc, cur, notify, out = _main(["x", "--deliveries"], routes=_routes(**{DELIVERY_ALREADY: [(1,)]}))
        notify.assert_not_called()
        self.assertIn("already raised today", out)


class TestFrame(unittest.TestCase):
    def test_selftest_runs_and_import_is_guarded(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--selftest"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("all output-drift assertions passed", r.stdout)
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
