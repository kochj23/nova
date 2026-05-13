"""
test_nova_agent_coder.py — All 7 test categories for nova_agent_coder.py
Written by Jordan Koch.
"""

import asyncio
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_agent_coder.py"

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "C0ATAF7NZG9"
sys.modules["nova_config"] = _nova_cfg
_redis_mod = MagicMock()
sys.modules["redis"] = _redis_mod
_logger_mock = MagicMock()
_logger_mock.LOG_INFO = "INFO"
_logger_mock.LOG_ERROR = "ERROR"
_logger_mock.log = MagicMock()
sys.modules["nova_logger"] = _logger_mock


class _MockSubAgent:
    name = "base"
    model = "test"
    backend = "ollama"
    channels = []
    description = ""
    temperature = 0.3
    max_tokens = 4096

    def __init__(self):
        self._redis = MagicMock()
        self._pubsub = MagicMock()
        self._running = False
        self._task_count = 0
        self._start_time = None
        self._last_error = None

    def run(self): pass
    def _register(self): pass
    def _deregister(self): pass
    async def infer(self, prompt, system="", **kwargs): return ""
    async def recall(self, query, n=5, source=None): return []
    async def remember(self, text, source="", metadata=None): pass
    async def notify(self, message, channel=None): pass
    async def report_to_jordan(self, message): pass
    async def _slack_post(self, message, channel=None): pass
    async def _publish_result(self, task, result): pass


_subagent_mock = MagicMock()
_subagent_mock.SubAgent = _MockSubAgent
sys.modules["nova_subagent"] = _subagent_mock

_spec = importlib.util.spec_from_file_location("nova_agent_coder", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

CoderAgent = _mod.CoderAgent


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-live", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_code_truncated_before_inference(self):
        """handle() must truncate code to 6000 chars."""
        agent = CoderAgent()
        captured = []

        async def capture_infer(prompt, system="", **kwargs):
            captured.append(prompt)
            return json.dumps({"summary": "ok", "issues": [], "quality_score": 8, "flag_jordan": False,
                               "security_concerns": [], "suggestions": []})

        agent.infer = capture_infer
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()

        task = {"content": "x" * 8000, "type": "review", "file": "test.swift"}
        _run(agent.handle(task))

        self.assertTrue(len(captured) > 0)
        self.assertLessEqual(len(captured[0]), 6300, "Code not truncated before inference")

    def test_security_concerns_always_notify_jordan(self):
        """Security concerns found by coder must always be flagged to Jordan."""
        agent = CoderAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()

        async def mock_infer(prompt, system="", **kwargs):
            return json.dumps({
                "summary": "Code looks OK but has security issue",
                "issues": [],
                "quality_score": 6,
                "flag_jordan": False,
                "security_concerns": ["SQL injection risk in query builder"],
                "suggestions": [],
            })

        agent.infer = mock_infer

        task = {"content": "func query(input: String) { db.exec(\"SELECT * FROM \" + input) }",
                "type": "review", "file": "DB.swift"}
        _run(agent.handle(task))

        agent.report_to_jordan.assert_called_once()


class TestPerformance(unittest.TestCase):

    def test_coder_model_is_qwen3(self):
        agent = CoderAgent()
        self.assertIn("qwen3", agent.model.lower())

    def test_json_parse_fast(self):
        response = json.dumps({"summary": "x", "issues": [], "quality_score": 7,
                               "flag_jordan": False, "security_concerns": [], "suggestions": []})
        start = time.perf_counter()
        for _ in range(1000):
            start_i = response.find("{")
            end_i = response.rfind("}") + 1
            json.loads(response[start_i:end_i])
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1, f"JSON parse 1000x took {elapsed:.3f}s")


class TestRetry(unittest.TestCase):

    def test_handle_returns_none_on_inference_failure(self):
        agent = CoderAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()

        async def fail_infer(prompt, system="", **kwargs):
            raise ConnectionError("Ollama down")

        agent.infer = fail_infer
        result = _run(agent.handle({"content": "def foo(): pass", "type": "review"}))
        self.assertIsNone(result)


class TestUnit(unittest.TestCase):

    def test_handle_returns_none_for_empty_content(self):
        agent = CoderAgent()
        result = _run(agent.handle({"content": "", "type": "review"}))
        self.assertIsNone(result)

    def test_handle_strips_no_think_tag(self):
        """handle() must strip '/no_think' markers from qwen3 responses."""
        agent = CoderAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()

        async def mock_infer(prompt, system="", **kwargs):
            return "/no_think\n" + json.dumps({
                "summary": "clean code",
                "issues": [],
                "quality_score": 9,
                "flag_jordan": False,
                "security_concerns": [],
                "suggestions": [],
            })

        agent.infer = mock_infer
        task = {"content": "func hello() { print('hello') }", "type": "review"}
        result = _run(agent.handle(task))
        self.assertIsNotNone(result)
        self.assertEqual(result["quality_score"], 9)

    def test_high_score_does_not_notify_when_no_issues(self):
        """Quality score >= 7 with no issues and no security concerns: no Slack post."""
        agent = CoderAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()

        async def mock_infer(prompt, system="", **kwargs):
            return json.dumps({
                "summary": "Clean, well-structured code",
                "issues": [],
                "quality_score": 9,
                "flag_jordan": False,
                "security_concerns": [],
                "suggestions": ["Minor: add docstring"],
            })

        agent.infer = mock_infer
        task = {"content": "func clean() { return 42 }", "type": "review"}
        _run(agent.handle(task))

        agent.notify.assert_not_called()
        agent.report_to_jordan.assert_not_called()

    def test_adds_source_metadata(self):
        agent = CoderAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()

        async def mock_infer(prompt, system="", **kwargs):
            return json.dumps({"summary": "ok", "issues": [], "quality_score": 5,
                               "flag_jordan": False, "security_concerns": [], "suggestions": []})

        agent.infer = mock_infer
        task = {"content": "some code here", "type": "script", "file": "main.py", "repo": "MLXCode"}
        result = _run(agent.handle(task))
        self.assertEqual(result["source_file"], "main.py")
        self.assertEqual(result["source_repo"], "MLXCode")

    def test_diff_key_falls_back_to_content(self):
        """handle() must accept 'diff' key in task."""
        agent = CoderAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        captured_prompt = []

        async def capture_infer(prompt, system="", **kwargs):
            captured_prompt.append(prompt)
            return json.dumps({"summary": "diff review", "issues": [], "quality_score": 7,
                               "flag_jordan": False, "security_concerns": [], "suggestions": []})

        agent.infer = capture_infer
        task = {"diff": "+ func newFeature() {}", "type": "review"}
        _run(agent.handle(task))
        self.assertTrue(len(captured_prompt) > 0)
        self.assertIn("newFeature", captured_prompt[0])


class TestIntegration(unittest.TestCase):

    def test_critical_issue_flags_jordan(self):
        agent = CoderAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()

        async def mock_infer(prompt, system="", **kwargs):
            return json.dumps({
                "summary": "Critical memory leak detected",
                "issues": [{"severity": "critical", "description": "Retain cycle in viewController",
                             "file": "VC.swift", "line": 42}],
                "quality_score": 3,
                "flag_jordan": True,
                "security_concerns": [],
                "suggestions": [],
            })

        agent.infer = mock_infer
        task = {"content": "class VC { var delegate: SomeDelegate? }", "type": "review"}
        result = _run(agent.handle(task))

        agent.report_to_jordan.assert_called_once()
        self.assertEqual(result["quality_score"], 3)


class TestFunctional(unittest.TestCase):

    def test_all_task_types_work(self):
        """All channel types must produce output."""
        agent = CoderAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()

        for task_type in ["review", "code", "script"]:
            async def mock_infer(prompt, system="", **kwargs):
                return json.dumps({"summary": "ok", "issues": [], "quality_score": 7,
                                   "flag_jordan": False, "security_concerns": [], "suggestions": []})
            agent.infer = mock_infer
            task = {"content": f"sample {task_type} content here", "type": task_type}
            result = _run(agent.handle(task))
            self.assertIsNotNone(result, f"handle() returned None for type={task_type}")

    def test_fallback_on_bad_json(self):
        agent = CoderAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()

        async def mock_infer(prompt, system="", **kwargs):
            return "The code looks good overall but needs some refactoring."

        agent.infer = mock_infer
        task = {"content": "def foo(): pass", "type": "review"}
        result = _run(agent.handle(task))
        self.assertIsNotNone(result)
        self.assertIn("summary", result)


class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_instantiates(self):
        agent = CoderAgent()
        self.assertEqual(agent.name, "coder")
        self.assertIn("code", agent.channels)

    def test_temperature_low_for_determinism(self):
        agent = CoderAgent()
        self.assertLessEqual(agent.temperature, 0.2, "Coder needs very low temperature")

    def test_system_prompt_mentions_security(self):
        self.assertIn("security", _mod.SYSTEM_PROMPT.lower())

    def test_max_tokens_large_for_code(self):
        agent = CoderAgent()
        self.assertGreaterEqual(agent.max_tokens, 4096, "Coder needs large context for diffs")


if __name__ == "__main__":
    unittest.main(verbosity=2)
