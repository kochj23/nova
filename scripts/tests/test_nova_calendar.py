#!/usr/bin/env python3
"""Tests for nova_calendar.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_calendar.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="nova_calendar_test_"))

# Frame helper: run the script as __main__ with subprocess.run stubbed so the import-time
# Keychain lookup (_get_ics_url) never reaches the real `security` binary.
_FRAME = ("import subprocess, sys, runpy\n"
          "def _off(*a, **k): raise OSError('offline')\n"
          "subprocess.run = _off\n"
          "sys.argv = [sys.argv[1], '--help']\n"
          "runpy.run_path(sys.argv[0], run_name='__main__')\n")


def _load():
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    spec = importlib.util.spec_from_file_location("ncal", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_notify": nn}), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")), \
         patch("subprocess.run", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    mod.STATE_FILE = TMP / "state.json"
    mod._ICS_CACHE_FILE = TMP / "cache.json"
    mod.ICS_URL = "https://calendar.invalid/x.ics"
    mod.notify = MagicMock(return_value=True)
    mod.nova_config = types.SimpleNamespace(post_both=MagicMock(), SLACK_FEED="C_FEED", JORDAN_DM="D_TEST")
    return mod


cal = _load()


def _ics(*events):
    body = "".join(f"BEGIN:VEVENT\r\nSUMMARY:{t}\r\nDTSTART:{s}\r\nDTEND:{e}\r\nEND:VEVENT\r\n" for t, s, e in events)
    return f"BEGIN:VCALENDAR\r\n{body}END:VCALENDAR\r\n"


def _resp(text):
    r = MagicMock(); r.read.return_value = text.encode()
    r.__enter__ = lambda s: s; r.__exit__ = lambda s, *a: False
    return r


def _ev(title, mins, **kw):
    start = (cal.NOW + timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%S")
    return {"title": title, "start": start, **kw}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_ics_url(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotRegex(SRC, r"https://outlook\.office365\.com/owa/calendar/[0-9a-f]")

    def test_ics_url_comes_from_keychain(self):
        self.assertIn('"nova-calendar-ics-url"', SRC)
        with patch.object(cal.subprocess, "run", return_value=MagicMock(returncode=0, stdout="https://x/y.ics\n")) as run:
            self.assertEqual(cal._get_ics_url(), "https://x/y.ics")
        self.assertEqual(run.call_args[0][0][:2], ["security", "find-generic-password"])

    def test_keychain_miss_returns_empty(self):
        with patch.object(cal.subprocess, "run", return_value=MagicMock(returncode=44, stdout="")):
            self.assertEqual(cal._get_ics_url(), "")


class TestPerformance(unittest.TestCase):
    def test_parse_and_dedup_10k_events(self):
        text = _ics(*[(f"Mtg {i % 500}", f"20260101T{(i % 24):02d}0000", "20260101T235900") for i in range(10_000)])
        t0 = time.perf_counter()
        evs = cal._parse_ics(text)
        uniq = cal._deduplicate_events(evs)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(evs), 10_000)
        self.assertLess(len(uniq), len(evs))


class TestRetry(unittest.TestCase):
    def test_fetch_failure_falls_back_to_stale_cache(self):
        # RETRY GAP: fetch_calendar_events — one ICS GET; on failure it serves the stale cache
        cal._ICS_CACHE_FILE.write_text(json.dumps({"ts": 0, "data": {"events": [{"title": "cached"}], "calendars": []}}))
        with patch.object(cal.urllib.request, "urlopen", side_effect=OSError("down")) as uo, redirect_stdout(io.StringIO()):
            data = cal.fetch_calendar_events()
        self.assertEqual(uo.call_count, 1)
        self.assertEqual(data["events"][0]["title"], "cached")

    def test_fetch_failure_without_cache_is_empty(self):
        cal._ICS_CACHE_FILE.unlink(missing_ok=True)
        with patch.object(cal.urllib.request, "urlopen", side_effect=OSError("down")), redirect_stdout(io.StringIO()):
            self.assertEqual(cal.fetch_calendar_events(), {"events": [], "calendars": []})


class TestUnit(unittest.TestCase):
    def test_parse_ics_datetime_forms(self):
        self.assertEqual(cal._parse_ics_datetime("20260414T090000").hour, 9)
        self.assertEqual(cal._parse_ics_datetime("America/Los_Angeles:20260414T090000").hour, 9)
        self.assertEqual(cal._parse_ics_datetime("20260414").day, 14)
        self.assertIsNone(cal._parse_ics_datetime("garbage"))

    def test_parse_ics_folding_and_fields(self):
        text = ("BEGIN:VEVENT\nSUMMARY:Long\n  title\\, here\nDTSTART;VALUE=DATE:20260414\n"
                "LOCATION:Room 1\nX-MICROSOFT-CDO-BUSYSTATUS:free\nEND:VEVENT\n")
        ev = cal._parse_ics(text)[0]
        self.assertEqual(ev["title"], "Long title, here")
        self.assertTrue(ev["allDay"])
        self.assertEqual((ev["location"], ev["busystatus"]), ("Room 1", "FREE"))

    def test_junk_and_dedup(self):
        self.assertTrue(cal._is_junk_event({"title": "Busy"}))
        self.assertTrue(cal._is_junk_event({"title": "Real", "busystatus": "FREE"}))
        self.assertFalse(cal._is_junk_event({"title": "Standup"}))
        out = cal._deduplicate_events([{"title": "Sync", "start": "s"}, {"title": "FW: Sync", "start": "s"}])
        self.assertEqual(len(out), 1)

    def test_format_helpers(self):
        self.assertEqual(cal.format_time("2026-04-12T14:30:00"), "2:30 PM")
        self.assertEqual(cal.format_time("nope"), "nope")
        self.assertIsNone(cal.minutes_until(None))
        self.assertIn("All day", cal.format_event_line({"title": "Holiday", "allDay": True}))


class TestIntegration(unittest.TestCase):
    def test_fetch_parses_sorts_and_caches(self):
        cal._ICS_CACHE_FILE.unlink(missing_ok=True)
        text = _ics(("B", "20260101T100000", "20260101T110000"), ("A", "20260101T080000", "20260101T090000"))
        with patch.object(cal.urllib.request, "urlopen", return_value=_resp(text)):
            data = cal.fetch_calendar_events()
        self.assertEqual([e["title"] for e in data["events"]], ["A", "B"])
        self.assertTrue(cal._ICS_CACHE_FILE.exists())
        with patch.object(cal.urllib.request, "urlopen", side_effect=AssertionError("cache must be hit")):
            self.assertEqual(cal.fetch_calendar_events()["events"][0]["title"], "A")


class TestFunctional(unittest.TestCase):
    def test_upcoming_alert_dms_once_per_event(self):
        noon = patch.object(cal, "NOW", cal.NOW.replace(hour=12, minute=0))   # never straddle midnight
        noon.start(); self.addCleanup(noon.stop)
        cal.STATE_FILE.unlink(missing_ok=True)
        cal.nova_config.post_both.reset_mock()
        evs = {"events": [_ev("1:1 with boss", 10, location="Zoom"), _ev("Later", 300)], "calendars": []}
        with patch.object(cal, "fetch_calendar_events", return_value=evs):
            cal.check_upcoming_alerts()
            cal.check_upcoming_alerts()
        cal.nova_config.post_both.assert_called_once()
        text, kw = cal.nova_config.post_both.call_args[0][0], cal.nova_config.post_both.call_args.kwargs
        self.assertIn("1:1 with boss", text); self.assertNotIn("Later", text)
        self.assertEqual(kw["slack_channel"], "D_TEST")

    def test_digest_main_notifies_with_content_hash(self):
        cal.notify.reset_mock()
        evs = {"events": [_ev("Planning", 60)], "calendars": [{"calendar": "c", "account": "Office 365"}]}
        with patch.object(cal, "fetch_calendar_events", return_value=evs), patch.object(cal, "vector_remember") as vr, \
             patch.object(sys, "argv", ["nova_calendar.py"]), redirect_stdout(io.StringIO()):
            cal.main()
        kw = cal.notify.call_args.kwargs
        self.assertRegex(kw["dedup_key"], rf"^calendar-digest-{cal.TODAY}-[0-9a-f]{{10}}$")
        self.assertEqual(kw["category"], "calendar")
        vr.assert_called_once()

    def test_empty_calendar_digest(self):
        with patch.object(cal, "fetch_calendar_events", return_value={"events": [], "calendars": []}):
            self.assertIn("No events today or tomorrow", cal.calendar_digest())


class TestFrame(unittest.TestCase):
    def test_help_exits_zero_offline(self):
        r = subprocess.run([sys.executable, "-c", _FRAME, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           cwd=str(SCRIPTS), env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--alerts", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        m = _load()
        m.notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
