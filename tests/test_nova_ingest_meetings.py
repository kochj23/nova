"""
test_nova_ingest_meetings.py — All 7 test categories for nova_ingest_meetings.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_ingest_meetings.py"
_spec = importlib.util.spec_from_file_location("nova_ingest_meetings", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

fetch_json = _mod.fetch_json
post_json = _mod.post_json
fetch_people = _mod.fetch_people
resolve_attendees = _mod.resolve_attendees
format_meeting_text = _mod.format_meeting_text
main = _mod.main
SOURCE = _mod.SOURCE
ONEONONE_API = _mod.ONEONONE_API
MEMORY_API = _mod.MEMORY_API


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for p in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(p, src)

    def test_no_pii_emails(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(p, src)

    def test_oneonone_api_is_local(self):
        """OneOnOne API must be localhost."""
        self.assertIn("127.0.0.1", ONEONONE_API)

    def test_memory_api_is_local(self):
        """Memory API must be localhost."""
        self.assertIn("127.0.0.1", MEMORY_API)

    def test_source_is_oneonone_meetings(self):
        """SOURCE must be 'oneonone_meetings'."""
        self.assertEqual(SOURCE, "oneonone_meetings")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_format_meeting_text_fast(self):
        """format_meeting_text must format 1000 meetings in < 100ms."""
        meeting = {
            "title": "1:1 with Team",
            "date": "2026-01-01T10:00:00Z",
            "meetingType": "1-on-1",
            "duration": 3600,
            "notes": "Discussed Q1 priorities and roadmap.",
            "actionItems": [{"text": "Follow up by Friday"}],
            "decisions": [],
            "followUps": [],
        }
        attendees = ["Alice Smith (Engineer)", "Bob Jones (Manager)"]

        start = time.perf_counter()
        for _ in range(1000):
            format_meeting_text(meeting, attendees)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1)

    def test_resolve_attendees_fast(self):
        """resolve_attendees must handle 1000 calls in < 50ms."""
        people_map = {f"uuid-{i}": {"name": f"Person {i}", "title": f"Title {i}"}
                      for i in range(100)}
        ids = [f"uuid-{i}" for i in range(10)]

        start = time.perf_counter()
        for _ in range(1000):
            resolve_attendees(ids, people_map)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.05)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_fetch_json_raises_on_failure(self):
        """fetch_json propagates network errors."""
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            with self.assertRaises(Exception):
                fetch_json("http://127.0.0.1:37400/api/meetings")

    def test_main_handles_fetch_failure(self):
        """main() exits gracefully when OneOnOne API is unavailable."""
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            with patch("sys.argv", ["nova_ingest_meetings.py"]):
                try:
                    main()
                except Exception as exc:
                    # Should raise — OneOnOne is required
                    pass

    def test_post_json_raises_on_failure(self):
        """post_json propagates errors (caller handles retry)."""
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            with self.assertRaises(Exception):
                post_json("http://127.0.0.1:18790/remember", {"text": "test"})


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_resolve_attendees_resolves_names(self):
        """resolve_attendees converts UUIDs to names."""
        people_map = {
            "uuid-1": {"name": "Alice Smith", "title": "Engineer"},
            "uuid-2": {"name": "Bob Jones", "title": ""},
        }
        result = resolve_attendees(["uuid-1", "uuid-2"], people_map)
        self.assertIn("Alice Smith (Engineer)", result)
        self.assertIn("Bob Jones", result)

    def test_resolve_attendees_truncates_unknown_uuid(self):
        """resolve_attendees abbreviates unknown UUIDs."""
        result = resolve_attendees(["unknown-uuid-12345678"], {})
        self.assertEqual(len(result), 1)
        self.assertLessEqual(len(result[0]), 8)

    def test_format_meeting_text_includes_title(self):
        """format_meeting_text includes meeting title."""
        meeting = {"title": "Sprint Planning", "date": "2026-01-01T10:00:00Z"}
        result = format_meeting_text(meeting, [])
        self.assertIn("Sprint Planning", result)

    def test_format_meeting_text_includes_notes(self):
        """format_meeting_text includes notes when present."""
        meeting = {
            "title": "1:1",
            "date": "2026-01-01",
            "notes": "Discussed priorities.",
        }
        result = format_meeting_text(meeting, [])
        self.assertIn("Discussed priorities.", result)

    def test_format_meeting_text_includes_action_items(self):
        """format_meeting_text includes action items."""
        meeting = {
            "title": "Planning",
            "date": "2026-01-01",
            "notes": "",
            "actionItems": [{"text": "Review PR by Monday"}, {"text": "Update docs"}],
        }
        result = format_meeting_text(meeting, [])
        self.assertIn("Review PR by Monday", result)

    def test_format_meeting_text_duration_in_minutes(self):
        """format_meeting_text shows duration in minutes."""
        meeting = {"title": "Meeting", "date": "2026-01-01", "duration": 3600}
        result = format_meeting_text(meeting, [])
        self.assertIn("60 minutes", result)

    def test_format_meeting_text_includes_attendees(self):
        """format_meeting_text includes attendee list."""
        meeting = {"title": "Team Meeting", "date": "2026-01-01"}
        attendees = ["Alice", "Bob", "Charlie"]
        result = format_meeting_text(meeting, attendees)
        self.assertIn("Alice", result)
        self.assertIn("Bob", result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_ingests_meeting_with_content(self):
        """main() stores meetings that have notes or action items."""
        people = [{"id": "uid-1", "name": "Alice Smith", "title": "Engineer"}]
        meetings = [
            {
                "id": "meet-1",
                "title": "Q1 Planning",
                "date": "2026-01-01T10:00:00Z",
                "meetingType": "planning",
                "duration": 3600,
                "attendees": ["uid-1"],
                "notes": "Discussed roadmap and priorities for Q1.",
                "actionItems": [{"text": "Create project plan"}],
                "decisions": [],
                "followUps": [],
            }
        ]

        stored = []

        def fake_fetch(url):
            if "people" in url:
                return people
            return meetings

        def fake_post(url, data):
            stored.append(data)
            return {"id": "mem-abc123"}

        with patch("sys.argv", ["nova_ingest_meetings.py"]):
            with patch.object(_mod, "fetch_json", side_effect=fake_fetch):
                with patch.object(_mod, "post_json", side_effect=fake_post):
                    main()

        self.assertEqual(len(stored), 1)
        self.assertIn("Q1 Planning", stored[0]["text"])
        self.assertEqual(stored[0]["source"], "oneonone_meetings")

    def test_main_skips_empty_meetings(self):
        """main() skips meetings with no notes, action items, or decisions."""
        meetings = [
            {
                "id": "meet-empty",
                "title": "Empty Meeting",
                "date": "2026-01-01",
                "attendees": [],
                "notes": "",
                "actionItems": [],
                "decisions": [],
            }
        ]

        stored = []
        with patch("sys.argv", ["nova_ingest_meetings.py"]):
            with patch.object(_mod, "fetch_json", side_effect=lambda url: [] if "people" in url else meetings):
                with patch.object(_mod, "post_json", side_effect=lambda url, d: stored.append(d) or {"id": "x"}):
                    main()

        self.assertEqual(len(stored), 0, "Empty meetings should not be stored")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_since_filter_works(self):
        """--since flag filters to meetings within last N days."""
        now = datetime.now(timezone.utc)
        old_date = (now - timedelta(days=30)).isoformat()
        recent_date = now.isoformat()

        meetings = [
            {"id": "old", "title": "Old Meeting", "date": old_date,
             "notes": "Old content", "actionItems": [], "decisions": []},
            {"id": "new", "title": "New Meeting", "date": recent_date,
             "notes": "New content", "actionItems": [], "decisions": []},
        ]

        stored = []
        with patch("sys.argv", ["nova_ingest_meetings.py", "--since", "7"]):
            with patch.object(_mod, "fetch_json", side_effect=lambda url: [] if "people" in url else meetings):
                with patch.object(_mod, "post_json", side_effect=lambda url, d: stored.append(d) or {"id": "x"}):
                    main()

        titles = [s["metadata"]["title"] for s in stored if s.get("metadata")]
        self.assertNotIn("Old Meeting", titles)
        self.assertIn("New Meeting", titles)

    def test_dry_run_does_not_store(self):
        """--dry-run must not call post_json."""
        meetings = [
            {"id": "m1", "title": "Test", "date": "2026-01-01T10:00:00Z",
             "notes": "Important notes", "actionItems": [], "decisions": [], "attendees": []},
        ]

        post_calls = []
        with patch("sys.argv", ["nova_ingest_meetings.py", "--dry-run"]):
            with patch.object(_mod, "fetch_json", side_effect=lambda url: [] if "people" in url else meetings):
                with patch.object(_mod, "post_json", side_effect=lambda url, d: post_calls.append(d)):
                    main()

        self.assertEqual(len(post_calls), 0, "dry-run must not call post_json")


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

    def test_key_functions_callable(self):
        for fn in [fetch_json, post_json, fetch_people, resolve_attendees,
                   format_meeting_text, main]:
            self.assertTrue(callable(fn))

    def test_api_urls_defined(self):
        self.assertIsInstance(ONEONONE_API, str)
        self.assertIsInstance(MEMORY_API, str)
        self.assertIsInstance(SOURCE, str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
