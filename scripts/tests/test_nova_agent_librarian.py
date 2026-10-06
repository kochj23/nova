#!/usr/bin/env python3
"""Tests for nova_agent_librarian.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
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
from unittest.mock import AsyncMock, MagicMock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_agent_librarian.py"
SRC = SCRIPT.read_text()
TMP = Path(tempfile.mkdtemp(prefix="librarian-test-"))

import nova_logger      # noqa: E402  (shared log sink; redirected to TMP for the life of this module)
import nova_subagent    # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


lib = _load("librarian_under_test", SCRIPT)

# Every outbound side effect is stubbed for the whole module: the event bus (nova_notify -> PG), the
# memory server / LLM (urlopen) and the structured log file. Started in setUpModule and stopped in
# tearDownModule so nothing leaks into the next test file of the session.
_PATCHES = [
    patch.object(nova_subagent, "nova_notify", MagicMock(return_value=True)),
    patch("urllib.request.urlopen", MagicMock(side_effect=AssertionError("offline: urlopen must be stubbed"))),
    patch.object(nova_logger, "LOG_DIR", TMP),
    patch.object(nova_logger, "LOG_FILE", TMP / "nova.jsonl"),
]


def setUpModule():
    for p in _PATCHES:
        p.start()


def tearDownModule():
    for p in reversed(_PATCHES):
        p.stop()


def _agent(recall=None, infer=None):
    """A LibrarianAgent with no Redis, no memory server, no LLM, no Slack."""
    a = lib.LibrarianAgent.__new__(lib.LibrarianAgent)
    a._redis = MagicMock(); a._pubsub = MagicMock(); a._running = False
    a._task_count = 0; a._start_time = None; a._last_error = None
    a.recall = AsyncMock(return_value=[] if recall is None else recall)
    a.infer = AsyncMock(return_value="" if infer is None else infer, side_effect=infer if isinstance(infer, Exception) else None)
    a.report_to_jordan = AsyncMock()
    return a


def _mem(i, text=None, source="journal"):
    return {"id": f"m{i}", "text": text or f"memory number {i}", "source": source, "score": 0.5}


def _run(coro):
    return asyncio.run(coro)


FINDINGS = {"findings": [
    {"type": "duplicate", "severity": "high", "memory_ids": ["m1", "m2"], "description": "same fact twice", "recommendation": "merge"},
    {"type": "stale", "severity": "low", "memory_ids": ["m3"], "description": "old address", "recommendation": "update"},
], "stats": {"memories_analyzed": 3, "duplicates_found": 1, "stale_found": 1}}


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_flag_and_report_only_never_writes_memory(self):
        # the Librarian contract: it may read (recall) but never remember/delete/update a memory or touch SQL
        self.assertNotIn("self.remember(", SRC)
        self.assertNotIn("/remember", SRC)
        self.assertNotIn("/forget", SRC)
        self.assertNotIn("/delete", SRC)
        self.assertIsNone(re.search(r"\b(INSERT INTO|UPDATE|DELETE FROM)\b", SRC))
        self.assertIn("NEVER modify or delete memories", lib.SYSTEM_PROMPT)

    def test_report_truncates_untrusted_llm_text(self):
        # a hostile / runaway model answer is clipped before it reaches Slack: 150-char descriptions, 3 ids, 10 findings
        bad = {"findings": [{"type": "duplicate", "severity": "high", "memory_ids": [f"id{i}" for i in range(50)],
                             "description": "<script>" + "x" * 5000, "recommendation": "merge"}] * 40}
        a = _agent(recall=[_mem(1), _mem(2)], infer=json.dumps(bad))
        _run(a._curate_batch({"query": "q"}))
        msg = a.report_to_jordan.call_args[0][0]
        self.assertLess(len(msg), 10 * 400)
        self.assertEqual(msg.count("DUPLICATE* (high)"), 10)
        self.assertNotIn("id3,", msg)
        self.assertNotIn("x" * 151, msg)


class TestPerformance(unittest.TestCase):
    def test_curate_10k_memories_formats_and_parses_fast(self):
        mems = [_mem(i, text="t" * 400) for i in range(10_000)]
        findings = {"findings": [{"type": "relationship", "severity": "low", "memory_ids": [f"m{i}", f"m{i+1}"],
                                  "description": f"link {i}", "recommendation": "link"} for i in range(10_000)],
                    "stats": {"memories_analyzed": 10_000}}
        a = _agent(recall=mems, infer="Here you go:\n" + json.dumps(findings) + "\nDone.")
        t0 = time.perf_counter()
        result = _run(a._curate_batch({"query": "everything", "batch_size": 10_000}))
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertEqual(len(result["findings"]), 10_000)
        prompt = a.infer.call_args[0][0]
        self.assertNotIn("t" * 301, prompt)            # each memory clipped to 300 chars in the prompt
        a.report_to_jordan.assert_called_once()


class TestRetry(unittest.TestCase):
    def test_inference_failure_fails_open(self):
        # RETRY GAP: _curate_batch/self.infer — one attempt; an LLM error returns None and nothing is posted
        a = _agent(recall=[_mem(1), _mem(2)], infer=RuntimeError("mlx down"))
        self.assertIsNone(_run(a._curate_batch({"query": "q"})))
        a.report_to_jordan.assert_not_called()
        self.assertEqual(a.infer.await_count, 1)

    def test_source_fetch_failure_fails_open(self):
        # RETRY GAP: _curate_batch/urllib.request.urlopen — a dead memory server yields an empty batch -> None
        a = _agent()
        calls = []
        with patch.object(lib.urllib.request, "urlopen", side_effect=lambda *x, **k: calls.append(1) or (_ for _ in ()).throw(OSError("refused"))):
            self.assertIsNone(_run(a._curate_batch({"source": "journal"})))
        self.assertEqual(calls, [1])
        a.infer.assert_not_awaited()

    def test_duplicate_check_inference_failure_fails_open(self):
        # RETRY GAP: _check_duplicates/self.infer — one attempt, None on failure
        a = _agent(recall=[_mem(1)], infer=RuntimeError("timeout"))
        self.assertIsNone(_run(a._check_duplicates({"text": "is this new?"})))


class TestUnit(unittest.TestCase):
    def test_curate_batch_edges(self):
        a = _agent()
        self.assertIsNone(_run(a._curate_batch({})))                       # neither query nor source
        a = _agent(recall=[_mem(1)])
        self.assertIsNone(_run(a._curate_batch({"query": "q"})))           # fewer than two memories -> nothing to compare
        a.infer.assert_not_awaited()

    def test_json_extraction_tolerates_prose_and_garbage(self):
        a = _agent(recall=[_mem(1), _mem(2)], infer="Sure! " + json.dumps(FINDINGS) + " hope that helps")
        self.assertEqual(len(_run(a._curate_batch({"query": "q"}))["findings"]), 2)
        a = _agent(recall=[_mem(1), _mem(2)], infer="no json here at all")
        r = _run(a._curate_batch({"query": "q"}))
        self.assertEqual(r, {"findings": [], "stats": {"memories_analyzed": 2}})
        a.report_to_jordan.assert_not_called()
        a = _agent(recall=[_mem(1), _mem(2)], infer="{ this is : not json }")
        self.assertEqual(_run(a._curate_batch({"query": "q"}))["findings"], [])

    def test_check_duplicates_edges(self):
        a = _agent()
        self.assertIsNone(_run(a._check_duplicates({})))
        self.assertEqual(_run(a._check_duplicates({"text": "new fact"})), {"duplicates": []})
        a = _agent(recall=[_mem(1), _mem(2)], infer="y" * 5000)
        r = _run(a._check_duplicates({"text": "new fact"}))
        self.assertEqual((r["similar_count"], len(r["raw_response"])), (2, 1000))
        self.assertIn("NEW: new fact", a.infer.call_args[0][0])

    def test_scan_source_edges(self):
        self.assertIsNone(_run(_agent()._scan_source({})))
        a = _agent(recall=[_mem(1), _mem(2)], infer="{}")
        task = {"source": "journal", "query": "q"}
        _run(a._scan_source(task))
        self.assertEqual((task["type"], task["batch_size"]), ("curate_batch", 30))
        a.recall.assert_awaited_once_with("q", n=30, source="journal")

    def test_class_attributes(self):
        self.assertEqual(lib.LibrarianAgent.name, "librarian")
        self.assertEqual(lib.LibrarianAgent.backend, "mlx")
        self.assertEqual(lib.LibrarianAgent.channels, ["memory", "curate", "knowledge"])
        self.assertLessEqual(lib.LibrarianAgent.temperature, 0.2)


class TestIntegration(unittest.TestCase):
    def test_built_on_the_shared_subagent_framework(self):
        # compared by module/name, not identity: other test files importlib.reload(nova_subagent) in the same session
        base = lib.LibrarianAgent.__mro__[1]
        self.assertEqual((base.__module__, base.__name__), ("nova_subagent", "SubAgent"))
        for helper in ("recall", "infer", "report_to_jordan", "run"):
            self.assertNotIn(f"def {helper}(", SRC)                 # inherited, not re-implemented
            self.assertTrue(hasattr(base, helper) and hasattr(nova_subagent.SubAgent, helper))
        self.assertEqual((lib.log.__module__, lib.log.__name__), ("nova_logger", "log"))

    def test_handle_routes_every_task_type(self):
        a = _agent()
        with patch.object(a, "_curate_batch", AsyncMock(return_value="cb")), \
             patch.object(a, "_check_duplicates", AsyncMock(return_value="cd")), \
             patch.object(a, "_scan_source", AsyncMock(return_value="ss")):
            self.assertEqual(_run(a.handle({"type": "curate_batch"})), "cb")
            self.assertEqual(_run(a.handle({"type": "check_duplicates"})), "cd")
            self.assertEqual(_run(a.handle({"type": "scan_source"})), "ss")
            self.assertEqual(_run(a.handle({"type": "weird"})), "cb")
            self.assertEqual(_run(a.handle({})), "cb")

    def test_source_only_batch_hits_the_memory_server_recall_endpoint(self):
        a = _agent(infer=json.dumps(FINDINGS))
        body = json.dumps({"memories": [_mem(1), _mem(2), _mem(3)]}).encode()
        resp = MagicMock(); resp.read.return_value = body
        with patch.object(lib.urllib.request, "urlopen", return_value=resp) as u:
            r = _run(a._curate_batch({"source": "journal", "batch_size": 7}))
        self.assertEqual(u.call_args[0][0], "http://memory-server.digitalnoise.net:18790/recall?q=*&n=7&source=journal")
        self.assertEqual(len(r["findings"]), 2)
        a.recall.assert_not_awaited()

    def test_slack_report_goes_through_the_notification_bus(self):
        # the inherited report_to_jordan funnels into nova_notify (category subagent, warning) — assert on the stub
        a = _agent()
        del a.report_to_jordan                                      # use the real inherited method
        nova_subagent.nova_notify.reset_mock()
        _run(a.report_to_jordan(":books: *Librarian Report* — Memory Curation\nbody line"))
        kw = nova_subagent.nova_notify.call_args.kwargs
        self.assertEqual((kw["category"], kw["level"], kw["dedup_key"]), ("subagent", "warning", "subagent-librarian"))


class TestFunctional(unittest.TestCase):
    def test_golden_path_reports_findings_to_jordan(self):
        a = _agent(recall=[_mem(1), _mem(2), _mem(3)], infer=json.dumps(FINDINGS))
        r = _run(a.handle({"type": "curate_batch", "query": "jordan address", "source": "journal"}))
        self.assertEqual(r["stats"]["duplicates_found"], 1)
        a.recall.assert_awaited_once_with("jordan address", n=20, source="journal")
        self.assertEqual(a.infer.call_args.kwargs["system"], lib.SYSTEM_PROMPT)
        self.assertIn("[ID: m1] (source: journal, score: 0.50)", a.infer.call_args[0][0])
        msg = a.report_to_jordan.call_args[0][0]
        self.assertTrue(msg.startswith(":books: *Librarian Report* — Memory Curation\n*Analyzed:* 3 memories (source: journal)"))
        self.assertIn(":busts_in_silhouette: *1. DUPLICATE* (high)\n   same fact twice\n   _Recommendation:_ merge\n   _IDs:_ m1, m2", msg)
        self.assertIn(":hourglass: *2. STALE* (low)", msg)
        self.assertTrue(msg.endswith("_Reply with which findings to act on, or ignore to keep as-is._"))

    def test_clean_batch_posts_nothing(self):
        a = _agent(recall=[_mem(1), _mem(2)], infer=json.dumps({"findings": [], "stats": {"memories_analyzed": 2}}))
        r = _run(a.handle({"query": "q"}))
        self.assertEqual(r["findings"], [])
        a.report_to_jordan.assert_not_called()

    def test_error_path_llm_down_posts_nothing(self):
        a = _agent(recall=[_mem(1), _mem(2)], infer=TimeoutError("Inference timeout after 120s"))
        self.assertIsNone(_run(a.handle({"query": "q"})))
        a.report_to_jordan.assert_not_called()


class TestFrame(unittest.TestCase):
    def test_import_never_starts_the_agent(self):
        # no --help/--selftest: the only entrypoint is LibrarianAgent().run(), guarded by __main__
        self.assertIn('if __name__ == "__main__":\n    LibrarianAgent().run()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_agent_librarian"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
