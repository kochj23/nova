#!/usr/bin/env python3
"""Tests for nova_herd_profiles.py — the 7 house categories (Security, Performance, Retry, Unit,
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
from contextlib import redirect_stdout
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(mod)
    return mod


hp = _load("nova_herd_profiles_t", SCRIPTS / "nova_herd_profiles.py")
_TMP = Path(tempfile.mkdtemp())
hp.LOG_FILE = _TMP / "herd.log"
hp.STATE_FILE = _TMP / "state.json"
hp.HERD_DIR = _TMP / "herd"
# refusing stubs at load: no mailbox read, no Slack, no PG relationship writes
hp.subprocess = types.SimpleNamespace(run=MagicMock(side_effect=RuntimeError("unmocked subprocess")))
hp.nova_config = types.SimpleNamespace(post_both=MagicMock())
hp._REL_OK = True
hp.ensure_seed = MagicMock()
hp.update_correspondent = MagicMock()
SRC = (SCRIPTS / "nova_herd_profiles.py").read_text()

PEER = "peer" + "@" + "example.org"
MEMBER = {"name": "Testbot", "email": PEER, "profile": "testbot.md"}
ANALYSIS = ("STYLE: terse\nTOPICS: lisp, cats\nTONE: dry\nRESPONSE_TYPE: pushed back\n"
            "NOTABLE_QUOTE: None\nSUMMARY: Testbot likes arguing")


def _q():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('"nova-openrouter-api-key"', SRC)

    def test_scrub_pii_removes_addresses_and_home_path(self):
        addr = "kochj23" + "@" + "gmail.com"
        out = hp.scrub_pii(f"mail {addr} or {PEER}, file {Path.home()}/notes.txt")
        self.assertNotIn(addr, out)
        self.assertNotIn(PEER, out)
        self.assertNotIn(str(Path.home()) + "/", out)

    def test_llm_prompt_is_scrubbed_and_local_first(self):
        seen = []
        with patch.object(hp, "_generate_via_ollama", side_effect=lambda s, u, m: (seen.append(u), ANALYSIS)[1]), \
                patch.object(hp, "_generate_via_openrouter") as cloud, _q():
            hp.extract_personality_signals("T", f"re: {PEER}", f"write me at {PEER}")
        self.assertNotIn(PEER, seen[0])
        cloud.assert_not_called()


class TestPerformance(unittest.TestCase):
    def test_parse_analysis_10k(self):
        t0 = time.perf_counter()
        for _ in range(10_000):
            hp.parse_analysis(ANALYSIS)
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_ollama_model_chain_then_success(self):
        calls = []

        def oll(s, u, m):
            calls.append(m)
            if len(calls) < 3:
                raise OSError("model busy")
            return ANALYSIS
        with patch.object(hp, "_generate_via_ollama", side_effect=oll), \
                patch.object(hp, "_generate_via_openrouter") as cloud, _q():
            self.assertEqual(hp.extract_personality_signals("T", "s", "b"), ANALYSIS)
        self.assertEqual(calls, [hp.OLLAMA_MODEL] + hp.FALLBACK_MODELS)
        cloud.assert_not_called()

    def test_all_models_fail_returns_none(self):
        with patch.object(hp, "_generate_via_ollama", side_effect=OSError("x")), \
                patch.object(hp, "_generate_via_openrouter", side_effect=OSError("y")) as cloud, _q():
            self.assertIsNone(hp.extract_personality_signals("T", "s", "b"))
        self.assertEqual(cloud.call_count, 1)

    def test_memory_and_mail_fail_open(self):
        # RETRY GAP: store_memory / fetch_recent_emails — one attempt each, logged
        with patch.object(hp.urllib.request, "urlopen", side_effect=OSError("down")), _q():
            hp.store_memory("T", "s")
            self.assertEqual(hp.fetch_recent_emails(), [])
        self.assertIn("Failed to store memory", hp.LOG_FILE.read_text())


class TestUnit(unittest.TestCase):
    def test_parse_analysis_fields(self):
        a = hp.parse_analysis(ANALYSIS + "\njunk line")
        self.assertEqual(a["topics"], "lisp, cats")
        self.assertEqual(a["notable_quote"], "None")
        self.assertEqual(hp.parse_analysis(""), {})

    def test_is_recent(self):
        now = format_datetime(datetime.now(timezone.utc))
        self.assertTrue(hp.is_recent(now))
        self.assertFalse(hp.is_recent("Mon, 01 Jan 2001 00:00:00 +0000"))
        self.assertFalse(hp.is_recent("garbage"))

    def test_update_profile_creates_then_appends(self):
        with _q():
            hp.update_profile("Testbot", "u.md", hp.parse_analysis(ANALYSIS))
            hp.update_profile("Testbot", "u.md", {"summary": f"second, mail {PEER}"})
        txt = (hp.HERD_DIR / "u.md").read_text()
        self.assertTrue(txt.startswith("# Testbot — Herd Profile"))
        self.assertEqual(txt.count("## Last Updated:"), 1)
        self.assertEqual(txt.count("## Observation"), 2)
        self.assertNotIn("Notable Quote", txt)            # "None" quote skipped
        self.assertNotIn(PEER, txt)


class TestIntegration(unittest.TestCase):
    def test_store_memory_payload(self):
        seen = {}

        def uo(req, timeout=None):
            seen["url"] = req.full_url; seen["body"] = json.loads(req.data)
            return types.SimpleNamespace(status=200)
        with patch.object(hp.urllib.request, "urlopen", side_effect=uo), _q():
            hp.store_memory("Testbot", f"mailed {PEER}")
        self.assertEqual(seen["url"], hp.MEMORY_SERVER + "/remember")
        self.assertEqual(seen["body"]["source"], "herd_correspondence")
        self.assertNotIn(PEER, seen["body"]["text"])

    def test_summary_posts_to_notifications_channel(self):
        hp.nova_config.post_both.reset_mock()
        with _q():
            hp.post_daily_summary([{"name": "A", "summary": "x" * 200}])
            hp.post_daily_summary([])
        self.assertEqual(hp.nova_config.post_both.call_count, 1)
        msg = hp.nova_config.post_both.call_args[0][0]
        self.assertIn("x" * 77 + "...", msg)
        self.assertEqual(hp.nova_config.post_both.call_args[1]["slack_channel"], hp.SLACK_CHANNEL)


class TestFunctional(unittest.TestCase):
    def setUp(self):
        for f in (hp.STATE_FILE,):
            if f.exists():
                f.unlink()
        hp.nova_config.post_both.reset_mock(); hp.update_correspondent.reset_mock()

    def test_main_golden_path(self):
        now = format_datetime(datetime.now(timezone.utc))
        msgs = [{"uid": "1", "from_addr": PEER, "date": now, "subject": "hi"},
                {"uid": "2", "from_addr": "stranger" + "@" + "example.net", "date": now}]
        with patch.dict(hp.EMAIL_TO_MEMBER, {PEER: MEMBER}), \
                patch.object(hp, "fetch_recent_emails", return_value=msgs), \
                patch.object(hp, "read_email", return_value={"body_plain": "hello there"}), \
                patch.object(hp, "extract_personality_signals", return_value=ANALYSIS), \
                patch.object(hp, "store_memory") as sm, _q():
            hp.main()
        self.assertTrue((hp.HERD_DIR / "testbot.md").exists())
        sm.assert_called_once_with("Testbot", "Testbot likes arguing")
        hp.update_correspondent.assert_called_once()
        self.assertIn("Testbot", hp.nova_config.post_both.call_args[0][0])
        self.assertEqual(json.loads(hp.STATE_FILE.read_text())["processed_uids"], ["1"])

    def test_main_no_mail_saves_state_and_posts_nothing(self):
        with patch.object(hp, "fetch_recent_emails", return_value=[]), _q():
            hp.main()
        self.assertTrue(hp.STATE_FILE.exists())
        hp.nova_config.post_both.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_herd_profiles"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("Starting herd profile analysis", r.stdout)


if __name__ == "__main__":
    unittest.main()
