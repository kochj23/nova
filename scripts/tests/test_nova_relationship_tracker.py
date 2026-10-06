#!/usr/bin/env python3
"""Tests for nova_relationship_tracker.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

The module reads the Slack token from Keychain at import time, so it is loaded here with
nova_config.slack_bot_token patched, and the subprocess frame checks patch it the same way."""
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_relationship_tracker.py"
SRC = PATH.read_text()

import nova_config  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("nova_relationship_tracker_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.object(nova_config, "slack_bot_token", return_value="xoxb-test"):
        spec.loader.exec_module(mod)
    return mod


rt = _load()
NOW = rt.NOW


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat()


def _mmddyy(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%m/%d/%y")


PEOPLE = [{"id": "p1", "name": "Alex Weekly", "meetingFrequency": "Weekly", "title": "SRE", "department": "Ops"},
          {"id": "p2", "name": "Bo Monthly", "meetingFrequency": "Monthly"},
          {"id": "p3", "name": "Cy Never", "meetingFrequency": ""},
          {"id": "p0", "name": "Jordan Koch", "meetingFrequency": "Weekly"}]
MEETINGS = [{"attendees": ["p1"], "title": "Alex 1:1", "notes": f"{_mmddyy(20)}: talked\n{_mmddyy(15)}: more"},
            {"attendees": ["p2"], "title": "Bo 1:1", "notes": "", "updatedAt": _iso(5)}]


def _api(url, timeout=10):
    if url.endswith("/people"):
        return PEOPLE
    if "/meetings" in url:
        return MEETINGS
    if "/search?" in url and "source=email&" in url + "&":
        return {"results": [{"created_at": _iso(3), "text": "From: x\nSubject: Lunch?\nbody"}]} if "Dana" in url else {"results": []}
    return {"results": []}


class _Env(unittest.TestCase):
    def setUp(self):
        herd = types.SimpleNamespace(HERD=[{"name": "Dana Herd", "email": "d@example.org"}, {"name": "Eli Herd"}, {"name": ""}])
        ps = [mock.patch.object(rt, "get", side_effect=_api),
              mock.patch.dict(sys.modules, {"herd_config": herd}),
              mock.patch.object(rt.urllib.request, "urlopen", side_effect=OSError("offline")),
              mock.patch("sys.stderr", new_callable=io.StringIO)]
        self.ps = ps
        self.m = [p.start() for p in ps]
        self.addCleanup(lambda: [p.stop() for p in ps])


class TestSecurity(_Env):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/\-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("xoxb-", SRC)
        self.assertIn("SLACK_TOKEN  = nova_config.slack_bot_token()", SRC)

    def test_names_are_url_quoted(self):
        self.m[0].side_effect = None
        self.m[0].return_value = {"results": []}
        rt.last_email_contact("O'Brien & Co?source=private")
        url = self.m[0].call_args_list[0][0][0]
        self.assertIn("q=O%27Brien%20%26%20Co%3Fsource%3Dprivate", url)
        self.assertTrue(url.endswith("&source=email"))

    def test_report_mode_never_posts(self):
        with mock.patch.object(sys, "argv", ["x", "--report"]), mock.patch.object(rt, "post_slack") as ps, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            rt.main()
        ps.assert_not_called()


class TestPerformance(_Env):
    def test_note_date_parse_10k_lines(self):
        notes = "\n".join(f"{_mmddyy(i % 300)}: update {i}" for i in range(10_000))
        t0 = time.perf_counter()
        latest = rt.extract_latest_date_from_notes(notes)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(latest.strftime("%m/%d/%y"), _mmddyy(0))


class TestRetry(_Env):
    def test_get_fails_open(self):
        # RETRY GAP: get() — one urlopen attempt, None on failure; callers treat None as "no data"
        self.ps[0].stop()
        try:
            with mock.patch.object(rt.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
                self.assertIsNone(rt.get("http://x"))
                self.assertEqual(rt.check_oneonone_contacts(), ([], []))
                self.assertEqual(rt.last_email_contact("Dana"), (None, "no email history found"))
            self.assertEqual(uo.call_count, 5)
        finally:
            self.ps[0].start()

    def test_slack_not_ok_is_reported_not_raised(self):
        # RETRY GAP: post_slack — one POST; ok=false returns False (logged), no retry
        r = mock.MagicMock(); r.__enter__.return_value.read.return_value = b'{"ok": false, "error": "ratelimited"}'
        with mock.patch.object(rt.urllib.request, "urlopen", return_value=r) as uo:
            self.assertFalse(rt.post_slack("hi"))
        self.assertEqual(uo.call_count, 1)


class TestUnit(_Env):
    def test_days_since_edges(self):
        self.assertIsNone(rt.days_since(""))
        self.assertIsNone(rt.days_since("not a date"))
        self.assertEqual(rt.days_since(_iso(4)), 4)
        self.assertEqual(rt.days_since((NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")), 2)

    def test_note_dates_ignore_future_and_ancient(self):
        self.assertIsNone(rt.extract_latest_date_from_notes(""))
        self.assertIsNone(rt.extract_latest_date_from_notes("1/1/19: old\n13/40/26 bad"))
        future = (NOW + timedelta(days=30)).strftime("%m/%d/%y")
        self.assertEqual(rt.extract_latest_date_from_notes(f"{future}: x\n{_mmddyy(1)}: y").strftime("%m/%d/%y"), _mmddyy(1))

    def test_format_all_current(self):
        self.assertIn("All relationships are current", rt.format_slack_message([], [], [], []))


class TestIntegration(_Env):
    def test_oneonone_cadence_classification(self):
        overdue, ok = rt.check_oneonone_contacts()
        status = {e["name"]: (e["status"], e["days_ago"]) for e in overdue + ok}
        self.assertEqual(status["Alex Weekly"], ("overdue", 15))       # weekly threshold 10d, notes say 15d
        self.assertEqual(status["Bo Monthly"], ("ok", 5))              # updatedAt fallback
        self.assertEqual(status["Cy Never"], ("never met", None))
        self.assertNotIn("Jordan Koch", status)
        self.assertEqual(len(overdue), 2)

    def test_herd_uses_email_search(self):
        overdue, ok = rt.check_herd_contacts()
        self.assertEqual([h["name"] for h in ok], ["Dana Herd"])
        self.assertEqual(ok[0]["snippet"], "Subject: Lunch?")
        self.assertEqual([h["name"] for h in overdue], ["Eli Herd"])


class TestFunctional(_Env):
    def _main(self, argv):
        with mock.patch.object(sys, "argv", ["x"] + argv), mock.patch.object(rt, "post_slack") as ps, \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            rt.main()
        return ps, out.getvalue()

    def test_digest_posted_to_email_channel(self):
        ps, _ = self._main([])
        msg = ps.call_args[0][0]
        self.assertIn("*📅 Overdue 1:1s (2)*", msg)
        self.assertIn("*Alex Weekly* — last met 15d ago (should be weekly) · _SRE · Ops_", msg)
        self.assertIn("*Eli Herd* — last contact no email history found", msg)
        self.assertIn("Current: Bo Monthly, Dana Herd", msg)
        self.assertEqual(rt.SLACK_CHAN, nova_config.SLACK_EMAIL)

    def test_quiet_mode_skips_when_all_current(self):
        with mock.patch.object(rt, "check_oneonone_contacts", return_value=([], [{"name": "a"}])), \
                mock.patch.object(rt, "check_herd_contacts", return_value=([], [])):
            ps, _ = self._main(["--quiet"])
        ps.assert_not_called()


class TestFrame(unittest.TestCase):
    PRE = "import nova_config; nova_config.slack_bot_token = lambda: ''; "

    def test_help_exits_zero(self):
        code = self.PRE + f"import runpy, sys; sys.argv=['x','--help']; runpy.run_path({str(PATH)!r}, run_name='__main__')"
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--quiet", r.stdout)

    def test_import_never_runs_main(self):
        r = subprocess.run([sys.executable, "-c", self.PRE + "import nova_relationship_tracker"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip() + r.stderr.strip(), "")


if __name__ == "__main__":
    unittest.main()
