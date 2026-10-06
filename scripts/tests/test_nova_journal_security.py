#!/usr/bin/env python3
"""Tests for nova_journal_security.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
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
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
PATH = SCRIPTS / "nova_journal_security.py"
SRC = PATH.read_text()
_TMP = Path(tempfile.mkdtemp(prefix="secjournal_test_"))

import nova_config as _real_config  # noqa: E402
import nova_journal  # noqa: E402,F401  (pre-import shared deps so the patched home below only hits this module)
import nova_voice  # noqa: E402,F401
import nova_image_utils  # noqa: E402,F401
import nova_notify  # noqa: E402,F401
import nova_resolve  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("nova_journal_security_t", PATH)
    mod = importlib.util.module_from_spec(spec)
    # import-time: resolve_url would read the PG service registry, and the module mkdirs under ~ — neither here.
    with mock.patch.object(nova_resolve, "resolve_url", return_value="http://searx.test/search"), \
            mock.patch.object(Path, "home", return_value=_TMP):
        spec.loader.exec_module(mod)
    return mod


sj = _load()
sj.LOG_FILE = _TMP / "sec.log"
sj.nova_config = types.SimpleNamespace(post_both=mock.MagicMock(), SLACK_CHAN="C_CHAT", SLACK_NOTIFY="C_NOTIFY",
                                       NOVA_HOST="127.0.0.1")
sj.nova_notify = mock.MagicMock()
sj.generate_image = mock.MagicMock(return_value=None)
sj.get_full_context = lambda hours=24: {"security": {"security_event_count": 3}}
sj.format_security_brief = lambda ctx: "fw block from 192.168.1.43 aa:bb:cc:dd:ee:ff Jordans-iPhone"

BRIEF = "Quiet Seas, Loud Patches\n" + ("BLUF: CISA added two KEVs today [CISA] [HIGH CONFIDENCE]. " * 10)


def _ctx(payload):
    r = mock.MagicMock()
    r.__enter__.return_value.read.return_value = json.dumps(payload).encode()
    return r


class _Env(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        for m in (sj.nova_config.post_both, sj.nova_notify, sj.generate_image):
            m.reset_mock()
        ps = {"content": mock.patch.object(sj, "CONTENT_DIR", root / "content"),
              "images": mock.patch.object(sj, "IMAGES_DIR", root / "images"),
              "llm": mock.patch.object(sj.nova_journal, "call_openrouter", return_value=BRIEF),
              "push": mock.patch.object(sj.nova_journal, "git_push"),
              "snap": mock.patch.object(sj.nova_journal, "grafana_panel_image", return_value=""),
              "voice": mock.patch.object(sj.nova_voice, "system_prompt", side_effect=lambda c="", **k: c),
              "url": mock.patch.object(sj.urllib.request, "urlopen", side_effect=OSError("offline")),
              "run": mock.patch.object(sj.subprocess, "run"),
              "out": mock.patch("sys.stdout", new_callable=io.StringIO)}
        self.m = {k: p.start() for k, p in ps.items()}
        (root / "content").mkdir()
        self.root = root
        self.addCleanup(lambda: ([p.stop() for p in ps.values()], self.td.cleanup()))


class TestSecurity(_Env):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_internal_telemetry_never_reaches_cloud(self):
        self.m["url"].side_effect = OSError("ollama down")     # local summarizer unavailable -> fail closed
        self.assertIn("local summarizer unavailable", sj.summarize_ops_brief_local("fw 192.168.1.43 blocked"))
        with mock.patch.object(sj, "call_local_llm", return_value="Scans from 10.0.0.7 and 192.168.1.9 (aa:bb:cc:dd:ee:ff) on Jordans-MacBook"):
            out = sj.summarize_ops_brief_local("raw")
        for leak in ("10.0.0.7", "192.168.1.9", "aa:bb", "Jordans"):
            self.assertNotIn(leak, out)

    def test_sql_sources_are_a_fixed_allowlist(self):
        self.assertIn('for source in ["intelligence", "military_history", "law", "politics"]', SRC)
        self.assertEqual(re.findall(r"get_recent_security_memories\((\w*)\)", SRC.split("def generate_daily_briefing")[1]), ["24"])


class TestPerformance(_Env):
    def test_strip_identifiers_10k(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            sj._strip_internal_identifiers(f"host 192.168.1.{i % 255} mac aa:bb:cc:dd:ee:{i % 99:02d}")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(_Env):
    def test_network_helpers_fail_open(self):
        # RETRY GAP: recall_memories / search_news / fetch_article_text / call_local_llm — one attempt, safe default
        self.assertEqual(sj.recall_memories("x"), [])
        self.assertEqual(sj.search_news("x"), [])
        self.assertEqual(sj.fetch_article_text("https://x"), "")
        self.assertEqual(sj.call_local_llm("s", "u"), "")
        self.assertEqual(self.m["url"].call_count, 4)

    def test_psql_failure_per_source_is_skipped(self):
        # RETRY GAP: get_recent_security_memories — each source tried once, failures skipped
        self.m["run"].side_effect = OSError("no psql")
        self.assertEqual(sj.get_recent_security_memories(), [])
        self.assertEqual(self.m["run"].call_count, 4)


class TestUnit(_Env):
    def test_strip_identifiers_edges(self):
        self.assertEqual(sj._strip_internal_identifiers(None), "")
        self.assertEqual(sj._strip_internal_identifiers("8.8.8.8 is public"), "8.8.8.8 is public")
        self.assertIn("an internal host", sj._strip_internal_identifiers("172.20.1.1"))

    def test_fetch_article_strips_markup(self):
        html = "<html><script>evil()</script><nav>menu</nav><p>Real &amp; story</p></html>"
        r = mock.Mock(); r.read.return_value = html.encode()
        self.m["url"].side_effect = None; self.m["url"].return_value = r
        self.assertEqual(sj.fetch_article_text("https://x"), "Real story")
        self.assertEqual(sj.fetch_article_text(""), "")

    def test_local_llm_drops_think_block(self):
        self.m["url"].side_effect = None
        self.m["url"].return_value = _ctx({"response": "<think>hmm</think> Routine noise."})
        self.assertEqual(sj.call_local_llm("s", "u"), "Routine noise.")


class TestIntegration(_Env):
    def test_every_nova_config_attr_used_exists(self):
        for attr in set(re.findall(r"nova_config\.([A-Z_]+)\b", SRC)):
            self.assertTrue(hasattr(_real_config, attr), f"nova_config.{attr} missing")   # SLACK_CHAT bug guard

    def test_psql_rows_parse_and_llm_delegates(self):
        self.m["run"].return_value = mock.Mock(returncode=0, stdout="CISA adds KEV|intelligence|{}\n\n")
        mems = sj.get_recent_security_memories(6)
        self.assertEqual(len(mems), 4)
        self.assertEqual(mems[0], {"text": "CISA adds KEV", "source": "intelligence"})
        self.assertIn("interval '6 hours'", self.m["run"].call_args[0][0][-1])
        sj.call_llm("s", "u", max_tokens=9, temperature=0.1)
        self.m["llm"].assert_called_with("s", "u", max_tokens=9, temperature=0.1)


class TestFunctional(_Env):
    def test_daily_briefing_publishes_pushes_and_notifies(self):
        self.m["run"].return_value = mock.Mock(returncode=0, stdout="CISA adds KEV for a router|intelligence|{}")
        sj.generate_daily_briefing()
        posts = list((self.root / "content").glob("*.md"))
        self.assertEqual(len(posts), 1)
        self.assertIn('title: "🛡️ Quiet Seas, Loud Patches"', posts[0].read_text())
        self.m["push"].assert_called_once_with("security", "Quiet Seas, Loud Patches")
        self.assertEqual(sj.nova_notify.call_args[1]["dedup_key"], "security-daily-briefing")
        cloud_prompt = self.m["llm"].call_args[0][1]
        self.assertNotIn("192.168.1.43", cloud_prompt)               # raw ops brief never sent up
        sj.nova_config.post_both.assert_not_called()

    def test_breaking_alert_pings_chat_and_guard_blocks_refusals(self):
        self.m["llm"].return_value = "Router Zero-Day\n" + "Active exploitation confirmed by CISA. " * 8
        sj.generate_breaking_alert("router zero-day", "x" * 500)
        self.assertEqual(sj.nova_notify.call_args[1]["level"], "critical")
        self.assertEqual(sj.nova_config.post_both.call_args[1]["slack_channel"], "C_CHAT")
        self.m["push"].reset_mock()
        self.assertEqual(sj.publish_hugo("Need input", "I need the URL to fetch the article. Can you provide it?", [], "d"), "")
        self.m["push"].assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        code = ("import nova_resolve; nova_resolve.resolve_url = lambda *a, **k: 'http://x'; "
                "import nova_journal_security")
        with tempfile.TemporaryDirectory() as home:
            r = subprocess.run([sys.executable, "-c", code], cwd=str(SCRIPTS), capture_output=True, text=True,
                               timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1", "HOME": home})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
