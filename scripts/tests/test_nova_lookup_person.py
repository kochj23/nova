#!/usr/bin/env python3
"""Tests for nova_lookup_person.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_lookup_person.py"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lp = _load("lookup_person_t", SCRIPT)
SRC = SCRIPT.read_text()

PEOPLE = [{"id": "p1", "name": "Jesse Smith ", "title": "SRE", "department": "Ops", "email": "j@example.com",
           "meetingFrequency": "weekly", "lastMeetingDate": "2026-01-02T10:00:00"},
          {"id": "p2", "name": "Dan Mick", "title": "Dev"}]
MEETINGS = [{"title": "1:1", "date": "2026-01-02T10:00", "attendees": ["p1", "zz"], "notes": "N" * 700,
             "actionItems": [{"title": "ship it"}, {"title": ""}]},
            {"title": "Other", "attendees": ["p2"]}]


def _api(people=PEOPLE, meetings=MEETINGS):
    return patch.object(lp, "get", side_effect=lambda path: people if path == "/people" else meetings)


def _main(*argv):
    with patch.object(sys, "argv", ["x", *argv]), redirect_stdout(io.StringIO()) as out:
        try:
            lp.main(); code = 0
        except SystemExit as e:
            code = e.code
    return code, json.loads(out.getvalue())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_only_talks_to_loopback_app(self):
        self.assertTrue(lp.ONEONONE.startswith("http://127.0.0.1:"))
        self.assertEqual(re.findall(r"https?://[^\s\"']+", SRC.split('"""', 2)[2]), [lp.ONEONONE])


class TestPerformance(unittest.TestCase):
    def test_find_person_on_10k_people(self):
        people = [{"id": str(i), "name": f"Person{i} Surname{i % 97}"} for i in range(10_000)]
        t0 = time.perf_counter()
        lp.find_person("Person42 Surname42", people)
        self.assertLess(time.perf_counter() - t0, 5.0)


class TestRetry(unittest.TestCase):
    def test_get_fails_open(self):
        # RETRY GAP: get — one HTTP attempt to the OneOnOne app; failure returns None, never raises
        with patch("urllib.request.urlopen", side_effect=OSError("refused")) as uo:
            self.assertIsNone(lp.get("/people"))
        self.assertEqual(uo.call_count, 1)

    def test_app_down_reports_error_json(self):
        with patch.object(lp, "get", return_value=None):
            code, out = _main("Jesse")
        self.assertEqual(code, 1)
        self.assertIn("not running", out["error"])


class TestUnit(unittest.TestCase):
    def test_find_person_partial_and_none(self):
        m = lp.find_person("jesse", PEOPLE)
        self.assertEqual(m[0][1]["id"], "p1")
        self.assertGreaterEqual(m[0][0], 0.75)
        self.assertEqual(lp.find_person("Zebulon Q", PEOPLE), [])
        self.assertEqual(lp.find_person("x", []), [])

    def test_format_meeting_truncates_and_names(self):
        txt = lp.format_meeting(MEETINGS[0], {"p1": "Jesse Smith"})
        self.assertIn("Attendees: Jesse Smith, zz", txt)
        self.assertIn("Action items: ship it", txt)
        self.assertTrue(txt.endswith("..."))
        self.assertEqual(lp.format_meeting({}, {}), "Meeting: Untitled ()")


class TestIntegration(unittest.TestCase):
    def test_meetings_filtered_by_attendee_id(self):
        self.assertEqual(lp.get_person_meetings("p1", MEETINGS, {}), [MEETINGS[0]])
        self.assertEqual(lp.get_person_meetings("nobody", MEETINGS, {}), [])

    def test_get_parses_json_from_app(self):
        r = MagicMock(); r.read.return_value = b'[{"id": "x"}]'
        r.__enter__ = lambda s: s; r.__exit__ = lambda *a: False
        with patch("urllib.request.urlopen", return_value=r) as uo:
            self.assertEqual(lp.get("/people"), [{"id": "x"}])
        self.assertEqual(uo.call_args.args[0], f"{lp.ONEONONE}/people")


class TestFunctional(unittest.TestCase):
    def test_golden_path(self):
        with _api():
            code, out = _main("Jesse", "Smith")
        self.assertEqual(code, 0)
        self.assertTrue(out["found"])
        top = out["matches"][0]
        self.assertEqual((top["name"], top["meeting_count"], top["last_meeting"]), ("Jesse Smith", 1, "2026-01-02"))

    def test_no_match_lists_contacts(self):
        with _api():
            code, out = _main("Quentin")
        self.assertFalse(out["found"])
        self.assertEqual(out["all_contacts"], ["Jesse Smith", "Dan Mick"])


class TestFrame(unittest.TestCase):
    def test_no_args_prints_usage_without_network(self):
        # the script has no --help (any argument is a name to look up); bare invocation is the safe smoke
        r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 1)
        self.assertIn("Usage", json.loads(r.stdout)["error"])

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_lookup_person"], cwd=str(SCRIPTS), capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""), r.stderr)


if __name__ == "__main__":
    unittest.main()
