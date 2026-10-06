#!/usr/bin/env python3
"""Tests for nova_morning_brief.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude).
Weather/calendar/memory HTTP, gh + mail-fetch subprocesses, PG, the ops context and nova_notify are all
mocked at module load: no real mailbox, no Slack post."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_morning_brief.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="morningbrief-test-"))


def _load():
    spec = importlib.util.spec_from_file_location("morning_brief_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mb = _load()
# module-level stubs: nothing leaves the process, state paths in a tempdir
mb.MEMORY_DIR = TMP / "memory"; mb.MEMORY_DIR.mkdir()
mb.SUMMARY_FILE = TMP / "nova_mail_fetch.txt"
mb.notify = MagicMock()
mb.log = MagicMock()
mb.urllib = types.SimpleNamespace(
    request=types.SimpleNamespace(Request=mb.urllib.request.Request, urlopen=MagicMock(side_effect=OSError("offline"))),
    error=mb.urllib.error, parse=mb.urllib.parse)
mb.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=OSError("offline: subprocess stubbed")))
mb.get_full_context = MagicMock(return_value={})
mb._autonomy = None


def _cm(body, status=200):
    r = MagicMock(); inner = r.__enter__.return_value
    inner.read.return_value = body.encode() if isinstance(body, str) else body
    inner.status = status
    r.status = status
    return r


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertNotIn("appid=", SRC)                         # keyless weather sources only

    def test_voice_output_stays_disabled(self):
        # HomePod TTS was disabled 2026-04-09 (fired during meetings); nothing may speak
        self.assertNotRegex(SRC, r"\b(say|afplay|homepod_speak)\b\s*\(")
        self.assertNotIn('"say"', SRC)

    def test_subprocesses_are_argv(self):
        self.assertNotIn("shell=True", SRC)


class TestPerformance(unittest.TestCase):
    def test_email_priorities_capped_on_large_file(self):
        (mb.MEMORY_DIR / f"{mb.TODAY}.md").write_text("\n".join(f"🔴 HIGH item {i} " + "x" * 300 for i in range(10_000)))
        t0 = time.perf_counter()
        highs = mb.get_email_priorities()
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(highs), 3)
        self.assertTrue(all(len(h) <= 120 for h in highs))
        (mb.MEMORY_DIR / f"{mb.TODAY}.md").unlink()


class TestRetry(unittest.TestCase):
    def test_weather_falls_through_two_failures_to_open_meteo(self):
        meteo = _cm(json.dumps({"current": {"temperature_2m": 71, "relative_humidity_2m": 40, "wind_speed_10m": 3}}))
        m = MagicMock(side_effect=[OSError("wttr 1"), OSError("wttr 2"), meteo])
        with patch.object(mb.urllib.request, "urlopen", m):
            self.assertEqual(mb.get_weather(), "71°F humidity 40% wind 3mph")
        self.assertEqual(m.call_count, 3)

    def test_weather_all_down_is_safe_default(self):
        self.assertEqual(mb.get_weather(), "Weather: unavailable")

    def test_calendar_falls_back_to_oneonone(self):
        today = {"title": "Standup", "date": mb.TODAY}
        r = types.SimpleNamespace(returncode=0, stdout=json.dumps({"meetings": [today, {"title": "Old", "date": "1999-01-01"}]}))
        with patch.object(mb.subprocess, "run", return_value=r):
            self.assertEqual(mb.get_calendar_events(), ["Standup"])


class TestUnit(unittest.TestCase):
    def test_weather_celsius_converted(self):
        with patch.object(mb.urllib.request, "urlopen", return_value=_cm("Sunny +20°C feels +19°C humidity 30%")):
            self.assertTrue(mb.get_weather().startswith("Sunny 68°F"))

    def test_calendar_formats(self):
        ev = {"today": [{"title": "1:1", "startDate": "2026-01-01T09:30:00", "durationMinutes": 30, "location": "Zoom"}]}
        with patch.object(mb.urllib.request, "urlopen", return_value=_cm(json.dumps(ev))):
            self.assertEqual(mb.get_calendar_events(), ["09:30 1:1 (30min) @ Zoom"])
        with patch.object(mb.urllib.request, "urlopen", return_value=_cm(json.dumps({"today": []}))):
            self.assertEqual(mb.get_calendar(), "No meetings today.")

    def test_health_and_autonomy_fail_open(self):
        issues, count = mb.get_system_health()
        self.assertIn("vector memory server is down", issues)
        self.assertEqual(count, 0)
        self.assertEqual(mb.get_autonomy_note(), [])

    def test_mail_summary_counts(self):
        mb.SUMMARY_FILE.write_text("📬 a@example.test — 2\n[UNREAD] FROM: Amex <x>\nSUBJ: Bill\n"
                                   "[UNREAD] FROM: Wayfair <y>\nSUBJ: Sale\n", encoding="utf-8")
        ok = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(mb.subprocess, "run", return_value=ok):
            s = mb.get_mail_summary()
        self.assertEqual((s["total_unread"], s["noise_count"], s["success"]), (2, 1, True))
        self.assertEqual(s["important"], ["Bill — Amex <x>"])


class TestIntegration(unittest.TestCase):
    def test_reuses_mail_deliver_parsers(self):
        import nova_mail_deliver
        self.assertIs(mb.parse_accounts_from_file.__code__, nova_mail_deliver.parse_accounts_from_file.__code__)
        self.assertNotIn("def parse_accounts_from_file", SRC)

    def test_slack_post_is_notify_bus_deduped_per_day(self):
        mb.notify.reset_mock()
        mb.slack_post("*🌅 Good morning*\nbody")
        self.assertEqual(mb.notify.call_args[1]["dedup_key"], f"morning-brief-{mb.TODAY}")
        self.assertEqual(mb.notify.call_args[1]["category"], "morning_brief")


class TestFunctional(unittest.TestCase):
    def _main(self, mail, issues=()):
        mb.notify.reset_mock()
        with patch.object(mb, "get_weather", return_value="Clear 70°F"), \
             patch.object(mb, "get_email_priorities", return_value=[]), \
             patch.object(mb, "get_calendar_events", return_value=["09:00 Standup (15min)"]), \
             patch.object(mb, "get_mail_summary", return_value=mail), \
             patch.object(mb, "get_github_overnight", return_value=["MLXCode: 5 stars"]), \
             patch.object(mb, "get_system_health", return_value=(list(issues), 1234)), \
             patch.object(mb, "get_autonomy_note", return_value=[]), \
             patch.object(mb, "vector_remember") as vr:
            mb.main()
        return mb.notify.call_args, vr

    def test_golden_path_posts_brief_and_remembers(self):
        mail = {"total_unread": 3, "important": ["Bill — Amex"], "noise_count": 1, "success": True}
        call, vr = self._main(mail)
        body = call[1]["body"]
        for s in ("Clear 70°F", "Standup", "3 unread", "Bill — Amex", "MLXCode", "1234 memories"):
            self.assertIn(s, body)
        self.assertIn("Mail: 3 unread, 1 important", vr.call_args[0][0])

    def test_system_issue_surfaces_and_no_clean_banner(self):
        call, _ = self._main({"total_unread": 0, "important": [], "noise_count": 0, "success": True},
                             issues=["vector memory server is down"])
        body = call[1]["body"]
        self.assertIn("System alerts", body)
        self.assertNotIn("Clean overnight", body)


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # no --help/--selftest: running the script fetches mail and posts the brief, so import is the smoke test
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_morning_brief as m; print(callable(m.main))"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
