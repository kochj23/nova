"""test_peace_agents.py — Tests for proactive peace and subagent framework. Written by Jordan Koch."""

import asyncio
import json
import os
import sys
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open, PropertyMock, AsyncMock, call

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Fixtures from conftest.py: mock_nova_config, mock_nova_logger, tmp_state_dir


# ============================================================================
# Helpers
# ============================================================================

def _make_subprocess_result(stdout="", stderr="", returncode=0):
    """Build a mock subprocess.CompletedProcess."""
    r = MagicMock()
    r.stdout = stdout
    r.stderr = stderr
    r.returncode = returncode
    return r


def _run_async(coro):
    """Run a coroutine synchronously for tests."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ============================================================================
# PROACTIVE PEACE — merged into nova_escalation.jordan_state() on 2026-10-09 (M10)
# The full 7-category suite lives in tests/test_nova_proactive_peace.py.
# ============================================================================


class TestProactivePeaceMerged:
    """Proactive peace is a thin wrapper: it delegates to jordan_state and never posts."""

    def test_should_alert_delegates_to_jordan_state(self, mock_nova_config):
        import nova_proactive_peace as npp
        st = {"depleted": True, "reasons": ["late night (01:10)"], "signals": {}, "available": False}
        with patch.object(npp, "_state", return_value=st):
            assert npp.should_alert() == (False, "late night (01:10)")
        mock_nova_config.post_both.assert_not_called()

    def test_main_is_a_logged_noop(self, mock_nova_config, capsys):
        import nova_proactive_peace as npp
        assert npp.main([]) == 0
        assert "merged" in capsys.readouterr().out
        mock_nova_config.post_both.assert_not_called()


# ============================================================================
# SUBAGENT FRAMEWORK — SubAgent base class
# ============================================================================


class TestSubAgentBase:

    @patch("nova_subagent.redis.from_url")
    def test_subagent_init(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """SubAgent.__init__ connects to Redis and sets up pubsub."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_agent"
            channels = ["test"]

            async def handle(self, task):
                return {"ok": True}

        agent = TestAgent()
        assert agent.name == "test_agent"
        assert agent._running is False
        assert agent._task_count == 0

    @patch("nova_subagent.redis.from_url")
    def test_subagent_register(self, mock_redis_from_url, mock_nova_config, mock_nova_logger, tmp_path):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)
        nova_subagent.REGISTRY_PATH = tmp_path / "subagents" / "runs.json"

        class TestAgent(nova_subagent.SubAgent):
            name = "test_reg"
            model = "test-model:1b"
            channels = ["test"]
            description = "Test registration"

            async def handle(self, task):
                return None

        agent = TestAgent()
        agent._register()

        registry = json.loads(nova_subagent.REGISTRY_PATH.read_text())
        assert "test_reg" in registry["runs"]
        assert registry["runs"]["test_reg"]["status"] == "running"
        assert registry["runs"]["test_reg"]["model"] == "test-model:1b"

    @patch("nova_subagent.redis.from_url")
    def test_subagent_deregister(self, mock_redis_from_url, mock_nova_config, mock_nova_logger, tmp_path):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)
        nova_subagent.REGISTRY_PATH = tmp_path / "subagents" / "runs.json"

        class TestAgent(nova_subagent.SubAgent):
            name = "test_dereg"
            channels = ["test"]

            async def handle(self, task):
                return None

        agent = TestAgent()
        agent._register()
        agent._task_count = 5
        agent._deregister()

        registry = json.loads(nova_subagent.REGISTRY_PATH.read_text())
        assert registry["runs"]["test_dereg"]["status"] == "stopped"
        assert registry["runs"]["test_dereg"]["task_count"] == 5
        mock_redis.delete.assert_called()

    @patch("nova_subagent.redis.from_url")
    def test_subagent_registry_empty_on_first_load(self, mock_redis_from_url, mock_nova_config, mock_nova_logger, tmp_path):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)
        nova_subagent.REGISTRY_PATH = tmp_path / "subagents" / "runs.json"

        class TestAgent(nova_subagent.SubAgent):
            name = "test_empty"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()
        registry = agent._load_registry()
        assert registry == {"version": 2, "runs": {}}

    @patch("nova_subagent.redis.from_url")
    def test_is_backend_healthy_ollama(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_health"
            backend = "ollama"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()

        with patch("nova_subagent.urllib.request.urlopen") as mock_url:
            mock_url.return_value = MagicMock()
            assert agent.is_backend_healthy() is True

        with patch("nova_subagent.urllib.request.urlopen") as mock_url:
            mock_url.side_effect = Exception("connection refused")
            assert agent.is_backend_healthy() is False

    @patch("nova_subagent.redis.from_url")
    def test_is_backend_healthy_mlx(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_health_mlx"
            backend = "mlx"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()

        with patch("nova_subagent.urllib.request.urlopen") as mock_url:
            mock_url.return_value = MagicMock()
            assert agent.is_backend_healthy() is True


# ============================================================================
# SUBAGENT — LLM Inference
# ============================================================================


class TestSubAgentInference:

    @patch("nova_subagent.redis.from_url")
    def test_infer_ollama_success(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_infer"
            backend = "ollama"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"response": "Hello from Ollama"}).encode()

        with patch("nova_subagent.urllib.request.urlopen", return_value=mock_resp) as mock_url:
            result = _run_async(agent.infer("test prompt", system="be helpful"))
            assert result == "Hello from Ollama"

    @patch("nova_subagent.redis.from_url")
    def test_infer_ollama_failure_raises(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_infer_fail"
            backend = "ollama"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()

        with patch("nova_subagent.urllib.request.urlopen", side_effect=Exception("Ollama down")):
            with pytest.raises(Exception, match="Ollama down"):
                _run_async(agent.infer("test prompt"))

    @patch("nova_subagent.redis.from_url")
    def test_infer_mlx_success(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_infer_mlx"
            backend = "mlx"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "choices": [{"message": {"content": "Hello from MLX"}}]
        }).encode()

        with patch("nova_subagent.urllib.request.urlopen", return_value=mock_resp):
            result = _run_async(agent.infer("test prompt", system="be helpful"))
            assert result == "Hello from MLX"

    @patch("nova_subagent.redis.from_url")
    def test_infer_unknown_backend_raises(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_bad_backend"
            backend = "xyzzy"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()
        with pytest.raises(ValueError, match="Unknown backend"):
            _run_async(agent.infer("test"))


# ============================================================================
# SUBAGENT — dispatch / publish
# ============================================================================


class TestSubAgentDispatch:

    @patch("nova_subagent.redis.from_url")
    def test_dispatch_publishes_to_channel(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        nova_subagent.SubAgent.dispatch("email", {"type": "new_email", "subject": "Test"})

        mock_redis.publish.assert_called_once()
        channel_arg = mock_redis.publish.call_args[0][0]
        assert channel_arg == "nova:task:email"

        payload = json.loads(mock_redis.publish.call_args[0][1])
        assert payload["type"] == "new_email"
        assert "_dispatched_at" in payload
        assert "id" in payload

    @patch("nova_subagent.redis.from_url")
    def test_publish_result_includes_agent_metadata(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_subagent
        importlib.reload(nova_subagent)

        class TestAgent(nova_subagent.SubAgent):
            name = "test_publish"
            channels = []

            async def handle(self, task):
                return None

        agent = TestAgent()
        task = {"id": "task-123", "_channel": "nova:task:test"}
        result = {"summary": "done"}

        _run_async(agent._publish_result(task, result))

        mock_redis.publish.assert_called_once()
        channel = mock_redis.publish.call_args[0][0]
        assert channel == "nova:result:test_publish"

        published = json.loads(mock_redis.publish.call_args[0][1])
        assert published["_agent"] == "test_publish"
        assert published["_task_id"] == "task-123"
        assert "_completed_at" in published


# ============================================================================
# ANALYST AGENT
# ============================================================================


class TestAnalystAgent:

    @patch("nova_subagent.redis.from_url")
    def test_analyst_handle_empty_content_returns_none(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_analyst
        importlib.reload(nova_agent_analyst)

        agent = nova_agent_analyst.AnalystAgent()
        result = _run_async(agent.handle({"type": "email", "content": ""}))
        assert result is None

    @patch("nova_subagent.redis.from_url")
    def test_analyst_handle_valid_json_response(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_analyst
        importlib.reload(nova_agent_analyst)

        agent = nova_agent_analyst.AnalystAgent()

        llm_response = json.dumps({
            "summary": "Important email about project deadline",
            "priority": "high",
            "action_items": ["Reply by Friday"],
            "sentiment": "urgent",
            "flag_jordan": True,
        })

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
            with patch.object(agent, "report_to_jordan", new_callable=AsyncMock) as mock_report:
                with patch.object(agent, "remember", new_callable=AsyncMock):
                    result = _run_async(agent.handle({
                        "type": "email",
                        "content": "Project deadline is Friday",
                        "subject": "URGENT: Deadline",
                    }))

        assert result["priority"] == "high"
        assert result["source_type"] == "email"
        assert result["flag_jordan"] is True
        mock_report.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_analyst_handle_think_tags_stripped(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """deepseek-r1 wraps reasoning in <think>...</think> — these must be stripped."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_analyst
        importlib.reload(nova_agent_analyst)

        agent = nova_agent_analyst.AnalystAgent()

        llm_response = '<think>Let me analyze this...</think>{"summary": "Test", "priority": "low", "action_items": [], "sentiment": "neutral", "flag_jordan": false}'

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
            with patch.object(agent, "notify", new_callable=AsyncMock):
                with patch.object(agent, "remember", new_callable=AsyncMock):
                    result = _run_async(agent.handle({
                        "type": "alert",
                        "content": "Test alert",
                        "subject": "Test",
                    }))

        assert result["summary"] == "Test"
        assert result["priority"] == "low"

    @patch("nova_subagent.redis.from_url")
    def test_analyst_handle_unparseable_response_fallback(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """When LLM returns non-JSON, fallback to raw text summary."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_analyst
        importlib.reload(nova_agent_analyst)

        agent = nova_agent_analyst.AnalystAgent()

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value="Just some text, no JSON here."):
            with patch.object(agent, "notify", new_callable=AsyncMock):
                with patch.object(agent, "remember", new_callable=AsyncMock):
                    result = _run_async(agent.handle({
                        "type": "email",
                        "content": "Hello there",
                        "subject": "Hi",
                    }))

        assert result["priority"] == "medium"
        assert result["flag_jordan"] is False
        assert "Just some text" in result["summary"]

    @patch("nova_subagent.redis.from_url")
    def test_analyst_inference_failure_returns_none(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_analyst
        importlib.reload(nova_agent_analyst)

        agent = nova_agent_analyst.AnalystAgent()

        with patch.object(agent, "infer", new_callable=AsyncMock, side_effect=Exception("Ollama down")):
            result = _run_async(agent.handle({
                "type": "email",
                "content": "Test",
                "subject": "Test",
            }))

        assert result is None

    @pytest.mark.frame
    @patch("nova_subagent.redis.from_url")
    def test_analyst_slack_message_format(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Verify Slack message includes emoji, priority, type, subject, summary, action items."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_analyst
        importlib.reload(nova_agent_analyst)

        agent = nova_agent_analyst.AnalystAgent()

        llm_response = json.dumps({
            "summary": "Meeting rescheduled to next week",
            "priority": "medium",
            "action_items": ["Update calendar", "Notify team"],
            "sentiment": "neutral",
            "flag_jordan": False,
        })

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
            with patch.object(agent, "notify", new_callable=AsyncMock) as mock_notify:
                with patch.object(agent, "remember", new_callable=AsyncMock):
                    _run_async(agent.handle({
                        "type": "meeting",
                        "content": "Meeting rescheduled",
                        "subject": "Team sync",
                    }))

        msg = mock_notify.call_args[0][0]
        assert "*Analyst Report*" in msg
        assert "MEDIUM" in msg
        assert "meeting" in msg
        assert "Action Items" in msg


# ============================================================================
# CODER AGENT
# ============================================================================


class TestCoderAgent:

    @patch("nova_subagent.redis.from_url")
    def test_coder_handle_empty_content_returns_none(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_coder
        importlib.reload(nova_agent_coder)

        agent = nova_agent_coder.CoderAgent()
        result = _run_async(agent.handle({"type": "review", "content": ""}))
        assert result is None

    @patch("nova_subagent.redis.from_url")
    def test_coder_handle_valid_review(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_coder
        importlib.reload(nova_agent_coder)

        agent = nova_agent_coder.CoderAgent()

        llm_response = json.dumps({
            "summary": "Well-structured code with minor issues",
            "issues": [
                {"severity": "medium", "description": "Missing error handling", "file": "main.py", "line": 42}
            ],
            "security_concerns": [],
            "suggestions": ["Add try/except around network call"],
            "quality_score": 7,
            "flag_jordan": False,
        })

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
            with patch.object(agent, "notify", new_callable=AsyncMock) as mock_notify:
                result = _run_async(agent.handle({
                    "type": "review",
                    "content": "def main(): pass",
                    "file": "main.py",
                    "repo": "MLXCode",
                }))

        assert result["quality_score"] == 7
        assert result["source_repo"] == "MLXCode"
        mock_notify.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_coder_security_concerns_flag_jordan(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Security concerns always flag Jordan."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_coder
        importlib.reload(nova_agent_coder)

        agent = nova_agent_coder.CoderAgent()

        llm_response = json.dumps({
            "summary": "Critical SQL injection vulnerability",
            "issues": [{"severity": "critical", "description": "SQL injection in login", "file": "auth.py", "line": 15}],
            "security_concerns": ["SQL injection via unsanitized user input"],
            "quality_score": 2,
            "flag_jordan": True,
        })

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
            with patch.object(agent, "report_to_jordan", new_callable=AsyncMock) as mock_report:
                result = _run_async(agent.handle({
                    "type": "review",
                    "content": "query = f'SELECT * FROM users WHERE name = {user_input}'",
                    "file": "auth.py",
                }))

        mock_report.assert_called_once()
        msg = mock_report.call_args[0][0]
        assert "Security" in msg

    @patch("nova_subagent.redis.from_url")
    def test_coder_no_think_tag_stripping(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Coder also strips /no_think tags from qwen3-coder."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_coder
        importlib.reload(nova_agent_coder)

        agent = nova_agent_coder.CoderAgent()

        llm_response = '/no_think {"summary": "Clean", "issues": [], "quality_score": 9, "flag_jordan": false}'

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
            result = _run_async(agent.handle({
                "type": "review",
                "content": "print('hello')",
            }))

        assert result["quality_score"] == 9

    @patch("nova_subagent.redis.from_url")
    def test_coder_reads_content_from_diff_key(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Coder checks 'content', then 'diff', then 'text' keys."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_coder
        importlib.reload(nova_agent_coder)

        agent = nova_agent_coder.CoderAgent()

        llm_response = json.dumps({"summary": "OK", "issues": [], "quality_score": 8, "flag_jordan": False})

        with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
            result = _run_async(agent.handle({
                "type": "review",
                "diff": "+def new_func(): pass",
            }))

        assert result is not None


# ============================================================================
# GARDENER AGENT
# ============================================================================


class TestGardenerAgent:

    @patch("nova_subagent.redis.from_url")
    def test_gardener_handle_with_source(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_gardener
        importlib.reload(nova_agent_gardener)

        agent = nova_agent_gardener.MemoryGardener()

        with patch.object(agent, "_scan_source", new_callable=AsyncMock, return_value={"findings": []}) as mock_scan:
            result = _run_async(agent.handle({"source": "email_archive"}))
            mock_scan.assert_called_once_with("email_archive")

    @patch("nova_subagent.redis.from_url")
    def test_gardener_handle_without_source_runs_full_scan(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_gardener
        importlib.reload(nova_agent_gardener)

        agent = nova_agent_gardener.MemoryGardener()

        with patch.object(agent, "_full_scan", new_callable=AsyncMock, return_value={"findings": []}) as mock_scan:
            result = _run_async(agent.handle({}))
            mock_scan.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_gardener_auto_merge_keeps_longest(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Auto-merge should keep the longest memory and delete shorter duplicates."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_gardener
        importlib.reload(nova_agent_gardener)

        agent = nova_agent_gardener.MemoryGardener()

        memories = [
            {"id": "short", "text": "Hello"},
            {"id": "long", "text": "Hello there, this is a much longer memory with more detail"},
        ]

        def mock_urlopen(url_or_req, timeout=None):
            url = url_or_req if isinstance(url_or_req, str) else url_or_req.full_url
            resp = MagicMock()
            if "/get?id=short" in url:
                resp.read.return_value = json.dumps(memories[0]).encode()
            elif "/get?id=long" in url:
                resp.read.return_value = json.dumps(memories[1]).encode()
            elif "/forget" in url:
                resp.read.return_value = b'{"ok": true}'
            return resp

        with patch("nova_agent_gardener.urllib.request.urlopen", side_effect=mock_urlopen):
            deleted = _run_async(agent._auto_merge(["short", "long"]))

        assert deleted == 1  # short one deleted

    @patch("nova_subagent.redis.from_url")
    def test_gardener_auto_merge_less_than_two_ids_no_op(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_gardener
        importlib.reload(nova_agent_gardener)

        agent = nova_agent_gardener.MemoryGardener()
        deleted = _run_async(agent._auto_merge(["only_one"]))
        assert deleted == 0

    @patch("nova_subagent.redis.from_url")
    def test_gardener_scan_source_few_memories_returns_empty(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """If fewer than 3 memories, skip scanning."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_gardener
        importlib.reload(nova_agent_gardener)

        agent = nova_agent_gardener.MemoryGardener()

        def mock_urlopen(url, timeout=None):
            resp = MagicMock()
            resp.read.return_value = json.dumps([{"id": "1", "text": "only one"}]).encode()
            return resp

        with patch("nova_agent_gardener.urllib.request.urlopen", side_effect=mock_urlopen):
            result = _run_async(agent._scan_source("email"))

        assert result == {"findings": []}


# ============================================================================
# LIBRARIAN AGENT
# ============================================================================


class TestLibrarianAgent:

    @patch("nova_subagent.redis.from_url")
    def test_librarian_dispatch_curate_batch(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_librarian
        importlib.reload(nova_agent_librarian)

        agent = nova_agent_librarian.LibrarianAgent()

        with patch.object(agent, "_curate_batch", new_callable=AsyncMock, return_value={"findings": []}) as mock_curate:
            result = _run_async(agent.handle({"type": "curate_batch", "source": "email"}))
            mock_curate.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_librarian_dispatch_check_duplicates(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_librarian
        importlib.reload(nova_agent_librarian)

        agent = nova_agent_librarian.LibrarianAgent()

        with patch.object(agent, "_check_duplicates", new_callable=AsyncMock, return_value={"duplicates": []}) as mock_dup:
            result = _run_async(agent.handle({"type": "check_duplicates", "text": "test memory"}))
            mock_dup.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_librarian_dispatch_scan_source(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_librarian
        importlib.reload(nova_agent_librarian)

        agent = nova_agent_librarian.LibrarianAgent()

        with patch.object(agent, "_scan_source", new_callable=AsyncMock, return_value=None) as mock_scan:
            result = _run_async(agent.handle({"type": "scan_source", "source": "music"}))
            mock_scan.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_librarian_curate_batch_too_few_memories(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_librarian
        importlib.reload(nova_agent_librarian)

        agent = nova_agent_librarian.LibrarianAgent()

        with patch.object(agent, "recall", new_callable=AsyncMock, return_value=[{"id": "1", "text": "solo"}]):
            result = _run_async(agent._curate_batch({"query": "test", "batch_size": 5}))
            assert result is None

    @patch("nova_subagent.redis.from_url")
    def test_librarian_check_duplicates_empty_text(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_librarian
        importlib.reload(nova_agent_librarian)

        agent = nova_agent_librarian.LibrarianAgent()
        result = _run_async(agent._check_duplicates({"text": ""}))
        assert result is None

    @pytest.mark.frame
    @patch("nova_subagent.redis.from_url")
    def test_librarian_findings_report_format(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Verify Slack report includes all expected sections."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_librarian
        importlib.reload(nova_agent_librarian)

        agent = nova_agent_librarian.LibrarianAgent()

        memories = [
            {"id": "a", "text": "Memory A", "source": "test", "score": 0.95},
            {"id": "b", "text": "Memory B", "source": "test", "score": 0.90},
            {"id": "c", "text": "Memory C", "source": "test", "score": 0.85},
        ]

        llm_response = json.dumps({
            "findings": [
                {"type": "duplicate", "severity": "high", "memory_ids": ["a", "b"],
                 "description": "Same info restated", "recommendation": "merge"}
            ],
            "stats": {"memories_analyzed": 3, "duplicates_found": 1},
        })

        with patch.object(agent, "recall", new_callable=AsyncMock, return_value=memories):
            with patch.object(agent, "infer", new_callable=AsyncMock, return_value=llm_response):
                with patch.object(agent, "report_to_jordan", new_callable=AsyncMock) as mock_report:
                    result = _run_async(agent._curate_batch({"query": "test", "batch_size": 5}))

        mock_report.assert_called_once()
        msg = mock_report.call_args[0][0]
        assert "*Librarian Report*" in msg
        assert "DUPLICATE" in msg
        assert "Recommendation" in msg


# ============================================================================
# LOOKOUT AGENT
# ============================================================================


class TestLookoutAgent:

    @patch("nova_subagent.redis.from_url")
    def test_lookout_no_image_returns_none(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_lookout
        importlib.reload(nova_agent_lookout)

        agent = nova_agent_lookout.LookoutAgent()
        result = _run_async(agent.handle({"type": "vision", "camera": "front_door"}))
        assert result is None

    @patch("nova_subagent.redis.from_url")
    def test_lookout_vehicle_suppressed(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Vehicle detections should be suppressed (too noisy)."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_lookout
        importlib.reload(nova_agent_lookout)

        agent = nova_agent_lookout.LookoutAgent()

        llm_response = json.dumps({
            "description": "Car parked on street",
            "anomaly_detected": True,
            "anomaly_type": "vehicle",
            "severity": "low",
            "flag_jordan": False,
        })

        with patch.object(agent, "_infer_vision", new_callable=AsyncMock, return_value=llm_response):
            result = _run_async(agent.handle({
                "type": "vision",
                "camera": "street",
                "image_base64": "AAAA",
            }))

        assert result["anomaly_detected"] is False

    @patch("nova_subagent.redis.from_url")
    def test_lookout_genuine_anomaly_notifies(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_lookout
        importlib.reload(nova_agent_lookout)

        agent = nova_agent_lookout.LookoutAgent()

        llm_response = json.dumps({
            "description": "Unknown person at back gate",
            "anomaly_detected": True,
            "anomaly_type": "person",
            "severity": "high",
            "confidence": 0.85,
            "details": "Unrecognized person trying gate",
            "flag_jordan": True,
        })

        with patch.object(agent, "_infer_vision", new_callable=AsyncMock, return_value=llm_response):
            with patch.object(agent, "report_to_jordan", new_callable=AsyncMock) as mock_report:
                result = _run_async(agent.handle({
                    "type": "vision",
                    "camera": "back_gate",
                    "image_base64": "AAAA",
                }))

        assert result["anomaly_detected"] is True
        mock_report.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_lookout_reads_image_from_path(self, mock_redis_from_url, mock_nova_config, mock_nova_logger, tmp_path):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_lookout
        importlib.reload(nova_agent_lookout)

        agent = nova_agent_lookout.LookoutAgent()

        # Create a fake image file
        img_path = tmp_path / "test.jpg"
        img_path.write_bytes(b"\xff\xd8\xff\xe0fake_jpeg_data")

        llm_response = json.dumps({
            "description": "Normal scene",
            "anomaly_detected": False,
            "severity": "none",
            "flag_jordan": False,
        })

        with patch.object(agent, "_infer_vision", new_callable=AsyncMock, return_value=llm_response):
            result = _run_async(agent.handle({
                "type": "vision",
                "camera": "test",
                "image_path": str(img_path),
            }))

        assert result is not None
        assert result["anomaly_detected"] is False

    @patch("nova_subagent.redis.from_url")
    def test_lookout_image_read_failure(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_lookout
        importlib.reload(nova_agent_lookout)

        agent = nova_agent_lookout.LookoutAgent()
        result = _run_async(agent.handle({
            "type": "vision",
            "camera": "test",
            "image_path": "/nonexistent/image.jpg",
        }))
        assert result is None

    @pytest.mark.frame
    @patch("nova_subagent.redis.from_url")
    def test_lookout_alert_message_format(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_lookout
        importlib.reload(nova_agent_lookout)

        agent = nova_agent_lookout.LookoutAgent()

        llm_response = json.dumps({
            "description": "Animal in yard",
            "anomaly_detected": True,
            "anomaly_type": "animal",
            "severity": "medium",
            "confidence": 0.7,
            "details": "Coyote spotted",
            "flag_jordan": False,
        })

        with patch.object(agent, "_infer_vision", new_callable=AsyncMock, return_value=llm_response):
            with patch.object(agent, "notify", new_callable=AsyncMock) as mock_notify:
                _run_async(agent.handle({
                    "type": "vision",
                    "camera": "backyard",
                    "image_base64": "AAAA",
                }))

        msg = mock_notify.call_args[0][0]
        assert "*Lookout Alert*" in msg
        assert "MEDIUM" in msg
        assert "backyard" in msg
        assert "Confidence" in msg


# ============================================================================
# SENTINEL AGENT
# ============================================================================


class TestSentinelAgent:

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_dispatch_nmap(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        with patch.object(agent, "_analyze_nmap", new_callable=AsyncMock, return_value=None) as mock_nmap:
            _run_async(agent.handle({"type": "nmap_scan"}))
            mock_nmap.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_dispatch_camera_alert(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        with patch.object(agent, "_analyze_camera", new_callable=AsyncMock, return_value=None) as mock_cam:
            _run_async(agent.handle({"type": "camera_alert"}))
            mock_cam.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_dispatch_unifi_event(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        with patch.object(agent, "_analyze_unifi", new_callable=AsyncMock, return_value=None) as mock_unifi:
            _run_async(agent.handle({"type": "unifi_event"}))
            mock_unifi.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_dispatch_threat_assessment(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        with patch.object(agent, "_threat_assessment", new_callable=AsyncMock, return_value=None) as mock_threat:
            _run_async(agent.handle({"type": "threat_assessment"}))
            mock_threat.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_dispatch_generic(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        with patch.object(agent, "_generic_security", new_callable=AsyncMock, return_value=None) as mock_gen:
            _run_async(agent.handle({"type": "something_else"}))
            mock_gen.assert_called_once()

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_parse_response_valid_json(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        response = json.dumps({"risk_level": "high", "flag_jordan": True, "summary": "Threat detected"})
        result = agent._parse_response(response)
        assert result["risk_level"] == "high"
        assert result["flag_jordan"] is True

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_parse_response_with_think_tags(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        response = '<think>Analyzing the signals...</think>{"risk_level": "low", "flag_jordan": false}'
        result = agent._parse_response(response)
        assert result["risk_level"] == "low"

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_parse_response_invalid_json_fallback(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        result = agent._parse_response("This is just plain text, no JSON.")
        assert result["risk_level"] == "unknown"
        assert result["flag_jordan"] is False
        assert "plain text" in result["summary"]

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_camera_vehicle_suppression(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Vehicle-only camera events should be suppressed (returns None)."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        result = _run_async(agent._analyze_camera({
            "type": "camera_alert",
            "smart_types": ["vehicle", "licensePlate"],
            "camera": "street_cam",
        }))
        assert result is None

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_nmap_no_devices_returns_none(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        # Mock the NovaControl API calls to return no devices
        with patch("nova_agent_sentinel.urllib.request.urlopen", side_effect=Exception("API down")):
            result = _run_async(agent._analyze_nmap({"type": "nmap_scan"}))

        assert result is None

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_threat_assessment_no_signals_returns_none(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()
        result = _run_async(agent._threat_assessment({"type": "threat_assessment", "signals": []}))
        assert result is None

    @patch("nova_subagent.redis.from_url")
    def test_sentinel_generic_security_empty_text_returns_none(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()
        result = _run_async(agent._generic_security({"text": ""}))
        assert result is None

    @pytest.mark.frame
    @patch("nova_subagent.redis.from_url")
    def test_sentinel_report_security_format(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Verify security report message format with emoji, risk level, findings."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib
        import nova_agent_sentinel
        importlib.reload(nova_agent_sentinel)

        agent = nova_agent_sentinel.SecuritySentinel()

        result = {
            "risk_level": "critical",
            "summary": "Unauthorized device detected on network",
            "findings": [
                {"description": "Unknown MAC address on VLAN 10"},
            ],
            "flag_jordan": True,
        }

        with patch.object(agent, "report_to_jordan", new_callable=AsyncMock) as mock_report:
            _run_async(agent._report_security(result, "Network Scan"))

        mock_report.assert_called_once()
        msg = mock_report.call_args[0][0]
        assert "*Sentinel" in msg
        assert "CRITICAL" in msg
        assert "Network Scan" in msg
        assert "Unauthorized device" in msg


# ============================================================================
# FUNCTIONAL — Full workflow tests
# ============================================================================


class TestFunctionalWorkflows:

    @pytest.mark.functional
    @patch("nova_subagent.redis.from_url")
    def test_agent_configuration_properties(self, mock_redis_from_url, mock_nova_config, mock_nova_logger):
        """Verify all agents have correct configuration: name, model, channels, backend."""
        mock_redis = MagicMock()
        mock_redis.pubsub.return_value = MagicMock()
        mock_redis_from_url.return_value = mock_redis

        import importlib

        # Reload all agent modules
        import nova_agent_analyst
        import nova_agent_coder
        import nova_agent_gardener
        import nova_agent_librarian
        import nova_agent_lookout
        import nova_agent_sentinel
        for mod in [nova_agent_analyst, nova_agent_coder, nova_agent_gardener,
                     nova_agent_librarian, nova_agent_lookout, nova_agent_sentinel]:
            importlib.reload(mod)

        agents = {
            "analyst": nova_agent_analyst.AnalystAgent(),
            "coder": nova_agent_coder.CoderAgent(),
            "gardener": nova_agent_gardener.MemoryGardener(),
            "librarian": nova_agent_librarian.LibrarianAgent(),
            "lookout": nova_agent_lookout.LookoutAgent(),
            "sentinel": nova_agent_sentinel.SecuritySentinel(),
        }

        for name, agent in agents.items():
            assert agent.name == name, f"{name} has wrong name: {agent.name}"
            assert agent.model, f"{name} has no model"
            assert isinstance(agent.channels, list), f"{name} channels not a list"
            assert len(agent.channels) > 0, f"{name} has no channels"
            assert agent.backend in ("ollama", "mlx"), f"{name} has invalid backend: {agent.backend}"
            assert agent.description, f"{name} has no description"

        # Specific model checks
        assert agents["analyst"].model == "deepseek-r1:8b"
        assert agents["coder"].model == "qwen3-coder:30b"
        assert agents["lookout"].model == "qwen3-vl:4b"
        assert agents["librarian"].backend == "mlx"
        assert agents["sentinel"].model == "deepseek-r1:8b"
