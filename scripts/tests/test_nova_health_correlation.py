#!/usr/bin/env python3
"""Tests for nova_health_correlation.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_health_correlation.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


with patch("psycopg2.connect", side_effect=OSError("offline test")):
    hc = _load("health_corr_t", SCRIPT)
SRC = SCRIPT.read_text()
hc.notify = MagicMock()
hc.log = lambda msg: None
_TMP = tempfile.TemporaryDirectory()
hc.HEALTH_DIR = Path(_TMP.name) / "health"     # never the real private health dir


def _offline(argv):
    """Run the script as __main__ in a child with PG + network blocked."""
    code = ("import sys,runpy,psycopg2,urllib.request;sys.path.insert(0,'.');"
            "psycopg2.connect=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
            "urllib.request.urlopen=lambda *a,**k:(_ for _ in ()).throw(OSError('offline'));"
            f"sys.argv={[str(SCRIPT)] + argv!r};runpy.run_path(sys.argv[0],run_name='__main__')")
    return subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                          timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})


def _day(i):
    return (hc.TODAY - timedelta(days=i)).isoformat()


def _write_health(days):
    hc.HEALTH_DIR.mkdir(parents=True, exist_ok=True)
    for f in hc.HEALTH_DIR.glob("*.json"):
        f.unlink()
    for i, rec in days.items():
        (hc.HEALTH_DIR / f"{_day(i)}.json").write_text(json.dumps(rec))


def _urlopen_json(payload):
    resp = MagicMock(); resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s; resp.__exit__ = lambda *a: False
    return resp


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_and_no_cloud_api(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIsNone(re.search(r"openrouter|openai\.com|anthropic\.com|googleapis", SRC, re.I))

    def test_stored_insight_is_local_only(self):
        cap = {}
        def fake(req, timeout=None):
            cap["url"] = req.full_url; cap["body"] = json.loads(req.data)
            return _urlopen_json({})
        with patch("urllib.request.urlopen", side_effect=fake):
            hc.store_insights([{"title": "T", "finding": "*x*"}], 7)
        self.assertEqual(cap["body"]["metadata"]["privacy"], "local-only")
        self.assertEqual(cap["body"]["source"], "health_correlation")
        self.assertTrue(cap["url"].startswith("http://memory-server."))


class TestPerformance(unittest.TestCase):
    def test_correlations_on_10k_days(self):
        health = {(hc.TODAY - timedelta(days=i)).isoformat(): {"sleep_hours": 6 + i % 3, "hrv": 40 + i % 7,
                  "steps": 4000 + i, "resting_heart_rate": 60 + i % 5, "active_energy": 300 + i % 50}
                  for i in range(10_000)}
        cal = {d: i % 4 for i, d in enumerate(health)}
        t0 = time.perf_counter()
        hc.correlate_sleep_vs_meetings(health, cal); hc.correlate_hrv_weekday_weekend(health)
        hc.correlate_steps_vs_coding(health, cal); hc.compute_summaries(health)
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_recall_fails_open(self):
        # RETRY GAP: _recall — one HTTP attempt to the memory server; failure returns [] and never raises
        with patch("urllib.request.urlopen", side_effect=OSError("down")) as uo:
            self.assertEqual(hc._recall("q", "calendar"), [])
        self.assertEqual(uo.call_count, 1)

    def test_store_failure_fails_open(self):
        # RETRY GAP: store_insights — one POST; failure is logged, never raised
        with patch("urllib.request.urlopen", side_effect=OSError("down")) as uo:
            hc.store_insights([{"title": "T", "finding": "f"}], 30)
        self.assertEqual(uo.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_helpers(self):
        self.assertEqual(hc._safe_avg([]), 0.0)
        self.assertEqual(hc._classify_day("2024-06-01"), "weekend")   # a Saturday
        self.assertEqual(hc._classify_day("2024-06-03"), "weekday")

    def test_correlations_need_data(self):
        self.assertIsNone(hc.correlate_sleep_vs_meetings({}, {}))
        self.assertIsNone(hc.correlate_sleep_vs_meetings({"a": {"sleep_hours": 7}}, {"a": 1}))
        self.assertIsNone(hc.correlate_hr_vs_meetings({}, {}))
        self.assertIsNone(hc.correlate_energy_vs_events({}, {"a": 1}))

    def test_steps_vs_coding_detects_drop(self):
        h = {"d1": {"steps": 2000}, "d2": {"steps": 2500}, "d3": {"steps": 9000}}
        r = hc.correlate_steps_vs_coding(h, {"d1": 3, "d2": 1})
        self.assertEqual(r["diff_steps"], -6750)
        self.assertIn("get up and walk", r["finding"])

    def test_recall_caps_n_and_unwraps(self):
        with patch("urllib.request.urlopen", return_value=_urlopen_json({"memories": [{"text": "a"}]})) as uo:
            self.assertEqual(hc._recall("q", "github", n=500), [{"text": "a"}])
        self.assertIn("n=50", uo.call_args.args[0].full_url)


class TestIntegration(unittest.TestCase):
    def test_load_then_correlate(self):
        _write_health({i: {"hrv": 60 if i % 7 in (0, 1) else 40, "junk": "x"} for i in range(14)})
        (hc.HEALTH_DIR / "latest.json").write_text("{}")
        (hc.HEALTH_DIR / "notadate.json").write_text("{}")
        h = hc.load_health_data(30)
        self.assertEqual(len(h), 14)
        self.assertTrue(all(set(v) == {"hrv"} for v in h.values()))
        self.assertIsInstance(hc.correlate_hrv_weekday_weekend(h) or {}, dict)

    def test_calendar_counts_from_memory_source(self):
        items = [{"metadata": {"date": _day(1)}}, {"metadata": {}, "text": f"standup {_day(2)} 9am"},
                 {"metadata": {"date": _day(400)}}]
        with patch.object(hc, "_recall", return_value=items) as rc:
            self.assertEqual(hc.get_calendar_events_by_date(7), {_day(1): 1, _day(2): 1})
        self.assertEqual(rc.call_args.kwargs["source"], "calendar")


class TestFunctional(unittest.TestCase):
    def test_report_golden_path_posts_and_stores(self):
        _write_health({i: {"sleep_hours": 5.0 if i % 2 else 8.0, "resting_heart_rate": 70 if i % 2 else 60}
                       for i in range(6)})
        cal = {_day(i): (5 if i % 2 else 1) for i in range(6)}
        hc.notify.reset_mock()
        with patch.object(hc, "get_calendar_events_by_date", return_value=cal), \
                patch.object(hc, "get_email_volume_by_date", return_value={}), \
                patch.object(hc, "get_coding_activity_by_date", return_value={}), \
                patch.object(hc, "store_insights") as st, patch.object(sys, "argv", ["x"]), \
                redirect_stdout(io.StringIO()) as out:
            hc.main()
        self.assertIn("Sleep vs Meeting Density", out.getvalue())
        self.assertEqual(hc.notify.call_args.kwargs["dedup_key"], "health-correlation-report")
        self.assertEqual(st.call_args.args[1], 7)

    def test_no_data_dry_run_posts_nothing(self):
        _write_health({})
        hc.notify.reset_mock()
        with patch.object(sys, "argv", ["x", "--dry-run", "--monthly"]), redirect_stdout(io.StringIO()) as out:
            hc.main()
        self.assertIn("No health data available for the last 30 days", out.getvalue())
        hc.notify.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = _offline(["--help"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--monthly", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
