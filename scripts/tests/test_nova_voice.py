#!/usr/bin/env python3
"""Tests for nova_voice.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import importlib.util
import os
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_voice.py"
SRC = SCRIPT.read_text()


def _load():
    spec = importlib.util.spec_from_file_location("nova_voice_ut", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


nv = _load()


def _offline():
    """Patch out every DB/organ lane so system_prompt builds from constants only."""
    return patch.multiple(nv, _live_facts=lambda: "", _recent_activity=lambda: "", _inner_state=lambda: "")


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_live_fact_queries_are_read_only_and_parameterless(self):
        self.assertIsNone(re.search(r"\.execute\(\s*f[\"']", SRC))
        for verb in ("INSERT", "UPDATE", "DELETE", "DROP"):
            self.assertNotIn(verb + " ", SRC)

    def test_hard_boundaries_stated_in_the_voice(self):
        self.assertIn("NO sexual or explicit content", nv.NOVA_VOICE)
        self.assertIn("NO emojis", nv.NOVA_VOICE)


class TestPerformance(unittest.TestCase):
    def test_10k_prompt_builds_stay_fast(self):
        with _offline():
            t0 = time.perf_counter()
            for _ in range(10_000):
                nv.system_prompt(nv.CONTEXT_CHAT, flavor=False)
            self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(unittest.TestCase):
    def test_live_facts_fails_open_on_db_error(self):
        # RETRY GAP: _live_facts/_recent_activity — one connect attempt, "" on any failure (never breaks 40+ callers)
        with patch("psycopg2.connect", side_effect=OSError("pg down")) as c:
            self.assertEqual(nv._live_facts(), "")
            self.assertEqual(nv._recent_activity(), "")
        self.assertTrue(c.called)

    def test_flavor_seasoning_failure_never_breaks_prompt(self):
        bad = types.ModuleType("nova_lexicon")
        bad.seasoning = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("lexicon down"))
        with _offline(), patch.dict(sys.modules, {"nova_lexicon": bad}):
            p = nv.system_prompt(nv.CONTEXT_JOURNAL_OPS, flavor=True)
        self.assertTrue(p.startswith(nv.NOVA_VOICE))


class TestUnit(unittest.TestCase):
    def test_infer_section_from_context_block(self):
        self.assertEqual(nv._infer_section(nv.CONTEXT_JOURNAL_SECURITY), "security")
        self.assertEqual(nv._infer_section(nv.CONTEXT_JOURNAL_RESEARCH), "research")
        self.assertEqual(nv._infer_section("a context no block matches"), "")

    def test_shared_context_is_facts_plus_activity(self):
        with patch.object(nv, "_live_facts", return_value="F"), patch.object(nv, "_recent_activity", return_value="A"):
            self.assertEqual(nv.shared_context(), "FA")

    def test_short_prompt_appends_context(self):
        with _offline():
            out = nv.system_prompt_short("EXTRA")   # dials block sits between voice and context (2026-10-08)
            bare = nv.system_prompt_short()
        self.assertTrue(out.startswith(nv.NOVA_VOICE_SHORT))
        self.assertTrue(out.endswith("\nEXTRA"))
        self.assertIn("YOUR DIALS", out)
        self.assertEqual(out, bare + "\nEXTRA")


class TestIntegration(unittest.TestCase):
    def test_seasoning_called_with_inferred_section(self):
        lex = types.ModuleType("nova_lexicon"); lex.seasoning = MagicMock(return_value="\n[seasoned]")
        with _offline(), patch.dict(sys.modules, {"nova_lexicon": lex}):
            p = nv.system_prompt(nv.CONTEXT_JOURNAL_ESSAY, topic="memory", flavor=True)
        lex.seasoning.assert_called_once_with("essays", "memory")
        self.assertIn("[seasoned]", p)

    def test_flavor_false_skips_seasoning(self):
        lex = types.ModuleType("nova_lexicon"); lex.seasoning = MagicMock()
        with _offline(), patch.dict(sys.modules, {"nova_lexicon": lex}):
            nv.system_prompt(nv.CONTEXT_JOURNAL_OPS, flavor=False)
        lex.seasoning.assert_not_called()


class TestFunctional(unittest.TestCase):
    def test_system_prompt_assembles_voice_facts_and_context(self):
        with patch.object(nv, "_live_facts", return_value="\nFACTS"), patch.object(nv, "_recent_activity", return_value=""), \
             patch.object(nv, "_inner_state", return_value=""):
            p = nv.system_prompt(nv.CONTEXT_CHAT, flavor=False)
        self.assertTrue(p.startswith(nv.NOVA_VOICE))
        self.assertIn("FACTS", p)
        self.assertTrue(p.endswith("\n" + nv.CONTEXT_CHAT))

    def test_live_facts_formats_memory_count_and_ground_truth(self):
        class _Cur:
            def __init__(self, rows): self.rows = rows
            def execute(self, *a): pass
            def fetchone(self): return self.rows[0]
            def fetchall(self): return self.rows[1:]
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class _Conn:
            seq = [[(1_600_000,)], [(0,), ("Nova runs on the Mac Studio.",)]]
            def __init__(self, *a, **k): self.cur = _Cur(_Conn.seq.pop(0))
            def cursor(self): return self.cur
            def __enter__(self): return self
            def __exit__(self, *a): return False
        with patch("psycopg2.connect", _Conn):
            out = nv._live_facts()
        self.assertIn("Current memory count: 1,600,000.", out)
        self.assertIn("- Nova runs on the Mac Studio.", out)


class TestFrame(unittest.TestCase):
    def test_import_is_side_effect_free(self):
        self.assertNotIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_voice as v; print('NO emojis' in v.NOVA_VOICE)"],
                           cwd=str(SCRIPTS), capture_output=True, text=True, timeout=30,
                           env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "True")


if __name__ == "__main__":
    unittest.main()
