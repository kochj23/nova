#!/usr/bin/env python3
"""Tests for nova_strip_thinking.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


st = _load("nova_strip_thinking_t", SCRIPTS / "nova_strip_thinking.py")
SRC = (SCRIPTS / "nova_strip_thinking.py").read_text()
REPLY = "Hey there — the backup finished cleanly last night and nothing needs your attention."


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials_or_io(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        for io_call in ("urlopen", "subprocess", "psycopg2", "open("):
            self.assertNotIn(io_call, SRC)

    def test_reasoning_never_leaks_into_reply(self):
        raw = "<think>Jordan's password is in the vault, don't mention it</think>\n" + REPLY
        out = st.strip_thinking(raw)
        self.assertEqual(out, REPLY)
        self.assertNotIn("password", out)


class TestPerformance(unittest.TestCase):
    def test_10k_responses_and_no_regex_blowup(self):
        t0 = time.perf_counter()
        for _ in range(10_000):
            st.strip_thinking("Okay, let me think.\n\n" + REPLY)
        st.strip_thinking("so " * 50_000)                    # pathological single line stays linear
        self.assertLess(time.perf_counter() - t0, 3.0)


class TestRetry(unittest.TestCase):
    def test_pure_function_is_idempotent_and_never_raises(self):
        # RETRY GAP: n/a — strip_thinking makes no external call; it is pure and fail-safe on odd input
        once = st.strip_thinking("Okay, drafting.\n\n" + REPLY)
        self.assertEqual(st.strip_thinking(once), once)
        for bad in (None, "", "   ", "\n\n\n"):
            st.strip_thinking(bad)


class TestUnit(unittest.TestCase):
    def test_empty_inputs_pass_through(self):
        self.assertIsNone(st.strip_thinking(None))
        self.assertEqual(st.strip_thinking(""), "")
        self.assertEqual(st.strip_thinking("  hi  "), "hi")

    def test_reasoning_without_blank_line_kept(self):
        txt = "Okay so this is the whole answer on one line"
        self.assertEqual(st.strip_thinking(txt), txt)          # nothing safe to cut to

    def test_short_tail_not_taken(self):
        txt = "Let me check.\n\nok"                             # candidate under 20 chars
        self.assertEqual(st.strip_thinking(txt), txt)

    def test_normal_reply_untouched(self):
        self.assertEqual(st.strip_thinking(REPLY), REPLY)


class TestIntegration(unittest.TestCase):
    def test_used_by_generators(self):
        for user in ("dream_generate.py", "nova_herd_outreach.py"):
            src = (SCRIPTS / user).read_text()
            self.assertRegex(src, r"(from nova_strip_thinking import|import nova_strip_thinking)")

    def test_think_block_then_leading_reasoning(self):
        raw = "<think>x</think>\nAlright, the user wants a reply.\n\n" + REPLY
        self.assertEqual(st.strip_thinking(raw), REPLY)


class TestFunctional(unittest.TestCase):
    def test_multiple_reasoning_paragraphs_stripped(self):
        raw = ("Okay, I need to write back.\nThe email asks about backups.\n\n"
               "Let me re-read the context first.\n\n"
               "Hmm, keep it short.\n\n" + REPLY)
        self.assertEqual(st.strip_thinking(raw), REPLY)

    def test_starter_regex_covers_common_leaks(self):
        for lead in ("Okay, ", "So, ", "Let me ", "The user ", "I'll ", "Hmm ", "Based on "):
            self.assertTrue(st._REASONING_STARTERS.match(lead + "x"), lead)


class TestFrame(unittest.TestCase):
    def test_runs_and_imports_silently(self):
        self.assertNotIn('if __name__ == "__main__":', SRC)     # library module, nothing to run
        r = subprocess.run([sys.executable, str(SCRIPTS / "nova_strip_thinking.py")], capture_output=True,
                           text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual((r.returncode, r.stdout), (0, ""), r.stderr)
        r = subprocess.run([sys.executable, "-c", "import nova_strip_thinking"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
