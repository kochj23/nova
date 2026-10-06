#!/usr/bin/env python3
"""Tests for slack_reclassify.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). psql, local Ollama and the Slack/Discord post are mocked; the log
goes to a tempdir. Written by Jordan Koch (via Claude)."""
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SRC = (SCRIPTS / "slack_reclassify.py").read_text()
_TMP = tempfile.TemporaryDirectory()


def _load():
    spec = importlib.util.spec_from_file_location("slackreclass", SCRIPTS / "slack_reclassify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sr = _load()
sr.LOG_FILE = Path(_TMP.name) / "reclassify.log"
sr.print = lambda *a, **k: None
sr.time = MagicMock(time=time.time, strftime=time.strftime)     # sleep is a no-op

TRANSCRIPT = ("In this video we look at the history of the small block V8 engine, camshaft profiles, "
              "carburetor tuning and why displacement mattered so much to American muscle cars.")


class _Resp:
    def __init__(self, text):
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return json.dumps({"response": self.text}).encode()


def _main(batches, llm):
    """Drive main() with psql + Ollama + post_both mocked; return the SQL statements executed."""
    execd = []
    pages = list(batches)

    def query(sql, params=None):
        if sql.startswith("SELECT count(*)"):
            return [[str(sum(len(b) for b in batches))]]
        return pages.pop(0) if pages else []

    post = MagicMock()
    with patch.object(sr, "db_query", side_effect=query), \
         patch.object(sr, "db_exec", side_effect=execd.append), \
         patch.object(sr, "classify_batch", side_effect=llm), \
         patch.object(sr.nova_config, "post_both", post):
        sr.main()
    return execd, post


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_llm_returned_id_cannot_inject_sql(self):
        hostile = "x' OR '1'='1"
        execd, _ = _main([[["m1", TRANSCRIPT]]],
                         lambda mems: [{"id": hostile, "vector": "automotive"}, {"id": "m1", "vector": "automotive"}])
        self.assertEqual(len(execd), 1)
        self.assertTrue(execd[0].endswith("WHERE id = 'm1'"))
        self.assertFalse(any(hostile in s for s in execd))

    def test_invalid_vector_never_written(self):
        execd, _ = _main([[["m1", TRANSCRIPT]]], lambda mems: [{"id": "m1", "vector": "evil'; --"}])
        self.assertEqual(execd, [])

    def test_text_stays_on_box(self):
        self.assertTrue(sr.OLLAMA_URL.startswith("http://127.0.0.1"))
        self.assertNotIn("openrouter", SRC.lower())


class TestPerformance(unittest.TestCase):
    def test_garbage_detector_10k_fast(self):
        texts = [("word " * 50) if i % 2 else TRANSCRIPT for i in range(10_000)]
        t0 = time.perf_counter()
        flagged = sum(sr.is_garbage(t) for t in texts)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(flagged, 5_000)


class TestRetry(unittest.TestCase):
    def test_classify_failure_fails_open(self):
        # RETRY GAP: classify_batch — one local Ollama call per batch; failure returns [] (nothing moved)
        with patch.object(sr.urllib.request, "urlopen", side_effect=OSError("ollama down")) as uo:
            self.assertEqual(sr.classify_batch([("m1", "text")]), [])
        self.assertEqual(uo.call_count, 1)
        self.assertIn("will NOT be sent to cloud", sr.LOG_FILE.read_text())

    def test_psql_error_returns_empty(self):
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=2, stdout="", stderr="conn refused")):
            self.assertEqual(sr.db_query("SELECT 1"), [])
            self.assertIsNone(sr.db_exec("SELECT 1"))


class TestUnit(unittest.TestCase):
    def test_is_garbage(self):
        self.assertTrue(sr.is_garbage("   short   "))
        self.assertTrue(sr.is_garbage("thank you thank you " * 20))
        self.assertTrue(sr.is_garbage("[attachment]\nsystem: [attachment]\n[attachment]\n" + " " * 30))
        self.assertFalse(sr.is_garbage(TRANSCRIPT))

    def test_is_actual_slack(self):
        self.assertTrue(sr.is_actual_slack("Slack #nova-chat: hi"))
        self.assertTrue(sr.is_actual_slack("see <http://x|link>"))
        self.assertFalse(sr.is_actual_slack(TRANSCRIPT))

    def test_classify_parses_wrapped_json(self):
        raw = '<think>hmm</think>Here you go:\n```\n[{"id": "m1", "vector": "music"}]\n```'
        with patch.object(sr.urllib.request, "urlopen", return_value=_Resp(raw)):
            self.assertEqual(sr.classify_batch([("m1", "t")]), [{"id": "m1", "vector": "music"}])


class TestIntegration(unittest.TestCase):
    def test_prompt_shape_and_quote_sanitising(self):
        seen = {}

        def uo(req, timeout=None):
            seen["body"] = json.loads(req.data); return _Resp("[]")

        with patch.object(sr.urllib.request, "urlopen", side_effect=uo):
            sr.classify_batch([("m1", 'he said "hi"\n' + "x" * 600)])
        prompt = seen["body"]["prompt"]
        self.assertEqual(seen["body"]["model"], sr.OLLAMA_MODEL)
        self.assertIn("he said 'hi'", prompt)
        self.assertNotIn("x" * 400, prompt)                     # previews capped at 300 chars
        self.assertIn("automotive", prompt)

    def test_db_query_targets_memories_db(self):
        with patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="a\tb\n", stderr="")) as r:
            self.assertEqual(sr.db_query("SELECT 1"), [["a", "b"]])
        self.assertIn("nova_memories", r.call_args[0][0])


class TestFunctional(unittest.TestCase):
    def test_pipeline_deletes_keeps_and_moves(self):
        batch = [["g1", "tiny"], ["s1", "Slack #general: lunch?" + " " * 20 + "ok see you there friend"],
                 ["t1", TRANSCRIPT], ["bad"]]
        execd, post = _main([batch], lambda mems: [{"id": "t1", "vector": "automotive"}])
        self.assertEqual(execd[0], "DELETE FROM memories WHERE id = 'g1'")
        self.assertTrue(execd[1].endswith("WHERE id = 't1'"))
        self.assertIn("source = 'automotive'", execd[1])
        final = post.call_args[0][0]
        self.assertIn("Kept in slack: 1", final)
        self.assertIn("Reclassified: 1", final)
        self.assertIn("Deleted (garbage): 1", final)

    def test_empty_table_reports_zeroes(self):
        execd, post = _main([], lambda mems: [])
        self.assertEqual(execd, [])
        self.assertIn("Reclassified: 0", post.call_args[0][0])


class TestFrame(unittest.TestCase):
    def test_import_never_runs_main(self):
        # main() rewrites nova_memories rows, so the smoke is an import only
        self.assertIn('if __name__ == "__main__":', SRC)
        r = subprocess.run([sys.executable, "-c", "import slack_reclassify"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
