#!/usr/bin/env python3
"""Tests for nova_nightly_report.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_nightly_report.py"
SRC = SCRIPT.read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("nnr_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nr = _load()
# stub every outbound side effect at module load; all file writes go to a tempdir
nr.notify = MagicMock()
nr.vector_remember = MagicMock()
nr.WORKSPACE = Path(_TMP.name) / "ws"
nr.MEMORY_DIR = nr.WORKSPACE / "memory"
nr.GATEWAY_LOG = Path(_TMP.name) / "gateway.log"

MAIL = """📬 me@example.com — 4 unread
[UNREAD] FROM: Fraud Team <alerts@bank.example> [UNREAD]
SUBJ: Suspicious login detected
[UNREAD] FROM: Hulu <promo@hulu.com>
SUBJ: New shows?
[UNREAD] FROM: Apple <noreply@apple.com>
SUBJ: Your receipt
[UNREAD] FROM: Stranger <x@nowhere.example>
SUBJ: Coffee sometime?
[READ] FROM: UPS <track@ups.com>
SUBJ: Your package is out for delivery
"""


def _p(out="", rc=0):
    return SimpleNamespace(stdout=out, stderr="", returncode=rc)


class _Base(unittest.TestCase):
    def setUp(self):
        nr.notify.reset_mock(); nr.vector_remember.reset_mock()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_subprocess_never_uses_shell(self):
        self.assertNotIn("shell=True", SRC)

    def test_awesome_repos_skipped(self):
        ts = (nr.NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        ev = [{"created_at": ts, "repo": {"name": "kochj23/awesome-x"}, "type": "WatchEvent"}]
        with patch.object(nr.subprocess, "run", side_effect=[_p(json.dumps(ev)), _p("[]")]):
            self.assertIn("No activity", nr.github_digest())


class TestPerformance(_Base):
    def test_email_parse_10k_messages(self):
        big = "\n".join(f"[UNREAD] FROM: Sender{i} <s{i}@example.com>\nSUBJ: question {i}?" for i in range(10_000))
        with patch.object(nr, "get_mail_data", return_value=big):
            t0 = time.perf_counter()
            out = nr.email_action_items()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(out.count("FYI"), 15)                   # capped at 15 items


class TestRetry(_Base):
    def test_sections_fail_open(self):
        # RETRY GAP: github_digest()/weather_report()/burbank_reddit() — one attempt; errors become text, never raise
        with patch.object(nr.subprocess, "run", side_effect=subprocess.TimeoutExpired("gh", 30)) as r:
            self.assertIn("_Error:", nr.github_digest())
            self.assertIn("_Error:", nr.weather_report())
        self.assertEqual(r.call_count, 2)
        with patch.object(nr.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertIn("_Error:", nr.burbank_reddit())

    def test_main_survives_a_raising_module(self):
        with patch.object(nr, "github_digest", side_effect=RuntimeError("boom")), \
             patch.object(nr, "email_action_items", return_value=""), patch.object(nr, "nova_memory_log", return_value=""), \
             patch.object(nr, "package_tracker", return_value=""), patch.object(nr, "weather_report", return_value=""), \
             patch.object(nr, "homekit_status", return_value=""), patch.object(nr, "moon_and_sky", return_value=""), \
             patch.object(nr, "burbank_reddit", return_value=""), patch.object(nr, "meeting_notes", return_value=""), \
             patch.object(nr, "write_dream_context") as wdc:
            nr.main()
        titles = [c[0][0] for c in nr.notify.call_args_list]
        self.assertIn("*GitHub Digest*", titles)
        self.assertEqual(wdc.call_args[0][0]["GitHub Digest"], "Error: boom")


class TestUnit(_Base):
    def test_moon_phase(self):
        self.assertEqual(nr._moon_phase_for(date(2000, 1, 6))[0], "New Moon")
        name, emoji, days, to_full = nr._moon_phase_for(date(2000, 1, 21))
        self.assertEqual(name, "Full Moon")
        self.assertLessEqual(to_full, 30)

    def test_email_priorities(self):
        with patch.object(nr, "get_mail_data", return_value=MAIL):
            out = nr.email_action_items()
        self.assertIn("🔴 HIGH *Suspicious login detected*", out)
        self.assertIn("🟡 REPLY *Your receipt*", out)
        self.assertIn("🔵 FYI *Coffee sometime?*", out)
        self.assertNotIn("New shows", out)                      # noise sender
        with patch.object(nr, "get_mail_data", return_value=None):
            self.assertIn("Could not fetch mail data", nr.email_action_items())

    def test_package_tracker(self):
        with patch.object(nr, "get_mail_data", return_value=MAIL):
            self.assertIn("🚚 Out for delivery [UPS]", nr.package_tracker())

    def test_homekit_status(self):
        acc = [{"name": "Lock", "room": "Front", "characteristics": [{"type": "battery", "value": 10}]},
               {"name": "Lamp", "room": "Den", "characteristics": [{"type": "on", "value": True}]},
               {"name": "Cam", "room": "Yard", "characteristics": [{"type": "reachable", "value": False}]}]
        with patch.object(nr.subprocess, "run", side_effect=[_p('{"uptimeSeconds": 7300}'), _p(json.dumps(acc))]):
            out = nr.homekit_status()
        self.assertIn("uptime 2h 1m", out)
        self.assertIn("Low battery: Lock (Front) — 10%", out)
        self.assertIn("Cam (Yard)", out)
        with patch.object(nr.subprocess, "run", return_value=_p("", 7)):
            self.assertIn("not running", nr.homekit_status())


class TestIntegration(_Base):
    def test_dream_context_preserves_history_and_stores_vectors(self):
        nr.MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        mem = nr.MEMORY_DIR / f"{nr.TODAY}.md"
        mem.write_text("# old\n## On This Day in History\n- 1066 something\n## Other\nx\n")
        results = {"Email Action Items": "*📋 Email*\n  🔴 HIGH *Fraud*", "Weather": "*🌤 Burbank Weather*\n  Now: sunny",
                   "GitHub Digest": "*🐙 GitHub*\n*Commits (2):*", "Broken": "Error: nope"}
        nr.write_dream_context(results)
        text = mem.read_text()
        self.assertIn("## Emails that need attention", text)
        self.assertIn("1066 something", text)
        self.assertNotIn("Broken", text)
        hb = (nr.WORKSPACE / "HEARTBEAT.md").read_text()
        self.assertIn("- 🔴 HIGH Fraud", hb)
        sources = [c.kwargs.get("source") for c in nr.vector_remember.call_args_list]
        self.assertIn("email", sources); self.assertIn("github", sources)

    def test_slack_post_goes_through_notify_bus(self):
        nr.slack_post("── Title ──\nbody line")
        args, kw = nr.notify.call_args
        self.assertEqual((args[0], kw["body"], kw["category"]), ("Title ──", "body line", "digest"))


class TestFunctional(_Base):
    def test_main_golden_path(self):
        fns = {n: MagicMock(return_value=f"*{n}*\n  content") for n in
               ("github_digest", "email_action_items", "nova_memory_log", "package_tracker", "weather_report",
                "homekit_status", "moon_and_sky", "burbank_reddit")}
        fns["meeting_notes"] = MagicMock(return_value="*m*\n no meetings today")
        with patch.multiple(nr, **fns), patch.object(nr, "write_dream_context") as wdc:
            nr.main()
        titles = [c[0][0] for c in nr.notify.call_args_list]
        self.assertTrue(titles[0].startswith("*🌙 Nova Nightly Report"))
        self.assertTrue(titles[-1].startswith("_— Nova nightly report complete"))
        self.assertEqual(len(titles), 2 + 8)                   # empty meeting section skipped
        self.assertEqual(len(wdc.call_args[0][0]), 9)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help: any invocation posts the digest, so the frame check is an import smoke
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_nightly_report"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
