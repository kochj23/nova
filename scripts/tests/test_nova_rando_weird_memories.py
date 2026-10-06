#!/usr/bin/env python3
"""Tests for nova_rando_weird_memories.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import urllib.error    # noqa: F401  (stdlib locked in before the sys.modules-scoped load)
import urllib.request  # noqa: F401
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import psycopg2  # noqa: F401

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_rando_weird_memories.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="rando_weird_test_"))

import nova_config as REAL_CFG  # noqa: E402
import nova_journal_guard  # noqa: E402,F401

NJ = types.ModuleType("nova_journal"); NJ.git_push = MagicMock(name="git_push")
IU = types.ModuleType("nova_image_utils"); IU.generate_image = MagicMock(name="generate_image", return_value=None)
NV = types.ModuleType("nova_voice"); NV.CONTEXT_JOURNAL_WEIRD_MEMORIES = "CTX"
NV.system_prompt = lambda ctx, section="": "SYSTEM " + ctx


def _load():
    spec = importlib.util.spec_from_file_location("nrwm", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"nova_journal": NJ, "nova_image_utils": IU}), \
         patch("subprocess.run", side_effect=RuntimeError("offline")), \
         patch("urllib.request.urlopen", side_effect=RuntimeError("offline")):
        spec.loader.exec_module(mod)
    return mod


rw = _load()
# The module's nova_config handle: the real privacy gate, but Slack/Discord posting is stubbed (no shared mutation).
CFG = types.SimpleNamespace(filter_private_memories=REAL_CFG.filter_private_memories,
                            post_both=MagicMock(name="post_both"), SLACK_NOTIFY="#test")
rw.nova_config = CFG
rw.LOG_FILE = TMP / "nova_rando_weird.log"
rw.HUGO_ROOT = TMP / "nova-journal"
rw.CONTENT_DIR = rw.HUGO_ROOT / "content/operations"
rw.IMAGES_DIR = rw.HUGO_ROOT / "static/images/operations"
rw.get_openrouter_key = MagicMock(return_value="test-key")      # never reads the Keychain
rw.subprocess = MagicMock(name="subprocess")                     # cwebp / security never launched
rw.subprocess.TimeoutExpired = subprocess.TimeoutExpired

ARTICLE = ("Tonight the vector database coughed up a parking ticket, a recipe for regret and four Slack "
           "threads about printer toner. ") * 8
PERSONAL = "kochj23" + "@" + "gmail.com"
HOME = str(Path.home())


def _resp(content):
    r = MagicMock(); r.read.return_value = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
    return r


def _quiet():
    return redirect_stdout(io.StringIO())


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_key_from_keychain(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/_-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('"find-generic-password"', SRC)
        self.assertNotIn(PERSONAL, SRC)

    def test_scrub_pii(self):
        txt = f"mail {PERSONAL} or bob@example.com, file {HOME}/Documents/x, nova@digitalnoise.net stays"
        out = rw.scrub_pii(txt)
        self.assertNotIn(PERSONAL, out); self.assertNotIn("bob@example.com", out)
        self.assertNotIn(HOME + "/", out)
        self.assertIn("nova@digitalnoise.net", out)
        self.assertEqual(rw.scrub_pii(""), "")

    def test_private_sources_never_reach_the_prompt(self):
        mems = [{"text": "secret text from imessage", "source": "imessage"},
                {"text": f"public reddit post by {PERSONAL}", "source": "reddit"}]
        safe = rw.sanitize_memories(mems)
        self.assertEqual([m["source"] for m in safe], ["reddit"])
        self.assertNotIn(PERSONAL, safe[0]["text"])

    def test_sql_is_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')
        cur = MagicMock(); cur.fetchall.return_value = []
        conn = MagicMock(); conn.cursor.return_value = cur
        with patch("psycopg2.connect", return_value=conn):
            rw.get_weird_memories(hours=24, limit=7)
        self.assertEqual(cur.execute.call_args[0][1][1], 7)


class TestPerformance(unittest.TestCase):
    def test_scrub_10k_memories_fast(self):
        mems = [{"text": f"memory {i} with someone{i}@example.com inside", "source": "reddit"} for i in range(10_000)]
        t0 = time.perf_counter()
        safe = rw.sanitize_memories(mems)
        self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertEqual(len(safe), 10_000)
        self.assertTrue(all("@example.com" not in m["text"] for m in safe))


class TestRetry(unittest.TestCase):
    def test_openrouter_402_skips_cleanly(self):
        # RETRY GAP: call_llm() — one POST, no retry; 402 (out of credits) exits 0 so the scheduler stays green
        err = urllib.error.HTTPError(rw.OPENROUTER_URL, 402, "Payment Required", {}, None)
        op = MagicMock(side_effect=err)
        with patch("urllib.request.urlopen", op), _quiet():
            with self.assertRaises(SystemExit) as cm:
                rw.call_llm("s", "u")
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(op.call_count, 1)

    def test_other_http_errors_propagate_once(self):
        err = urllib.error.HTTPError(rw.OPENROUTER_URL, 500, "boom", {}, None)
        op = MagicMock(side_effect=err)
        with patch("urllib.request.urlopen", op):
            with self.assertRaises(urllib.error.HTTPError):
                rw.call_llm("s", "u")
        self.assertEqual(op.call_count, 1)


class TestUnit(unittest.TestCase):
    def test_title_is_unquoted(self):
        with patch.object(rw, "call_llm", return_value=' "The "Night" Shift" '):
            self.assertEqual(rw.generate_title("x"), "The Night Shift")

    def test_call_llm_sends_bearer_and_parses(self):
        seen = {}

        def op(req, timeout=None):
            seen["auth"] = req.get_header("Authorization"); seen["body"] = json.loads(req.data)
            return _resp("hello")
        with patch("urllib.request.urlopen", side_effect=op):
            self.assertEqual(rw.call_llm("sys", "usr", max_tokens=9), "hello")
        self.assertEqual(seen["auth"], "Bearer test-key")
        self.assertEqual(seen["body"]["max_tokens"], 9)

    def test_generate_article_caps_entries_at_real_count(self):
        mems = [{"text": f"thing {i}\nline", "source": "reddit"} for i in range(7)]
        with patch.dict(sys.modules, {"nova_voice": NV}), patch.object(rw, "call_llm", return_value="A") as cl:
            rw.generate_article(mems, {"total": 1234, "sources": {"reddit": 7}})
        system, user = cl.call_args[0]
        self.assertIn("pick the 7 weirdest", user)
        self.assertIn("Total new memories today: 1,234", user)
        self.assertIn("GROUNDING (CRITICAL)", system)


class TestIntegration(unittest.TestCase):
    def test_uses_canonical_gate_and_guard(self):
        self.assertIn("nova_config.filter_private_memories(memories)", SRC)
        self.assertIn("from nova_journal_guard import is_publishable", SRC)
        self.assertIn('nj.git_push("operations", title)', SRC)

    def test_guard_blocks_refusal_and_alerts_without_writing(self):
        CFG.post_both.reset_mock(); NJ.git_push.reset_mock()
        with _quiet():
            rw.publish("I can't write this", "You handed me a grocery list of Wikipedia excerpts. " * 10, None)
        NJ.git_push.assert_not_called()
        self.assertIn("Suppressed a non-publishable", CFG.post_both.call_args[0][0])
        self.assertFalse(rw.CONTENT_DIR.exists() and any(rw.CONTENT_DIR.glob("*cant-write*")))


class TestFunctional(unittest.TestCase):
    def test_golden_path_writes_pushes_and_posts(self):
        CFG.post_both.reset_mock(); NJ.git_push.reset_mock()
        mems = [{"text": f"odd memory {i}", "source": "reddit", "created_at": "t"} for i in range(10)]
        with patch.object(rw, "get_memory_stats_24h", return_value={"total": 500, "sources": {"reddit": 500}}), \
             patch.object(rw, "get_weird_memories", return_value=mems), \
             patch.object(rw, "generate_article", return_value=ARTICLE), \
             patch.object(rw, "generate_title", return_value="Printer Toner Haunts Me"), _quiet():
            rw.main()
        post = next(rw.CONTENT_DIR.glob("*printer-toner-haunts-me.md")).read_text()
        self.assertIn('title: "Printer Toner Haunts Me"', post)
        NJ.git_push.assert_called_once_with("operations", "Printer Toner Haunts Me")
        self.assertIn("Nightly Weird Memories posted", CFG.post_both.call_args[0][0])

    def test_grounding_guard_refuses_thin_sample(self):
        with patch.object(rw, "get_memory_stats_24h", return_value={"total": 500, "sources": {}}), \
             patch.object(rw, "get_weird_memories", return_value=[{"text": "x", "source": "imessage"}] * 20), \
             patch.object(rw, "generate_article") as ga, _quiet():
            rw.main()
        ga.assert_not_called()                      # every row was private -> 0 real memories -> no column
        self.assertIn("GROUNDING GUARD", rw.LOG_FILE.read_text())

    def test_quiet_day_skips(self):
        with patch.object(rw, "get_memory_stats_24h", return_value={"total": 5, "sources": {}}), \
             patch.object(rw, "get_weird_memories") as gw, _quiet():
            rw.main()
        gw.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_compiles(self):
        r = subprocess.run([sys.executable, "-m", "py_compile", str(SCRIPT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_import_never_runs_main(self):
        # no --help: a bare run publishes, so the smoke is an import
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_rando_weird_memories"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=60, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("[rando-weird", r.stdout)


if __name__ == "__main__":
    unittest.main()
