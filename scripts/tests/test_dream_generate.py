#!/usr/bin/env python3
"""Tests for dream_generate.py — the 7 house categories (Security, Performance, Retry, Unit,
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
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "dream_generate.py"
SRC = SCRIPT.read_text()

import nova_config  # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("dream_generate_t", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dg = _load()
_guards = []


def setUpModule():
    for p in (patch("urllib.request.urlopen", side_effect=OSError("offline")),
              patch.object(nova_config, "post_both")):
        p.start()
        _guards.append(p)


def tearDownModule():
    for p in _guards:
        p.stop()


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _resp(obj):
    return _Resp(json.dumps(obj).encode())


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.ps = [patch.object(dg, "CIRCUIT_BREAKER_FILE", t / "cb"),
                   patch.object(dg, "JOURNAL_DIR", t / "dreams"),
                   patch.object(dg, "PENDING", t / "pending.json"),
                   patch.object(dg, "WORKSPACE", t),
                   patch.object(dg, "TODAY", "2026-02-03")]
        for p in self.ps:
            p.start()
        self.out = io.StringIO()
        self._r = redirect_stdout(self.out)
        self._r.__enter__()

    def tearDown(self):
        self._r.__exit__(None, None, None)
        for p in self.ps:
            p.stop()
        self.tmp.cleanup()


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/_-]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))
        self.assertIn('"nova-openrouter-api-key"', SRC)

    def test_sql_parameterized(self):
        self.assertNotRegex(SRC, r'execute\(\s*f["\']')
        self.assertIn("source NOT IN %s", SRC)

    def test_scrub_pii_redacts_emails_and_home(self):
        addr = "kochj23" + "@" + "gmail.com"
        out = dg._scrub_pii(f"mail {addr} at {Path.home()}/secret.txt or bob@x.org, keep nova@digitalnoise.net")
        self.assertNotIn(addr, out)
        self.assertNotIn(str(Path.home()) + "/", out)
        self.assertNotIn("bob@x.org", out)
        self.assertIn("nova@digitalnoise.net", out)

    def test_sanitize_inspirations_drops_private(self):
        mems = [{"source": "imessage", "label": "a", "memory": "hi"},
                {"source": "wikipedia", "label": "b", "memory": "ping bob@x.org"}]
        safe = dg.sanitize_inspirations(mems)
        self.assertEqual([m["source"] for m in safe], ["wikipedia"])
        self.assertIn("[redacted]", safe[0]["memory"])


class TestPerformance(unittest.TestCase):
    def test_scrub_10k_fast(self):
        t0 = time.perf_counter()
        for i in range(10_000):
            dg._scrub_pii(f"memory {i} with user{i}@example.com inside")
        self.assertLess(time.perf_counter() - t0, 2.0)


class TestRetry(_Tmp):
    def test_ollama_fallback_chain_and_circuit(self):
        calls = []

        def fake_ollama(prompt, model):
            calls.append(model)
            if len(calls) < 3:
                raise OSError("busy")
            return "word " * 150
        with patch.object(dg, "query_recent_memories_for_theme", return_value=("", [])), \
             patch.object(dg, "derive_theme", return_value="a theme"), \
             patch.object(dg, "fetch_unused", return_value=[]), \
             patch.object(dg, "_generate_via_openrouter", side_effect=RuntimeError("no key")), \
             patch.object(dg, "get_available_model", return_value=dg.OLLAMA_MODEL), \
             patch.object(dg, "_generate_via_ollama", side_effect=fake_ollama):
            text, _, meta = dg.generate_narrative()
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0], dg.OLLAMA_MODEL)
        self.assertGreater(len(text.split()), 100)
        self.assertFalse(dg.CIRCUIT_BREAKER_FILE.exists())  # reset after success

    def test_circuit_breaker_opens_after_three_failures(self):
        for _ in range(3):
            dg._ollama_circuit_record_failure()
        self.assertTrue(dg._ollama_circuit_open())
        dg._ollama_circuit_reset()
        self.assertFalse(dg._ollama_circuit_open())

    def test_recall_fails_open(self):
        # RETRY GAP: recall — single attempt, returns [] on error
        with patch.object(dg.urllib.request, "urlopen", side_effect=OSError("down")):
            self.assertEqual(dg.recall("x"), [])


class TestUnit(_Tmp):
    def test_derive_theme_without_text_picks_canned(self):
        with patch.object(dg.random, "random", return_value=0.99):
            self.assertTrue(len(dg.derive_theme("")) > 5)

    def test_summarize_image_fallback_uses_last_line(self):
        with patch.object(dg, "_generate_short", return_value=""):
            self.assertEqual(dg._summarize_dream_for_image("a\nthe final scene", "noir"), "the final scene")

    def test_get_available_model_falls_back(self):
        body = {"models": [{"name": dg.FALLBACK_MODELS[1]}]}
        with patch.object(dg.urllib.request, "urlopen", return_value=_resp(body)):
            self.assertEqual(dg.get_available_model(), dg.FALLBACK_MODELS[1])

    def test_write_journal_dedupes_inspirations(self):
        insp = [{"source": "wiki", "label": "L", "memory": "m1"}, {"source": "wiki", "label": "L", "memory": "m2"}]
        p = dg.write_journal("story", None, insp, {"theme": "t", "mood": "noir"})
        txt = p.read_text()
        self.assertEqual(txt.count("**[wiki]**"), 1)
        self.assertIn("Dream Journal — 2026-02-03", txt)


class TestIntegration(_Tmp):
    def test_recent_query_uses_memories_table_and_exclusions(self):
        cur = MagicMock()
        cur.fetchall.return_value = [("x" * 60, '{"title": "T"}', "wiki", None)]
        conn = MagicMock(cursor=MagicMock(return_value=cur))
        with patch.object(dg, "_pg_connect", return_value=conn):
            text, recs = dg.query_recent_memories_for_theme()
        sql, params = cur.execute.call_args[0]
        self.assertIn("FROM memories", sql)
        self.assertEqual(params, (dg.EXCLUDE_SOURCES,))
        self.assertEqual(recs[0]["label"], "T")
        self.assertIn("[T]", text)

    def test_sanitize_delegates_to_nova_config(self):
        with patch.object(nova_config, "filter_private_memories", side_effect=lambda m: m[:1]) as f:
            out = dg.sanitize_inspirations([{"source": "a", "memory": "x"}, {"source": "b", "memory": "y"}])
        f.assert_called_once()
        self.assertEqual(len(out), 1)


class TestFunctional(_Tmp):
    def test_main_golden_path_writes_journal_and_pending(self):
        meta = {"theme": "t", "mood": "noir", "used_ids": [1, 2]}
        with patch.object(dg, "generate_narrative", return_value=("dream text " * 60, [], meta)), \
             patch.object(dg, "generate_dream_image", return_value=""), \
             patch.object(dg, "store_memory") as sm, patch.object(dg, "mark_used", return_value=2) as mu, \
             patch.object(dg, "deliver_dream", return_value=True) as dd, \
             patch.object(nova_config, "post_both") as pb:
            dg.main()
        pend = json.loads(dg.PENDING.read_text())
        self.assertEqual(pend["date"], "2026-02-03")
        self.assertIsNone(pend["image"])
        self.assertTrue((dg.JOURNAL_DIR / "2026-02-03.md").exists())
        mu.assert_called_once_with([1, 2])
        sm.assert_called_once()
        dd.assert_called_once()
        pb.assert_called_once()  # image-failure alert went through the (mocked) poster

    def test_main_empty_narrative_exits_1(self):
        with patch.object(dg, "generate_narrative", return_value=("", [], {})), \
             self.assertRaises(SystemExit) as cm:
            dg.main()
        self.assertEqual(cm.exception.code, 1)

    def test_main_skips_when_pending_exists(self):
        dg.PENDING.write_text(json.dumps({"date": "2026-02-03", "narrative": "x"}))
        with patch.object(dg, "generate_narrative") as gn, patch.object(dg, "deliver_dream") as dd:
            dg.main()
        gn.assert_not_called()
        dd.assert_called_once()


class TestFrame(unittest.TestCase):
    def test_import_smoke_does_not_run_main(self):
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import dream_generate; print('ok')"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "ok")


if __name__ == "__main__":
    unittest.main()
