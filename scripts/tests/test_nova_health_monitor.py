#!/usr/bin/env python3
"""Tests for nova_health_monitor.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_health_monitor.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with patch("psycopg2.connect", side_effect=OSError("offline test")):
    hm = _load("health_monitor_t", SCRIPT)
SRC = SCRIPT.read_text()
# Stub every outbound path at load: Slack DM, memory server, brctl; temp iCloud dir + state file.
DMS = []
hm.nova_config = types.SimpleNamespace(post_both=lambda t, **k: DMS.append((t, k)), JORDAN_DM="DM-JORDAN")
URLOPEN = MagicMock()
hm.urllib = types.SimpleNamespace(request=types.SimpleNamespace(Request=urllib.request.Request, urlopen=URLOPEN))
hm.subprocess = MagicMock()
hm.log = lambda m: None
_TMP = tempfile.TemporaryDirectory()
hm.ICLOUD_HEALTH = Path(_TMP.name) / "health"
hm.STATE_FILE = Path(_TMP.name) / "state.json"


def _offline(argv):
    code = ("import sys,runpy,psycopg2;sys.path.insert(0,'.');"
            "psycopg2.connect=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
            f"sys.argv={[str(SCRIPT)] + argv!r};runpy.run_path(sys.argv[0],run_name='__main__')")
    return subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                          timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})


def _drop(readings, name=None):
    hm.ICLOUD_HEALTH.mkdir(parents=True, exist_ok=True)
    for f in hm.ICLOUD_HEALTH.glob("*"):
        f.unlink()
    (hm.ICLOUD_HEALTH / (name or f"health-{hm.TODAY}.json")).write_text(json.dumps({"readings": readings}))


def _bodies():
    return [json.loads(c.args[0].data) for c in URLOPEN.call_args_list]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_no_cloud(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r"openrouter\.ai|api\.openai|anthropic\.com", SRC))

    def test_alerts_go_to_dm_only(self):
        DMS.clear()
        hm.slack_dm("x")
        self.assertEqual(DMS[0][1], {"slack_channel": "DM-JORDAN"})
        self.assertNotIn("SLACK_CHAT", SRC)


class TestPerformance(unittest.TestCase):
    def test_summarize_and_alert_10k(self):
        r = {"heart_rate": [{"value": 60 + i % 40, "unit": "bpm"} for i in range(10_000)],
             "sleep": [{"stage": "deep", "duration_min": 1} for _ in range(10_000)]}
        t0 = time.perf_counter()
        hm.summarize_readings(r); hm.check_alerts(r)
        self.assertLess(time.perf_counter() - t0, 1.0)


class TestRetry(unittest.TestCase):
    def test_vector_writes_fail_open(self):
        # RETRY GAP: vector_remember / vector_remember_async — one POST each, failures swallowed
        URLOPEN.reset_mock(); URLOPEN.side_effect = OSError("memory server down")
        try:
            hm.vector_remember("t"); hm.vector_remember_async("t")
        finally:
            URLOPEN.side_effect = None
        self.assertEqual(URLOPEN.call_count, 2)

    def test_icloud_placeholders_trigger_download(self):
        hm.ICLOUD_HEALTH.mkdir(parents=True, exist_ok=True)
        for f in hm.ICLOUD_HEALTH.glob("*"):
            f.unlink()
        (hm.ICLOUD_HEALTH / ".health-x.json.icloud").write_text("")
        hm.subprocess.reset_mock()
        self.assertIsNone(hm.read_health_data())
        self.assertEqual(hm.subprocess.run.call_args.args[0][:2], ["brctl", "download"])


class TestUnit(unittest.TestCase):
    def test_check_alerts_thresholds(self):
        a = hm.check_alerts({"heart_rate": [{"value": 130, "unit": "bpm"}], "blood_oxygen": [{"value": 90}],
                             "blood_glucose": [{"value": 100}], "steps": [{"value": 1}]})
        self.assertEqual(len(a), 2)
        self.assertIn("Heart rate is HIGH", a[0])
        self.assertEqual(hm.check_alerts({}), [])

    def test_summarize_sleep_and_single(self):
        s = hm.summarize_readings({"sleep": [{"stage": "deep", "duration_min": 60}, {"stage": "rem", "duration_min": 30},
                                             {"stage": "awake", "duration_min": 99}],
                                   "hrv": [{"value": 42, "unit": "ms"}], "empty": []})
        self.assertEqual(s, ["Sleep: 1.5 hours total (60 min deep, 30 min REM)", "Hrv: 42 ms"])

    def test_state_defaults_on_corrupt(self):
        hm.STATE_FILE.write_text("{nope")
        self.assertEqual(hm.load_state(), {"last_ingest": "", "last_alert_date": ""})


class TestIntegration(unittest.TestCase):
    def test_auto_export_format_parsed(self):
        hm.ICLOUD_HEALTH.mkdir(parents=True, exist_ok=True)
        for f in hm.ICLOUD_HEALTH.glob("*"):
            f.unlink()
        (hm.ICLOUD_HEALTH / "HealthAutoExport-a.json").write_text(json.dumps({"data": {"metrics": [
            {"name": "heart_rate", "units": "bpm", "data": [{"date": "2026-01-01 08:00", "qty": 70}, {"qty": None}]}]}}))
        d = hm.read_health_data(hours=24)
        self.assertEqual(d["readings"]["heart_rate"], [{"value": 70, "unit": "bpm", "date": "2026-01-01 08:00", "source": ""}])

    def test_memory_writes_use_apple_health_source(self):
        URLOPEN.reset_mock()
        hm.vector_remember("x", {"a": 1})
        self.assertEqual(URLOPEN.call_args.args[0].full_url, hm.VECTOR_URL)
        self.assertEqual(_bodies()[0]["source"], "apple_health")


class TestFunctional(unittest.TestCase):
    def test_ingest_stores_and_alerts_once_per_day(self):
        _drop({"heart_rate": [{"value": 140, "unit": "bpm", "date": hm.TODAY}], "steps": [{"value": 5}]})
        hm.STATE_FILE.write_text(json.dumps({"last_ingest": "", "last_alert_date": ""}))
        URLOPEN.reset_mock(); DMS.clear()
        hm.ingest()
        self.assertTrue(_bodies()[0]["text"].startswith(f"Health readings for {hm.TODAY}"))
        self.assertEqual(len(_bodies()), 2)                  # daily summary + heart_rate (steps skipped)
        self.assertIn("Heart rate is HIGH", DMS[0][0])
        hm.ingest()
        self.assertEqual(len(DMS), 1)                        # deduped by last_alert_date
        self.assertEqual(json.loads(hm.STATE_FILE.read_text())["last_alert_date"], hm.TODAY)

    def test_ingest_without_folder_does_nothing(self):
        for f in hm.ICLOUD_HEALTH.glob("*"):
            f.unlink()
        hm.ICLOUD_HEALTH.rmdir()
        URLOPEN.reset_mock(); DMS.clear()
        hm.ingest()
        self.assertEqual((URLOPEN.call_count, DMS), (0, []))

    def test_trends_prints_direction(self):
        _drop({"heart_rate": [{"value": v, "unit": "bpm"} for v in (60, 60, 80, 80)]})
        with redirect_stdout(io.StringIO()) as out:
            hm.trends(7)
        self.assertIn("trending UP", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = _offline(["--help"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--trends", r.stdout)

    def test_main_is_guarded(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
