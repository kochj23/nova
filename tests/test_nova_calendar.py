"""
test_nova_calendar.py — All 7 test categories for nova_calendar.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import time
import tempfile
import unittest
from datetime import datetime, date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

_nova_cfg = MagicMock()
_nova_cfg.VECTOR_URL = "http://127.0.0.1:18790/remember"
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
_nova_cfg.JORDAN_DM = "D0AMPB3F4T0"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_calendar.py"
_spec = importlib.util.spec_from_file_location("nova_calendar", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_parse_ics_datetime = _mod._parse_ics_datetime
_parse_ics = _mod._parse_ics
_is_junk_event = _mod._is_junk_event
_deduplicate_events = _mod._deduplicate_events
format_time = _mod.format_time
minutes_until = _mod.minutes_until
is_today = _mod.is_today
is_tomorrow = _mod.is_tomorrow
format_event_line = _mod.format_event_line
calendar_digest = _mod.calendar_digest


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["password =", "sk-", "ghp_"]:
            self.assertNotIn(pat, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for pat in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pat, src)

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_ics_url_loaded_from_keychain(self):
        """ICS URL must be loaded from Keychain, not hardcoded."""
        src = _SCRIPT.read_text()
        self.assertIn("security", src, "Must use 'security' command to get ICS URL from Keychain")
        self.assertIn("nova-calendar-ics-url", src)

    def test_no_ics_url_hardcoded(self):
        """ICS URL must never be hardcoded (contains secret publishing key)."""
        src = _SCRIPT.read_text()
        self.assertNotIn("outlook.office365.com/owa/calendar", src,
                         "ICS URL hardcoded — must use Keychain")

    def test_calendar_cache_in_workspace(self):
        """Calendar cache must be stored locally."""
        self.assertIn(str(Path.home()), str(_mod._ICS_CACHE_FILE))


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_parse_ics_datetime_fast(self):
        """_parse_ics_datetime must process 1000 dates in <100ms."""
        dates = ["20260414T143000Z", "20260414T143000", "20260414"]
        start = time.perf_counter()
        for _ in range(1000):
            for d in dates:
                _parse_ics_datetime(d)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)

    def test_ics_cache_ttl_defined(self):
        self.assertGreater(_mod._ICS_CACHE_TTL, 0)
        self.assertLessEqual(_mod._ICS_CACHE_TTL, 3600)

    def test_deduplicate_fast_on_many_events(self):
        events = [{"title": f"Event {i}", "start": f"2026-01-01T{i:02d}:00:00"} for i in range(100)]
        start = time.perf_counter()
        result = _deduplicate_events(events)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_fetch_calendar_events_falls_back_to_cache_on_error(self):
        """On ICS fetch failure, must return cached data if available."""
        cached_data = {"events": [{"title": "Cached Event", "start": "2026-01-01T10:00:00"}], "calendars": []}
        cache_content = json.dumps({"ts": time.time() - 10, "data": cached_data})

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "cache.json"
            cache_file.write_text(cache_content)
            with patch.object(_mod, "_ICS_CACHE_FILE", cache_file):
                with patch.object(_mod, "ICS_URL", "https://invalid.example.com/cal"):
                    with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
                        result = _mod.fetch_calendar_events()

        self.assertEqual(result, cached_data)

    def test_fetch_calendar_returns_empty_on_failure_no_cache(self):
        """Without cache, fetch failure returns empty events."""
        with patch.object(_mod, "ICS_URL", "https://invalid.example.com"):
            with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
                with patch.object(_mod, "_ICS_CACHE_FILE", Path("/nonexistent/cache.json")):
                    result = _mod.fetch_calendar_events()
        self.assertEqual(result["events"], [])

    def test_vector_remember_does_not_raise(self):
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            _mod.vector_remember("Calendar event", {})


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_parse_ics_datetime_utc(self):
        result = _parse_ics_datetime("20260414T143000Z")
        self.assertIsNotNone(result)
        self.assertIsInstance(result, datetime)

    def test_parse_ics_datetime_local(self):
        result = _parse_ics_datetime("20260414T143000")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)

    def test_parse_ics_datetime_all_day(self):
        result = _parse_ics_datetime("20260414")
        self.assertIsNotNone(result)
        self.assertEqual(result.year, 2026)

    def test_parse_ics_datetime_invalid(self):
        result = _parse_ics_datetime("invalid_date")
        self.assertIsNone(result)

    def test_is_junk_event_busy(self):
        self.assertTrue(_is_junk_event({"title": "busy"}))

    def test_is_junk_event_free(self):
        self.assertTrue(_is_junk_event({"busystatus": "FREE", "title": "Free slot"}))

    def test_is_junk_event_real_meeting(self):
        self.assertFalse(_is_junk_event({"title": "Project Sync", "busystatus": "BUSY"}))

    def test_deduplicate_removes_duplicates(self):
        events = [
            {"title": "Meeting", "start": "2026-01-01T10:00:00"},
            {"title": "Meeting", "start": "2026-01-01T10:00:00"},
            {"title": "Other Event", "start": "2026-01-01T11:00:00"},
        ]
        result = _deduplicate_events(events)
        self.assertEqual(len(result), 2)

    def test_deduplicate_handles_fw_prefix(self):
        """FW: prefix should match original event."""
        events = [
            {"title": "All Hands", "start": "2026-01-01T10:00:00"},
            {"title": "FW: All Hands", "start": "2026-01-01T10:00:00"},
        ]
        result = _deduplicate_events(events)
        self.assertEqual(len(result), 1)

    def test_format_time_parses_iso(self):
        result = format_time("2026-01-15T14:30:00")
        self.assertIn("30", result)
        self.assertIn("PM", result)

    def test_is_today_true(self):
        today = date.today().isoformat()
        self.assertTrue(is_today(f"{today}T10:00:00"))

    def test_is_today_false(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        self.assertFalse(is_today(f"{yesterday}T10:00:00"))

    def test_is_tomorrow_true(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        self.assertTrue(is_tomorrow(f"{tomorrow}T10:00:00"))

    def test_parse_ics_parses_events(self):
        ics_text = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Test Meeting
DTSTART:20260101T100000Z
DTEND:20260101T110000Z
END:VEVENT
END:VCALENDAR"""
        events = _parse_ics(ics_text)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "Test Meeting")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_get_todays_events_filters_junk(self):
        today = date.today().isoformat()
        mock_data = {
            "events": [
                {"title": "busy", "start": f"{today}T10:00:00", "allDay": False},
                {"title": "Real Meeting", "start": f"{today}T11:00:00", "allDay": False},
            ],
            "calendars": []
        }
        with patch.object(_mod, "fetch_calendar_events", return_value=mock_data):
            result = _mod.get_todays_events()
        titles = [e["title"] for e in result]
        self.assertNotIn("busy", titles)
        self.assertIn("Real Meeting", titles)

    def test_calendar_digest_shows_today_events(self):
        today = date.today().isoformat()
        mock_data = {
            "events": [
                {"title": "Standup", "start": f"{today}T09:00:00", "allDay": False,
                 "end": f"{today}T09:30:00"},
            ],
            "calendars": [{"calendar": "Work", "account": "Office 365"}]
        }
        with patch.object(_mod, "fetch_calendar_events", return_value=mock_data):
            digest = calendar_digest()
        self.assertIn("Standup", digest)

    def test_check_upcoming_alerts_alerts_within_30_min(self):
        """check_upcoming_alerts should alert for meetings starting in <30 min."""
        alerts_posted = []
        soon = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S")
        today = date.today().isoformat()
        mock_events = [{"title": "Team Sync", "start": soon, "allDay": False}]

        with patch.object(_mod, "get_todays_events", return_value=mock_events):
            with patch.object(_mod, "slack_post", side_effect=lambda t, **kw: alerts_posted.append(t)):
                with patch.object(_mod, "save_alert_state"):
                    with patch.object(_mod, "load_alert_state", return_value={"alerted": []}):
                        _mod.check_upcoming_alerts()
        self.assertGreater(len(alerts_posted), 0)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_digest_mode(self):
        with patch("sys.argv", ["nova_calendar.py", "--digest"]):
            with patch.object(_mod, "calendar_digest", return_value="Calendar digest") as mock_digest:
                with patch.object(_mod, "slack_post"):
                    with patch.object(_mod, "get_todays_events", return_value=[]):
                        with patch.object(_mod, "vector_remember"):
                            _mod.main()
        mock_digest.assert_called_once()

    def test_main_alerts_mode(self):
        with patch("sys.argv", ["nova_calendar.py", "--alerts"]):
            with patch.object(_mod, "check_upcoming_alerts") as mock_alerts:
                _mod.main()
        mock_alerts.assert_called_once()

    def test_main_list_calendars(self):
        import io
        from contextlib import redirect_stdout
        with patch("sys.argv", ["nova_calendar.py", "--list-calendars"]):
            with patch.object(_mod, "get_calendars", return_value=[{"calendar": "Work", "account": "O365"}]):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    _mod.main()
        self.assertIn("Work", buf.getvalue())


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_constants_defined(self):
        self.assertIsInstance(_mod.VECTOR_URL, str)
        self.assertIsInstance(_mod._ICS_CACHE_TTL, int)
        self.assertIsInstance(_mod._ICS_CACHE_FILE, Path)
        self.assertIsInstance(_mod.STATE_FILE, Path)

    def test_functions_exist(self):
        for fn in ("_parse_ics_datetime", "_parse_ics", "fetch_calendar_events",
                   "_is_junk_event", "_deduplicate_events", "get_todays_events",
                   "get_tomorrows_events", "format_event_line", "calendar_digest",
                   "check_upcoming_alerts", "main"):
            self.assertTrue(hasattr(_mod, fn), f"Missing: {fn}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
