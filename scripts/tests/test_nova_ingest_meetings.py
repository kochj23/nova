#!/usr/bin/env python3
"""Tests for nova_ingest_meetings.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
The OneOnOne API and memory server (fetch_json/post_json/urlopen) are mocked; nothing is ingested."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_ingest_meetings.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_ingest_meetings_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


im = _load()
PEOPLE = [{"id": "uuid-aaaa-1111", "name": "Ann", "title": "SRE"}, {"id": "uuid-bbbb-2222", "name": "Bob"}]


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def _meetings():
    return [
        {"id": "m1", "title": "1:1 Ann", "date": _iso(1), "notes": "talked", "attendees": ["uuid-aaaa-1111"],
         "duration": 1800, "meetingType": "1on1", "actionItems": [{"text": "ship it"}], "tags": ["t"]},
        {"id": "m2", "title": "Old", "date": _iso(60), "notes": "ancient"},
        {"id": "m3", "title": "Empty", "date": _iso(1)},
    ]


def _fetch(url):
    return PEOPLE if url.endswith("/api/people") else _meetings()


def _resp(obj):
    m = MagicMock()
    m.__enter__.return_value.read.return_value = json.dumps(obj).encode()
    return m


class TestSecurity(unittest.TestCase):
    def test_no_credentials(self):
        self.assertIsNone(re.search(r"(password|api[_-]?key|token|secret)\s*=\s*['\"]", SRC, re.I))

    def test_oneonone_api_is_loopback(self):
        self.assertTrue(im.ONEONONE_API.startswith("http://127.0.0.1"))

    def test_dry_run_never_posts(self):
        with patch.object(im, "fetch_json", side_effect=_fetch), patch.object(im, "post_json") as pj, \
             patch.object(sys, "argv", ["x", "--dry-run"]), patch("builtins.print"):
            im.main()
        pj.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_format_10k_meetings_fast(self):
        m = {"title": "t", "date": "2026-01-01T10:00:00Z", "notes": "n", "actionItems": ["a"] * 5}
        t0 = time.perf_counter()
        for _ in range(10_000):
            im.format_meeting_text(m, ["Ann"])
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_post_failure_is_per_meeting_and_continues(self):
        # RETRY GAP: main()/post_json — one POST per meeting; failure logged to stderr, loop continues
        with patch.object(im, "fetch_json", side_effect=_fetch), \
             patch.object(im, "post_json", side_effect=OSError("memory down")) as pj, \
             patch.object(sys, "argv", ["x"]), patch("builtins.print") as p:
            im.main()
        self.assertEqual(pj.call_count, 2)
        self.assertIn("Ingested: 0, Skipped (empty): 1", p.call_args_list[-1][0][0])

    def test_fetch_failure_propagates(self):
        # RETRY GAP: fetch_people() — single GET, no retry; error escapes (cron re-runs)
        with patch.object(im.urllib.request, "urlopen", side_effect=OSError("app closed")) as u:
            with self.assertRaises(OSError):
                im.fetch_people()
        self.assertEqual(u.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_resolve_attendees(self):
        pm = {p["id"]: p for p in PEOPLE}
        self.assertEqual(im.resolve_attendees(["uuid-aaaa-1111", "uuid-bbbb-2222", "zzzzzzzzzz"], pm),
                         ["Ann (SRE)", "Bob", "zzzzzzzz"])
        self.assertEqual(im.resolve_attendees([], pm), [])

    def test_format_minimal_and_bad_date(self):
        self.assertEqual(im.format_meeting_text({}, []), "Meeting: Untitled Meeting\nDate: unknown date")
        self.assertIn("Date: 2026-13-99", im.format_meeting_text({"date": "2026-13-99Tjunk"}, []))

    def test_format_full(self):
        t = im.format_meeting_text({"title": "X", "date": "2026-02-03T04:05:00Z", "duration": 3600,
                                    "meetingType": "staff", "notes": "N", "actionItems": [{"title": "a"}],
                                    "decisions": ["d"], "followUps": [{"text": "f"}]}, ["Ann"])
        for s in ("Date: 2026-02-03 04:05", "Type: staff", "Duration: 60 minutes", "Attendees: Ann",
                  "Notes:\nN", "  - a", "Decisions:\n  - d", "Follow-ups:\n  - f"):
            self.assertIn(s, t)


class TestIntegration(unittest.TestCase):
    def test_post_payload_shape(self):
        with patch.object(im, "fetch_json", side_effect=_fetch), \
             patch.object(im, "post_json", return_value={"id": "abcdef123456"}) as pj, \
             patch.object(sys, "argv", ["x", "--since", "7"]), patch("builtins.print"):
            im.main()
        self.assertEqual(pj.call_count, 1)
        url, body = pj.call_args[0]
        self.assertEqual(url, f"{im.MEMORY_API}/remember")
        self.assertEqual(body["source"], "oneonone_meetings")
        self.assertEqual(body["metadata"]["attendees"], ["Ann (SRE)"])
        self.assertIn("ship it", body["text"])

    def test_post_json_sends_json(self):
        with patch.object(im.urllib.request, "urlopen", return_value=_resp({"id": "x"})) as u:
            self.assertEqual(im.post_json("http://h/remember", {"a": 1}), {"id": "x"})
        req = u.call_args[0][0]
        self.assertEqual((req.method, json.loads(req.data)), ("POST", {"a": 1}))


class TestFunctional(unittest.TestCase):
    def test_full_run_ingests_nonempty_meetings(self):
        with patch.object(im, "fetch_json", side_effect=_fetch), \
             patch.object(im, "post_json", return_value={"id": "abcdef123456"}) as pj, \
             patch.object(sys, "argv", ["x"]), patch("builtins.print") as p:
            im.main()
        self.assertEqual([c[0][1]["metadata"]["meeting_id"] for c in pj.call_args_list], ["m1", "m2"])
        self.assertIn("Ingested: 2, Skipped (empty): 1", p.call_args_list[-1][0][0])


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--since", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", "import nova_ingest_meetings"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout.strip()), (0, ""))


if __name__ == "__main__":
    unittest.main()
