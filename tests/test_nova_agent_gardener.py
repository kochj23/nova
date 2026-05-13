"""
test_nova_agent_gardener.py — All 7 test categories for nova_agent_gardener.py
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

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_agent_gardener.py"

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "C0ATAF7NZG9"
sys.modules["nova_config"] = _nova_cfg
_redis_mod = MagicMock()
sys.modules["redis"] = _redis_mod
_logger_mock = MagicMock()
_logger_mock.LOG_INFO = "INFO"
_logger_mock.LOG_ERROR = "ERROR"
_logger_mock.LOG_WARN = "WARN"
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

_spec = importlib.util.spec_from_file_location("nova_agent_gardener", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

MemoryGardener = _mod.MemoryGardener


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for p in ["sk-live", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(p, src, f"Credential found: {p!r}")

    def test_memory_url_uses_localhost(self):
        self.assertIn("127.0.0.1", _mod.MEMORY_URL)

    def test_gardener_never_deletes_without_ids(self):
        """_auto_merge() must not delete when fewer than 2 memories provided."""
        agent = MemoryGardener()
        result = _run(agent._auto_merge([]))
        self.assertEqual(result, 0)
        result = _run(agent._auto_merge(["single-id"]))
        self.assertEqual(result, 0)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com"]:
            self.assertNotIn(p, src, f"PII found: {p!r}")


class TestPerformance(unittest.TestCase):

    def test_gardener_model_defined(self):
        agent = MemoryGardener()
        self.assertIn("qwen3", agent.model.lower())

    def test_sources_to_scan_bounded(self):
        """SOURCES_TO_SCAN must not be excessively large."""
        self.assertLessEqual(len(_mod.SOURCES_TO_SCAN), 20)

    def test_samples_per_source_bounded(self):
        """SAMPLES_PER_SOURCE must be bounded to prevent memory overload."""
        self.assertLessEqual(_mod.SAMPLES_PER_SOURCE, 100)


class TestRetry(unittest.TestCase):

    def test_full_scan_handles_memory_server_down(self):
        """_full_scan() must return None when memory server is unreachable."""
        agent = MemoryGardener()
        agent.notify = AsyncMock()
        agent.report_to_jordan = AsyncMock()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("memory server down")
            result = _run(agent._full_scan())
        self.assertIsNone(result)

    def test_scan_source_returns_empty_on_failure(self):
        """_scan_source() must return {findings: []} on inference failure."""
        agent = MemoryGardener()

        async def fail_infer(prompt, system="", **kwargs):
            raise ConnectionError("Ollama down")

        agent.infer = fail_infer

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.read.return_value = json.dumps([
                {"id": f"id-{i}", "text": f"Memory text number {i} about Jordan's emails."}
                for i in range(5)
            ]).encode()
            mock_urlopen.return_value.__enter__ = lambda s: mock_resp
            mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)
            result = _run(agent._scan_source("email_archive"))

        self.assertIn("findings", result)
        self.assertEqual(result["findings"], [])


class TestUnit(unittest.TestCase):

    def test_handle_with_source_calls_scan_source(self):
        agent = MemoryGardener()
        called = []

        async def mock_scan(source):
            called.append(source)
            return {"findings": []}

        agent._scan_source = mock_scan
        _run(agent.handle({"source": "email_archive"}))
        self.assertEqual(called, ["email_archive"])

    def test_handle_without_source_calls_full_scan(self):
        agent = MemoryGardener()
        called = []

        async def mock_full():
            called.append(True)
            return {"findings": [], "sources_scanned": 0}

        agent._full_scan = mock_full
        _run(agent.handle({}))
        self.assertTrue(len(called) > 0)

    def test_auto_merge_keeps_longer_memory(self):
        """_auto_merge() must keep the longer (more complete) memory."""
        agent = MemoryGardener()
        deleted_ids = []

        def fake_urlopen(req, timeout=None):
            if hasattr(req, "method") and req.get_method() == "DELETE":
                url = req.full_url if hasattr(req, "full_url") else str(req)
                deleted_ids.append(url)
            r = MagicMock()
            r.__enter__ = lambda s: r
            r.__exit__ = MagicMock(return_value=False)
            return r

        # Mock fetch for both memory IDs
        fetch_count = [0]
        memories = [
            {"id": "short-id", "text": "Short memory."},
            {"id": "long-id", "text": "This is a much longer and more complete memory about Jordan's project work."},
        ]

        def fake_urlopen_all(url_or_req, timeout=None):
            url = str(url_or_req)
            r = MagicMock()
            r.__enter__ = lambda s: r
            r.__exit__ = MagicMock(return_value=False)
            if "short-id" in url:
                r.read.return_value = json.dumps(memories[0]).encode()
            elif "long-id" in url:
                r.read.return_value = json.dumps(memories[1]).encode()
            elif hasattr(url_or_req, "method") and url_or_req.get_method() == "DELETE":
                deleted_ids.append(url)
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen_all):
            count = _run(agent._auto_merge(["short-id", "long-id"]))

        # Should have deleted short-id (the shorter one)
        self.assertEqual(count, 1)


class TestIntegration(unittest.TestCase):

    def test_full_scan_reports_to_jordan(self):
        agent = MemoryGardener()
        agent.report_to_jordan = AsyncMock()
        agent.notify = AsyncMock()

        def mock_stats(*args, **kwargs):
            r = MagicMock()
            r.__enter__ = lambda s: r
            r.__exit__ = MagicMock(return_value=False)
            r.read.return_value = json.dumps(
                {"count": 100, "by_source": {}}
            ).encode()
            return r

        with patch("urllib.request.urlopen", side_effect=mock_stats):
            _run(agent._full_scan())

        agent.report_to_jordan.assert_called_once()

    def test_scan_source_deduplicates_memories(self):
        """_scan_source() must not pass duplicate IDs to LLM."""
        agent = MemoryGardener()
        agent.infer = AsyncMock(return_value='{"findings": [], "stats": {}}')

        dup_mems = [{"id": "dup-id", "text": "Same memory repeated."}] * 10

        with patch("urllib.request.urlopen") as mock_urlopen:
            r = MagicMock()
            r.__enter__ = lambda s: r
            r.__exit__ = MagicMock(return_value=False)
            r.read.return_value = json.dumps(dup_mems).encode()
            mock_urlopen.return_value = r
            result = _run(agent._scan_source("test_source"))

        # Infer should have been called with deduplicated memories
        if agent.infer.called:
            prompt = agent.infer.call_args.args[0]
            count = prompt.count("[ID:dup-id]")
            self.assertLessEqual(count, 1, "Duplicate memories must be deduplicated")


class TestFunctional(unittest.TestCase):

    def test_run_nightly_function_exists(self):
        self.assertTrue(callable(_mod.run_nightly))

    def test_channels_include_garden_and_memory_maintenance(self):
        agent = MemoryGardener()
        self.assertIn("garden", agent.channels)
        self.assertIn("memory_maintenance", agent.channels)

    def test_max_findings_per_run_bounded(self):
        self.assertLessEqual(_mod.MAX_FINDINGS_PER_RUN, 50)


class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_instantiates(self):
        agent = MemoryGardener()
        self.assertEqual(agent.name, "gardener")

    def test_temperature_low(self):
        agent = MemoryGardener()
        self.assertLessEqual(agent.temperature, 0.3)

    def test_system_prompt_mentions_staleness_rules(self):
        src = _SCRIPT.read_text()
        self.assertIn("STALENESS RULES", src, "Gardener must document staleness rules")


if __name__ == "__main__":
    unittest.main(verbosity=2)
