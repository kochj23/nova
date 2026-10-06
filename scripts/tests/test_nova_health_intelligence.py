#!/usr/bin/env python3
"""Tests for nova_health_intelligence.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import ExitStack, contextmanager, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_health_intelligence.py"
SRC = SCRIPT.read_text()


@contextmanager
def _stub_modules(**mods):
    """Install import stubs and afterwards restore ONLY those keys (a whole-dict restore would drop every module
    the script imported for the first time, leaving e.g. a second `urllib` package without `.request`)."""
    mp = pytest.MonkeyPatch()
    for k, v in mods.items():
        mp.setitem(sys.modules, k, v)
    try:
        yield
    finally:
        mp.undo()


def _stub_config():
    cfg = types.ModuleType("nova_config")
    cfg.VECTOR_URL = "http://memory.test/remember"; cfg.JORDAN_DM = "D_TEST_DM"
    cfg.post_both = MagicMock()
    return cfg


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with _stub_modules(nova_config=_stub_config()):   # bound at import; only that key restored
        spec.loader.exec_module(mod)
    return mod


hi = _load("hi", SCRIPT)


def _day(n):
    return (date.today() - timedelta(days=n)).isoformat()


def _series(days, **by_type):
    """{date: {type: [values]}} for the last `days` days; by_type maps type -> callable(i) or constant."""
    out = {}
    for i in range(days - 1, -1, -1):
        out[_day(i)] = {t: [v(i) if callable(v) else v] for t, v in by_type.items()}
    return out


class _Env:
    """Redirect ICLOUD_HEALTH + STATE_FILE into a tempdir and silence Slack/vector/gh/log for one test."""
    def __init__(self, files=None):
        self.files = files or {}

    def __enter__(self):
        self.stack = ExitStack()
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        (root / "health").mkdir(); (root / "state").mkdir()
        for name, data in self.files.items():
            (root / "health" / name).write_text(data if isinstance(data, str) else json.dumps(data))
        self.post = MagicMock(); self.urlopen = MagicMock(); self.out = io.StringIO()
        for cm in (patch.object(hi, "ICLOUD_HEALTH", root / "health"),
                   patch.object(hi, "STATE_FILE", root / "state" / "st.json"),
                   patch.object(hi.nova_config, "post_both", self.post),
                   patch.object(hi.urllib.request, "urlopen", self.urlopen),
                   patch.object(hi, "get_coding_days", MagicMock(return_value=set())),
                   redirect_stdout(self.out)):
            self.stack.enter_context(cm)
        self.state_file = root / "state" / "st.json"
        return self

    def __exit__(self, *a):
        self.stack.close()
        return False


def _hfile(n, **readings):
    d = _day(n)
    return f"health-{d}.json", {"readings": {t: [{"date": d, "value": v}] for t, v in readings.items()}}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_sql(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM|psycopg2)\b", SRC))

    def test_health_alerts_go_only_to_jordans_dm(self):
        hi.slack_dm("hello")
        hi.nova_config.post_both.assert_called_once_with("hello", slack_channel="D_TEST_DM")
        self.assertEqual(SRC.count("post_both("), 1)
        self.assertNotIn("openrouter_api_key", SRC)          # the docstring promise: never touches OpenRouter

    def test_vector_memory_url_comes_from_nova_config(self):
        self.assertIn("VECTOR_URL = nova_config.VECTOR_URL", SRC)
        self.assertEqual(hi.VECTOR_URL, "http://memory.test/remember")

    def test_github_lookup_is_an_argv_list_without_shell(self):
        with patch.object(hi.subprocess, "run", return_value=MagicMock(returncode=0, stdout="[]")) as run:
            self.assertEqual(hi.get_coding_days(), set())
        self.assertEqual(run.call_args.args[0][:2], ["gh", "api"])
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_malformed_health_files_are_skipped_not_executed(self):
        with _Env({"health-%s.json" % _day(1): "{not json", "health-%s.json" % _day(0): {"readings": {"steps": {"value": 5}}}}):
            data = hi.load_health_days(14)
        self.assertEqual(data, {_day(0): {"steps": [5]}})


class TestPerformance(unittest.TestCase):
    def test_trends_and_averages_over_10k_days_are_fast(self):
        data = _series(10_000, resting_heart_rate=lambda i: 60 + (i % 7), blood_pressure_sys=120, hrv=40, steps=9000)
        t0 = time.perf_counter()
        for _ in range(3):
            hi.detect_trends(data)
        avgs = hi.daily_averages(data, "resting_heart_rate")
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(avgs), 10_000)


class TestRetry(unittest.TestCase):
    def test_vector_remember_is_one_shot_and_fails_open(self):
        # RETRY GAP: vector_remember() — single POST, exception swallowed, nothing returned
        with patch.object(hi.urllib.request, "urlopen", side_effect=OSError("down")) as u:
            self.assertIsNone(hi.vector_remember("t", {"a": 1}))
        self.assertEqual(u.call_count, 1)

    def test_calendar_recall_is_one_shot_and_fails_open(self):
        # RETRY GAP: get_calendar_days() — one recall GET, falls back to an empty set
        cal = types.ModuleType("nova_calendar"); cal.get_todays_events = MagicMock()
        with _stub_modules(nova_calendar=cal), \
             patch.object(hi.urllib.request, "urlopen", side_effect=OSError("down")) as u:
            self.assertEqual(hi.get_calendar_days(), set())
        self.assertEqual(u.call_count, 1)

    def test_coding_days_is_one_shot_and_fails_open(self):
        # RETRY GAP: get_coding_days() — one `gh api`, timeout/non-zero -> empty set
        with patch.object(hi.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 15)) as run:
            self.assertEqual(hi.get_coding_days(), set())
        self.assertEqual(run.call_count, 1)
        with patch.object(hi.subprocess, "run", return_value=MagicMock(returncode=1, stdout="")):
            self.assertEqual(hi.get_coding_days(), set())

    def test_slack_failure_does_not_lose_the_dedup_state(self):
        # RETRY GAP: slack_dm()/post_both — not retried; the exception escapes daily_analysis() BEFORE save_state,
        # so the alert is NOT marked sent and will be re-attempted on the next run (fail-safe for the human)
        files = dict(_hfile(n, resting_heart_rate=70 + (4 - n) * 4) for n in range(5))
        with _Env(files) as env:
            env.post.side_effect = RuntimeError("slack 500")
            with self.assertRaises(RuntimeError):
                hi.daily_analysis()
            self.assertFalse(env.state_file.exists())


class TestUnit(unittest.TestCase):
    def test_daily_averages_edges(self):
        self.assertEqual(hi.daily_averages({}, "hrv"), {})
        data = {"2026-01-02": {"hrv": [10, 20]}, "2026-01-01": {"hrv": []}, "2026-01-03": {"steps": [1]}}
        self.assertEqual(hi.daily_averages(data, "hrv"), {"2026-01-02": 15.0})

    def test_detect_trends_needs_three_days(self):
        self.assertEqual(hi.detect_trends({}), [])
        self.assertEqual(hi.detect_trends(_series(2, resting_heart_rate=100)), [])

    def test_rising_resting_hr_alert(self):
        alerts = hi.detect_trends(_series(5, resting_heart_rate=lambda i: 60 + (4 - i) * 3))   # +12 over 5 days
        kinds = {(a["type"], a["pattern"]) for a in alerts}
        self.assertIn(("resting_heart_rate", "rising"), kinds)
        a = next(a for a in alerts if a["pattern"] == "rising")
        self.assertEqual(a["severity"], "warning"); self.assertAlmostEqual(a["change"], 12.0)
        self.assertIn("trending UP over the last 5 days (+12.0 bpm", a["message"])
        self.assertEqual(a["advice"], hi.TREND_ALERTS["resting_heart_rate"]["advice"])

    def test_falling_hrv_and_sustained_low_spo2(self):
        alerts = hi.detect_trends(_series(5, hrv=lambda i: 50 - (4 - i) * 4, blood_oxygen=93))
        kinds = {(a["type"], a["pattern"]) for a in alerts}
        self.assertIn(("hrv", "falling"), kinds)
        self.assertIn(("blood_oxygen", "sustained_low"), kinds)
        self.assertNotIn(("blood_oxygen", "falling"), kinds)
        low = next(a for a in alerts if a["pattern"] == "sustained_low")
        self.assertEqual(low["severity"], "concern"); self.assertEqual(low["current_avg"], 93)

    def test_flat_healthy_vitals_raise_nothing(self):
        data = _series(14, resting_heart_rate=62, blood_pressure_sys=118, blood_pressure_dia=76, hrv=45, blood_oxygen=98, weight=180)
        self.assertEqual(hi.detect_trends(data), [])

    def test_weekend_days_are_computed_at_call_time(self):
        w = hi.get_weekend_days(14)
        self.assertEqual(len(w), 4)
        self.assertTrue(all(date.fromisoformat(d).weekday() >= 5 for d in w))
        self.assertEqual(hi.get_weekend_days(0), set())

    def test_state_round_trip_and_missing_file_default(self):
        with _Env() as env:
            self.assertEqual(hi.load_state(), {"sent_alerts": set(), "last_daily": "", "last_weekly": ""})
            hi.save_state({"sent_alerts": {"k1", "k2"}, "last_daily": "x"})
            raw = json.loads(env.state_file.read_text())
            self.assertEqual(sorted(raw["sent_alerts"]), ["k1", "k2"])
            self.assertEqual(hi.load_state()["sent_alerts"], {"k1", "k2"})
            env.state_file.write_text("garbage")
            self.assertEqual(hi.load_state()["sent_alerts"], set())

    def test_load_health_days_honours_the_cutoff_and_missing_dir(self):
        files = dict([_hfile(1, steps=100), _hfile(20, steps=999)])
        with _Env(files):
            self.assertEqual(hi.load_health_days(14), {_day(1): {"steps": [100]}})
            self.assertEqual(len(hi.load_health_days(30)), 2)
        with patch.object(hi, "ICLOUD_HEALTH", Path(tempfile.gettempdir()) / "does-not-exist-nova"):
            self.assertEqual(hi.load_health_days(), {})

    def test_cross_reference_sleep_vs_meetings_and_bp_weekends(self):
        data = _series(14, sleep=lambda i: 8.0 if i % 2 else 6.0, blood_pressure_sys=lambda i: 130, steps=5000)
        meetings = {_day(i - 1) for i in range(14) if i % 2 == 0 and i >= 1}  # meeting the morning after short nights
        for d in data:
            if date.fromisoformat(d).weekday() >= 5:
                data[d]["blood_pressure_sys"] = [110]
        with patch.object(hi, "get_calendar_days", return_value=meetings), patch.object(hi, "get_coding_days", return_value=set()):
            corr = hi.cross_reference(data)
        self.assertTrue(any("hours less* on nights before meeting days" in c for c in corr), corr)
        self.assertTrue(any("mmHg higher* on weekdays vs weekends" in c for c in corr), corr)
        self.assertEqual(hi.cross_reference({}), [])


class TestIntegration(unittest.TestCase):
    def test_files_to_trends_chain_produces_alert_shape(self):
        files = dict(_hfile(n, resting_heart_rate=70 + (4 - n) * 4) for n in range(5))
        with _Env(files):
            data = hi.load_health_days(14)
            alerts = hi.detect_trends(data)
        self.assertEqual(len(data), 5)
        self.assertTrue(alerts)
        self.assertEqual(set(alerts[0]) >= {"type", "pattern", "label", "message", "advice", "severity", "current_avg"}, True)

    def test_daily_analysis_dedups_through_the_state_file(self):
        files = dict(_hfile(n, resting_heart_rate=70 + (4 - n) * 4) for n in range(5))
        with _Env(files) as env:
            hi.daily_analysis(); hi.daily_analysis()
            self.assertEqual(env.post.call_count, 1)
            keys = json.loads(env.state_file.read_text())["sent_alerts"]
        self.assertTrue(all(k.startswith(f"{hi.TODAY}_resting_heart_rate_") for k in keys), keys)

    def test_alert_thresholds_cover_every_vital_in_both_directions(self):
        for name, cfg in hi.TREND_ALERTS.items():
            self.assertTrue({"window_days", "label", "unit", "advice"} <= set(cfg), name)
            self.assertTrue(cfg.get("rising_threshold") or cfg.get("falling_threshold"), name)


class TestFunctional(unittest.TestCase):
    def test_daily_golden_path_posts_and_remembers(self):
        files = dict(_hfile(n, resting_heart_rate=70 + (4 - n) * 4, blood_oxygen=98) for n in range(5))
        with _Env(files) as env:
            hi.daily_analysis()
            msg = env.post.call_args.args[0]
            self.assertTrue(msg.startswith("*Health Intelligence*\n  ! *Resting heart rate* has been trending UP"))
            self.assertIn("_Consider checking in with your doctor", msg)
            self.assertEqual(env.post.call_args.kwargs, {"slack_channel": "D_TEST_DM"})
            body = json.loads(env.urlopen.call_args.args[0].data)
            self.assertEqual(body["source"], "health_intelligence")
            self.assertEqual(body["metadata"]["type"], "health_trend_alert")
            self.assertTrue(env.state_file.exists())
            self.assertIn("Sent 1 trend alert(s)", env.out.getvalue())

    def test_daily_with_no_data_posts_nothing(self):
        with _Env() as env:
            hi.daily_analysis()
            env.post.assert_not_called(); env.urlopen.assert_not_called()
            self.assertIn("No health data available.", env.out.getvalue())

    def test_weekly_report_golden_path(self):
        files = dict(_hfile(n, resting_heart_rate=62, blood_pressure_sys=118, weight=180) for n in range(10))
        with _Env(files) as env:
            cal = types.ModuleType("nova_calendar"); cal.get_todays_events = MagicMock()
            with _stub_modules(nova_calendar=cal):
                hi.weekly_intelligence()
            msg = env.post.call_args.args[0]
        self.assertTrue(msg.startswith("*Weekly Health Intelligence — "))
        self.assertIn("Resting Heart Rate: 62.0 →", msg)
        self.assertIn("_All vitals stable. No concerning patterns detected._", msg)
        self.assertIn("_Based on 10 days of health data_", msg)

    def test_weekly_error_path_memory_server_down_still_posts(self):
        files = dict(_hfile(n, resting_heart_rate=62) for n in range(4))
        with _Env(files) as env:
            env.urlopen.side_effect = OSError("down")
            hi.weekly_intelligence()
            self.assertEqual(env.post.call_count, 1)
            self.assertIn("Weekly report posted", env.out.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        for flag in ("--daily", "--weekly", "--correlations", "--trends"):
            self.assertIn(flag, r.stdout)

    def test_import_never_runs_analysis(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_health_intelligence"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
