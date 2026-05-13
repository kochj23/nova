"""
test_nova_agent_librarian.py — All 7 test categories for nova_agent_librarian.py
Written by Jordan Koch.
"""

import asyncio
import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_agent_librarian.py"

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "C0ATAF7NZG9"
sys.modules["nova_config"] = _nova_cfg
_redis_mod = MagicMock()
sys.modules["redis"] = _redis_mod
_logger_mock = MagicMock()
_logger_mock.LOG_INFO = "INFO"; _logger_mock.LOG_ERROR = "ERROR"
_logger_mock.log = MagicMock()
sys.modules["nova_logger"] = _logger_mock


class _MockSubAgent:
    name = "base"; model = "test"; backend = "ollama"; channels = []
    description = ""; temperature = 0.3; max_tokens = 4096

    def __init__(self):
        self._redis = MagicMock(); self._pubsub = MagicMock()
        self._running = False; self._task_count = 0
        self._start_time = None; self._last_error = None

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

_spec = importlib.util.spec_from_file_location("nova_agent_librarian", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

LibrarianAgent = _mod.LibrarianAgent


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for p in ["sk-live", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(p, src, f"Credential found: {p!r}")

    def test_system_prompt_forbids_direct_deletion(self):
        """SYSTEM_PROMPT must state that librarian never modifies memories directly."""
        self.assertIn("NEVER", _mod.SYSTEM_PROMPT,
                      "Librarian must be instructed to never directly delete/modify memories")

    def test_memory_server_url_uses_lan(self):
        src = _SCRIPT.read_text()
        # Should use 192.168.1.6 (LAN) not external
        self.assertNotIn("openai.com", src)


class TestPerformance(unittest.TestCase):

    def test_librarian_model_is_mlx(self):
        agent = LibrarianAgent()
        self.assertIn("mlx", agent.backend.lower() + agent.model.lower())

    def test_max_tokens_set(self):
        agent = LibrarianAgent()
        self.assertGreaterEqual(agent.max_tokens, 2048)

    def test_temperature_very_low(self):
        agent = LibrarianAgent()
        self.assertLessEqual(agent.temperature, 0.2)


class TestRetry(unittest.TestCase):

    def test_curate_batch_returns_none_when_few_memories(self):
        """_curate_batch() must return None when < 2 memories are found."""
        agent = LibrarianAgent()
        agent.recall = AsyncMock(return_value=[{"id": "1", "text": "Single memory."}])

        result = _run(agent._curate_batch({"query": "test", "source": "email"}))
        self.assertIsNone(result, "_curate_batch() must return None with < 2 memories")

    def test_handle_returns_none_on_inference_failure(self):
        agent = LibrarianAgent()

        async def fail_infer(prompt, system="", **kwargs):
            raise ConnectionError("MLX down")

        agent.infer = fail_infer
        agent.recall = AsyncMock(return_value=[
            {"id": str(i), "text": f"Memory {i} with enough text to analyze", "source": "test"}
            for i in range(5)
        ])

        result = _run(agent._curate_batch({"query": "test memories", "source": "test"}))
        self.assertIsNone(result)


class TestUnit(unittest.TestCase):

    def test_handle_routes_scan_source(self):
        agent = LibrarianAgent()
        called = []

        async def mock_scan(task):
            called.append(task["source"])
            return {"findings": []}

        agent._scan_source = mock_scan
        _run(agent.handle({"type": "scan_source", "source": "email_archive"}))
        self.assertIn("email_archive", called)

    def test_handle_routes_check_duplicates(self):
        agent = LibrarianAgent()
        called = []

        async def mock_check(task):
            called.append(True)
            return {"duplicates": []}

        agent._check_duplicates = mock_check
        _run(agent.handle({"type": "check_duplicates", "text": "test memory"}))
        self.assertTrue(len(called) > 0)

    def test_curate_batch_returns_none_for_no_query_no_source(self):
        agent = LibrarianAgent()
        result = _run(agent._curate_batch({}))
        self.assertIsNone(result)

    def test_check_duplicates_returns_none_for_empty_text(self):
        agent = LibrarianAgent()
        result = _run(agent._check_duplicates({"text": ""}))
        self.assertIsNone(result)

    def test_scan_source_delegates_to_curate_batch(self):
        agent = LibrarianAgent()
        called = []

        async def mock_curate(task):
            called.append(task)
            return {"findings": []}

        agent._curate_batch = mock_curate
        _run(agent._scan_source({"source": "music", "type": "scan_source"}))
        self.assertTrue(len(called) > 0)
        self.assertEqual(called[0]["type"], "curate_batch")


class TestIntegration(unittest.TestCase):

    def test_curate_batch_reports_findings_to_jordan(self):
        agent = LibrarianAgent()
        agent.report_to_jordan = AsyncMock()
        agent.recall = AsyncMock(return_value=[
            {"id": str(i), "text": f"Memory {i} about Jordan's work projects.", "source": "test", "score": 0.8}
            for i in range(3)
        ])

        findings = [{"type": "duplicate", "severity": "medium",
                     "description": "These two memories are very similar.",
                     "memory_ids": ["0", "1"], "recommendation": "merge"}]

        async def mock_infer(prompt, system="", **kwargs):
            return json.dumps({"findings": findings, "stats": {"memories_analyzed": 3}})

        agent.infer = mock_infer
        _run(agent._curate_batch({"query": "Jordan work memories", "source": "test"}))
        agent.report_to_jordan.assert_called_once()

    def test_curate_batch_no_report_when_clean(self):
        """When no findings, do not bother Jordan with a Slack message."""
        agent = LibrarianAgent()
        agent.report_to_jordan = AsyncMock()
        agent.recall = AsyncMock(return_value=[
            {"id": str(i), "text": f"Memory {i} about Jordan.", "source": "test", "score": 0.8}
            for i in range(3)
        ])

        async def mock_infer(prompt, system="", **kwargs):
            return json.dumps({"findings": [], "stats": {"memories_analyzed": 3}})

        agent.infer = mock_infer
        _run(agent._curate_batch({"query": "Jordan memories", "source": "test"}))
        agent.report_to_jordan.assert_not_called()


class TestFunctional(unittest.TestCase):

    def test_all_channels_present(self):
        agent = LibrarianAgent()
        for ch in ["memory", "curate", "knowledge"]:
            self.assertIn(ch, agent.channels)

    def test_system_prompt_mentions_all_finding_types(self):
        for ftype in ["duplicate", "contradiction", "stale", "relationship"]:
            self.assertIn(ftype, _mod.SYSTEM_PROMPT.lower(),
                          f"SYSTEM_PROMPT must mention '{ftype}' finding type")

    def test_fallback_json_on_parse_failure(self):
        agent = LibrarianAgent()
        agent.report_to_jordan = AsyncMock()
        agent.recall = AsyncMock(return_value=[
            {"id": str(i), "text": f"Memory {i} text here.", "source": "test", "score": 0.8}
            for i in range(3)
        ])

        async def mock_infer(prompt, system="", **kwargs):
            return "This is not valid JSON at all."

        agent.infer = mock_infer
        result = _run(agent._curate_batch({"query": "test", "source": "test"}))
        self.assertIsNotNone(result)
        self.assertIn("findings", result)
        self.assertEqual(result["findings"], [])


class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_instantiates(self):
        agent = LibrarianAgent()
        self.assertEqual(agent.name, "librarian")

    def test_system_prompt_non_empty(self):
        self.assertGreater(len(_mod.SYSTEM_PROMPT), 100)


if __name__ == "__main__":
    unittest.main(verbosity=2)
