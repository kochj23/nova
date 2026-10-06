#!/usr/bin/env python3
"""Tests for nova_dead_mans_switch.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_dead_mans_switch.py"
SRC = SCRIPT.read_text()


@contextlib.contextmanager
def _modules(**mods):
    """Set sys.modules keys for the block and restore ONLY those keys afterwards."""
    missing = object()
    saved = {k: sys.modules.get(k, missing) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is missing:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load(name, path):
    notify_mod = types.ModuleType("nova_notify"); notify_mod.notify = MagicMock(return_value=True)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with _modules(nova_notify=notify_mod):
        spec.loader.exec_module(mod)
    return mod


dm = _load("dead_mans_switch_under_test", SCRIPT)
TODAY_TS = datetime.now().replace(hour=7, minute=5).timestamp()
YESTERDAY_TS = (datetime.now() - timedelta(days=1)).timestamp()


class _Resp:
    def __init__(self, d):
        self._d = json.dumps(d).encode()

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _tasks(**exit_codes):
    return {tid: {"last_run": TODAY_TS, "last_exit_code": code} for tid, code in exit_codes.items()}


def _cp(rc=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def _main(tasks, hour=20, run=None):
    run = run or MagicMock(return_value=_cp(0, "delivered"))
    dm.notify = MagicMock(return_value=True)
    out = io.StringIO()
    with patch.object(dm.urllib.request, "urlopen", return_value=_Resp(tasks)), patch.object(dm, "NOW_HOUR", hour), \
         patch.object(dm.subprocess, "run", run), redirect_stdout(out):
        dm.main()
    return run, dm.notify, out.getvalue()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_recovery_runs_the_pinned_script_as_an_argv_list_with_no_shell(self):
        run = MagicMock(return_value=_cp(0))
        with patch.object(dm.subprocess, "run", run), redirect_stdout(io.StringIO()):
            dm.run_script(SCRIPTS / "nova_morning_brief.py")
        args, kw = run.call_args
        self.assertEqual(args[0], [sys.executable, str(SCRIPTS / "nova_morning_brief.py")])
        self.assertFalse(kw.get("shell")); self.assertEqual(kw["timeout"], 120)
        self.assertTrue(dm.SCHEDULER_API.startswith("http://127.0.0.1:"))

    def test_only_scripts_from_the_delivery_table_are_ever_run(self):
        hostile = {"morning_brief": {"last_run": TODAY_TS, "last_exit_code": 0, "script": "/tmp/evil.py"}}
        run, _, _ = _main(hostile)
        ran = {c[0][0][1] for c in run.call_args_list}
        self.assertTrue(ran <= {str(s) for _, s, _, _ in dm.DELIVERIES})
        self.assertNotIn("/tmp/evil.py", ran)


class TestPerformance(unittest.TestCase):
    def test_task_state_checks_scale_to_10k_tasks(self):
        tasks = {f"t{i}": {"last_run": TODAY_TS if i % 2 else YESTERDAY_TS, "last_exit_code": i % 3} for i in range(10_000)}
        t0 = time.perf_counter()
        ok = sum(1 for tid in tasks if dm.task_ran_today(tasks, tid))
        self.assertLess(time.perf_counter() - t0, 0.5)
        self.assertEqual(ok, sum(1 for i in range(10_000) if i % 2 and i % 3 == 0))


class TestRetry(unittest.TestCase):
    def test_scheduler_api_outage_fails_open_and_skips_recovery(self):
        # RETRY GAP: get_scheduler_tasks — one 5 s GET; an outage returns {} and main() does nothing (no false re-runs)
        run = MagicMock(); dm.notify = MagicMock()
        out = io.StringIO()
        with patch.object(dm.urllib.request, "urlopen", side_effect=OSError("scheduler down")), \
             patch.object(dm.subprocess, "run", run), redirect_stdout(out):
            self.assertEqual(dm.get_scheduler_tasks(), {})
            dm.main()
        run.assert_not_called(); dm.notify.assert_not_called()
        self.assertIn("Scheduler API unreachable: scheduler down", out.getvalue())
        self.assertIn("Could not reach scheduler API — skipping", out.getvalue())

    def test_recovery_run_timeouts_and_crashes_are_reported_not_raised(self):
        # RETRY GAP: run_script — one attempt per missed delivery; failure is reported in the Slack recovery note
        out = io.StringIO()
        with patch.object(dm.subprocess, "run", side_effect=dm.subprocess.TimeoutExpired("x", 120)), redirect_stdout(out):
            self.assertFalse(dm.run_script(Path("/x/y.py")))
        self.assertIn("Timeout running y.py", out.getvalue())
        with patch.object(dm.subprocess, "run", side_effect=OSError("boom")), redirect_stdout(out):
            self.assertFalse(dm.run_script(Path("/x/y.py")))
        self.assertIn("Error running y.py: boom", out.getvalue())


class TestUnit(unittest.TestCase):
    def test_task_ran_today_edges(self):
        self.assertFalse(dm.task_ran_today({}, "nope"))
        self.assertFalse(dm.task_ran_today({"t": {"last_run": 0, "last_exit_code": 0}}, "t"))
        self.assertTrue(dm.task_ran_today({"t": {"last_run": TODAY_TS, "last_exit_code": 0}}, "t"))
        self.assertFalse(dm.task_ran_today({"t": {"last_run": TODAY_TS, "last_exit_code": 1}}, "t"))
        self.assertFalse(dm.task_ran_today({"t": {"last_run": TODAY_TS}}, "t"))          # no exit code -> not a success
        self.assertFalse(dm.task_ran_today({"t": {"last_run": YESTERDAY_TS, "last_exit_code": 0}}, "t"))

    def test_run_script_logs_stdout_and_stderr_tails(self):
        out = io.StringIO()
        with patch.object(dm.subprocess, "run", return_value=_cp(3, "o" * 300, "bad things")), redirect_stdout(out):
            self.assertFalse(dm.run_script(Path("/x/z.py")))
        self.assertIn("Ran z.py: exit=3", out.getvalue())
        self.assertIn("stdout: " + "o" * 200 + "\n", out.getvalue())
        self.assertIn("stderr: bad things", out.getvalue())

    def test_slack_post_splits_title_and_body(self):
        dm.notify = MagicMock()
        dm.slack_post("*Dead Man's Switch — Missed Deliveries Recovered*\n\n✅ `x` — ok")
        dm.notify.assert_called_once_with("Dead Man's Switch — Missed Deliveries Recovered", body="✅ `x` — ok",
                                          level="warning", category="scheduler", dedup_key="dead-mans-switch-recovery")
        dm.slack_post("only a title")
        self.assertIsNone(dm.notify.call_args[1]["body"])


class TestIntegration(unittest.TestCase):
    def test_delivery_table_points_at_real_scripts_with_sane_check_hours(self):
        self.assertEqual([d[0] for d in dm.DELIVERIES], ["morning_brief", "mail_deliver_am", "mail_deliver_pm"])
        for task_id, script, hour, label in dm.DELIVERIES:
            self.assertTrue(script.exists(), script); self.assertEqual(script.parent, SCRIPTS)
            self.assertIn(hour, (9, 19))
        self.assertEqual(dm.DELIVERIES[2][2], 19)

    def test_too_early_checks_are_skipped_per_delivery(self):
        run, notify, out = _main(_tasks(morning_brief=1, mail_deliver_am=1, mail_deliver_pm=1), hour=10)
        self.assertEqual(run.call_count, 2)                      # the 6 pm mail is not checked at 10 am
        self.assertIn("Evening Mail Summary (6pm) — too early to check (now=10h, min=19h)", out)
        self.assertIn("Recovered 2 missed deliveries", out)


class TestFunctional(unittest.TestCase):
    def test_golden_path_everything_delivered(self):
        run, notify, out = _main(_tasks(morning_brief=0, mail_deliver_am=0, mail_deliver_pm=0))
        run.assert_not_called(); notify.assert_not_called()
        self.assertEqual(out.count("— delivered ✓"), 3)
        self.assertIn("All deliveries confirmed — nothing to recover", out)

    def test_missed_delivery_is_rerun_and_announced(self):
        run, notify, out = _main({**_tasks(morning_brief=0, mail_deliver_am=0), "mail_deliver_pm": {"last_run": YESTERDAY_TS, "last_exit_code": 0}})
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0][1], str(SCRIPTS / "nova_mail_deliver.py"))
        title, kw = notify.call_args[0][0], notify.call_args[1]
        self.assertEqual(title, "Dead Man's Switch — Missed Deliveries Recovered")
        self.assertEqual(kw["body"], "✅ `Evening Mail Summary (6pm)` — was missing, ran now (ok)")
        self.assertIn("Recovered 1 missed deliveries", out)

    def test_error_path_failed_rerun_is_flagged(self):
        run, notify, out = _main(_tasks(morning_brief=2, mail_deliver_am=0, mail_deliver_pm=0),
                                 run=MagicMock(return_value=_cp(1, "", "traceback")))
        self.assertIn("❌ `Morning Brief (7am)` — was missing, ran now (FAILED)", notify.call_args[1]["body"])
        self.assertIn("stderr: traceback", out)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_dead_mans_switch"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr); self.assertEqual(r.stdout, "")


if __name__ == "__main__":
    unittest.main()
