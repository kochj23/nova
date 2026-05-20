"""
test_nova_lookup_person.py — All 7 test categories for nova_lookup_person.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_lookup_person.py"
_spec = importlib.util.spec_from_file_location("nova_lookup_person", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

similarity = _mod.similarity
find_person = _mod.find_person
format_meeting = _mod.format_meeting
get_person_meetings = _mod.get_person_meetings


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        forbidden = ["sk-", "ghp_", "AKIA"]
        for p in forbidden:
            self.assertNotIn(p, src, f"Credential: {p!r}")

    def test_no_pii_email_literals(self):
        src = _SCRIPT.read_text()
        _at = "@"
        pii = [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp.com",
        ]
        for p in pii:
            self.assertNotIn(p, src, f"PII: {p!r}")

    def test_oneonone_api_local(self):
        self.assertIn("127.0.0.1", _mod.ONEONONE)

    def test_notes_truncated_in_output(self):
        """format_meeting must truncate long notes to 600 chars."""
        meeting = {
            "title": "1:1 with Alice",
            "date": "2026-05-13",
            "notes": "N" * 1000,
            "actionItems": [],
            "attendees": [],
        }
        result = format_meeting(meeting, {})
        # Notes should appear but be truncated
        self.assertIn("...", result, "Long notes must be truncated with ...")
        # The displayed notes should not exceed 600 + some buffer
        notes_start = result.find("Notes:")
        if notes_start >= 0:
            notes_section = result[notes_start:]
            self.assertLessEqual(len(notes_section), 700)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_similarity_fast(self):
        start = time.perf_counter()
        for _ in range(5000):
            similarity("Alice Smith", "alice smith")
            similarity("Bob Jones", "Robert Jones")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"similarity 10000x: {elapsed:.3f}s")

    def test_find_person_fast_on_large_list(self):
        people = [{"name": f"Person {i}", "id": str(i)} for i in range(200)]
        start = time.perf_counter()
        find_person("Person 100", people)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"find_person 200 people: {elapsed:.3f}s")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_get_returns_none_on_network_error(self):
        """get() must return None when OneOnOne is unavailable."""
        with patch("urllib.request.urlopen", side_effect=OSError("refused")):
            result = _mod.get("/people")
        self.assertIsNone(result)

    def test_main_handles_api_unavailable(self):
        """main() must output error JSON when OneOnOne is down."""
        with patch("sys.argv", ["nova_lookup_person.py", "Alice Smith"]):
            with patch.object(_mod, "get", return_value=None):
                with patch("builtins.print") as mock_print:
                    with self.assertRaises(SystemExit):
                        _mod.main()
                # Should have printed an error
                self.assertTrue(any("error" in str(c).lower()
                                    for c in mock_print.call_args_list))


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_similarity_exact_match(self):
        self.assertAlmostEqual(similarity("alice", "alice"), 1.0)

    def test_similarity_case_insensitive(self):
        self.assertAlmostEqual(similarity("Alice", "alice"), 1.0)

    def test_similarity_different_strings(self):
        self.assertLess(similarity("alice", "bob"), 0.5)

    def test_find_person_exact_match(self):
        people = [
            {"name": "Alice Smith", "id": "1"},
            {"name": "Bob Jones", "id": "2"},
        ]
        matches = find_person("Alice Smith", people)
        self.assertGreater(len(matches), 0)
        self.assertAlmostEqual(matches[0][0], 1.0)

    def test_find_person_fuzzy_match(self):
        people = [
            {"name": "Alice Smith", "id": "1"},
            {"name": "Bob Jones", "id": "2"},
        ]
        matches = find_person("Alise Smith", people)
        # Should still find Alice with fuzzy match
        self.assertGreater(len(matches), 0)

    def test_find_person_no_match(self):
        people = [{"name": "Alice Smith", "id": "1"}]
        matches = find_person("XYZ NonExistent", people)
        self.assertEqual(len(matches), 0)

    def test_find_person_partial_first_name(self):
        people = [{"name": "Alice Smith", "id": "1"}]
        matches = find_person("alice", people)
        # Should match on first name
        self.assertGreater(len(matches), 0)

    def test_get_person_meetings_filters_by_attendee(self):
        meetings = [
            {"id": "m1", "attendees": ["p1", "p2"], "title": "1:1"},
            {"id": "m2", "attendees": ["p3"], "title": "Other"},
        ]
        result = get_person_meetings("p1", meetings, {})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "m1")

    def test_format_meeting_basic(self):
        meeting = {
            "title": "1:1 Review",
            "date": "2026-05-13",
            "notes": "Good meeting.",
            "actionItems": [],
            "attendees": ["p1"],
        }
        id_to_name = {"p1": "Alice"}
        result = format_meeting(meeting, id_to_name)
        self.assertIn("1:1 Review", result)
        self.assertIn("2026-05-13", result)
        self.assertIn("Good meeting.", result)

    def test_format_meeting_skips_blank_notes(self):
        meeting = {
            "title": "Review",
            "date": "2026-05-13",
            "notes": "\n\n\n\n\n\n\n",
            "actionItems": [],
            "attendees": [],
        }
        result = format_meeting(meeting, {})
        self.assertNotIn("Notes:", result)

    def test_format_meeting_shows_action_items(self):
        meeting = {
            "title": "Review",
            "date": "2026-05-13",
            "notes": "",
            "actionItems": [{"title": "Follow up on PR"}, {"title": "Schedule next"}],
            "attendees": [],
        }
        result = format_meeting(meeting, {})
        self.assertIn("Follow up on PR", result)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_outputs_found_json(self):
        people = [{"id": "p1", "name": "Alice Smith", "title": "Engineer",
                    "department": "Eng", "email": "alice@example.com",
                    "meetingFrequency": "Weekly", "lastMeetingDate": "2026-05-01"}]
        meetings = [{"id": "m1", "attendees": ["p1"], "title": "1:1 Alice",
                      "date": "2026-05-01", "notes": "Good progress.", "actionItems": []}]

        captured = []
        with patch("sys.argv", ["nova_lookup_person.py", "Alice Smith"]):
            with patch.object(_mod, "get", side_effect=[people, meetings]):
                with patch("builtins.print", side_effect=lambda s: captured.append(s)):
                    _mod.main()

        self.assertGreater(len(captured), 0)
        output = json.loads(captured[0])
        self.assertTrue(output.get("found"))
        self.assertIn("Alice Smith", output["matches"][0]["name"])

    def test_main_outputs_not_found_json(self):
        people = [{"id": "p1", "name": "Bob Jones", "title": "", "department": "",
                    "email": "", "meetingFrequency": "", "lastMeetingDate": ""}]
        meetings = []

        captured = []
        with patch("sys.argv", ["nova_lookup_person.py", "Nonexistent Person"]):
            with patch.object(_mod, "get", side_effect=[people, meetings]):
                with patch("builtins.print", side_effect=lambda s: captured.append(s)):
                    _mod.main()

        self.assertGreater(len(captured), 0)
        output = json.loads(captured[0])
        self.assertFalse(output.get("found"))


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_main_exits_on_no_args(self):
        with patch("sys.argv", ["nova_lookup_person.py"]):
            with self.assertRaises(SystemExit) as ctx:
                _mod.main()
        self.assertEqual(ctx.exception.code, 1)

    def test_find_person_caps_results_at_top_matches(self):
        """find_person should return only matches above threshold."""
        people = [{"name": f"Person {i}", "id": str(i)} for i in range(50)]
        matches = find_person("Person 1", people)
        # All matches must be above threshold (0.5)
        for score, p in matches:
            self.assertGreater(score, 0.5)

    def test_get_person_meetings_empty_when_not_attendee(self):
        meetings = [{"id": "m1", "attendees": ["p2"], "title": "Other 1:1"}]
        result = get_person_meetings("p1", meetings, {})
        self.assertEqual(len(result), 0)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"nova_lookup_person.py has syntax errors: {e}")

    def test_constants_present(self):
        self.assertIsInstance(_mod.ONEONONE, str)
        self.assertIn("37400", _mod.ONEONONE)

    def test_all_functions_callable(self):
        for fn in [similarity, find_person, format_meeting,
                    get_person_meetings, _mod.get, _mod.main]:
            self.assertTrue(callable(fn))

    def test_script_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
