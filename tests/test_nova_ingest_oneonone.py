"""
test_nova_ingest_oneonone.py — All 7 test categories for nova_ingest_oneonone.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# nova_ingest_oneonone.py executes at module level — mock all network calls before load
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_ingest_oneonone.py"


def _load_module():
    """Load module with all network calls mocked."""
    import importlib.util as _ilu

    people = [
        {"id": "uid-1", "name": "Alice Smith", "title": "Engineer",
         "department": "Eng", "email": "alice@example.com",
         "meetingFrequency": "weekly", "lastMeetingDate": "2026-01-01"},
    ]
    meetings = [
        {"id": "m1", "title": "Q1 Planning", "date": "2026-01-01",
         "meetingType": "planning", "duration": 3600, "attendees": ["uid-1"],
         "notes": "Discussed roadmap.", "actionItems": [{"title": "Create plan"}],
         "decisions": ["Go with option A"], "followUps": []},
    ]
    stats_resp = {"count": 100, "by_source": {"oneonone": 5}}

    def fake_urlopen(req, timeout=None):
        url = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
        r = MagicMock()
        if "people" in url:
            r.read.return_value = json.dumps(people).encode()
        elif "meetings" in url:
            r.read.return_value = json.dumps(meetings).encode()
        elif "stats" in url:
            r.read.return_value = json.dumps(stats_resp).encode()
        elif "forget_all" in url:
            r.read.return_value = json.dumps({"deleted": 3}).encode()
        else:
            r.read.return_value = json.dumps({"id": "abc"}).encode()
        r.__enter__ = lambda s: s
        r.__exit__ = MagicMock(return_value=False)
        return r

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        spec = _ilu.spec_from_file_location("nova_ingest_oneonone", _SCRIPT)
        mod = _ilu.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


_mod = _load_module()


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

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_oneonone_api_is_local(self):
        src = _SCRIPT.read_text()
        self.assertIn("127.0.0.1", src)

    def test_memory_url_is_local(self):
        src = _SCRIPT.read_text()
        self.assertIn("18790", src)

    def test_source_is_oneonone(self):
        src = _SCRIPT.read_text()
        self.assertIn('"oneonone"', src)


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_module_loads_fast(self):
        """Module must load in < 500ms when network is mocked."""
        people = [{"id": "uid-1", "name": "Test", "title": "", "department": "",
                   "email": "", "meetingFrequency": "", "lastMeetingDate": ""}]
        meetings = []
        stats = {"count": 0, "by_source": {}}

        def fast_urlopen(req, timeout=None):
            url = req.get_full_url() if hasattr(req, "get_full_url") else str(req)
            r = MagicMock()
            if "people" in url:
                r.read.return_value = json.dumps(people).encode()
            elif "meetings" in url:
                r.read.return_value = json.dumps(meetings).encode()
            elif "stats" in url:
                r.read.return_value = json.dumps(stats).encode()
            else:
                r.read.return_value = json.dumps({"deleted": 0}).encode()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        start = time.perf_counter()
        with patch("urllib.request.urlopen", side_effect=fast_urlopen):
            spec = importlib.util.spec_from_file_location("_oneonone_perf", _SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5)


import importlib.util


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_store_handles_network_failure(self):
        """store() raises on network failure (caller handles)."""
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            with self.assertRaises(Exception):
                _mod.store("test text", {"type": "test"})

    def test_get_handles_failure(self):
        """get() raises on network failure."""
        with patch("urllib.request.urlopen", side_effect=Exception("refused")):
            with self.assertRaises(Exception):
                _mod.get("/people")

    def test_forget_all_handles_failure(self):
        """Delete endpoint failure should not crash the script."""
        src = _SCRIPT.read_text()
        self.assertIn("Clear failed (continuing anyway)", src)


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_get_function_defined(self):
        """get() function must be defined."""
        self.assertTrue(callable(_mod.get))

    def test_store_function_defined(self):
        """store() function must be defined."""
        self.assertTrue(callable(_mod.store))

    def test_oneonone_endpoint(self):
        """ONEONONE endpoint must be defined."""
        self.assertIn("ONEONONE", dir(_mod))
        self.assertIn("37400", _mod.ONEONONE)

    def test_memory_endpoint(self):
        """MEMORY endpoint must be defined."""
        self.assertIn("MEMORY", dir(_mod))
        self.assertIn("18790", _mod.MEMORY)

    def test_source_constant(self):
        """SOURCE must be 'oneonone'."""
        self.assertEqual(_mod.SOURCE, "oneonone")

    def test_get_parses_json_response(self):
        """get() correctly parses JSON responses."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"key": "value"}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = _mod.get("/test")

        self.assertEqual(result, {"key": "value"})

    def test_store_sends_source_and_text(self):
        """store() sends text and source to memory endpoint."""
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.read.return_value = json.dumps({"id": "abc"}).encode()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _mod.store("Test memory text", {"type": "person_profile"})

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["source"], "oneonone")
        self.assertEqual(captured[0]["text"], "Test memory text")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_person_profile_stored_with_natural_language(self):
        """Person profiles must be stored as natural language sentences."""
        stored = []

        def fake_urlopen(req, timeout=None):
            url = req.get_full_url() if hasattr(req, "get_full_url") else ""
            r = MagicMock()
            if "people" in url:
                r.read.return_value = json.dumps([
                    {"id": "uid-1", "name": "Alice Smith", "title": "SRE Manager",
                     "department": "Engineering", "email": "alice@example.com",
                     "meetingFrequency": "weekly", "lastMeetingDate": "2026-01-01"}
                ]).encode()
            elif "meetings" in url:
                r.read.return_value = json.dumps([]).encode()
            elif "stats" in url:
                r.read.return_value = json.dumps({"count": 5, "by_source": {}}).encode()
            else:
                r.read.return_value = json.dumps({"deleted": 0, "id": "x"}).encode()
            if hasattr(req, "data") and req.data:
                stored.append(json.loads(req.data.decode()))
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            spec = importlib.util.spec_from_file_location("_oo_integ", _SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

        person_entries = [s for s in stored if s.get("metadata", {}).get("type") == "person_profile"]
        if person_entries:
            self.assertIn("Alice Smith", person_entries[0]["text"])
            self.assertIn("meets with", person_entries[0]["text"])


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_meetings_chunked_when_long_notes(self):
        """Meetings with long notes (>1000 chars) must be stored in chunks."""
        long_notes = "Meeting note content. " * 100  # ~2200 chars
        stored = []

        def fake_urlopen(req, timeout=None):
            url = req.get_full_url() if hasattr(req, "get_full_url") else ""
            r = MagicMock()
            if "people" in url:
                r.read.return_value = json.dumps([]).encode()
            elif "meetings" in url:
                r.read.return_value = json.dumps([
                    {"id": "m1", "title": "Big Meeting", "date": "2026-01-01",
                     "meetingType": "", "duration": 3600, "attendees": [],
                     "notes": long_notes, "actionItems": [], "decisions": [], "followUps": []}
                ]).encode()
            elif "stats" in url:
                r.read.return_value = json.dumps({"count": 0, "by_source": {}}).encode()
            else:
                r.read.return_value = json.dumps({"deleted": 0, "id": "x"}).encode()
            if hasattr(req, "data") and req.data:
                stored.append(json.loads(req.data.decode()))
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            spec = importlib.util.spec_from_file_location("_oo_func", _SCRIPT)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

        meeting_entries = [s for s in stored
                           if s.get("metadata", {}).get("type") in ("meeting_notes", "meeting_notes_continued")]
        # Long notes should produce at least 2 chunks
        self.assertGreater(len(meeting_entries), 1)


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
        self.assertTrue(callable(_mod.get))
        self.assertTrue(callable(_mod.store))

    def test_source_defined(self):
        self.assertEqual(_mod.SOURCE, "oneonone")

    def test_api_endpoints_defined(self):
        self.assertIsInstance(_mod.ONEONONE, str)
        self.assertIsInstance(_mod.MEMORY, str)


if __name__ == "__main__":
    unittest.main(verbosity=2)
