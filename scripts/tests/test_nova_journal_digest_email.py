#!/usr/bin/env python3
"""Tests for nova_journal_digest_email.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import email
import email.policy
import importlib.util
import io
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
SCRIPT = SCRIPTS / "nova_journal_digest_email.py"
SRC = SCRIPT.read_text()


def _stubs():
    cfg = types.ModuleType("nova_config"); cfg.JORDAN_EMAIL = "jordan@example.test"; cfg.post_both = MagicMock()
    nn = types.ModuleType("nova_notify"); nn.notify = MagicMock(return_value=True)
    herd = types.ModuleType("herd_config"); herd.HERD_EMAILS = ["a@example.test", "b@example.test"]
    return {"nova_config": cfg, "nova_notify": nn, "herd_config": herd}


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, _stubs()):       # notify bus / config / herd list stubbed at import; restored after
        spec.loader.exec_module(mod)
    return mod


jd = _load("jd_mod", SCRIPT)
_TMP = tempfile.TemporaryDirectory()
jd.JOURNAL_DIR = Path(_TMP.name) / "content"       # never read ~/nova-journal
jd.TODAY = "2026-10-05"


def _post(section, stem, title="A Post", summary="One line."):
    d = jd.JOURNAL_DIR / section
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{stem}.md").write_text(f'---\ntitle: "{title}"\nsummary: {summary}\n---\nbody\n')


def _clean():
    import shutil
    if jd.JOURNAL_DIR.exists():
        shutil.rmtree(jd.JOURNAL_DIR)
    jd.JOURNAL_DIR.mkdir(parents=True)
    for n in ("about", "search"):
        (jd.JOURNAL_DIR / n).mkdir()
    (jd.JOURNAL_DIR / "_index.md").write_text("x")


class _SMTP:
    instances = []

    def __init__(self, host, port):
        self.host, self.port = host, port; self.calls = []; _SMTP.instances.append(self)

    def starttls(self):
        self.calls.append("starttls")

    def login(self, u, p):
        self.calls.append(("login", u, p))

    def sendmail(self, frm, to, msg):
        self.calls.append(("sendmail", frm, to, msg))

    def quit(self):
        self.calls.append("quit")


def _keychain(pw="app-pass"):
    return types.SimpleNamespace(returncode=0 if pw else 1, stdout=pw)


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_password_comes_from_keychain_via_argv(self):
        with patch.object(jd.subprocess, "run", return_value=_keychain("pw")) as sp:
            self.assertEqual(jd.get_app_password(), "pw")
        self.assertEqual(sp.call_args[0][0], ["security", "find-generic-password", "-a", jd.NOVA_EMAIL, "-s", "nova-gmail-app-password", "-w"])
        self.assertNotIn("shell=True", SRC)
        with patch.object(jd.subprocess, "run", return_value=_keychain("")):
            self.assertEqual(jd.get_app_password(), "")

    def test_no_password_means_no_smtp_session(self):
        _SMTP.instances.clear()
        with patch.object(jd.subprocess, "run", return_value=_keychain("")), patch.object(jd.smtplib, "SMTP", _SMTP), \
             redirect_stdout(io.StringIO()) as out:
            self.assertFalse(jd.send_digest("body", 1))
        self.assertEqual(_SMTP.instances, [])
        self.assertIn("Cannot get email password", out.getvalue())

    def test_frontmatter_is_data_not_executed(self):
        _clean(); _post("essays", "2026-10-05-x", title="$(rm -rf /)", summary="{__import__('os')}")
        posts = jd.find_today_posts()
        self.assertEqual(posts[0]["title"], "$(rm -rf /)")
        self.assertEqual(posts[0]["summary"], "{__import__('os')}")


class TestPerformance(unittest.TestCase):
    def test_build_digest_10k_posts_under_bound(self):
        posts = [{"title": f"t{i}", "summary": "s", "section": f"sec{i % 7}", "url": f"u{i}"} for i in range(10_000)]
        t0 = time.perf_counter()
        body = jd.build_digest(posts)
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertIn("Total: 10000 posts today.", body)


class TestRetry(unittest.TestCase):
    def test_smtp_failure_fails_open_false(self):
        # RETRY GAP: send_digest()/smtplib.SMTP — one attempt; any SMTP error logs and returns False (no re-send)
        attempts = []

        def boom(*a, **k):
            attempts.append(1); raise OSError("smtp down")
        with patch.object(jd.subprocess, "run", return_value=_keychain()), patch.object(jd.smtplib, "SMTP", side_effect=boom), \
             redirect_stdout(io.StringIO()) as out:
            self.assertFalse(jd.send_digest("body", 2))
        self.assertEqual(len(attempts), 1)
        self.assertIn("ERROR sending digest", out.getvalue())

    def test_main_reports_failure_as_warning_notification(self):
        _clean(); _post("essays", "2026-10-05-a")
        jd.notify.reset_mock()
        with patch.object(jd, "send_digest", return_value=False), redirect_stdout(io.StringIO()):
            jd.main()
        args, kw = jd.notify.call_args
        self.assertEqual(args[0], "Journal Digest FAILED")
        self.assertEqual((kw["level"], kw["dedup_key"]), ("warning", "journal-digest-email-fail"))


class TestUnit(unittest.TestCase):
    def test_find_today_posts_filters_and_parses(self):
        _clean()
        _post("essays", "2026-10-05-one", title="One", summary="S" * 300)
        _post("dreams", "2026-10-04-old", title="Old")
        _post("about", "2026-10-05-skip", title="Skip")
        (jd.JOURNAL_DIR / "ops").mkdir(); (jd.JOURNAL_DIR / "ops" / "2026-10-05-notitle.md").write_text("no frontmatter\n")
        posts = sorted(jd.find_today_posts(), key=lambda p: p["section"])
        self.assertEqual([p["title"] for p in posts], ["One", "2026-10-05-notitle"])
        self.assertEqual(len(posts[0]["summary"]), 200)
        self.assertEqual(posts[0]["url"], "https://nova.digitalnoise.net/essays/2026-10-05-one/")

    def test_build_digest_empty_and_sections(self):
        self.assertEqual(jd.build_digest([]), "")
        body = jd.build_digest([{"title": "B", "summary": "", "section": "weird-memories", "url": "u2"},
                                {"title": "A", "summary": "sum", "section": "essays", "url": "u1"}])
        self.assertIn("Good evening, Herd.", body)
        self.assertLess(body.index("Essays"), body.index("Weird Memories"))
        self.assertIn("  A\n  sum\n  u1\n", body)
        self.assertIn("  B\n  u2\n", body)
        self.assertIn("Total: 2 posts today.", body); self.assertTrue(body.endswith("— Nova"))

    def test_send_digest_builds_message_and_recipients(self):
        _SMTP.instances.clear()
        with patch.object(jd.subprocess, "run", return_value=_keychain("pw")), patch.object(jd.smtplib, "SMTP", _SMTP), \
             redirect_stdout(io.StringIO()):
            self.assertTrue(jd.send_digest("hello herd", 3))
        s = _SMTP.instances[0]
        self.assertEqual((s.host, s.port), ("smtp.gmail.com", 587))
        self.assertEqual(s.calls[0], "starttls"); self.assertEqual(s.calls[1], ("login", jd.NOVA_EMAIL, "pw"))
        _, frm, to, msg = s.calls[2]
        self.assertEqual(to, ["a@example.test", "b@example.test", "jordan@example.test"])
        m = email.message_from_string(msg, policy=email.policy.default)
        self.assertEqual(m["Subject"], "Nova's Journal — 2026-10-05 (3 posts)")
        self.assertEqual(m["From"], f"Nova <{jd.NOVA_EMAIL}>"); self.assertEqual(m["Cc"], "jordan@example.test")
        self.assertEqual(m.get_payload()[0].get_content().strip(), "hello herd"); self.assertEqual(s.calls[-1], "quit")


class TestIntegration(unittest.TestCase):
    def test_uses_shared_config_and_notify_bus(self):
        self.assertIn("import nova_config", SRC); self.assertIn("from nova_notify import notify", SRC)
        self.assertIn("JORDAN_CC = nova_config.JORDAN_EMAIL", SRC)
        self.assertIn("from herd_config import HERD_EMAILS", SRC)
        self.assertEqual(jd.JORDAN_CC, "jordan@example.test")
        self.assertEqual(jd.HERD_EMAILS, ["a@example.test", "b@example.test"])

    def test_find_then_build_chain(self):
        _clean(); _post("essays", "2026-10-05-z", title="Zed", summary="zz")
        body = jd.build_digest(jd.find_today_posts())
        self.assertIn("  Zed\n  zz\n  https://nova.digitalnoise.net/essays/2026-10-05-z/", body)


class TestFunctional(unittest.TestCase):
    def test_golden_path_sends_and_notifies_info(self):
        _clean(); _post("essays", "2026-10-05-a", title="A"); _post("dreams", "2026-10-05-b", title="B")
        _SMTP.instances.clear(); jd.notify.reset_mock()
        with patch.object(jd.subprocess, "run", return_value=_keychain("pw")), patch.object(jd.smtplib, "SMTP", _SMTP), \
             redirect_stdout(io.StringIO()) as out:
            jd.main()
        self.assertEqual(len(_SMTP.instances), 1)
        m = email.message_from_string(_SMTP.instances[0].calls[2][3], policy=email.policy.default)
        self.assertTrue(m["Subject"].endswith("(2 posts)"))
        self.assertIn("Total: 2 posts today.", m.get_payload()[0].get_content())
        args, kw = jd.notify.call_args
        self.assertEqual(args[0], "Journal Digest sent to Herd")
        self.assertEqual((kw["level"], kw["category"], kw["dedup_key"]), ("info", "journal", "journal-digest-email"))
        self.assertIn("Sent to 2 Herd members", kw["body"])
        self.assertIn("Found 2 posts", out.getvalue())

    def test_no_posts_skips_everything(self):
        _clean(); jd.notify.reset_mock()
        with patch.object(jd.smtplib, "SMTP") as smtp, redirect_stdout(io.StringIO()) as out:
            jd.main()
        smtp.assert_not_called(); jd.notify.assert_not_called()
        self.assertIn("No posts today", out.getvalue())


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":\n    main()', SRC)
        snippet = ("import sys, types; from unittest.mock import MagicMock\n"
                   "nn = types.ModuleType('nova_notify'); nn.notify = MagicMock(); sys.modules['nova_notify'] = nn\n"
                   "import smtplib; smtplib.SMTP = MagicMock(side_effect=AssertionError('smtp at import'))\n"
                   "import nova_journal_digest_email as m; assert m.SMTP_PORT == 587")
        r = subprocess.run([sys.executable, "-c", snippet], cwd=str(SCRIPTS), capture_output=True, text=True,
                           timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
