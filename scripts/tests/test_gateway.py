"""
test_gateway.py — Unit and mock tests for the Nova Gateway context store.

Covers CRUD operations, session management, and analytics from:
    ~/.openclaw/gateway/nova_gateway/context/store.py

All database access is mocked via asyncpg so tests run without live services.

Written by Jordan Koch.
"""

import asyncio
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Set up import path and mock the config module before importing store
# ---------------------------------------------------------------------------

GATEWAY_DIR = Path.home() / ".openclaw/gateway"
sys.path.insert(0, str(GATEWAY_DIR))

# The store module does `from .. import config`, so we need to ensure
# the nova_gateway package imports work. We mock config at the module level.
_mock_config = MagicMock()
_mock_config.pg_dsn.return_value = "postgresql://localhost/nova_ops_test"
_mock_config.context_ttl.return_value = 3600
_mock_config.get.return_value = {"context": {"cleanup_interval_seconds": 300}}

with patch.dict("sys.modules", {"nova_gateway.config": _mock_config}):
    # Patch config before store.py tries to import it
    import nova_gateway.context.store as store_module
    store_module.config = _mock_config
    from nova_gateway.context.store import ContextStore, _now, _future


# ======================================================================
# Fixtures
# ======================================================================

class _FakeAcquire:
    """Async context manager that mimics pool.acquire()."""

    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        return False


def _make_mock_pool():
    """Create a mock asyncpg pool with acquire() context manager."""
    mock_conn = AsyncMock()
    mock_pool = MagicMock()  # MagicMock so .acquire() is not a coroutine

    # acquire() returns an async context manager (not a coroutine)
    mock_pool.acquire.return_value = _FakeAcquire(mock_conn)
    mock_pool.close = AsyncMock()

    return mock_pool, mock_conn


@pytest.fixture
def store_and_mocks():
    """Create a ContextStore with a mocked pool, returning (store, pool, conn)."""
    s = ContextStore()
    mock_pool, mock_conn = _make_mock_pool()
    s._pool = mock_pool
    s._cleanup_task = None  # Prevent background task
    return s, mock_pool, mock_conn


# ======================================================================
# ContextStore.write()
# ======================================================================

class TestContextStoreWrite:
    """Test ContextStore.write() — upsert key/value with optional TTL."""

    @pytest.mark.asyncio
    async def test_write_basic(self, store_and_mocks):
        """Basic write inserts a key-value pair for a session."""
        cs, pool, conn = store_and_mocks
        conn.execute = AsyncMock()

        await cs.write("session-abc", "mood", "curious")

        # Should call execute at least once (the upsert) + once for _touch_session
        assert conn.execute.call_count >= 1
        first_call = conn.execute.call_args_list[0]
        sql = first_call[0][0]
        assert "INSERT INTO gateway_context_entries" in sql
        assert "ON CONFLICT" in sql
        # Verify the values passed
        args = first_call[0]
        assert args[1] == "session-abc"
        assert args[2] == "mood"
        assert args[3] == "curious"

    @pytest.mark.asyncio
    async def test_write_with_explicit_ttl(self, store_and_mocks):
        """Write with ttl_seconds should set expires_at in the future."""
        cs, pool, conn = store_and_mocks
        conn.execute = AsyncMock()

        await cs.write("session-abc", "temp_key", "temp_val", ttl_seconds=60)

        first_call = conn.execute.call_args_list[0]
        # expires_at is the 5th positional arg ($5)
        expires_at = first_call[0][5]
        assert expires_at is not None
        assert isinstance(expires_at, datetime)
        # Should be roughly 60 seconds in the future
        delta = expires_at - _now()
        assert 50 < delta.total_seconds() < 70

    @pytest.mark.asyncio
    async def test_write_default_ttl_from_config(self, store_and_mocks):
        """Write without explicit TTL should use config.context_ttl()."""
        cs, pool, conn = store_and_mocks
        conn.execute = AsyncMock()

        # config.context_ttl() returns 3600 from our mock
        await cs.write("session-abc", "key1", "val1")

        first_call = conn.execute.call_args_list[0]
        expires_at = first_call[0][5]
        assert expires_at is not None
        delta = expires_at - _now()
        assert 3500 < delta.total_seconds() < 3700

    @pytest.mark.asyncio
    async def test_write_touches_session(self, store_and_mocks):
        """Write should call _touch_session to upsert the session row."""
        cs, pool, conn = store_and_mocks
        conn.execute = AsyncMock()

        await cs.write("session-xyz", "key", "value")

        # The second execute call should be the session upsert
        calls = conn.execute.call_args_list
        assert len(calls) >= 2
        session_upsert = calls[1][0][0]
        assert "gateway_sessions" in session_upsert


# ======================================================================
# ContextStore.read()
# ======================================================================

class TestContextStoreRead:
    """Test ContextStore.read() — read single key, respecting TTL."""

    @pytest.mark.asyncio
    async def test_read_existing_key(self, store_and_mocks):
        """Reading an existing, non-expired key returns its value."""
        cs, pool, conn = store_and_mocks
        conn.fetchrow = AsyncMock(return_value={"value": "hello world"})

        result = await cs.read("session-abc", "greeting")

        assert result == "hello world"
        conn.fetchrow.assert_called_once()
        sql = conn.fetchrow.call_args[0][0]
        assert "gateway_context_entries" in sql
        assert "expires_at" in sql

    @pytest.mark.asyncio
    async def test_read_missing_key_returns_none(self, store_and_mocks):
        """Reading a nonexistent key returns None."""
        cs, pool, conn = store_and_mocks
        conn.fetchrow = AsyncMock(return_value=None)

        result = await cs.read("session-abc", "nonexistent")

        assert result is None

    @pytest.mark.asyncio
    async def test_read_respects_expiry(self, store_and_mocks):
        """The read query filters out expired entries (expires_at > now)."""
        cs, pool, conn = store_and_mocks
        conn.fetchrow = AsyncMock(return_value=None)

        await cs.read("session-abc", "expired_key")

        sql = conn.fetchrow.call_args[0][0]
        assert "expires_at IS NULL OR expires_at >" in sql


# ======================================================================
# ContextStore.read_all()
# ======================================================================

class TestContextStoreReadAll:
    """Test ContextStore.read_all() — read all keys for a session."""

    @pytest.mark.asyncio
    async def test_read_all_returns_dict(self, store_and_mocks):
        """read_all returns a dict of all non-expired keys for a session."""
        cs, pool, conn = store_and_mocks
        conn.fetch = AsyncMock(return_value=[
            {"key": "mood", "value": "calm"},
            {"key": "topic", "value": "weather"},
            {"key": "language", "value": "english"},
        ])

        result = await cs.read_all("session-abc")

        assert result == {"mood": "calm", "topic": "weather", "language": "english"}
        conn.fetch.assert_called_once()

    @pytest.mark.asyncio
    async def test_read_all_empty_session(self, store_and_mocks):
        """read_all on a session with no entries returns empty dict."""
        cs, pool, conn = store_and_mocks
        conn.fetch = AsyncMock(return_value=[])

        result = await cs.read_all("empty-session")

        assert result == {}


# ======================================================================
# ContextStore.delete()
# ======================================================================

class TestContextStoreDelete:
    """Test ContextStore.delete() — delete a single key."""

    @pytest.mark.asyncio
    async def test_delete_single_key(self, store_and_mocks):
        """delete() issues DELETE for the specific session+key."""
        cs, pool, conn = store_and_mocks
        conn.execute = AsyncMock()

        await cs.delete("session-abc", "old_key")

        conn.execute.assert_called_once()
        sql = conn.execute.call_args[0][0]
        assert "DELETE FROM gateway_context_entries" in sql
        assert conn.execute.call_args[0][1] == "session-abc"
        assert conn.execute.call_args[0][2] == "old_key"


# ======================================================================
# ContextStore.delete_session()
# ======================================================================

class TestContextStoreDeleteSession:
    """Test delete_session() — removes all data for a session."""

    @pytest.mark.asyncio
    async def test_delete_session_cleans_all_tables(self, store_and_mocks):
        """delete_session() should delete from entries, context, and sessions."""
        cs, pool, conn = store_and_mocks
        conn.execute = AsyncMock()

        await cs.delete_session("session-to-remove")

        assert conn.execute.call_count == 3
        tables_deleted = set()
        for call in conn.execute.call_args_list:
            sql = call[0][0]
            if "gateway_context_entries" in sql:
                tables_deleted.add("entries")
            elif "gateway_context" in sql and "entries" not in sql:
                tables_deleted.add("context")
            elif "gateway_sessions" in sql:
                tables_deleted.add("sessions")

        assert tables_deleted == {"entries", "context", "sessions"}


# ======================================================================
# Session management — create, write, read back
# ======================================================================

class TestSessionManagement:
    """Test the full session lifecycle: create, write context, read back."""

    @pytest.mark.asyncio
    async def test_full_session_lifecycle(self, store_and_mocks):
        """Write multiple keys, read them all back, then delete the session."""
        cs, pool, conn = store_and_mocks

        execute_calls = []
        async def track_execute(sql, *args):
            execute_calls.append((sql, args))
        conn.execute = AsyncMock(side_effect=track_execute)

        # Write two keys
        await cs.write("lifecycle-session", "name", "Nova")
        await cs.write("lifecycle-session", "role", "assistant")

        # Verify the upserts happened
        entry_inserts = [c for c in execute_calls if "gateway_context_entries" in c[0] and "INSERT" in c[0]]
        assert len(entry_inserts) == 2

        # Read all — mock the return
        conn.fetch = AsyncMock(return_value=[
            {"key": "name", "value": "Nova"},
            {"key": "role", "value": "assistant"},
        ])
        all_data = await cs.read_all("lifecycle-session")
        assert all_data == {"name": "Nova", "role": "assistant"}

        # Delete session
        execute_calls.clear()
        await cs.delete_session("lifecycle-session")
        session_deletes = [c for c in execute_calls if "DELETE" in c[0]]
        assert len(session_deletes) == 3


# ======================================================================
# ContextStore.log_query()
# ======================================================================

class TestLogQuery:
    """Test log_query() — analytics logging to gateway_query_log."""

    @pytest.mark.asyncio
    async def test_log_query_inserts_record(self, store_and_mocks):
        """log_query() should insert into gateway_query_log."""
        cs, pool, conn = store_and_mocks
        conn.execute = AsyncMock()

        await cs.log_query(
            session_id="session-abc",
            task_type="conversation",
            backend_used="ollama",
            model_used="qwen3-coder:30b",
            prompt_length=500,
            response_length=1200,
            latency_ms=850.5,
            fallback_used=False,
            validated=True,
        )

        # First call is the INSERT, second is _touch_session
        assert conn.execute.call_count >= 1
        sql = conn.execute.call_args_list[0][0][0]
        assert "INSERT INTO gateway_query_log" in sql

        args = conn.execute.call_args_list[0][0]
        assert args[1] == "session-abc"
        assert args[2] == "conversation"
        assert args[3] == "ollama"
        assert args[4] == "qwen3-coder:30b"
        assert args[5] == 500
        assert args[6] == 1200

    @pytest.mark.asyncio
    async def test_log_query_without_session_skips_touch(self, store_and_mocks):
        """When session_id is None, _touch_session should not be called."""
        cs, pool, conn = store_and_mocks
        conn.execute = AsyncMock()

        await cs.log_query(
            session_id=None,
            task_type="search",
            backend_used="searxng",
            model_used=None,
            prompt_length=100,
            response_length=500,
            latency_ms=200.0,
        )

        # Only 1 call (the INSERT), not 2 (no _touch_session)
        assert conn.execute.call_count == 1

    @pytest.mark.asyncio
    async def test_log_query_with_session_touches_session(self, store_and_mocks):
        """When session_id is provided, _touch_session should be called."""
        cs, pool, conn = store_and_mocks
        conn.execute = AsyncMock()

        await cs.log_query(
            session_id="active-session",
            task_type="code",
            backend_used="ollama",
            model_used="qwen3-coder:30b",
            prompt_length=200,
            response_length=800,
            latency_ms=1500.0,
        )

        # 2 calls: INSERT + _touch_session
        assert conn.execute.call_count == 2
        session_sql = conn.execute.call_args_list[1][0][0]
        assert "gateway_sessions" in session_sql


# ======================================================================
# ContextStore.stats()
# ======================================================================

class TestStats:
    """Test stats() — return active session and query counts."""

    @pytest.mark.asyncio
    async def test_stats_returns_counts(self, store_and_mocks):
        """stats() should return active_sessions and total_queries."""
        cs, pool, conn = store_and_mocks

        call_count = 0
        async def mock_fetchrow(sql, *args):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"n": 5}  # active sessions
            else:
                return {"n": 142}  # total queries

        conn.fetchrow = AsyncMock(side_effect=mock_fetchrow)

        result = await cs.stats()

        assert result["active_sessions"] == 5
        assert result["total_queries"] == 142

    @pytest.mark.asyncio
    async def test_stats_handles_empty_db(self, store_and_mocks):
        """stats() should handle empty database gracefully."""
        cs, pool, conn = store_and_mocks
        conn.fetchrow = AsyncMock(return_value=None)

        result = await cs.stats()

        assert result["active_sessions"] == 0
        assert result["total_queries"] == 0


# ======================================================================
# ContextStore.recent_queries()
# ======================================================================

class TestRecentQueries:
    """Test recent_queries() — last N query log entries."""

    @pytest.mark.asyncio
    async def test_recent_queries_returns_list(self, store_and_mocks):
        """recent_queries() should return dicts with datetime fields as ISO strings."""
        cs, pool, conn = store_and_mocks
        now = datetime.now(timezone.utc)

        mock_row1 = {
            "id": 1,
            "session_id": "s1",
            "task_type": "conversation",
            "backend_used": "ollama",
            "model_used": "qwen3-coder:30b",
            "prompt_length": 200,
            "response_length": 500,
            "latency_ms": 800.0,
            "fallback_used": False,
            "validated": True,
            "created_at": now,
        }
        mock_row2 = {
            "id": 2,
            "session_id": "s2",
            "task_type": "search",
            "backend_used": "searxng",
            "model_used": None,
            "prompt_length": 50,
            "response_length": 300,
            "latency_ms": 150.0,
            "fallback_used": False,
            "validated": False,
            "created_at": now - timedelta(minutes=5),
        }
        conn.fetch = AsyncMock(return_value=[mock_row1, mock_row2])

        result = await cs.recent_queries(limit=10)

        assert len(result) == 2
        assert result[0]["session_id"] == "s1"
        assert isinstance(result[0]["created_at"], str)  # Should be ISO string
        assert result[1]["task_type"] == "search"

    @pytest.mark.asyncio
    async def test_recent_queries_with_default_limit(self, store_and_mocks):
        """recent_queries() default limit is 20."""
        cs, pool, conn = store_and_mocks
        conn.fetch = AsyncMock(return_value=[])

        await cs.recent_queries()

        sql = conn.fetch.call_args[0][0]
        assert "LIMIT $1" in sql
        assert conn.fetch.call_args[0][1] == 20


# ======================================================================
# Helper functions
# ======================================================================

class TestHelperFunctions:
    """Test _now() and _future() time helpers."""

    def test_now_returns_utc(self):
        result = _now()
        assert result.tzinfo is not None
        assert result.tzinfo == timezone.utc

    def test_future_positive_seconds(self):
        before = _now()
        result = _future(120)
        after = _now()
        assert result > before
        assert (result - before).total_seconds() >= 119
        assert (result - before).total_seconds() <= 122

    def test_future_negative_for_cutoff(self):
        """Negative seconds creates a past timestamp (used in cleanup)."""
        result = _future(-86400)
        now = _now()
        assert result < now
        delta = (now - result).total_seconds()
        assert 86390 < delta < 86410


# ======================================================================
# Schema definitions
# ======================================================================

class TestExtraSchema:
    """Verify the _EXTRA_SCHEMA DDL statements are well-formed."""

    def test_schema_creates_expected_tables(self):
        import re
        tables_created = set()
        for stmt in store_module._EXTRA_SCHEMA:
            if "CREATE TABLE" in stmt:
                m = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", stmt)
                if m:
                    tables_created.add(m.group(1))

        assert "gateway_context_entries" in tables_created
        assert "gateway_query_log" in tables_created

    def test_schema_has_indexes(self):
        index_count = sum(1 for stmt in store_module._EXTRA_SCHEMA if "CREATE INDEX" in stmt)
        assert index_count >= 3  # idx_gce_session, idx_gce_expires, idx_gql_created
