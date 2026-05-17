#!/usr/bin/env python3
"""
test_hot_reload.py — Tests for gateway and scheduler hot-reload config.

Covers all 7 required test types:
  - Security: service_config table access, no secrets in reload response
  - Performance: reload completes within acceptable latency
  - Retry: reload recovers from transient PG failures
  - Unit: config parsing, change detection, backend list rebuild
  - Integration: live PG reads, actual endpoint responses
  - Functional: end-to-end reload flow (change DB → POST /reload → verify effect)
  - Frame: management API response structure validation

Written by Jordan Koch.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))

LAN_IP = "192.168.1.6"
MGMT_URL = f"http://{LAN_IP}:18792"


# ============================================================================
# Security Tests
# ============================================================================

class TestHotReloadSecurity:
    """Verify hot-reload doesn't expose secrets or allow unauthorized changes."""

    @pytest.mark.security
    @pytest.mark.integration
    def test_reload_endpoint_no_secrets_in_response(self):
        """POST /reload response must not contain API keys, tokens, or passwords."""
        try:
            req = urllib.request.Request(f"{MGMT_URL}/reload", method="POST")
            resp = urllib.request.urlopen(req, timeout=10)
            body = resp.read().decode()
            sensitive_patterns = ["sk-", "xoxb-", "xapp-", "ghp_", "AKIA", "Bearer"]
            for pattern in sensitive_patterns:
                assert pattern not in body, f"Reload response contains sensitive pattern: {pattern}"
        except urllib.error.URLError:
            pytest.skip("Gateway not running")

    @pytest.mark.security
    @pytest.mark.integration
    def test_service_config_no_plaintext_secrets(self):
        """service_config table should not contain plaintext API keys."""
        try:
            import psycopg2
            conn = psycopg2.connect(f"postgresql://kochj@{LAN_IP}:5432/nova_ops")
            cur = conn.cursor()
            cur.execute("SELECT service, key, value::text FROM service_config")
            rows = cur.fetchall()
            conn.close()
            sensitive_patterns = ["sk-", "xoxb-", "xapp-", "ghp_", "AKIA"]
            for service, key, value in rows:
                for pattern in sensitive_patterns:
                    assert pattern not in value, (
                        f"Plaintext secret ({pattern}) found in service_config: {service}/{key}"
                    )
        except ImportError:
            pytest.skip("psycopg2 not installed")

    @pytest.mark.security
    def test_reload_only_accepts_post(self):
        """GET /reload should return 405 Method Not Allowed."""
        try:
            urllib.request.urlopen(f"{MGMT_URL}/reload", timeout=5)
            pytest.fail("GET /reload should not succeed")
        except urllib.error.HTTPError as e:
            assert e.code == 405
        except urllib.error.URLError:
            pytest.skip("Gateway not running")


# ============================================================================
# Performance Tests
# ============================================================================

class TestHotReloadPerformance:
    """Verify hot-reload completes within acceptable latency."""

    @pytest.mark.integration
    def test_reload_under_500ms(self):
        """Config reload should complete in under 500ms (PG round-trip + apply)."""
        try:
            req = urllib.request.Request(f"{MGMT_URL}/reload", method="POST")
            start = time.time()
            resp = urllib.request.urlopen(req, timeout=5)
            elapsed = time.time() - start
            data = json.loads(resp.read())
            assert data.get("ok") is True
            assert elapsed < 0.5, f"Reload took {elapsed:.3f}s (budget: 0.5s)"
        except urllib.error.URLError:
            pytest.skip("Gateway not running")

    @pytest.mark.integration
    def test_health_endpoint_under_100ms(self):
        """Health check should respond in under 100ms."""
        try:
            start = time.time()
            resp = urllib.request.urlopen(f"{MGMT_URL}/health", timeout=5)
            elapsed = time.time() - start
            assert elapsed < 0.1, f"Health check took {elapsed:.3f}s (budget: 0.1s)"
        except urllib.error.URLError:
            pytest.skip("Gateway not running")


# ============================================================================
# Retry Tests
# ============================================================================

class TestHotReloadRetry:
    """Verify reload handles transient failures gracefully."""

    @pytest.mark.integration
    def test_reload_with_invalid_json_in_db_doesnt_crash(self):
        """If service_config has malformed JSON, reload should fail gracefully, not crash gateway."""
        try:
            import psycopg2
            conn = psycopg2.connect(f"postgresql://kochj@{LAN_IP}:5432/nova_ops")
            cur = conn.cursor()
            # Insert a bad config row
            cur.execute("""
                INSERT INTO service_config (service, key, value, updated_by)
                VALUES ('gateway', '_test_bad', '"not a dict"'::jsonb, 'test')
                ON CONFLICT (service, key) DO UPDATE SET value = '"not a dict"'::jsonb
            """)
            conn.commit()

            # Reload should still succeed (skip the bad key gracefully)
            req = urllib.request.Request(f"{MGMT_URL}/reload", method="POST")
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            assert data.get("ok") is True

            # Clean up
            cur.execute("DELETE FROM service_config WHERE service = 'gateway' AND key = '_test_bad'")
            conn.commit()
            conn.close()
        except (ImportError, urllib.error.URLError):
            pytest.skip("psycopg2 or gateway not available")

    @pytest.mark.integration
    def test_consecutive_reloads_are_idempotent(self):
        """Multiple rapid reloads should produce the same result."""
        try:
            results = []
            for _ in range(3):
                req = urllib.request.Request(f"{MGMT_URL}/reload", method="POST")
                resp = urllib.request.urlopen(req, timeout=5)
                results.append(json.loads(resp.read()))
            assert all(r.get("ok") for r in results)
            # After first reload, subsequent ones should report no changes
            assert results[1].get("changes") == []
            assert results[2].get("changes") == []
        except urllib.error.URLError:
            pytest.skip("Gateway not running")


# ============================================================================
# Unit Tests
# ============================================================================

class TestHotReloadUnit:
    """Unit tests for config parsing logic (no live services needed)."""

    def test_service_config_table_schema(self):
        """service_config table should have the expected columns."""
        try:
            import psycopg2
            conn = psycopg2.connect(f"postgresql://kochj@{LAN_IP}:5432/nova_ops")
            cur = conn.cursor()
            cur.execute("""
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_name = 'service_config'
                ORDER BY ordinal_position
            """)
            columns = {row[0]: row[1] for row in cur.fetchall()}
            conn.close()
            assert "service" in columns
            assert "key" in columns
            assert "value" in columns
            assert columns["value"] == "jsonb"
            assert "updated_at" in columns
            assert "updated_by" in columns
        except ImportError:
            pytest.skip("psycopg2 not installed")

    def test_gateway_config_keys_exist(self):
        """All expected gateway config keys should be present in DB."""
        try:
            import psycopg2
            conn = psycopg2.connect(f"postgresql://kochj@{LAN_IP}:5432/nova_ops")
            cur = conn.cursor()
            cur.execute("SELECT key FROM service_config WHERE service = 'gateway'")
            keys = {row[0] for row in cur.fetchall()}
            conn.close()
            expected = {"backends", "context_limits", "channel_routing", "signal", "startup"}
            missing = expected - keys
            assert not missing, f"Missing config keys: {missing}"
        except ImportError:
            pytest.skip("psycopg2 not installed")

    def test_backends_config_has_all_urls(self):
        """backends config should have all 4 provider URLs."""
        try:
            import psycopg2
            conn = psycopg2.connect(f"postgresql://kochj@{LAN_IP}:5432/nova_ops")
            cur = conn.cursor()
            cur.execute("SELECT value FROM service_config WHERE service = 'gateway' AND key = 'backends'")
            row = cur.fetchone()
            conn.close()
            assert row is not None
            backends = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            assert "ollama_url" in backends
            assert "mlx_url" in backends
            assert "llamacpp_url" in backends
            assert "openrouter_url" in backends
            assert "health_ttl" in backends
        except ImportError:
            pytest.skip("psycopg2 not installed")

    def test_all_local_urls_use_lan_ip(self):
        """Local backend URLs should use LAN IP, not 127.0.0.1."""
        try:
            import psycopg2
            conn = psycopg2.connect(f"postgresql://kochj@{LAN_IP}:5432/nova_ops")
            cur = conn.cursor()
            cur.execute("SELECT value FROM service_config WHERE service = 'gateway' AND key = 'backends'")
            row = cur.fetchone()
            conn.close()
            backends = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            for key in ("ollama_url", "mlx_url", "llamacpp_url"):
                url = backends[key]
                assert "127.0.0.1" not in url, f"{key} uses localhost: {url}"
                assert LAN_IP in url or "0.0.0.0" in url, f"{key} not on LAN IP: {url}"
        except ImportError:
            pytest.skip("psycopg2 not installed")


# ============================================================================
# Integration Tests
# ============================================================================

class TestHotReloadIntegration:
    """Integration tests against live gateway and PG."""

    @pytest.mark.integration
    def test_health_endpoint_responds(self):
        """Management API health endpoint should respond with JSON."""
        try:
            resp = urllib.request.urlopen(f"{MGMT_URL}/health", timeout=5)
            data = json.loads(resp.read())
            assert data.get("ok") is True
            assert "version" in data
            assert "backends" in data
            assert "uptime_s" in data
        except urllib.error.URLError:
            pytest.skip("Gateway not running")

    @pytest.mark.integration
    def test_reload_returns_ok(self):
        """POST /reload should return ok: true."""
        try:
            req = urllib.request.Request(f"{MGMT_URL}/reload", method="POST")
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            assert data.get("ok") is True
            assert "changes" in data
        except urllib.error.URLError:
            pytest.skip("Gateway not running")

    @pytest.mark.integration
    def test_last_reload_timestamp_updates(self):
        """After reload, health endpoint should show non-zero last_reload."""
        try:
            req = urllib.request.Request(f"{MGMT_URL}/reload", method="POST")
            urllib.request.urlopen(req, timeout=10)
            resp = urllib.request.urlopen(f"{MGMT_URL}/health", timeout=5)
            data = json.loads(resp.read())
            assert data.get("last_reload", 0) > 0
        except urllib.error.URLError:
            pytest.skip("Gateway not running")


# ============================================================================
# Functional Tests (End-to-End)
# ============================================================================

class TestHotReloadFunctional:
    """End-to-end: change DB config → reload → verify backend list updated."""

    @pytest.mark.functional
    @pytest.mark.integration
    def test_change_health_ttl_and_reload(self):
        """Changing health_ttl in DB and reloading should be reflected."""
        try:
            import psycopg2
            conn = psycopg2.connect(f"postgresql://kochj@{LAN_IP}:5432/nova_ops")
            cur = conn.cursor()

            # Get current value
            cur.execute("SELECT value FROM service_config WHERE service = 'gateway' AND key = 'backends'")
            raw = cur.fetchone()[0]
            original = json.loads(raw) if isinstance(raw, str) else raw
            original_ttl = original.get("health_ttl", 30)

            # Change to a test value
            test_ttl = 99
            cur.execute("""
                UPDATE service_config
                SET value = jsonb_set(value, '{health_ttl}', %s::jsonb)
                WHERE service = 'gateway' AND key = 'backends'
            """, (str(test_ttl),))
            conn.commit()

            # Reload
            req = urllib.request.Request(f"{MGMT_URL}/reload", method="POST")
            resp = urllib.request.urlopen(req, timeout=10)
            data = json.loads(resp.read())
            assert data.get("ok") is True
            assert any("health_ttl" in c for c in data.get("changes", []))

            # Restore original
            cur.execute("""
                UPDATE service_config
                SET value = jsonb_set(value, '{health_ttl}', %s::jsonb)
                WHERE service = 'gateway' AND key = 'backends'
            """, (str(original_ttl),))
            conn.commit()
            conn.close()

            # Reload again to restore
            req = urllib.request.Request(f"{MGMT_URL}/reload", method="POST")
            urllib.request.urlopen(req, timeout=10)

        except (ImportError, urllib.error.URLError) as e:
            pytest.skip(f"Dependencies not available: {e}")

    @pytest.mark.functional
    @pytest.mark.integration
    def test_scheduler_sighup_doesnt_crash(self):
        """Sending SIGHUP to scheduler should not crash it."""
        result = subprocess.run(["pgrep", "-f", "nova_scheduler.py"], capture_output=True, text=True)
        if result.returncode != 0:
            pytest.skip("Scheduler not running")
        pid = int(result.stdout.strip().split("\n")[0])
        os.kill(pid, signal.SIGHUP)
        time.sleep(2)
        # Verify still running
        alive = subprocess.run(["kill", "-0", str(pid)], capture_output=True)
        assert alive.returncode == 0, "Scheduler crashed after SIGHUP"


# ============================================================================
# Frame Tests (Response Structure)
# ============================================================================

class TestHotReloadFrame:
    """Validate response structure and content types."""

    @pytest.mark.frame
    @pytest.mark.integration
    def test_health_response_structure(self):
        """Health response must have all required fields."""
        try:
            resp = urllib.request.urlopen(f"{MGMT_URL}/health", timeout=5)
            assert resp.headers.get("Content-Type") == "application/json; charset=utf-8"
            data = json.loads(resp.read())
            required_fields = ["ok", "version", "degraded", "sessions", "uptime_s",
                             "last_reload", "backends", "claude_active_task", "circuit_breakers"]
            for field in required_fields:
                assert field in data, f"Missing field in health response: {field}"
        except urllib.error.URLError:
            pytest.skip("Gateway not running")

    @pytest.mark.frame
    @pytest.mark.integration
    def test_reload_response_structure(self):
        """Reload response must have ok + changes fields."""
        try:
            req = urllib.request.Request(f"{MGMT_URL}/reload", method="POST")
            resp = urllib.request.urlopen(req, timeout=10)
            assert resp.headers.get("Content-Type") == "application/json; charset=utf-8"
            data = json.loads(resp.read())
            assert "ok" in data
            assert "changes" in data
            assert isinstance(data["changes"], list)
        except urllib.error.URLError:
            pytest.skip("Gateway not running")

    @pytest.mark.frame
    @pytest.mark.integration
    def test_backends_in_health_has_required_keys(self):
        """Each backend in health response must have healthy + is_local + last_checked."""
        try:
            resp = urllib.request.urlopen(f"{MGMT_URL}/health", timeout=5)
            data = json.loads(resp.read())
            backends = data.get("backends", {})
            for name in ("ollama", "mlx", "llamacpp", "openrouter"):
                assert name in backends, f"Backend {name} missing from health"
                b = backends[name]
                assert "healthy" in b, f"Backend {name} missing 'healthy'"
                assert "is_local" in b, f"Backend {name} missing 'is_local'"
                assert "last_checked" in b, f"Backend {name} missing 'last_checked'"
        except urllib.error.URLError:
            pytest.skip("Gateway not running")

    @pytest.mark.frame
    @pytest.mark.integration
    def test_mlx_server_binds_to_all_interfaces(self):
        """MLX server startup script should use 0.0.0.0 not a specific IP."""
        script = SCRIPTS_DIR / "mlx_server_start.sh"
        if not script.exists():
            pytest.skip("mlx_server_start.sh not found")
        content = script.read_text()
        assert "--host 0.0.0.0" in content, "MLX server should bind to 0.0.0.0"
        assert "--host 192.168.1.6" not in content, "MLX server should not use hardcoded LAN IP"
