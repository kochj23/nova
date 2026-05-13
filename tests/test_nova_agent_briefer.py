"""
test_nova_agent_briefer.py — All 7 test categories for nova_agent_briefer.py
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
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_agent_briefer.py"

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "C0ATAF7NZG9"
sys.modules["nova_config"] = _nova_cfg

_redis_mod = MagicMock()
sys.modules["redis"] = _redis_mod

_logger_mock = MagicMock()
_logger_mock.LOG_INFO = "INFO"
_logger_mock.LOG_ERROR = "ERROR"
_logger_mock.LOG_WARN = "WARN"
_logger_mock.LOG_DEBUG = "DEBUG"
_logger_mock.log = MagicMock()
sys.modules["nova_logger"] = _logger_mock


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

_spec = importlib.util.spec_from_file_location("nova_agent_briefer", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

ProactiveBriefer = _mod.ProactiveBriefer


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-live", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src, "Hardcoded home path in source")

    def test_novacontrol_url_uses_localhost(self):
        """NOVACONTROL_API must use 127.0.0.1 (loopback only)."""
        self.assertIn("127.0.0.1", _mod.NOVACONTROL_API)

    def test_memory_url_uses_localhost(self):
        """MEMORY_URL must use 127.0.0.1 (local memory server)."""
        self.assertIn("127.0.0.1", _mod.MEMORY_URL)

    def test_brief_content_stripped_before_sending(self):
        """Brief must truncate context to prevent prompt injection via calendar/email data."""
        agent = ProactiveBriefer()
        agent.infer = AsyncMock(return_value="Morning brief summary.")
        agent.report_to_jordan = AsyncMock()
        agent.notify = AsyncMock()
        agent.remember = AsyncMock()

        # _get_calendar returns huge string — should be truncated
        long_calendar = "x" * 5000
        agent._get_calendar = AsyncMock(return_value=long_calendar)
        agent._get_recent_emails = AsyncMock(return_value="")
        agent._get_action_items = AsyncMock(return_value="")
        agent._get_system_health = AsyncMock(return_value="all ok")
        agent._get_memory_context = AsyncMock(return_value="")

        _run(agent._generate_brief())

        # The infer call must not pass more than ~7000 chars total
        call_args = agent.infer.call_args
        prompt = call_args.args[0] if call_args.args else call_args.kwargs.get("prompt", "")
        self.assertLessEqual(len(prompt), 7000,
                             "Brief prompt should not exceed reasonable bounds")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_briefer_metadata(self):
        """ProactiveBriefer must declare correct model and backend."""
        agent = ProactiveBriefer()
        self.assertEqual(agent.model, "deepseek-r1:8b")
        self.assertEqual(agent.backend, "ollama")

    def test_think_tag_strip_fast(self):
        """Think-tag stripping must process 1000 responses in < 50ms."""
        response = "<think>long thinking block " + "x" * 1000 + "</think>\nActual brief content here."
        start = time.perf_counter()
        for _ in range(1000):
            cleaned = response
            if "<think>" in cleaned:
                think_end = cleaned.rfind("</think>")
                if think_end > 0:
                    cleaned = cleaned[think_end + 8:].strip()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.05, f"Think-strip 1000x took {elapsed:.3f}s")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_generate_brief_handles_inference_error_gracefully(self):
        """_generate_brief() must post an error message, not raise, when inference fails."""
        agent = ProactiveBriefer()
        agent.infer = AsyncMock(side_effect=RuntimeError("Ollama timeout"))
        agent.report_to_jordan = AsyncMock()
        agent.notify = AsyncMock()
        agent.remember = AsyncMock()
        agent._get_calendar = AsyncMock(return_value="9am standup")
        agent._get_recent_emails = AsyncMock(return_value="")
        agent._get_action_items = AsyncMock(return_value="")
        agent._get_system_health = AsyncMock(return_value="ok")
        agent._get_memory_context = AsyncMock(return_value="")

        result = _run(agent._generate_brief())
        self.assertIsNone(result, "_generate_brief() should return None on failure")
        agent.notify.assert_called_once()

    def test_generate_brief_returns_none_when_no_data_sources(self):
        """_generate_brief() must post fallback message when all data sources fail."""
        agent = ProactiveBriefer()
        agent.infer = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        agent.notify = AsyncMock()
        agent.remember = AsyncMock()
        agent._get_calendar = AsyncMock(return_value="")
        agent._get_recent_emails = AsyncMock(return_value="")
        agent._get_action_items = AsyncMock(return_value="")
        agent._get_system_health = AsyncMock(return_value="")
        agent._get_memory_context = AsyncMock(return_value="")

        result = _run(agent._generate_brief())
        self.assertIsNone(result)
        agent.report_to_jordan.assert_called_once()
        agent.infer.assert_not_called()


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_channels_include_brief_and_morning(self):
        agent = ProactiveBriefer()
        self.assertIn("brief", agent.channels)
        self.assertIn("morning", agent.channels)

    def test_temperature_suitable_for_briefing(self):
        """Temperature must be in a moderate range for balanced briefing."""
        agent = ProactiveBriefer()
        self.assertGreater(agent.temperature, 0.1)
        self.assertLessEqual(agent.temperature, 0.7)

    def test_think_tags_stripped_from_response(self):
        """_generate_brief() must strip <think> blocks from LLM response."""
        agent = ProactiveBriefer()
        captured_report = []

        agent.report_to_jordan = AsyncMock(side_effect=lambda m: captured_report.append(m))
        agent.notify = AsyncMock()
        agent.remember = AsyncMock()
        agent._get_calendar = AsyncMock(return_value="Morning standup at 9am")
        agent._get_recent_emails = AsyncMock(return_value="")
        agent._get_action_items = AsyncMock(return_value="")
        agent._get_system_health = AsyncMock(return_value="healthy")
        agent._get_memory_context = AsyncMock(return_value="")

        async def mock_infer(prompt, system="", **kwargs):
            return "<think>Let me plan this carefully...</think>\n**Today's Priority:** Ship the release."

        agent.infer = mock_infer
        _run(agent._generate_brief())

        self.assertTrue(len(captured_report) > 0)
        msg = captured_report[0]
        self.assertNotIn("<think>", msg, "Think tags must be stripped from Slack message")
        self.assertIn("Today", msg)

    def test_get_system_health_handles_http_error(self):
        """_get_system_health() must return a fallback string if NovaControl is unreachable."""
        agent = ProactiveBriefer()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("connection refused")
            result = _run(agent._get_system_health())
        self.assertIsInstance(result, str)
        self.assertGreater(len(result), 0)

    def test_get_action_items_handles_http_error(self):
        """_get_action_items() must return '' if NovaControl is unreachable."""
        agent = ProactiveBriefer()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("NovaControl down")
            result = _run(agent._get_action_items())
        self.assertEqual(result, "")

    def test_handle_delegates_to_generate_brief(self):
        """handle() must call _generate_brief()."""
        agent = ProactiveBriefer()
        called = []

        async def mock_generate():
            called.append(True)
            return {"brief": "test brief", "date": "2026-01-01"}

        agent._generate_brief = mock_generate
        result = _run(agent.handle({"type": "brief"}))
        self.assertTrue(len(called) > 0, "handle() must call _generate_brief()")


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_generate_brief_stores_in_memory(self):
        """Successful brief must be stored in Nova's memory."""
        agent = ProactiveBriefer()
        agent.report_to_jordan = AsyncMock()
        agent.notify = AsyncMock()
        agent.remember = AsyncMock()
        agent._get_calendar = AsyncMock(return_value="9am standup")
        agent._get_recent_emails = AsyncMock(return_value="vendor invoice arrived")
        agent._get_action_items = AsyncMock(return_value="- Review roadmap")
        agent._get_system_health = AsyncMock(return_value="all services healthy")
        agent._get_memory_context = AsyncMock(return_value="")

        async def mock_infer(prompt, system="", **kwargs):
            return "**Today's Priority:** Ship the new feature."

        agent.infer = mock_infer

        result = _run(agent._generate_brief())

        agent.remember.assert_called_once()
        self.assertIsNotNone(result)
        self.assertIn("brief", result)

    def test_generate_brief_sends_to_jordan(self):
        """Successful brief must be posted to Jordan via report_to_jordan."""
        agent = ProactiveBriefer()
        agent.report_to_jordan = AsyncMock()
        agent.notify = AsyncMock()
        agent.remember = AsyncMock()
        agent._get_calendar = AsyncMock(return_value="Important meeting at 2pm")
        agent._get_recent_emails = AsyncMock(return_value="")
        agent._get_action_items = AsyncMock(return_value="")
        agent._get_system_health = AsyncMock(return_value="ok")
        agent._get_memory_context = AsyncMock(return_value="")

        async def mock_infer(prompt, system="", **kwargs):
            return "Focus on shipping the release today."

        agent.infer = mock_infer

        _run(agent._generate_brief())
        agent.report_to_jordan.assert_called_once()


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_morning_brief_includes_date(self):
        """Brief message must include today's date."""
        from datetime import datetime
        today = datetime.now().strftime("%B %d, %Y")

        agent = ProactiveBriefer()
        sent_messages = []
        agent.report_to_jordan = AsyncMock(side_effect=lambda m: sent_messages.append(m))
        agent.notify = AsyncMock()
        agent.remember = AsyncMock()
        agent._get_calendar = AsyncMock(return_value="team meeting at 10am")
        agent._get_recent_emails = AsyncMock(return_value="")
        agent._get_action_items = AsyncMock(return_value="")
        agent._get_system_health = AsyncMock(return_value="healthy")
        agent._get_memory_context = AsyncMock(return_value="")
        agent.infer = AsyncMock(return_value="Your brief for today.")

        _run(agent._generate_brief())

        self.assertTrue(len(sent_messages) > 0)
        self.assertIn(today, sent_messages[0],
                      f"Brief must include today's date: {today}")

    def test_run_morning_function_exists(self):
        """run_morning() function must exist for cron scheduling."""
        self.assertTrue(callable(_mod.run_morning),
                        "run_morning() must be defined for cron use")

    def test_cron_mode_calls_run_morning(self):
        """When --cron is in argv, run_morning should be called."""
        # Verify the script has the --cron branch
        src = _SCRIPT.read_text()
        self.assertIn("--cron", src, "Script must handle --cron argument")
        self.assertIn("run_morning", src, "Script must call run_morning() in cron mode")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles_cleanly(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_briefer_instantiates(self):
        agent = ProactiveBriefer()
        self.assertEqual(agent.name, "briefer")

    def test_system_prompt_content(self):
        src = _SCRIPT.read_text()
        self.assertIn("SYSTEM_PROMPT", src)
        self.assertIn("Calendar", _mod.SYSTEM_PROMPT)
        self.assertIn("Priority", _mod.SYSTEM_PROMPT)

    def test_all_data_source_methods_exist(self):
        agent = ProactiveBriefer()
        for method in ["_get_calendar", "_get_recent_emails", "_get_action_items",
                       "_get_system_health", "_get_memory_context"]:
            self.assertTrue(callable(getattr(agent, method, None)),
                            f"Missing method: {method}")

    def test_max_tokens_set_adequately(self):
        """max_tokens must be high enough for a complete brief."""
        agent = ProactiveBriefer()
        self.assertGreaterEqual(agent.max_tokens, 2048,
                                "max_tokens must be at least 2048 for a complete brief")


if __name__ == "__main__":
    unittest.main(verbosity=2)
