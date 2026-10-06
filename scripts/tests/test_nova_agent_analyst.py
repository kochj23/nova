#!/usr/bin/env python3
"""Tests for nova_agent_analyst.py — the 7 house categories (Security, Performance, Retry, Unit,
Integration, Functional, Frame). Written by Jordan Koch (via Claude)."""
import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SCRIPT = SCRIPTS / "nova_agent_analyst.py"
SRC = SCRIPT.read_text()


class _FakeSubAgent:
    """Stand-in for nova_subagent.SubAgent: no Redis, no Ollama, no Slack, no memory server."""
    name = "unnamed"; model = ""; backend = "ollama"; channels = []; description = ""; temperature = 0.3

    def __init__(self):
        self.infer_calls = []; self.notified = []; self.reported = []; self.remembered = []
        self.responses = []

    async def infer(self, prompt, system="", **kw):
        self.infer_calls.append((prompt, system))
        r = self.responses.pop(0) if self.responses else "{}"
        if isinstance(r, Exception):
            raise r
        return r

    async def notify(self, message, channel=None):
        self.notified.append(message)

    async def report_to_jordan(self, message):
        self.reported.append(message)

    async def remember(self, text, source="", metadata=None):
        self.remembered.append((text, source, metadata))

    def run(self):
        raise AssertionError("run() must never be reached from an import")


@contextmanager
def _stubbed(mods):
    old = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def _load():
    sub = types.ModuleType("nova_subagent"); sub.SubAgent = _FakeSubAgent
    lg = types.ModuleType("nova_logger"); lg.log = MagicMock(); lg.LOG_INFO = "info"; lg.LOG_ERROR = "error"
    spec = importlib.util.spec_from_file_location("aa", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    with _stubbed({"nova_subagent": sub, "nova_logger": lg}):
        spec.loader.exec_module(mod)
    return mod


aa = _load()
GOOD = json.dumps({"summary": "Budget review moved", "priority": "high", "action_items": ["reply to CFO"],
                   "sentiment": "urgent", "flag_jordan": False})


def _run(*responses, task=None):
    a = aa.AnalystAgent(); a.responses = list(responses)
    return a, asyncio.run(a.handle(task if task is not None else {"content": "x", "type": "email", "subject": "s"}))


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_content_capped_at_4000_chars_in_prompt(self):
        a, _ = _run(GOOD, task={"content": "Z" * 20_000, "type": "email"})
        self.assertEqual(a.infer_calls[0][0].count("Z"), 4000)

    def test_message_fields_are_bounded(self):
        r = {"summary": "Q" * 2000, "priority": "low", "action_items": [f"a{i}" for i in range(20)], "flag_jordan": False}
        a, _ = _run(json.dumps(r), task={"content": "x", "subject": "Z" * 500})
        msg = a.notified[0]
        self.assertEqual(msg.count("Q"), 300)
        self.assertEqual(msg.count("Z"), 80)
        self.assertNotIn("a5", msg)          # only the first five action items
        self.assertIn("a4", msg)


class TestPerformance(unittest.TestCase):
    def test_10k_action_items_handled_fast(self):
        r = {"summary": "s", "priority": "low", "action_items": [f"item {i}" for i in range(10_000)]}
        t0 = time.perf_counter()
        a, res = _run(json.dumps(r))
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(res["action_items"]), 10_000)
        self.assertEqual(a.notified[0].count("  • "), 5)


class TestRetry(unittest.TestCase):
    def test_inference_failure_is_one_shot_and_fails_open(self):
        # RETRY GAP: AnalystAgent.handle/self.infer — one inference attempt, None returned on failure
        a, res = _run(TimeoutError("timeout"), GOOD)
        self.assertIsNone(res)
        self.assertEqual(len(a.infer_calls), 1)
        self.assertEqual((a.notified, a.reported, a.remembered), ([], [], []))


class TestUnit(unittest.TestCase):
    def test_empty_content_short_circuits(self):
        a, res = _run(GOOD, task={"type": "email"})
        self.assertIsNone(res)
        self.assertEqual(a.infer_calls, [])

    def test_think_tags_stripped(self):
        _, res = _run("<think>{\"priority\": \"low\"}</think>\n" + GOOD)
        self.assertEqual(res["priority"], "high")

    def test_non_json_falls_back_to_medium(self):
        _, res = _run("just prose")
        self.assertEqual((res["priority"], res["summary"], res["flag_jordan"]), ("medium", "just prose", False))
        _, res = _run("x {broken: json} y")
        self.assertEqual(res["summary"], "x {broken: json} y")

    def test_text_key_fallback_and_prompt_shape(self):
        a, _ = _run(GOOD, task={"text": "hello", "type": "meeting", "subject": "1:1"})
        self.assertEqual(a.infer_calls[0][0], "Analyze this meeting:\nSubject: 1:1\n\nhello")
        self.assertEqual(a.infer_calls[0][1], aa.SYSTEM_PROMPT)

    def test_unknown_priority_gets_memo_emoji(self):
        a, _ = _run(json.dumps({"summary": "s", "priority": "weird"}))
        self.assertTrue(a.notified[0].startswith(":memo: *Analyst Report* (WEIRD)"))


class TestIntegration(unittest.TestCase):
    def test_subagent_wiring(self):
        self.assertTrue(issubclass(aa.AnalystAgent, aa.SubAgent))
        self.assertEqual(aa.AnalystAgent.channels, ["email", "meeting", "alert"])
        self.assertEqual((aa.AnalystAgent.name, aa.AnalystAgent.model), ("analyst", "deepseek-r1:8b"))

    def test_result_stored_in_memory_with_metadata(self):
        a, res = _run(GOOD)
        self.assertEqual(res["source_type"], "email")
        text, src, meta = a.remembered[0]
        self.assertEqual(src, "subagent.analyst")
        self.assertEqual(meta, {"priority": "high", "type": "email"})
        self.assertIn("Budget review moved", text)


class TestFunctional(unittest.TestCase):
    def test_golden_path_notifies_channel(self):
        a, res = _run(GOOD)
        self.assertEqual(a.reported, [])
        self.assertTrue(a.notified[0].startswith(":warning: *Analyst Report* (HIGH)"))
        self.assertIn("  • reply to CFO", a.notified[0])

    def test_flag_jordan_routes_to_jordan(self):
        a, _ = _run(json.dumps({"summary": "s", "priority": "critical", "flag_jordan": True}))
        self.assertEqual(a.notified, [])
        self.assertTrue(a.reported[0].startswith(":rotating_light:"))


class TestFrame(unittest.TestCase):
    def test_import_never_starts_the_agent(self):
        self.assertIn('if __name__ == "__main__":\n    AnalystAgent().run()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_agent_analyst"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
