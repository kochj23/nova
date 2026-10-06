#!/usr/bin/env python3
"""Tests for nova_imessage.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Never touches the real Messages DB, Contacts or Messages.app: MESSAGES_DB is a synthetic SQLite file in a
tempdir, the contact lookup is pre-seeded, osascript / urlopen are mocked in every test, and the module's
nova_config is a local proxy whose post_both is a MagicMock."""
import importlib.util
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_imessage.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


im = _load("nova_imessage_t", SCRIPT)
im.nova_config = types.SimpleNamespace(post_both=MagicMock(), SLACK_NOTIFY="C_NOTIFY")


def _mac(dt):
    return int((dt.timestamp() - 978307200) * 1_000_000_000)


def _make_db(path, rows):
    """rows: (text, is_from_me, datetime, handle)."""
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT)")
    c.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, is_from_me INT, date INT, "
              "service TEXT, handle_id INT, date_read INT, item_type INT)")
    handles = {}
    for text, me, dt, h in rows:
        if h not in handles:
            handles[h] = len(handles) + 1
            c.execute("INSERT INTO handle VALUES (?, ?)", (handles[h], h))
        c.execute("INSERT INTO message (text, is_from_me, date, service, handle_id, date_read, item_type) "
                  "VALUES (?, ?, ?, 'iMessage', ?, 0, 0)", (text, me, _mac(dt), handles[h]))
    c.commit()
    c.close()


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        root = Path(self.td.name)
        for attr, val in (("MESSAGES_DB", root / "chat.db"), ("STATE_FILE", root / "state.json"),
                          ("CONTACTS_CACHE", root / "contacts.json"), ("_contact_lookup", {"5551234567": "Amy"})):
            p = patch.object(im, attr, val)
            p.start()
            self.addCleanup(p.stop)
        boom = MagicMock(side_effect=AssertionError("unmocked outbound"))
        for p in (patch.object(im.subprocess, "run", boom), patch.object(im.urllib.request, "urlopen", boom)):
            p.start()
            self.addCleanup(p.stop)
        im.nova_config.post_both.reset_mock()

    def db(self, rows):
        _make_db(str(im.MESSAGES_DB), rows)


def _cp(rc=0, err=""):
    return subprocess.CompletedProcess([], rc, stdout="", stderr=err)


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_applescript_injection_escaped(self):
        with patch.object(im.subprocess, "run", return_value=_cp()) as run, redirect_stdout(io.StringIO()):
            im.send_imessage('+1555" & do shell script "id', 'hi" & do shell script "rm x')
        script = run.call_args.args[0][2]
        self.assertIn('participant "+1555\\" & do shell script \\"id"', script)
        self.assertIn('send "hi\\" & do shell script \\"rm x', script)

    def test_messages_db_opened_read_only_and_parameterized(self):
        self.assertEqual(SRC.count('mode=ro", uri=True'), 3)
        self.db([("hello", 0, datetime.now(), "+15551234567")])
        self.assertEqual(im.get_recent_messages(hours=1, contact="%' OR 1=1 --"), [])


class TestPerformance(_Base):
    def test_spam_and_phone_normalization_10k(self):
        msgs = [{"text": f"hi {i}", "sender": str(i)} for i in range(10_000)]
        t0 = time.perf_counter()
        for i, m in enumerate(msgs):
            im.is_spam(m)
            im._normalize_phone(f"+1 (555) 123-{i:04d}")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(_Base):
    def test_send_falls_back_to_alternate_once(self):
        with patch.object(im.subprocess, "run", side_effect=[_cp(1, "no participant"), _cp(0)]) as run, \
                redirect_stdout(io.StringIO()):
            self.assertTrue(im.send_imessage("+15551234567", "hey"))
        self.assertEqual(run.call_count, 2)
        self.assertIn('buddy "+15551234567" of service "iMessage"', run.call_args.args[0][2])

    def test_vector_remember_fails_open(self):
        # RETRY GAP: vector_remember/urlopen — one attempt, exception swallowed
        with patch.object(im.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertIsNone(im.vector_remember("x"))
        self.assertEqual(uo.call_count, 1)

    def test_send_exception_returns_false(self):
        with patch.object(im.subprocess, "run", side_effect=subprocess.TimeoutExpired("osascript", 15)), \
                redirect_stdout(io.StringIO()):
            self.assertFalse(im.send_imessage("+15551234567", "x"))


class TestUnit(_Base):
    def test_normalize_phone(self):
        self.assertEqual(im._normalize_phone("+1 (555) 123-4567"), "5551234567")
        self.assertEqual(im._normalize_phone("12345"), "12345")
        self.assertEqual(im._normalize_phone(""), "")

    def test_mac_timestamp(self):
        self.assertIsNone(im._mac_timestamp_to_datetime(0))
        self.assertIsNone(im._mac_timestamp_to_datetime(None))
        dt = datetime(2026, 1, 2, 3, 4, 5)
        self.assertEqual(im._mac_timestamp_to_datetime(_mac(dt)), dt)

    def test_is_spam(self):
        self.assertTrue(im.is_spam({"text": "x", "sender": "+1555"}))
        self.assertTrue(im.is_spam({"text": "code 1234", "sender": "73981"}))
        self.assertTrue(im.is_spam({"text": "deal!", "sender": "promo@shop.example"}))
        self.assertFalse(im.is_spam({"text": "dinner?", "sender": "+15551234567"}))

    def test_resolve_contact_and_signature(self):
        self.assertEqual(im.resolve_contact("+1 555 123 4567"), "Amy")
        self.assertEqual(im.resolve_contact(""), "Unknown")
        self.assertEqual(im.resolve_contact("stranger@x.example"), "stranger@x.example")
        with patch.object(im.subprocess, "run", return_value=_cp()) as run, redirect_stdout(io.StringIO()):
            im.send_imessage("+15551234567", "hi\n— Nova")
        self.assertEqual(run.call_args.args[0][2].count("— Nova"), 1)    # never double-signed


class TestIntegration(_Base):
    def test_contact_cache_built_from_swift_dump_and_cached(self):
        im._contact_lookup = None
        dump = json.dumps([{"phone": "+1 555-123-4567", "name": "Amy"}, {"email": "B@X.EXAMPLE", "name": "Bo"}])
        with patch.object(im.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, dump, "")) as run, \
                patch.object(im.Path, "home", return_value=Path(self.td.name)), redirect_stdout(io.StringIO()):
            (Path(self.td.name) / ".openclaw/workspace/state").mkdir(parents=True)
            self.assertEqual(im.resolve_contact("b@x.example"), "Bo")
            self.assertEqual(im._build_contact_cache()["5551234567"], "Amy")   # served from fresh cache file
        self.assertEqual(run.call_count, 1)

    def test_unread_advances_state_cursor(self):
        now = datetime.now()
        self.db([("a", 0, now - timedelta(minutes=5), "+15551234567"), ("b", 1, now, "+15551234567")])
        got = im.get_unread_messages()
        self.assertEqual([m["text"] for m in got], ["a"])
        self.assertEqual(json.loads(im.STATE_FILE.read_text())["last_check_ts"], got[0]["raw_date"])
        self.assertEqual(im.get_unread_messages(), [])


class TestFunctional(_Base):
    def test_watch_stores_all_and_alerts_on_real_incoming(self):
        now = datetime.now()
        self.db([("dinner tonight?", 0, now - timedelta(minutes=3), "+15551234567"),
                 ("your code is 1234", 0, now - timedelta(minutes=2), "73981"),
                 ("sure", 1, now - timedelta(minutes=1), "+15551234567")])
        with patch.object(im.urllib.request, "urlopen", MagicMock()) as uo, redirect_stdout(io.StringIO()):
            im.watch()
        self.assertEqual(uo.call_count, 3)
        bodies = [json.loads(c.args[0].data) for c in uo.call_args_list]
        self.assertTrue(all(b["source"] == "imessage" for b in bodies))
        self.assertIn("iMessage to Amy", bodies[2]["text"])
        text, kw = im.nova_config.post_both.call_args.args[0], im.nova_config.post_both.call_args.kwargs
        self.assertIn("*iMessage — 1 new*", text)
        self.assertIn("*Amy*", text)
        self.assertEqual(kw["slack_channel"], im.JORDAN_DM)

    def test_watch_without_db_posts_nothing(self):
        with redirect_stdout(io.StringIO()) as out:
            im.watch()
        self.assertIn("No new messages", out.getvalue())
        im.nova_config.post_both.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_help_exits_zero(self):
        r = subprocess.run([sys.executable, str(SCRIPT), "--help"], capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--watch", r.stdout)

    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        self.assertLess(SRC.index("def watch"), SRC.index('if __name__ == "__main__":'))


if __name__ == "__main__":
    unittest.main()
