#!/usr/bin/env python3
"""Tests for nova_ingest_oneonone.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The script is a run-on-import one-off (no main(), no __main__ guard): it wipes the `oneonone` source, pulls
people + meetings from the local OneOnOne API and re-stores them. Every load here goes through _load(), which
replaces urllib.request.urlopen with an in-memory fake API, so no OneOnOne server, memory server, mailbox or
network is ever touched. Fixture data only."""
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
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_ingest_oneonone.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="oneonone-test-"))

PEOPLE = [{"id": "u1", "name": " Alice Example ", "title": "SRE", "department": "Infra", "email": "alice@example.test",
           "meetingFrequency": "Weekly", "lastMeetingDate": "2026-10-01T10:00:00Z"},
          {"id": "u2", "name": "Bob", "title": "", "department": "", "email": "", "meetingFrequency": "", "lastMeetingDate": ""}]
MEETINGS = [{"title": "Sync", "date": "2026-10-01T10:00:00Z", "notes": "short notes", "meetingType": "1:1", "duration": 1800,
             "attendees": ["u1", "u9"], "actionItems": [{"title": "ship it"}, {"title": ""}], "decisions": ["go"], "followUps": ["check"]},
            {"title": "Empty", "date": "2026-09-01", "notes": "", "attendees": [], "actionItems": [], "decisions": []},
            {"title": "Long", "date": "2026-09-15", "notes": "N" * 3200, "attendees": ["u2"], "actionItems": [], "decisions": []}]


class _Resp:
    def __init__(self, d): self._d = json.dumps(d).encode()
    def read(self): return self._d
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _fake_api(people=PEOPLE, meetings=MEETINGS, forget_exc=None, store_exc=None, get_exc=None, stats=None):
    """urlopen stand-in for the OneOnOne API + memory server; records every call."""
    calls = []

    def urlopen(req, timeout=0):
        url = req if isinstance(req, str) else req.full_url
        method = "GET" if isinstance(req, str) else req.get_method()
        data = None if isinstance(req, str) else (json.loads(req.data) if req.data else None)
        calls.append((method, url, data))
        if "/forget_all" in url:
            if forget_exc: raise forget_exc
            return _Resp({"deleted": 7})
        if "/api/oneonone/people" in url:
            if get_exc: raise get_exc
            return _Resp(people)
        if "/api/oneonone/meetings" in url:
            return _Resp(meetings)
        if url.endswith("/remember"):
            if store_exc: raise store_exc
            return _Resp({"id": len(calls)})
        if url.endswith("/stats"):
            return _Resp(stats or {"by_source": {"oneonone": 5}, "count": 1_600_000})
        raise AssertionError(f"unexpected url {url}")
    return MagicMock(side_effect=urlopen), calls


def _load(name="oneonone_under_test", **kw):
    uo, calls = _fake_api(**kw)
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    with patch("urllib.request.urlopen", uo), redirect_stdout(buf):
        spec.loader.exec_module(mod)
    return mod, calls, buf.getvalue()


M, CALLS, OUT = _load()
STORES = [c for c in CALLS if c[1].endswith("/remember")]


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_sources_are_local_api_and_fleet_memory_server_only(self):
        self.assertEqual(M.ONEONONE, "http://127.0.0.1:37400/api/oneonone")
        for banned in ("Library/Mail", "mailbox", "osascript", "subprocess", "psycopg2", "imaplib"):
            self.assertNotIn(banned, SRC, banned)
        hosts = set(re.findall(r"https?://([^/:\"]+)", SRC))
        self.assertEqual(hosts, {"127.0.0.1", "memory-server.digitalnoise.net"})

    def test_forget_is_scoped_to_the_oneonone_source(self):
        m, url, _ = [c for c in CALLS if "/forget_all" in c[1]][0]
        self.assertEqual(m, "DELETE"); self.assertTrue(url.endswith("/forget_all?source=oneonone"))
        self.assertTrue(all(c[2]["source"] == "oneonone" for c in STORES))

    def test_person_payload_contains_only_profile_fields(self):
        p = STORES[0][2]
        self.assertEqual(set(p), {"text", "source", "metadata"}); self.assertEqual(p["metadata"], {"person": "Alice Example", "type": "person_profile"})


class TestPerformance(unittest.TestCase):
    def test_10k_people_ingest_quickly(self):
        people = [{"id": f"u{i}", "name": f"P{i}", "title": "t", "department": "d", "email": f"p{i}@x.test", "meetingFrequency": "Weekly", "lastMeetingDate": "2026-10-01"} for i in range(10_000)]
        t0 = time.perf_counter()
        mod, calls, out = _load("oneonone_perf", people=people, meetings=[])
        self.assertLess(time.perf_counter() - t0, 8.0)
        self.assertEqual(len([c for c in calls if c[1].endswith("/remember")]), 10_000)
        self.assertEqual(len(mod.id_to_name), 10_000)

    def test_notes_chunking_is_linear(self):
        mod, calls, out = _load("oneonone_chunks", people=[], meetings=[{"title": "Big", "date": "2026-09-15", "notes": "N" * 150_000, "attendees": [], "actionItems": [], "decisions": []}])
        stores = [c for c in calls if c[1].endswith("/remember")]
        self.assertEqual(len(stores), 100)                          # 1500 + 99 x 1500
        self.assertIn("(100 chunks)", out)


class TestRetry(unittest.TestCase):
    def test_forget_failure_is_swallowed_and_ingest_continues(self):
        # RETRY GAP: forget_all — one DELETE attempt; failure prints and continues (stale chunks may remain)
        mod, calls, out = _load("oneonone_forget_fail", forget_exc=OSError("memory down"))
        self.assertIn("Clear failed (continuing anyway): memory down", out)
        self.assertEqual(len([c for c in calls if c[1].endswith("/remember")]), len(STORES))

    def test_api_failure_aborts_before_any_store(self):
        # RETRY GAP: get — a single urlopen; the OneOnOne API being down raises and nothing is stored
        with self.assertRaises(OSError):
            _load("oneonone_get_fail", get_exc=OSError("api down"))
        uo, calls = _fake_api(get_exc=OSError("api down"))
        spec = importlib.util.spec_from_file_location("oneonone_get_fail2", SCRIPT); mod = importlib.util.module_from_spec(spec)
        with patch("urllib.request.urlopen", uo), redirect_stdout(io.StringIO()):
            with self.assertRaises(OSError):
                spec.loader.exec_module(mod)
        self.assertEqual([c for c in calls if c[1].endswith("/remember")], [])

    def test_store_failure_propagates_mid_run(self):
        # RETRY GAP: store — one POST per chunk; a memory-server error escapes and the run dies partially ingested
        with self.assertRaises(OSError):
            _load("oneonone_store_fail", store_exc=OSError("remember 500"))


class TestUnit(unittest.TestCase):
    def test_get_and_store_helpers(self):
        uo, calls = _fake_api()
        with patch("urllib.request.urlopen", uo):
            self.assertEqual(M.get("/people"), PEOPLE)
            self.assertEqual(M.store("t", {"k": 1}), {"id": 2})
        self.assertEqual(calls[0][1], "http://127.0.0.1:37400/api/oneonone/people")
        self.assertEqual(calls[1], ("POST", "http://memory-server.digitalnoise.net:18790/remember", {"text": "t", "source": "oneonone", "metadata": {"k": 1}}))

    def test_person_profile_text_is_natural_language(self):
        txt = STORES[0][2]["text"]
        self.assertTrue(txt.startswith("Alice Example is someone Jordan Koch meets with weekly. Alice Example is a SRE in Infra. Their email is alice@example.test. Jordan last met with Alice Example on 2026-10-01."))
        self.assertIn("Contact: Alice Example. OneOnOne contact for Jordan Koch. Email: alice@example.test.", txt)
        self.assertEqual(STORES[1][2]["text"], "Bob is someone Jordan Koch meets with.\n\nContact: Bob. OneOnOne contact for Jordan Koch.")

    def test_meeting_text_resolves_attendees_and_skips_empty(self):
        sync = STORES[2][2]
        self.assertIn("Meeting: Sync\nDate: 2026-10-01\nType: 1:1\nDuration: 30 minutes\nAttendees: Alice Example, u9", sync["text"])
        self.assertIn("Action Items:\n- ship it", sync["text"]); self.assertIn("Decisions:\n- go", sync["text"]); self.assertIn("Follow-ups:\n- check", sync["text"])
        self.assertIn("Notes:\nshort notes", sync["text"])
        self.assertEqual(sync["metadata"], {"meeting": "Sync", "date": "2026-10-01", "attendees": ["Alice Example", "u9"], "type": "meeting_notes"})
        self.assertIn("2026-09-01 Empty (no content, skipping)", OUT)
        self.assertEqual(M.stored_meetings, 2)

    def test_long_notes_are_chunked_with_continuations(self):
        long_stores = [s[2] for s in STORES if s[2]["metadata"].get("meeting") == "Long"]
        self.assertEqual(len(long_stores), 3)
        self.assertIn("Notes (part 1):\n" + "N" * 1500, long_stores[0]["text"]); self.assertEqual(long_stores[0]["metadata"]["type"], "meeting_notes")
        self.assertTrue(long_stores[1]["text"].startswith("Meeting: Long (2026-09-15) — continued (part 2):\n"))
        self.assertEqual(long_stores[2]["metadata"]["type"], "meeting_notes_continued"); self.assertEqual(len(long_stores[2]["text"].split("\n", 1)[1]), 200)


class TestIntegration(unittest.TestCase):
    def test_order_is_forget_then_people_then_meetings_then_stats(self):
        kinds = []
        for m, url, _ in CALLS:
            kinds.append("forget" if "forget_all" in url else "people" if url.endswith("/people") else "meetings" if "/meetings" in url
                         else "stats" if url.endswith("/stats") else "store")
        self.assertEqual(kinds[:3], ["forget", "people", "meetings"]); self.assertEqual(kinds[-1], "stats")
        self.assertEqual(kinds.count("store"), 2 + 1 + 3)
        self.assertEqual(CALLS[2][1], "http://127.0.0.1:37400/api/oneonone/meetings?limit=500")

    def test_people_lookup_feeds_meeting_attendees(self):
        self.assertEqual(M.id_to_name, {"u1": "Alice Example", "u2": "Bob"})
        long_meta = [s[2]["metadata"] for s in STORES if s[2]["metadata"].get("meeting") == "Long"][0]
        self.assertEqual(long_meta["attendees"], ["Bob"])


class TestFunctional(unittest.TestCase):
    def test_golden_path_output(self):
        self.assertIn("Clearing existing oneonone chunks...\n  Deleted 7 existing chunks", OUT)
        self.assertIn("People: 2  |  Meetings: 3", OUT)
        self.assertIn("=== PEOPLE ===\n  ✓ Alice Example\n  ✓ Bob", OUT)
        self.assertIn("✓ 2026-10-01 Sync", OUT); self.assertIn("✓ 2026-09-15 Long (3 chunks)", OUT)
        self.assertIn("Done. 'oneonone' source now has 5 chunks in Nova's memory.\nTotal memories: 1600000", OUT)
        self.assertEqual(M.oneonone_count, 5)

    def test_empty_api_runs_clean(self):
        mod, calls, out = _load("oneonone_empty", people=[], meetings=[], stats={})
        self.assertEqual([c for c in calls if c[1].endswith("/remember")], [])
        self.assertIn("People: 0  |  Meetings: 0", out); self.assertIn("now has 0 chunks", out); self.assertIn("Total memories: ?", out)


class TestFrame(unittest.TestCase):
    def test_one_off_runs_to_completion_against_a_stub_api(self):
        # No __main__ guard by design; run it in a throwaway interpreter with urlopen faked in-process.
        self.assertNotIn('if __name__ == "__main__"', SRC)
        code = ("import json, io, sys, urllib.request\n"
                "class R:\n"
                "    def __init__(s, d): s.d = json.dumps(d).encode()\n"
                "    def read(s): return s.d\n"
                "    def __enter__(s): return s\n"
                "    def __exit__(s, *a): return False\n"
                "def uo(req, timeout=0):\n"
                "    url = req if isinstance(req, str) else req.full_url\n"
                "    if 'forget_all' in url: return R({'deleted': 0})\n"
                "    if url.endswith('/people'): return R([])\n"
                "    if '/meetings' in url: return R([])\n"
                "    if url.endswith('/stats'): return R({'by_source': {}, 'count': 1})\n"
                "    raise AssertionError(url)\n"
                "urllib.request.urlopen = uo\n"
                f"exec(compile(open({str(SCRIPT)!r}).read(), 'nova_ingest_oneonone.py', 'exec'), {{'__name__': 'frame'}})\n"
                "print('FRAME-OK')\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(TMP), capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("People: 0  |  Meetings: 0", r.stdout); self.assertTrue(r.stdout.rstrip().endswith("FRAME-OK"))


if __name__ == "__main__":
    unittest.main()
