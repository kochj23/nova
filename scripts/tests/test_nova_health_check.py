#!/usr/bin/env python3
"""Tests for nova_health_check.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_health_check.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_health_check_test_"))


def _load():
    import nova_config
    spec = importlib.util.spec_from_file_location("nhealthcheck", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.object(nova_config, "slack_bot_token", return_value="xoxb-test"), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    mod.JOBS_FILE = TMP / "cron" / "jobs.json"
    mod.nova_config = types.SimpleNamespace(post_both=MagicMock(), SLACK_BB="C_BB")
    return mod


hc = _load()


def _resp(obj):
    r = MagicMock(); r.read.return_value = json.dumps(obj).encode()
    r.__enter__ = lambda s: s; r.__exit__ = lambda s, *a: False
    return r


def _sched(tasks):
    return patch.object(hc.urllib.request, "urlopen", return_value=_resp(tasks))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"xox[bp]-\d")
        self.assertIn("SLACK_TOKEN   = nova_config.slack_bot_token()", SRC)

    def test_scheduler_api_is_loopback(self):
        self.assertTrue(hc.SCHEDULER_API.startswith("http://127.0.0.1:"))

    def test_token_only_in_auth_header(self):
        with patch.object(hc.urllib.request, "urlopen", return_value=_resp({"ok": True, "messages": []})) as uo:
            hc.fetch_recent_slack_messages()
        req = uo.call_args[0][0]
        self.assertNotIn(hc.SLACK_TOKEN, req.full_url)
        self.assertEqual(req.get_header("Authorization"), f"Bearer {hc.SLACK_TOKEN}")


class TestPerformance(unittest.TestCase):
    def test_audit_10k_scheduler_tasks(self):
        now = time.time()
        tasks = {f"t{i}": {"schedule": "cron 0 6 * * *", "last_run": now - 100, "last_duration": 5,
                           "last_exit_code": 0, "consecutive_failures": i % 3} for i in range(10_000)}
        t0 = time.perf_counter()
        with _sched(tasks), redirect_stdout(io.StringIO()):
            issues = hc.audit_jobs()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(issues), sum(1 for i in range(10_000) if i % 3 == 2))


class TestRetry(unittest.TestCase):
    def test_scheduler_down_falls_back_to_jobs_json(self):
        # RETRY GAP: audit_jobs/scheduler API — one GET; on failure it falls back to cron/jobs.json
        hc.JOBS_FILE.unlink(missing_ok=True)
        with patch.object(hc.urllib.request, "urlopen", side_effect=OSError("refused")) as uo, redirect_stdout(io.StringIO()):
            issues = hc.audit_jobs()
        self.assertEqual(uo.call_count, 1)
        self.assertEqual(issues[0]["severity"], "critical")
        self.assertEqual(issues[0]["name"], "cron/jobs.json")

    def test_slack_history_failure_returns_empty(self):
        with patch.object(hc.urllib.request, "urlopen", side_effect=OSError("down")), redirect_stdout(io.StringIO()):
            self.assertEqual(hc.fetch_recent_slack_messages(), [])
        with patch.object(hc.urllib.request, "urlopen", return_value=_resp({"ok": False, "error": "ratelimited"})), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(hc.fetch_recent_slack_messages(), [])


class TestUnit(unittest.TestCase):
    def test_scheduler_rules(self):
        now = time.time()
        tasks = {
            "broken": {"consecutive_failures": 3, "last_exit_code": 1},
            "empty_promise": {"schedule": "cron 0 6 * * *", "last_run": now, "last_duration": 0.01, "last_exit_code": 0},
            "home_watchdog": {"schedule": "cron */5 * * * *", "last_run": now, "last_duration": 0.01, "last_exit_code": 0},
            "stale_daily": {"schedule": "cron 0 6 * * *", "last_run": now - 30 * 3600, "last_duration": 9, "last_exit_code": 0},
            "weekly_thing": {"schedule": "cron 0 6 * * 1", "last_run": now - 100 * 3600, "last_duration": 9, "last_exit_code": 0},
            "off": {"enabled": False, "consecutive_failures": 9},
        }
        with _sched(tasks):
            names = {i["name"]: i["severity"] for i in hc.audit_jobs()}
        self.assertEqual(names, {"broken": "error", "empty_promise": "warning", "stale_daily": "warning"})

    def test_run_history_counts_trailing_errors(self):
        runs = hc.JOBS_FILE.parent / "runs"; runs.mkdir(parents=True, exist_ok=True)
        lines = [{"action": "finished", "ts": 1, "status": "ok"}, {"action": "finished", "ts": 2, "status": "error", "error": "e1"},
                 {"action": "started", "ts": 3}, {"action": "finished", "ts": 4, "status": "error", "error": "e2"}]
        (runs / "job1.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\nnot json\n")
        h = hc._load_run_history("job1")
        self.assertEqual((h["consecutiveErrors"], h["lastError"], h["lastRunAtMs"]), (2, "e2", 4))
        self.assertEqual(hc._load_run_history("missing"), {})

    def test_format_message(self):
        self.assertIn("All cron jobs running normally", hc.format_message([]))
        m = hc.format_message([{"severity": "error", "name": "a", "reason": "r"},
                               {"severity": "warning", "name": "b", "reason": "w"}])
        self.assertIn("1 error:", m); self.assertIn("1 warning:", m)


class TestIntegration(unittest.TestCase):
    def test_legacy_jobs_json_with_run_history(self):
        hc.JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        hc.JOBS_FILE.write_text(json.dumps({"jobs": [
            {"id": "job1", "name": "Nightly"}, {"name": "Fast", "state": {"lastRunStatus": "ok", "lastDurationMs": 10}},
            {"name": "Nova Home Watchdog", "state": {"lastRunStatus": "ok", "lastDurationMs": 10}}]}))
        runs = hc.JOBS_FILE.parent / "runs"; runs.mkdir(exist_ok=True)
        (runs / "job1.jsonl").write_text(json.dumps({"action": "finished", "ts": 1, "status": "error", "error": "boom"}) + "\n")
        with patch.object(hc.urllib.request, "urlopen", side_effect=OSError("down")), redirect_stdout(io.StringIO()):
            issues = {i["name"]: i["reason"] for i in hc.audit_jobs()}
        self.assertIn("boom", issues["Nightly"])
        self.assertIn("empty promise", issues["Fast"])
        self.assertNotIn("Nova Home Watchdog", issues)

    def test_delivery_audit_from_scheduler(self):
        now = time.time()
        tasks = {"morning_brief": {"last_run": now, "last_exit_code": 0},
                 "mail_deliver_midday": {"last_run": now, "last_exit_code": 2}}
        with _sched(tasks):
            issues = {i["name"]: i["reason"] for i in hc.audit_slack_deliveries()}
        self.assertIn("failed (exit 2)", issues["Slack delivery: Mail Summary"])
        self.assertIn("expected within 20h", issues["Slack delivery: Nightly Report"])
        self.assertNotIn("Slack delivery: Morning Brief", issues)


class TestFunctional(unittest.TestCase):
    def test_main_posts_summary_to_critical_channel(self):
        hc.nova_config.post_both.reset_mock()
        with _sched({"broken": {"consecutive_failures": 5, "last_exit_code": 1}}), redirect_stdout(io.StringIO()):
            hc.main()
        text = hc.nova_config.post_both.call_args[0][0]
        self.assertIn("`broken` — 5 consecutive failures", text)
        self.assertEqual(hc.nova_config.post_both.call_args.kwargs["slack_channel"], "C_BB")

    def test_main_all_clear_still_posts(self):
        hc.nova_config.post_both.reset_mock()
        now = time.time()
        ok = {k: {"last_run": now, "last_exit_code": 0} for k in ("morning_brief", "mail_deliver_midday", "nightly_report")}
        with _sched(ok), redirect_stdout(io.StringIO()):
            hc.main()
        self.assertIn("All cron jobs running normally", hc.nova_config.post_both.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_import_smoke_without_keychain(self):
        # no --help: a bare run audits and posts to Slack, so the frame check is an import with Keychain stubbed
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import nova_config; "
                "nova_config.slack_bot_token = lambda: ''; import nova_health_check as h; print(h.STALE_HOURS)")
        r = subprocess.run([sys.executable, "-c", code, str(SCRIPTS)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "26")

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        m = _load()
        m.nova_config.post_both.assert_not_called()


if __name__ == "__main__":
    unittest.main()
