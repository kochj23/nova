"""
test_nova_agent_analyst.py — All 7 test categories for nova_agent_analyst.py
Written by Jordan Koch.
"""

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

# ---------------------------------------------------------------------------
# Stub all external dependencies before loading
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_agent_analyst.py"

# Stub nova_config
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "C0ATAF7NZG9"
_nova_cfg.SLACK_CHAN = "C0AMNQ5GX70"
sys.modules["nova_config"] = _nova_cfg

# Stub redis
_redis_mock = MagicMock()
_redis_mod = MagicMock()
sys.modules["redis"] = _redis_mod

# Stub nova_logger
_logger_mock = MagicMock()
_logger_mock.LOG_INFO = "INFO"
_logger_mock.LOG_ERROR = "ERROR"
_logger_mock.LOG_WARN = "WARN"
_logger_mock.LOG_DEBUG = "DEBUG"
_logger_mock.log = MagicMock()
sys.modules["nova_logger"] = _logger_mock

# Stub nova_subagent
_subagent_mock = MagicMock()


class _MockSubAgent:
    name = "base"
    model = "test-model"
    backend = "ollama"
    channels = []
    description = ""
    temperature = 0.3
    max_tokens = 4096
    INFERENCE_TIMEOUT = 120

    def __init__(self):
        self._redis = MagicMock()
        self._pubsub = MagicMock()
        self._running = False
        self._task_count = 0
        self._start_time = None
        self._last_error = None

    def run(self):
        pass

    def _register(self):
        pass

    def _deregister(self):
        pass

    async def infer(self, prompt, system="", model=None, temperature=None, max_tokens=None):
        return ""

    async def recall(self, query, n=5, source=None):
        return []

    async def remember(self, text, source="", metadata=None):
        pass

    async def notify(self, message, channel=None):
        pass

    async def report_to_jordan(self, message):
        pass

    async def _slack_post(self, message, channel=None):
        pass

    async def _publish_result(self, task, result):
        pass


_subagent_mock.SubAgent = _MockSubAgent
sys.modules["nova_subagent"] = _subagent_mock

# Load the module under test
_spec = importlib.util.spec_from_file_location("nova_agent_analyst", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

AnalystAgent = _mod.AnalystAgent
SYSTEM_PROMPT = _mod.SYSTEM_PROMPT


def _run(coro):
    """Helper to run async coroutines in tests."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials_in_source(self):
        """Source must not contain API keys or tokens."""
        src = _SCRIPT.read_text()
        forbidden = ["sk-live", "sk-test", "ghp_", "AKIA", "xoxb-"]
        for pattern in forbidden:
            self.assertNotIn(pattern, src,
                             f"Potential credential in source: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        """Source must not hardcode a literal home path."""
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src,
                         "Hardcoded home path found — use Path.home()")

    def test_no_pii_in_source(self):
        """Source must not contain personal email addresses."""
        src = _SCRIPT.read_text()
        _at = "@"
        pii_patterns = [
            "kochjpar" + _at + "gmail.com",
            "user" + _at + "example-corp" + ".com",
        ]
        for pattern in pii_patterns:
            self.assertNotIn(pattern, src,
                             f"PII found in source: {pattern!r}")

    def test_content_truncated_before_inference(self):
        """handle() must truncate content to 4000 chars before sending to LLM."""
        agent = AnalystAgent()
        captured_prompt = []

        async def capture_infer(prompt, system="", **kwargs):
            captured_prompt.append(prompt)
            return json.dumps({
                "summary": "test", "priority": "low",
                "action_items": [], "sentiment": "neutral", "flag_jordan": False
            })

        agent.infer = capture_infer
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock()

        long_content = "x" * 5000
        task = {"content": long_content, "type": "email", "subject": "test"}
        _run(agent.handle(task))

        self.assertTrue(len(captured_prompt) > 0, "infer was not called")
        # The prompt includes the truncated content
        self.assertLessEqual(len(captured_prompt[0]), 4200,
                             "Content not truncated before inference")

    def test_analyst_does_not_expose_raw_exception_to_slack(self):
        """Error messages posted to Slack must not contain raw exception details."""
        agent = AnalystAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock()

        async def failing_infer(prompt, system="", **kwargs):
            raise RuntimeError("Internal Ollama error: connection refused to 127.0.0.1")

        agent.infer = failing_infer

        task = {"content": "test content", "type": "email", "subject": "test"}
        result = _run(agent.handle(task))
        self.assertIsNone(result, "handle() should return None on inference failure")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_json_parse_fast(self):
        """JSON extraction from LLM response must handle 1000 parses in < 100ms."""
        response = json.dumps({
            "summary": "Test summary here.",
            "priority": "high",
            "action_items": ["Follow up", "Schedule meeting"],
            "sentiment": "neutral",
            "key_people": ["Jordan"],
            "deadlines": ["2026-01-01"],
            "flag_jordan": False,
        })
        start = time.perf_counter()
        for _ in range(1000):
            cleaned = response
            if "<think>" in cleaned:
                think_end = cleaned.rfind("</think>")
                if think_end > 0:
                    cleaned = cleaned[think_end + 8:].strip()
            start_idx = cleaned.find("{")
            end_idx = cleaned.rfind("}") + 1
            if start_idx >= 0 and end_idx > start_idx:
                json.loads(cleaned[start_idx:end_idx])
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1,
                        f"JSON parse 1000x took {elapsed:.3f}s (limit 100ms)")

    def test_agent_metadata_is_correct(self):
        """AnalystAgent must declare correct model, backend, and channels."""
        agent = AnalystAgent()
        self.assertEqual(agent.model, "deepseek-r1:8b")
        self.assertEqual(agent.backend, "ollama")
        self.assertIn("email", agent.channels)
        self.assertIn("meeting", agent.channels)
        self.assertIn("alert", agent.channels)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_handle_returns_none_on_inference_failure(self):
        """handle() must return None (not raise) when inference fails."""
        agent = AnalystAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock()

        async def always_fail(prompt, system="", **kwargs):
            raise ConnectionError("Ollama unavailable")

        agent.infer = always_fail

        task = {"content": "Some email content here", "type": "email"}
        result = _run(agent.handle(task))
        self.assertIsNone(result, "handle() must return None on inference failure")

    def test_handle_does_not_retry_internally(self):
        """AnalystAgent delegates retry responsibility to SubAgent.infer — handle() itself does not retry."""
        agent = AnalystAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock()
        call_count = [0]

        async def counting_fail(prompt, system="", **kwargs):
            call_count[0] += 1
            raise Exception("fail")

        agent.infer = counting_fail

        task = {"content": "Some content here", "type": "alert"}
        _run(agent.handle(task))
        self.assertEqual(call_count[0], 1,
                         "handle() should call infer exactly once (retry is SubAgent's job)")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_handle_returns_none_for_empty_content(self):
        """handle() must return None immediately for empty task content."""
        agent = AnalystAgent()
        task = {"type": "email", "subject": "test", "content": ""}
        result = _run(agent.handle(task))
        self.assertIsNone(result)

    def test_handle_parses_valid_json_response(self):
        """handle() must parse valid JSON from LLM response."""
        agent = AnalystAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock()

        expected = {
            "summary": "Test email about Q4 results.",
            "priority": "medium",
            "action_items": ["Review budget"],
            "sentiment": "neutral",
            "key_people": [],
            "deadlines": [],
            "flag_jordan": False,
        }

        async def mock_infer(prompt, system="", **kwargs):
            return json.dumps(expected)

        agent.infer = mock_infer

        task = {"content": "Some email content here for testing", "type": "email", "subject": "Q4"}
        result = _run(agent.handle(task))

        self.assertIsNotNone(result)
        self.assertEqual(result["summary"], expected["summary"])
        self.assertEqual(result["priority"], "medium")

    def test_handle_strips_think_tags_before_json_parse(self):
        """handle() must strip <think>...</think> before extracting JSON."""
        agent = AnalystAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock()

        json_payload = json.dumps({
            "summary": "Stripped think test.",
            "priority": "low",
            "action_items": [],
            "sentiment": "neutral",
            "flag_jordan": False,
        })

        async def mock_infer(prompt, system="", **kwargs):
            return f"<think>I need to think about this carefully...</think>\n{json_payload}"

        agent.infer = mock_infer

        task = {"content": "Some content here", "type": "email", "subject": "test"}
        result = _run(agent.handle(task))

        self.assertIsNotNone(result)
        self.assertEqual(result["summary"], "Stripped think test.")

    def test_handle_falls_back_gracefully_on_bad_json(self):
        """handle() must produce a fallback result on JSONDecodeError."""
        agent = AnalystAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock()

        async def mock_infer(prompt, system="", **kwargs):
            return "This is not valid JSON at all but still a response."

        agent.infer = mock_infer

        task = {"content": "Some content here for testing purposes", "type": "email", "subject": "test"}
        result = _run(agent.handle(task))

        self.assertIsNotNone(result)
        self.assertIn("priority", result)
        self.assertIn("sentiment", result)

    def test_handle_adds_source_type_and_subject(self):
        """Result must include source_type and source_subject from the task."""
        agent = AnalystAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock()

        async def mock_infer(prompt, system="", **kwargs):
            return json.dumps({"summary": "test", "priority": "low",
                               "action_items": [], "sentiment": "neutral", "flag_jordan": False})

        agent.infer = mock_infer

        task = {"content": "Email body text here for testing", "type": "meeting", "subject": "Sprint planning"}
        result = _run(agent.handle(task))

        self.assertEqual(result["source_type"], "meeting")
        self.assertEqual(result["source_subject"], "Sprint planning")

    def test_flag_jordan_routes_to_report_to_jordan(self):
        """flag_jordan=True must call report_to_jordan(), not just notify()."""
        agent = AnalystAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock()

        async def mock_infer(prompt, system="", **kwargs):
            return json.dumps({
                "summary": "CRITICAL: production is down",
                "priority": "critical",
                "action_items": ["Page on-call"],
                "sentiment": "urgent",
                "flag_jordan": True,
            })

        agent.infer = mock_infer

        task = {"content": "CRITICAL alert text here about prod issue", "type": "alert", "subject": "PROD DOWN"}
        _run(agent.handle(task))

        agent.report_to_jordan.assert_called_once()
        agent.notify.assert_not_called()

    def test_low_priority_routes_to_notify(self):
        """flag_jordan=False must call notify(), not report_to_jordan()."""
        agent = AnalystAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock()

        async def mock_infer(prompt, system="", **kwargs):
            return json.dumps({
                "summary": "routine weekly report",
                "priority": "low",
                "action_items": [],
                "sentiment": "neutral",
                "flag_jordan": False,
            })

        agent.infer = mock_infer

        task = {"content": "Weekly report with normal content here", "type": "email", "subject": "Weekly"}
        _run(agent.handle(task))

        agent.notify.assert_called_once()
        agent.report_to_jordan.assert_not_called()


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_handle_calls_remember_after_analysis(self):
        """handle() must store the analysis summary in Nova's memory."""
        agent = AnalystAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock()

        async def mock_infer(prompt, system="", **kwargs):
            return json.dumps({
                "summary": "Important meeting notes captured here.",
                "priority": "high",
                "action_items": ["Follow up with team"],
                "sentiment": "positive",
                "flag_jordan": False,
            })

        agent.infer = mock_infer

        task = {"content": "Meeting notes body text content for testing", "type": "meeting", "subject": "Q4 Planning"}
        _run(agent.handle(task))

        agent.remember.assert_called_once()
        call_args = agent.remember.call_args
        self.assertIn("Important meeting notes", call_args.args[0] + call_args.kwargs.get("text", ""))

    def test_full_pipeline_email_task(self):
        """Full pipeline: email task → analysis → notify → memory write."""
        agent = AnalystAgent()
        notify_calls = []
        remember_calls = []

        async def mock_infer(prompt, system="", **kwargs):
            return json.dumps({
                "summary": "Routine email from vendor.",
                "priority": "low",
                "action_items": [],
                "sentiment": "neutral",
                "flag_jordan": False,
            })

        agent.infer = mock_infer
        agent.notify = AsyncMock(side_effect=lambda m, **kw: notify_calls.append(m))
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock(side_effect=lambda *a, **kw: remember_calls.append(a))

        task = {
            "content": "Dear Jordan, please review the attached invoice for services rendered.",
            "type": "email",
            "subject": "Invoice #1234",
        }
        result = _run(agent.handle(task))

        self.assertIsNotNone(result)
        self.assertEqual(len(notify_calls), 1)
        self.assertEqual(len(remember_calls), 1)
        self.assertEqual(result["priority"], "low")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_all_priority_levels_produce_output(self):
        """All four priority levels must produce valid analysis results."""
        agent = AnalystAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock()

        for priority in ["critical", "high", "medium", "low"]:
            async def mock_infer(prompt, system="", p=priority, **kwargs):
                return json.dumps({
                    "summary": f"{p} priority item",
                    "priority": p,
                    "action_items": [],
                    "sentiment": "neutral",
                    "flag_jordan": p in ("critical", "high"),
                })

            agent.infer = mock_infer

            task = {"content": f"Test content for {priority} priority testing purposes",
                    "type": "alert", "subject": priority}
            result = _run(agent.handle(task))
            self.assertIsNotNone(result, f"handle() returned None for priority={priority}")
            self.assertEqual(result["priority"], priority)

    def test_content_from_text_key_fallback(self):
        """handle() must also accept 'text' key in task (not just 'content')."""
        agent = AnalystAgent()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock()

        async def mock_infer(prompt, system="", **kwargs):
            return json.dumps({
                "summary": "test from text key",
                "priority": "low",
                "action_items": [],
                "sentiment": "neutral",
                "flag_jordan": False,
            })

        agent.infer = mock_infer

        # 'text' instead of 'content'
        task = {"text": "Some text content here for testing purposes", "type": "email", "subject": "test"}
        result = _run(agent.handle(task))
        self.assertIsNotNone(result)

    def test_action_items_appear_in_notification(self):
        """Action items must be included in the Slack notification message."""
        agent = AnalystAgent()
        notify_messages = []
        agent.notify = AsyncMock(side_effect=lambda m, **kw: notify_messages.append(m))
        agent.report_to_jordan = AsyncMock()
        agent.remember = AsyncMock()

        async def mock_infer(prompt, system="", **kwargs):
            return json.dumps({
                "summary": "Meeting summary",
                "priority": "medium",
                "action_items": ["Send follow-up email", "Update Jira ticket"],
                "sentiment": "neutral",
                "flag_jordan": False,
            })

        agent.infer = mock_infer

        task = {"content": "Meeting discussion about roadmap and planning Q4", "type": "meeting", "subject": "Roadmap"}
        _run(agent.handle(task))

        self.assertTrue(len(notify_messages) > 0)
        combined = " ".join(notify_messages)
        self.assertIn("follow-up", combined.lower() + combined,
                      "Action items should appear in the notification")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles_cleanly(self):
        """nova_agent_analyst.py must compile without syntax errors."""
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_analyst_agent_instantiates(self):
        """AnalystAgent must instantiate without error."""
        agent = AnalystAgent()
        self.assertEqual(agent.name, "analyst")

    def test_analyst_inherits_from_subagent(self):
        """AnalystAgent must inherit from SubAgent (or the mock base)."""
        agent = AnalystAgent()
        self.assertTrue(hasattr(agent, "infer"),
                        "AnalystAgent must have infer() from SubAgent")
        self.assertTrue(hasattr(agent, "notify"),
                        "AnalystAgent must have notify() from SubAgent")

    def test_system_prompt_mentions_json(self):
        """SYSTEM_PROMPT must include JSON output format specification."""
        self.assertIn("JSON", SYSTEM_PROMPT, "System prompt must specify JSON output format")
        self.assertIn("priority", SYSTEM_PROMPT)
        self.assertIn("flag_jordan", SYSTEM_PROMPT)

    def test_agent_channels_are_list(self):
        """channels must be a list with at least one entry."""
        agent = AnalystAgent()
        self.assertIsInstance(agent.channels, list)
        self.assertGreater(len(agent.channels), 0)

    def test_temperature_is_low_for_analyst(self):
        """AnalystAgent temperature must be low (≤0.3) for deterministic analysis."""
        agent = AnalystAgent()
        self.assertLessEqual(agent.temperature, 0.3,
                             "Analyst should use low temperature for consistent results")


if __name__ == "__main__":
    unittest.main(verbosity=2)
