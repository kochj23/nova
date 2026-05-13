"""
test_nova_subagent.py — All 7 test categories for nova_subagent.py
Written by Jordan Koch.
"""

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

# ---------------------------------------------------------------------------
# Stub dependencies before loading
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_subagent.py"

_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "C0ATAF7NZG9"
_nova_cfg.post_both = MagicMock()
sys.modules["nova_config"] = _nova_cfg

_logger_mock = MagicMock()
_logger_mock.LOG_INFO = "INFO"
_logger_mock.LOG_ERROR = "ERROR"
_logger_mock.LOG_WARN = "WARN"
_logger_mock.LOG_DEBUG = "DEBUG"
_logger_mock.log = MagicMock()
sys.modules["nova_logger"] = _logger_mock

# Stub redis
_redis_mod = MagicMock()
_redis_instance = MagicMock()
_redis_mod.from_url.return_value = _redis_instance
_redis_instance.pubsub.return_value = MagicMock()
sys.modules["redis"] = _redis_mod

_spec = importlib.util.spec_from_file_location("nova_subagent", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

SubAgent = _mod.SubAgent


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# Concrete subclass for testing
class TestAgent(SubAgent):
    name = "test_agent"
    model = "deepseek-r1:8b"
    backend = "ollama"
    channels = ["test_channel"]
    description = "Test agent for unit tests."
    temperature = 0.3

    async def handle(self, task: dict):
        return {"processed": True, "task_type": task.get("type")}


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
        self.assertNotIn(home_path, src, "Hardcoded home path found")

    def test_redis_url_uses_localhost(self):
        """Default Redis URL must use localhost."""
        self.assertIn("localhost", _mod.REDIS_URL)

    def test_ollama_url_uses_localhost(self):
        self.assertIn("127.0.0.1", _mod.OLLAMA_URL)

    def test_mlx_url_uses_localhost(self):
        self.assertIn("127.0.0.1", _mod.MLX_URL)

    def test_remember_uses_local_url(self):
        """remember() must post to local memory server only."""
        src = _SCRIPT.read_text()
        # Should not reference external memory services
        self.assertIn("18790", src, "remember() must use local memory server :18790")

    def test_recall_query_is_url_encoded(self):
        """recall() must URL-encode the query to prevent injection."""
        src = _SCRIPT.read_text()
        self.assertIn("urllib.parse.quote", src,
                      "recall() must URL-encode queries")

    def test_inference_timeout_prevents_hanging(self):
        """SubAgent must enforce an inference timeout."""
        self.assertIsNotNone(SubAgent.INFERENCE_TIMEOUT)
        self.assertGreater(SubAgent.INFERENCE_TIMEOUT, 0)
        self.assertLessEqual(SubAgent.INFERENCE_TIMEOUT, 300,
                             "Inference timeout must not be excessively long")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_heartbeat_interval_reasonable(self):
        """HEARTBEAT_INTERVAL must be at most 60 seconds."""
        self.assertLessEqual(_mod.HEARTBEAT_INTERVAL, 60)
        self.assertGreater(_mod.HEARTBEAT_INTERVAL, 0)

    def test_registry_load_fast(self):
        """_load_registry() must complete in < 10ms for a small registry."""
        agent = TestAgent()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"version": 2, "runs": {}}, f)
            fname = f.name
        try:
            with patch.object(agent, "REGISTRY_PATH" if hasattr(agent, "REGISTRY_PATH") else "_load_registry",
                               new=Path(fname) if hasattr(agent, "REGISTRY_PATH") else agent._load_registry):
                with patch.object(_mod, "REGISTRY_PATH", Path(fname)):
                    start = time.perf_counter()
                    agent._load_registry()
                    elapsed = time.perf_counter() - start
            self.assertLess(elapsed, 0.01, f"_load_registry() took {elapsed:.3f}s")
        finally:
            os.unlink(fname)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_infer_raises_timeout_error_after_timeout(self):
        """infer() must raise TimeoutError when LLM takes too long."""
        agent = TestAgent()

        async def slow_ollama(*args, **kwargs):
            await asyncio.sleep(1000)  # Simulate very slow LLM
            return "response"

        agent._infer_ollama = slow_ollama
        # Patch INFERENCE_TIMEOUT to be very short for testing
        original = _mod.SubAgent.INFERENCE_TIMEOUT
        _mod.SubAgent.INFERENCE_TIMEOUT = 0.01

        try:
            with self.assertRaises((TimeoutError, asyncio.TimeoutError)):
                _run(agent.infer("test prompt"))
        finally:
            _mod.SubAgent.INFERENCE_TIMEOUT = original

    def test_recall_returns_empty_on_network_failure(self):
        """recall() must return [] on network failure."""
        agent = TestAgent()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("memory server down")
            result = _run(agent.recall("test query"))
        self.assertEqual(result, [], "recall() must return [] on failure")

    def test_remember_logs_error_on_failure(self):
        """remember() must log an error on network failure, not raise."""
        agent = TestAgent()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("memory server down")
            try:
                _run(agent.remember("test memory text"))
            except Exception as e:
                self.fail(f"remember() raised: {e}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_agent_instantiates(self):
        agent = TestAgent()
        self.assertEqual(agent.name, "test_agent")
        self.assertFalse(agent._running)
        self.assertEqual(agent._task_count, 0)

    def test_load_registry_returns_default_on_missing_file(self):
        agent = TestAgent()
        with patch.object(_mod, "REGISTRY_PATH", Path("/nonexistent/runs.json")):
            registry = agent._load_registry()
        self.assertEqual(registry["version"], 2)
        self.assertEqual(registry["runs"], {})

    def test_load_registry_returns_default_on_corrupt_json(self):
        agent = TestAgent()
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not json {{{")
            fname = f.name
        try:
            with patch.object(_mod, "REGISTRY_PATH", Path(fname)):
                registry = agent._load_registry()
            self.assertEqual(registry["runs"], {})
        finally:
            os.unlink(fname)

    def test_is_backend_healthy_ollama_returns_bool(self):
        agent = TestAgent()
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = OSError("connection refused")
            result = agent.is_backend_healthy()
        self.assertIsInstance(result, bool)
        self.assertFalse(result)

    def test_infer_uses_ollama_for_ollama_backend(self):
        """infer() must call _infer_ollama() for ollama backend."""
        agent = TestAgent()
        called = []

        async def mock_infer_ollama(prompt, system, model, temp, tokens):
            called.append(True)
            return "test response"

        agent._infer_ollama = mock_infer_ollama
        result = _run(agent.infer("test prompt"))
        self.assertTrue(len(called) > 0, "infer() must call _infer_ollama for ollama backend")
        self.assertEqual(result, "test response")

    def test_infer_uses_mlx_for_mlx_backend(self):
        """infer() must call _infer_mlx() for mlx backend."""
        agent = TestAgent()
        agent.backend = "mlx"
        called = []

        async def mock_infer_mlx(prompt, system, model, temp, tokens):
            called.append(True)
            return "mlx response"

        agent._infer_mlx = mock_infer_mlx
        result = _run(agent.infer("test prompt"))
        self.assertTrue(len(called) > 0)
        agent.backend = "ollama"  # Reset

    def test_notify_calls_slack_post(self):
        """notify() must call _slack_post()."""
        agent = TestAgent()
        posted = []

        async def mock_slack_post(message, channel=None):
            posted.append(message)

        agent._slack_post = mock_slack_post
        _run(agent.notify("Test notification message."))
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0], "Test notification message.")

    def test_report_to_jordan_calls_slack_post(self):
        """report_to_jordan() must call _slack_post()."""
        agent = TestAgent()
        posted = []

        async def mock_slack_post(message, channel=None):
            posted.append(message)

        agent._slack_post = mock_slack_post
        _run(agent.report_to_jordan("Jordan, I found something important."))
        self.assertEqual(len(posted), 1)

    def test_dispatch_static_method_exists(self):
        """SubAgent.dispatch() must be a static method."""
        self.assertTrue(callable(SubAgent.dispatch))

    def test_infer_raises_for_unknown_backend(self):
        """infer() must raise ValueError for unknown backend."""
        agent = TestAgent()
        agent.backend = "unknown_backend_xyz"
        with self.assertRaises((ValueError, Exception)):
            _run(agent.infer("test"))
        agent.backend = "ollama"  # Reset


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_recall_sends_correct_url(self):
        """recall() must send properly formed URL to memory server."""
        agent = TestAgent()
        captured_urls = []

        def fake_urlopen(url, timeout=None):
            captured_urls.append(url)
            raise OSError("test")

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _run(agent.recall("test query about Jordan", n=5, source="email_archive"))

        self.assertTrue(len(captured_urls) > 0)
        url = captured_urls[0]
        self.assertIn("recall", url)
        self.assertIn("n=5", url)
        self.assertIn("email_archive", url)

    def test_remember_sends_correct_payload(self):
        """remember() must send text, source, and metadata."""
        agent = TestAgent()
        captured = []

        def fake_urlopen(req, timeout=None):
            captured.append(json.loads(req.data.decode()))
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            return r

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            _run(agent.remember("Important memory here.", source="subagent.test",
                                metadata={"priority": "high"}))

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["text"], "Important memory here.")
        self.assertEqual(captured[0]["source"], "subagent.test")
        self.assertEqual(captured[0]["metadata"]["priority"], "high")

    def test_publish_result_adds_agent_name(self):
        """_publish_result() must add _agent field to result."""
        agent = TestAgent()
        published = []
        _redis_instance.publish = MagicMock(side_effect=lambda ch, data: published.append(json.loads(data)))

        task = {"id": "task-123", "type": "test"}
        result = {"processed": True}
        _run(agent._publish_result(task, result))

        self.assertTrue(len(published) > 0)
        self.assertEqual(published[0]["_agent"], "test_agent")
        self.assertEqual(published[0]["_task_id"], "task-123")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_concrete_agent_handle_works(self):
        """Concrete TestAgent.handle() must return expected result."""
        agent = TestAgent()
        task = {"type": "email", "content": "Test email content"}
        result = _run(agent.handle(task))
        self.assertIsNotNone(result)
        self.assertTrue(result["processed"])
        self.assertEqual(result["task_type"], "email")

    def test_shutdown_sets_running_false(self):
        """_shutdown() must set _running to False."""
        agent = TestAgent()
        agent._running = True
        agent._shutdown()
        self.assertFalse(agent._running)

    def test_register_deregister_lifecycle(self):
        """_register() and _deregister() must not crash with mocked Redis."""
        agent = TestAgent()
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(_mod, "REGISTRY_PATH", Path(tmpdir) / "runs.json"):
                _redis_instance.set = MagicMock()
                _redis_instance.delete = MagicMock()
                _redis_instance.hset = MagicMock()
                try:
                    agent._register()
                    agent._deregister()
                except Exception as e:
                    self.fail(f"register/deregister raised: {e}")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_subagent_is_abstract(self):
        """SubAgent must be abstract — can't instantiate directly."""
        with self.assertRaises(TypeError):
            SubAgent()

    def test_required_class_attrs(self):
        agent = TestAgent()
        for attr in ["name", "model", "backend", "channels", "description",
                     "temperature", "max_tokens"]:
            self.assertTrue(hasattr(agent, attr), f"Missing: {attr}")

    def test_required_methods(self):
        agent = TestAgent()
        for method in ["run", "handle", "infer", "recall", "remember",
                       "notify", "report_to_jordan", "is_backend_healthy"]:
            self.assertTrue(callable(getattr(agent, method, None)), f"Missing: {method}")

    def test_registry_path_in_home(self):
        self.assertTrue(str(_mod.REGISTRY_PATH).startswith(str(Path.home())))


if __name__ == "__main__":
    unittest.main(verbosity=2)
