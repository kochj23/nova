#!/usr/bin/env python3
"""Tests for nova_agent_coder.py — the 7 house categories (Security, Performance, Retry, Unit,
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
SCRIPT = SCRIPTS / "nova_agent_coder.py"
SRC = SCRIPT.read_text()


class _FakeSubAgent:
    """Stand-in for nova_subagent.SubAgent: no Redis, no Ollama, no Slack — records every outbound call."""
    name = "unnamed"; model = ""; backend = "ollama"; channels = []; description = ""
    temperature = 0.3; max_tokens = 4096

    def __init__(self):
        self.infer_calls = []; self.notified = []; self.reported = []
        self.responses = []           # queue of str or Exception, consumed by infer()

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

    def run(self):
        raise AssertionError("run() must never be reached from an import")


def _stub_modules():
    sub = types.ModuleType("nova_subagent"); sub.SubAgent = _FakeSubAgent
    lg = types.ModuleType("nova_logger"); lg.log = MagicMock(); lg.LOG_INFO = "info"; lg.LOG_ERROR = "error"
    return {"nova_subagent": sub, "nova_logger": lg}


@contextmanager
def _stubbed(mods):
    """Set sys.modules keys for the duration and restore ONLY those keys afterwards."""
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


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    with _stubbed(_stub_modules()):
        spec.loader.exec_module(mod)
    return mod


ac = _load("ac", SCRIPT)


def _agent(*responses):
    a = ac.CoderAgent()
    a.responses = list(responses)
    return a


def _handle(agent, task):
    return asyncio.run(agent.handle(task))


GOOD = json.dumps({"summary": "adds retry", "issues": [{"severity": "high", "description": "no timeout", "file": "x.py", "line": 3}],
                   "security_concerns": [], "suggestions": ["add timeout"], "quality_score": 6, "flag_jordan": False})


class TestSecurity(unittest.TestCase):
    def test_no_hardcoded_credentials(self):
        pat = re.compile(r"(api[_-]?key|password|secret|token)\s*=\s*['\"][A-Za-z0-9+/]{16,}['\"]", re.I)
        self.assertIsNone(pat.search(SRC))

    def test_prompt_content_is_capped_at_6000_chars(self):
        a = _agent(GOOD)
        _handle(a, {"content": "A" * 20_000, "type": "diff"})
        prompt = a.infer_calls[0][0]
        self.assertLess(len(prompt), 6200)
        self.assertEqual(prompt.count("A"), 6000)

    def test_security_concerns_always_escalate_to_jordan_and_are_truncated(self):
        r = {"summary": "s", "issues": [], "security_concerns": ["X" * 500, "Y" * 500, "Z", "W"], "quality_score": 9, "flag_jordan": False}
        a = _agent(json.dumps(r))
        _handle(a, {"content": "x"})
        self.assertEqual(len(a.reported), 1)
        self.assertEqual(a.notified, [])
        self.assertNotIn("W", a.reported[0])                       # only the first 3 concerns
        self.assertLess(a.reported[0].count("X"), 100)             # each capped at 80 chars

    def test_summary_and_issue_text_are_bounded_in_the_message(self):
        r = {"summary": "Q" * 2000, "issues": [{"severity": "critical", "description": "Z" * 2000}],
             "quality_score": 2, "flag_jordan": True}
        a = _agent(json.dumps(r))
        _handle(a, {"content": "x"})
        msg = a.reported[0]
        self.assertEqual(msg.count("Q"), 300)
        self.assertEqual(msg.count("Z"), 100)


class TestPerformance(unittest.TestCase):
    def test_10k_issues_render_only_three_and_stay_fast(self):
        issues = [{"severity": "high" if i % 2 else "low", "description": f"issue {i}"} for i in range(10_000)]
        a = _agent(json.dumps({"summary": "s", "issues": issues, "quality_score": 5, "flag_jordan": False}))
        t0 = time.perf_counter()
        res = _handle(a, {"content": "x"})
        self.assertLess(time.perf_counter() - t0, 2.0)
        self.assertEqual(len(res["issues"]), 10_000)
        self.assertEqual(a.notified[0].count(":red_circle:"), 3)
        self.assertIn("Issues (5000 critical/high)", a.notified[0])


class TestRetry(unittest.TestCase):
    def test_inference_failure_is_one_shot_and_returns_none(self):
        # RETRY GAP: CoderAgent.handle/self.infer — a failed inference is logged once, never retried
        a = _agent(TimeoutError("Inference timeout"), GOOD)
        self.assertIsNone(_handle(a, {"content": "x"}))
        self.assertEqual(len(a.infer_calls), 1)
        self.assertEqual((a.notified, a.reported), ([], []))
        ac.log.assert_any_call("Inference failed: Inference timeout", level="error", source="subagent.coder")

    def test_garbage_response_fails_open_to_a_neutral_result(self):
        a = _agent("totally not json")
        res = _handle(a, {"content": "x"})
        self.assertEqual((res["quality_score"], res["issues"], res["flag_jordan"]), (5, [], False))
        self.assertEqual(res["summary"], "totally not json")
        self.assertEqual((a.notified, a.reported), ([], []))     # neutral result: nothing posted


class TestUnit(unittest.TestCase):
    def test_empty_content_short_circuits_before_inference(self):
        a = _agent(GOOD)
        self.assertIsNone(_handle(a, {"type": "review"}))
        self.assertIsNone(_handle(a, {"content": ""}))
        self.assertEqual(a.infer_calls, [])

    def test_content_fallback_keys_and_prompt_shape(self):
        a = _agent(GOOD, GOOD)
        _handle(a, {"diff": "--- a\n+++ b", "repo": "kochj23/MLXCode", "file": "a.py", "type": "pr"})
        _handle(a, {"text": "print(1)"})
        p1, p2 = a.infer_calls[0][0], a.infer_calls[1][0]
        self.assertTrue(p1.startswith("Review this pr:\nRepository: kochj23/MLXCode\nFile: a.py\n"))
        self.assertIn("```\n--- a\n+++ b\n```", p1)
        self.assertTrue(p2.startswith("Review this review:\n"))
        self.assertEqual(a.infer_calls[0][1], ac.SYSTEM_PROMPT)

    def test_think_block_and_no_think_marker_are_stripped(self):
        a = _agent("<think>I wonder</think>\n/no_think " + GOOD)
        res = _handle(a, {"content": "x"})
        self.assertEqual(res["summary"], "adds retry")

    def test_invalid_json_inside_braces_falls_back_to_raw_response(self):
        a = _agent("prefix {not: valid json} suffix")
        res = _handle(a, {"content": "x"})
        self.assertEqual(res["summary"], "prefix {not: valid json} suffix")
        self.assertEqual(res["quality_score"], 5)

    def test_score_emoji_bands(self):
        for score, emoji in ((9, ":white_check_mark:"), (7, ":white_check_mark:"), (4, ":warning:"), (3, ":x:")):
            a = _agent(json.dumps({"summary": "s", "issues": [{"severity": "low"}], "quality_score": score}))
            _handle(a, {"content": "x"})
            self.assertTrue(a.notified[0].startswith(f"{emoji} *Coder Review* (score: {score}/10)"), (score, a.notified))


class TestIntegration(unittest.TestCase):
    def test_agent_is_a_subagent_on_the_code_channels(self):
        self.assertTrue(issubclass(ac.CoderAgent, ac.SubAgent))
        self.assertEqual(ac.CoderAgent.channels, ["code", "review", "script"])
        self.assertEqual((ac.CoderAgent.name, ac.CoderAgent.backend), ("coder", "ollama"))
        self.assertEqual(ac.CoderAgent.model, "qwen3-coder:30b")
        self.assertEqual(ac.CoderAgent.temperature, 0.1)

    def test_result_carries_the_task_provenance(self):
        a = _agent(GOOD)
        res = _handle(a, {"content": "x", "type": "script", "file": "nova_x.py", "repo": "r"})
        self.assertEqual((res["source_type"], res["source_file"], res["source_repo"]), ("script", "nova_x.py", "r"))
        ac.log.assert_any_call("Reviewing script: nova_x.py", level="info", source="subagent.coder")

    def test_system_prompt_demands_the_json_contract(self):
        for key in ("summary", "issues", "security_concerns", "quality_score", "flag_jordan"):
            self.assertIn(f'"{key}"', ac.SYSTEM_PROMPT)


class TestFunctional(unittest.TestCase):
    def test_golden_path_issue_review_notifies_the_channel(self):
        a = _agent(GOOD)
        res = _handle(a, {"content": "def f(): pass", "type": "review", "file": "x.py"})
        self.assertEqual(res["quality_score"], 6)
        self.assertEqual(len(a.notified), 1)
        self.assertEqual(a.reported, [])
        msg = a.notified[0]
        self.assertIn("*Type:* review | *File:* x.py", msg)
        self.assertIn("*Issues (1 critical/high):*\n  :red_circle: [high] no timeout", msg)

    def test_flag_jordan_routes_to_jordan(self):
        a = _agent(json.dumps({"summary": "breaking change", "issues": [], "quality_score": 1, "flag_jordan": True}))
        _handle(a, {"content": "x"})
        self.assertEqual(len(a.reported), 1)
        self.assertTrue(a.reported[0].startswith(":x: *Coder Review* (score: 1/10)"))

    def test_clean_review_posts_nothing(self):
        a = _agent(json.dumps({"summary": "fine", "issues": [], "quality_score": 10, "flag_jordan": False}))
        res = _handle(a, {"content": "x"})
        self.assertEqual(res["summary"], "fine")
        self.assertEqual((a.notified, a.reported), ([], []))

    def test_error_path_inference_exception(self):
        a = _agent(RuntimeError("ollama down"))
        self.assertIsNone(_handle(a, {"content": "x"}))
        self.assertEqual((a.notified, a.reported), ([], []))


class TestFrame(unittest.TestCase):
    def test_import_never_starts_the_agent(self):
        self.assertIn('if __name__ == "__main__":\n    CoderAgent().run()', SRC)
        r = subprocess.run([sys.executable, "-c", "import nova_agent_coder"], cwd=str(SCRIPTS),
                           capture_output=True, text=True, timeout=30, env={**os.environ, "NOVA_TEST_QUIET": "1"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
