#!/usr/bin/env python3
"""Tests for nova_mail_agent.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).

Never reads a real mailbox or the Keychain and never sends mail: IMAP is a FakeIMAP, smtplib.SMTP is a
MagicMock, _get_app_password is patched, Ollama/memory-server urlopen and subprocess are mocked in every
test, notify/post_both are local MagicMocks, herd/known-sender lists are synthetic, and every state file
lives in a tempdir."""
import email.utils
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
from datetime import timedelta
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
SCRIPT = SCRIPTS / "nova_mail_agent.py"
SRC = SCRIPT.read_text()
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mg = _load("nova_mail_agent_t", SCRIPT)
NOVA = mg.NOVA_EMAIL
HERD_A, HERD_B, JORDAN, CC = "a@herd.example", "b@herd.example", "j@home.example", "cc@home.example"


def _raw(frm, subject, body, to=NOVA, msgid="<m1@x>"):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Message-ID"] = frm, to, subject, msgid
    m.set_content(body)
    return m.as_bytes()


class FakeIMAP:
    def __init__(self, messages):
        self.messages = {str(i + 1).encode(): raw for i, raw in enumerate(messages)}
        self.ops, self.appended = [], []

    def select(self, box): self.ops.append(("select", box))

    def uid(self, cmd, *args):
        self.ops.append((cmd, args))
        if cmd == "SEARCH":
            return "OK", [b" ".join(self.messages)]
        if cmd == "FETCH":
            return "OK", [(b"1", self.messages[args[0]])]
        return "OK", [b""]

    def expunge(self): self.ops.append(("expunge",))
    def create(self, label): self.ops.append(("create", label))
    def append(self, box, flags, dt, data): self.appended.append(data); return "OK", [b""]
    def logout(self): self.ops.append(("logout",))

    def trashed(self):
        return [op[1][0] for op in self.ops if op[0] == "COPY" and op[1][1] == mg.TRASH_FOLDER]


class _Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        st = Path(self.td.name)
        self.notify = MagicMock()
        self.cfg = types.SimpleNamespace(post_both=MagicMock(), SLACK_EMAIL="C_EMAIL")
        boom = MagicMock(side_effect=AssertionError("unmocked outbound"))
        patches = [
            patch.object(mg, "DAILY_COUNTER_FILE", st / "daily.json"),
            patch.object(mg, "THREAD_COOLDOWN_FILE", st / "cool.json"),
            patch.object(mg, "THREAD_STATE_FILE", st / "threads.json"),
            patch.object(mg, "ENGAGEMENT_FILE", st / "eng.json"),
            patch.object(mg, "WORKSPACE", st / "ws"),
            patch.object(mg, "nova_notify", self.notify),
            patch.object(mg, "nova_config", self.cfg),
            patch.object(mg, "HERD", [{"name": "Amy", "email": HERD_A}, {"name": "Bo", "email": HERD_B}]),
            patch.object(mg, "HERD_EMAILS", {HERD_A, HERD_B, NOVA}),
            patch.object(mg, "HERD_REPLY_TO", [HERD_A, HERD_B]),
            patch.object(mg, "JORDAN_EMAILS", {JORDAN}),
            patch.object(mg, "JORDAN_CC", CC),
            patch.object(mg, "KNOWN_SENDERS", {"bank.example"}),
            patch.object(mg.urllib.request, "urlopen", boom),
            patch.object(mg.subprocess, "run", boom),
            patch("smtplib.SMTP", boom),
            patch("imaplib.IMAP4_SSL", boom),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.out = io.StringIO()
        r = redirect_stdout(self.out)
        r.__enter__()
        self.addCleanup(r.__exit__, None, None, None)

    def run_main(self, messages, llm=None):
        """Drive main() with a FakeIMAP inbox; `llm` answers _ollama_generate."""
        imap = FakeIMAP(messages)
        smtp = MagicMock()
        with patch.object(mg, "_get_app_password", return_value="app-pass"), \
                patch.object(mg, "imap_connect", return_value=imap), \
                patch("smtplib.SMTP", smtp), patch.object(mg, "vector_remember") as vr, \
                patch.object(mg, "get_random_memory", return_value=""), \
                patch.object(mg, "_ollama_generate", side_effect=llm or (lambda *a, **k: "")):
            mg.main()
        return imap, smtp.return_value.__enter__.return_value, vr


GOOD_REPLY = "I disagree, because the data shows otherwise. What if we measured it specifically tomorrow?\n— Nova"


class TestSecurity(_Base):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('"nova-smtp-app-password"', SRC)          # app password comes from Keychain

    def test_sensitive_and_incident_threads_never_reach_llm(self):
        with patch.object(mg, "_ollama_generate") as llm:
            for subj in ("Re: Nova's daily digest", "plex viewing stats"):
                self.assertFalse(mg.should_reply(HERD_A, subj, "hello", {})[0])
        llm.assert_not_called()

    def test_own_and_system_mail_is_never_answered(self):
        imap, smtp, _ = self.run_main([_raw(NOVA, "loop", "x"), _raw("mailer-daemon@g.example", "bounce", "x")])
        smtp.sendmail.assert_not_called()
        self.assertEqual(len(imap.trashed()), 2)


class TestPerformance(_Base):
    def test_shallow_detection_10k_fast(self):
        bodies = ["Beautifully said.", "We should benchmark the NAS because the rsync numbers look off?"] * 5000
        t0 = time.perf_counter()
        flags = [mg._is_shallow_message(b) for b in bodies]
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(sum(flags), 5000)


class TestRetry(_Base):
    def test_ollama_failure_fails_open(self):
        # RETRY GAP: _ollama_generate/urlopen — one attempt, "" on failure; main() leaves mail in inbox
        with patch.object(mg.urllib.request, "urlopen", side_effect=OSError("down")) as uo:
            self.assertEqual(mg._ollama_generate("p"), "")
        self.assertEqual(uo.call_count, 1)

    def test_smtp_failure_is_one_shot_and_notifies(self):
        # RETRY GAP: smtp_send/smtplib.SMTP — one attempt; failure -> warning notify, no post_both
        smtp = MagicMock(side_effect=OSError("smtp down"))
        with patch.object(mg, "_get_app_password", return_value="p"), \
                patch.object(mg, "imap_connect", return_value=FakeIMAP([_raw(HERD_A, "Idea", "What about LoRa?")])), \
                patch("smtplib.SMTP", smtp), patch.object(mg, "vector_remember"), \
                patch.object(mg, "get_random_memory", return_value=""), \
                patch.object(mg, "_ollama_generate", side_effect=lambda p, **k: "REPLY: new angle" if "quality evaluator" in p else GOOD_REPLY):
            mg.main()
        self.assertEqual(smtp.call_count, 1)
        self.assertEqual(self.notify.call_args.args[0], "Herd email reply FAILED")
        self.cfg.post_both.assert_not_called()

    def test_no_password_or_imap_failure_returns_quietly(self):
        with patch.object(mg, "_get_app_password", return_value=""):
            mg.main()
        with patch.object(mg, "_get_app_password", return_value="p"), \
                patch.object(mg, "imap_connect", side_effect=OSError("auth")):
            mg.main()
        self.assertIn("IMAP connect failed", self.out.getvalue())


class TestUnit(_Base):
    def test_decide_response_type(self):
        self.assertEqual(mg.decide_response_type({}, True), "graduation")
        self.assertEqual(mg.decide_response_type({"nova_replies": 2, "total_messages": 6}, False), "synthesis")
        self.assertEqual(mg.decide_response_type({"nova_replies": 2, "total_messages": 2}, False), "artifact")
        self.assertEqual(mg.decide_response_type({"nova_replies": 0, "total_messages": 3}, False), "roles")
        self.assertEqual(mg.decide_response_type({"nova_replies": 1, "total_messages": 1}, False), "reply")

    def test_quality_filter(self):
        self.assertFalse(mg.passes_quality_filter("short"))
        self.assertFalse(mg.passes_quality_filter("Beautifully said, this really resonates with me deeply."))
        self.assertFalse(mg.passes_quality_filter("A perfectly fine sentence with nothing concrete in it at all."))
        self.assertTrue(mg.passes_quality_filter(GOOD_REPLY))

    def test_strip_reasoning_and_thread_limits(self):
        self.assertEqual(mg._strip_reasoning("Okay, let me think.\n\nThe real answer is right here."),
                         "The real answer is right here.")
        self.assertEqual(mg._strip_reasoning("Plain reply"), "Plain reply")
        old = (mg.NOW - timedelta(days=mg.THREAD_TTL_DAYS)).isoformat()
        self.assertTrue(mg._is_thread_dead({"total_messages": 1, "first_seen": old}))
        self.assertTrue(mg._is_thread_dead({"total_messages": mg.THREAD_MAX_MESSAGES, "first_seen": mg.NOW.isoformat()}))
        self.assertTrue(mg._is_thread_budget_exhausted({"nova_replies": mg.MAX_THREAD_REPLIES}))

    def test_engagement_bounds_and_cooldown(self):
        eng = {}
        for _ in range(30):
            mg._record_engagement(eng, "x", is_quality=False)
        self.assertEqual(mg._get_engagement_score(eng, "x"), -5.0)
        self.assertFalse(mg._is_thread_on_cooldown("t"))
        mg._record_thread_cooldown("t")
        self.assertTrue(mg._is_thread_on_cooldown("t"))
        mg._increment_daily_reply_count()
        self.assertEqual(mg._get_daily_reply_count(), 1)


class TestIntegration(_Base):
    def test_reply_gate_parses_llm_verdict(self):
        with patch.object(mg, "_ollama_generate", return_value="REPLY: adds a counterpoint\nextra"):
            self.assertEqual(mg.should_reply(HERD_A, "s", "b", {}), (True, "adds a counterpoint"))
        with patch.object(mg, "_ollama_generate", return_value="SILENCE: nothing to add"):
            self.assertEqual(mg.should_reply(HERD_A, "s", "b", {}), (False, "nothing to add"))

    def test_notifications_route_through_shared_notifier(self):
        mg.slack_post("*Title here*\nbody text")
        self.notify.assert_called_once_with("Title here", body="body text", level="info", category="email")
        self.assertIn("from nova_notify import notify as nova_notify", SRC)


class TestFunctional(_Base):
    def test_herd_reply_golden_path(self):
        llm = lambda p, **k: "REPLY: new angle" if "quality evaluator" in p else GOOD_REPLY  # noqa: E731
        imap, smtp, vr = self.run_main([_raw(f"Amy <{HERD_A}>", "LoRa mesh", "Should we add a second gateway?")], llm)
        frm, rcpts, data = smtp.sendmail.call_args.args
        self.assertEqual(frm, NOVA)
        self.assertEqual(rcpts, [HERD_A, HERD_B, CC])
        sent = email.message_from_bytes(data)
        self.assertEqual(sent["Subject"], "Re: LoRa mesh")
        self.assertEqual(sent["In-Reply-To"], "<m1@x>")
        self.assertEqual(len(imap.appended), 1)                                  # saved to Sent
        self.assertIn("Herd mail [reply]", self.cfg.post_both.call_args.args[0])
        threads = json.loads(mg.THREAD_STATE_FILE.read_text())
        self.assertEqual(threads["lora mesh"]["nova_replies"], 1)

    def test_jordan_and_unknown_are_stored_and_notified_never_replied(self):
        imap, smtp, vr = self.run_main([_raw(JORDAN, "hi", "note to self"), _raw("x@rand.example", "offer", "buy")])
        smtp.sendmail.assert_not_called()
        titles = [c.args[0] for c in self.notify.call_args_list]
        self.assertEqual(len(titles), 2)
        self.assertIn("Email from Jordan", titles[0])
        self.assertIn("Unknown sender email", titles[1])
        self.assertEqual(vr.call_count, 2)

    def test_daily_cap_blocks_send(self):
        mg.DAILY_COUNTER_FILE.write_text(json.dumps({"date": mg.TODAY, "count": mg.MAX_REPLIES_PER_DAY}))
        imap, smtp, _ = self.run_main([_raw(HERD_A, "Topic", "A real question?")], lambda *a, **k: GOOD_REPLY)
        smtp.sendmail.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # running the script reads the live mailbox, so the smoke is an import in a child (IMAP/SMTP poisoned)
        code = ("import imaplib, smtplib, importlib.util as u\n"
                "def _no(*a, **k): raise SystemExit('network at import')\n"
                "imaplib.IMAP4_SSL = _no; smtplib.SMTP = _no\n"
                f"s=u.spec_from_file_location('m', {str(SCRIPT)!r}); m=u.module_from_spec(s)\n"
                "s.loader.exec_module(m); print(m.MAX_THREAD_REPLIES)\n")
        r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "3")
        self.assertIn('if __name__ == "__main__":', SRC)


if __name__ == "__main__":
    unittest.main()
