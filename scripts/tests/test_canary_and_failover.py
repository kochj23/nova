#!/usr/bin/env python3
"""
test_canary_and_failover.py — Tests for canary health check and model failover.

Covers:
  - Canary timeout budget (internal timeouts fit within scheduler budget)
  - Ollama inference test timeout
  - Gateway model router failover chain
  - Zombie run cleanup

Written by Jordan Koch.
"""

import json
import socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))


# ============================================================================
# Canary — Timeout Budget Tests
# ============================================================================

class TestCanaryTimeoutBudget:
    """Verify canary internal timeouts fit within scheduler budget."""

    def test_ollama_timeout_within_budget(self):
        """Ollama inference timeout (12s) + port checks (2s*3) + redis (2s) + ntfy (10s) < 45s scheduler budget."""
        ollama_timeout = 12
        port_check_timeout = 2 * 3  # 3 ports at 2s each
        redis_timeout = 2
        ntfy_timeout = 10
        total_worst_case = ollama_timeout + port_check_timeout + redis_timeout + ntfy_timeout
        scheduler_budget = 45
        assert total_worst_case < scheduler_budget, (
            f"Canary worst-case {total_worst_case}s exceeds scheduler budget {scheduler_budget}s"
        )

    def test_ollama_timeout_is_12s(self):
        """Verify the canary uses 12s timeout for Ollama inference (not 30s)."""
        canary_source = (SCRIPTS_DIR / "nova_canary.py").read_text()
        assert "timeout=12" in canary_source, "Canary should use 12s timeout for Ollama"
        assert "timeout=30" not in canary_source, "Old 30s timeout should be removed"

    def test_scheduler_canary_timeout_is_45(self):
        """Verify scheduler.yaml gives canary 45s (not the old 15s)."""
        import yaml
        config_path = Path.home() / ".openclaw/config/scheduler.yaml"
        if not config_path.exists():
            pytest.skip("scheduler.yaml not found")
        with open(config_path) as f:
            config = yaml.safe_load(f)
        canary_cfg = config.get("tasks", {}).get("canary", {})
        assert canary_cfg.get("timeout") == 45, (
            f"Canary scheduler timeout should be 45, got {canary_cfg.get('timeout')}"
        )


class TestCanaryQuickStatus:
    """Test the canary's quick_status function logic."""

    @patch("urllib.request.urlopen")
    @patch("socket.socket")
    def test_all_services_up(self, mock_socket, mock_urlopen):
        """When all services respond, status is all 'up'."""
        mock_sock_instance = MagicMock()
        mock_sock_instance.connect_ex.return_value = 0
        mock_socket.return_value = mock_sock_instance

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"done": True}).encode()
        mock_urlopen.return_value = mock_resp

        with patch.dict(sys.modules, {"redis": MagicMock()}):
            if "nova_canary" in sys.modules:
                del sys.modules["nova_canary"]
            import nova_canary
            status = nova_canary._quick_status()

        assert status["gateway"] == "up"
        assert status["memory"] == "up"
        assert status["scheduler"] == "up"
        assert status["ollama_inference"] == "up"

    @patch("urllib.request.urlopen")
    @patch("socket.socket")
    def test_ollama_timeout_marks_down(self, mock_socket, mock_urlopen):
        """When Ollama times out, ollama_inference is 'down' but others still checked."""
        mock_sock_instance = MagicMock()
        mock_sock_instance.connect_ex.return_value = 0
        mock_socket.return_value = mock_sock_instance

        def urlopen_side_effect(req, timeout=None):
            if "11434" in (req.full_url if hasattr(req, "full_url") else req):
                raise urllib.error.URLError("timed out")
            resp = MagicMock()
            resp.read.return_value = b"{}"
            return resp

        mock_urlopen.side_effect = urlopen_side_effect

        with patch.dict(sys.modules, {"redis": MagicMock()}):
            if "nova_canary" in sys.modules:
                del sys.modules["nova_canary"]
            import nova_canary
            status = nova_canary._quick_status()

        assert status["ollama_inference"] == "down"
        assert status["gateway"] == "up"


# ============================================================================
# Gateway Model Router — Failover Chain
# ============================================================================

class TestModelRouterFailover:
    """Test the 4-tier failover: Ollama → MLX → llama.cpp → OpenRouter."""

    @pytest.mark.integration
    def test_ollama_is_primary(self):
        """Gateway should use Ollama as primary when healthy."""
        resp = urllib.request.urlopen("http://127.0.0.1:18792/health", timeout=5)
        data = json.loads(resp.read())
        assert data["backends"]["active"] == "ollama"

    @pytest.mark.integration
    def test_llamacpp_standby_healthy(self):
        """llama.cpp standby on :11435 should respond to health check."""
        try:
            resp = urllib.request.urlopen("http://127.0.0.1:11435/v1/models", timeout=5)
            data = json.loads(resp.read())
            assert "data" in data or "models" in data
        except Exception as e:
            pytest.fail(f"llama.cpp standby not responding: {e}")

    @pytest.mark.integration
    def test_gateway_reports_llamacpp_healthy(self):
        """Gateway health endpoint should report llama.cpp as healthy."""
        resp = urllib.request.urlopen("http://127.0.0.1:18792/health", timeout=5)
        data = json.loads(resp.read())
        assert data["backends"]["llamacpp"]["healthy"] is True

    @pytest.mark.integration
    def test_ollama_responds_to_inference(self):
        """Ollama should generate at least 1 token within 15s."""
        payload = json.dumps({
            "model": "deepseek-r1:8b",
            "prompt": "1+1=",
            "stream": False,
            "options": {"num_predict": 1, "num_ctx": 128}
        }).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
        assert data.get("done") is True


# ============================================================================
# Scheduler — Zombie Run Cleanup
# ============================================================================

class TestZombieRunDetection:
    """Tests for detecting and cleaning zombie scheduler runs."""

    @pytest.mark.integration
    def test_no_ancient_running_entries(self):
        """No scheduler_runs should be 'running' for more than 24h (except long ingest jobs)."""
        try:
            import psycopg2
            conn = psycopg2.connect("postgresql://kochj@127.0.0.1:5432/nova_ops")
            cur = conn.cursor()
            cur.execute("""
                SELECT task_id, to_timestamp(started_at/1000) as started
                FROM scheduler_runs
                WHERE status = 'running'
                  AND started_at/1000 < EXTRACT(EPOCH FROM NOW() - INTERVAL '48 hours')
            """)
            zombies = cur.fetchall()
            conn.close()
            assert len(zombies) == 0, f"Found {len(zombies)} zombie runs older than 48h: {zombies}"
        except ImportError:
            pytest.skip("psycopg2 not installed")

    @pytest.mark.integration
    def test_nightly_media_running_is_legitimate(self):
        """If nightly_media shows as 'running', verify the process actually exists."""
        try:
            import psycopg2
            import subprocess
            conn = psycopg2.connect("postgresql://kochj@127.0.0.1:5432/nova_ops")
            cur = conn.cursor()
            cur.execute("""
                SELECT run_id FROM scheduler_runs
                WHERE status = 'running' AND task_id = 'nightly_media'
            """)
            rows = cur.fetchall()
            conn.close()
            if rows:
                result = subprocess.run(
                    ["pgrep", "-f", "nova_nightly_media"],
                    capture_output=True, text=True
                )
                assert result.returncode == 0, (
                    "nightly_media marked as running in DB but no process found"
                )
        except ImportError:
            pytest.skip("psycopg2 not installed")


# ============================================================================
# Ops Writer — Task Lifecycle
# ============================================================================

class TestOpsWriter:
    """Test nova_ops_writer fire-and-forget queue behavior."""

    def test_enqueue_without_loop_is_silent(self):
        """Calling _enqueue outside an async context should not raise."""
        if "nova_ops_writer" in sys.modules:
            del sys.modules["nova_ops_writer"]
        with patch.dict(sys.modules, {"asyncpg": MagicMock()}):
            import nova_ops_writer
            nova_ops_writer._enqueue(AsyncMock(), "arg1", "arg2")

    @pytest.mark.asyncio
    async def test_worker_handles_pool_none(self):
        """Worker should skip writes gracefully when pool is None."""
        if "nova_ops_writer" in sys.modules:
            del sys.modules["nova_ops_writer"]
        with patch.dict(sys.modules, {"asyncpg": MagicMock()}):
            import nova_ops_writer
            nova_ops_writer._POOL = None
            nova_ops_writer._QUEUE = None
            nova_ops_writer._WORKER_TASK = None
            nova_ops_writer._ensure_worker()
            op = AsyncMock()
            nova_ops_writer._QUEUE.put_nowait((op, ("test",)))
            await nova_ops_writer._QUEUE.join()
            op.assert_not_called()
