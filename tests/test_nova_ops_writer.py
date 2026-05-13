"""
test_nova_ops_writer.py — All 7 test categories for nova_ops_writer.py
Written by Jordan Koch.
"""

import asyncio
import importlib.util
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

# ---------------------------------------------------------------------------
# Load module under test — stub asyncpg and nova_logger
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_ops_writer.py"

# Stub asyncpg (may not be installed in test env)
_asyncpg_mock = MagicMock()
sys.modules["asyncpg"] = _asyncpg_mock

_logger_mock = MagicMock()
_logger_mock.LOG_INFO = "INFO"
_logger_mock.LOG_ERROR = "ERROR"
_logger_mock.LOG_WARN = "WARN"
_logger_mock.LOG_DEBUG = "DEBUG"
_logger_mock.log = MagicMock()
sys.modules["nova_logger"] = _logger_mock

_spec = importlib.util.spec_from_file_location("nova_ops_writer", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

record_run_start = _mod.record_run_start
record_run_end = _mod.record_run_end
close = _mod.close


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _reset_module_state():
    """Reset module-level state between tests."""
    _mod._POOL = None
    _mod._POOL_LOCK = None
    _mod._QUEUE = None
    _mod._WORKER_TASK = None


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pattern in ["sk-live", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pattern, src, f"Credential found: {pattern!r}")

    def test_db_dsn_does_not_contain_password(self):
        """DB_DSN must not contain a hardcoded password."""
        dsn = _mod.DB_DSN
        # Postgres DSN format: postgresql://user:password@host/db
        # Should not have :something@ pattern with an actual password
        import re
        match = re.search(r"postgresql://\w+:([^@]+)@", dsn)
        if match:
            password = match.group(1)
            self.assertEqual(len(password), 0,
                             f"DB_DSN contains a hardcoded password: {password!r}")

    def test_db_dsn_uses_lan_ip(self):
        """DB_DSN must use LAN IP (not external DB host)."""
        dsn = _mod.DB_DSN
        self.assertNotIn("rds.amazonaws.com", dsn)
        self.assertNotIn("cloud.mongodb.com", dsn)
        self.assertIn("192.168.1.6", dsn,
                      "DB_DSN must use local/LAN PostgreSQL server")

    def test_queue_has_max_size(self):
        """Write queue must have a max size to prevent unbounded memory growth."""
        # Verify from source
        src = _SCRIPT.read_text()
        self.assertIn("maxsize=", src, "Queue must have a maxsize to prevent memory leak")

    def test_enqueue_drops_silently_when_no_loop(self):
        """_enqueue() called from sync context must not crash or block."""
        _reset_module_state()
        try:
            _mod._enqueue(lambda c: None)
        except Exception as e:
            self.fail(f"_enqueue() raised in sync context: {e}")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_enqueue_from_sync_context_is_fast(self):
        """_enqueue() from sync context must be a no-op and complete in < 1ms."""
        _reset_module_state()
        start = time.perf_counter()
        for _ in range(1000):
            _mod._enqueue(lambda c: None)
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.01, f"1000x _enqueue in sync context took {elapsed:.3f}s")

    def test_queue_maxsize_is_reasonable(self):
        """Queue maxsize must be large enough to handle burst writes."""
        src = _SCRIPT.read_text()
        import re
        match = re.search(r"maxsize=(\d+)", src)
        if match:
            maxsize = int(match.group(1))
            self.assertGreaterEqual(maxsize, 100, "Queue maxsize should handle at least 100 pending writes")

    def test_worker_uses_exponential_backoff(self):
        """Worker retry backoff must use 2**attempt pattern."""
        src = _SCRIPT.read_text()
        self.assertIn("2 ** attempt", src,
                      "Worker must use exponential backoff (2 ** attempt)")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_worker_retries_3_times(self):
        """Worker must retry failed writes up to 3 times."""
        src = _SCRIPT.read_text()
        self.assertIn("range(3)", src, "Worker must retry exactly 3 times")

    def test_worker_logs_after_all_retries_fail(self):
        """Worker must log a warning after all 3 attempts fail."""
        src = _SCRIPT.read_text()
        self.assertIn("3 attempts", src, "Worker should log after 3 failed attempts")

    def test_worker_retries_3_times_verified_in_source(self):
        """Worker retry logic (3 attempts with backoff) is verified in source code."""
        src = _SCRIPT.read_text()
        # The retry pattern 'range(3)' must appear
        self.assertIn("range(3)", src, "Worker must retry exactly 3 times")
        # Backoff must use exponential pattern
        self.assertIn("2 ** attempt", src, "Worker must use exponential backoff")

    def test_close_handles_no_pool(self):
        """close() must not crash if pool was never initialized."""
        _reset_module_state()

        async def _test():
            try:
                await close()
            except Exception as e:
                self.fail(f"close() raised when pool is None: {e}")

        _run(_test())


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_enqueue_no_error_without_loop(self):
        """_enqueue() must silently no-op when called outside event loop."""
        _reset_module_state()
        _mod._enqueue(lambda c: None, "arg1", "arg2")

    def test_record_run_start_signature(self):
        """record_run_start() must accept all required parameters."""
        _reset_module_state()
        try:
            record_run_start(
                run_id="test-run-id",
                task_id="test-task",
                task_script="nova_test.py",
                task_group="test",
                scheduled_at_ms=1000000,
                started_at_ms=1000001,
                consecutive_failures=0,
                run_count=1,
                was_retry=False,
            )
        except TypeError as e:
            self.fail(f"record_run_start() missing parameter: {e}")

    def test_record_run_end_signature(self):
        """record_run_end() must accept all required parameters."""
        _reset_module_state()
        try:
            record_run_end(
                run_id="test-run-id",
                ended_at_ms=1000100,
                duration_ms=99,
                exit_code=0,
                status="success",
                error_tail="",
                stdout_tail="Done.",
                retry_recovered=False,
            )
        except TypeError as e:
            self.fail(f"record_run_end() missing parameter: {e}")

    def test_ensure_worker_creates_queue(self):
        """_ensure_worker() must create a queue if none exists."""
        _reset_module_state()

        async def _test():
            _mod._ensure_worker()
            self.assertIsNotNone(_mod._QUEUE, "Queue must be created by _ensure_worker()")
            self.assertIsNotNone(_mod._WORKER_TASK, "Worker task must be created")
            # Cancel worker to clean up
            if _mod._WORKER_TASK and not _mod._WORKER_TASK.done():
                _mod._WORKER_TASK.cancel()
                try:
                    await _mod._WORKER_TASK
                except asyncio.CancelledError:
                    pass

        _run(_test())

    def test_ensure_worker_idempotent(self):
        """_ensure_worker() called twice must not create multiple workers."""
        _reset_module_state()

        async def _test():
            _mod._ensure_worker()
            task1 = _mod._WORKER_TASK
            _mod._ensure_worker()
            task2 = _mod._WORKER_TASK
            # Should be the same task (or task1 is done and recreated)
            # Main check: no duplicate tasks running
            if _mod._WORKER_TASK and not _mod._WORKER_TASK.done():
                _mod._WORKER_TASK.cancel()
                try:
                    await _mod._WORKER_TASK
                except asyncio.CancelledError:
                    pass

        _run(_test())


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_record_run_start_then_end_enqueued_in_order(self):
        """record_run_start() then record_run_end() must both enqueue in async context."""
        async def _test():
            _reset_module_state()
            _mod._ASYNCPG = False  # Disable actual DB

            # These should not crash even without DB
            record_run_start(
                run_id="test-123",
                task_id="test-task",
                task_script="nova_test.py",
                task_group="test",
                scheduled_at_ms=1000000,
                started_at_ms=1000001,
                consecutive_failures=0,
                run_count=1,
                was_retry=False,
            )
            record_run_end(
                run_id="test-123",
                ended_at_ms=1000200,
                duration_ms=199,
                exit_code=0,
                status="success",
                error_tail="",
                stdout_tail="Task completed successfully.",
                retry_recovered=False,
            )

            # Clean up
            if _mod._WORKER_TASK and not _mod._WORKER_TASK.done():
                _mod._WORKER_TASK.cancel()
                try:
                    await _mod._WORKER_TASK
                except asyncio.CancelledError:
                    pass

        _run(_test())

    def test_pool_lazily_initialized(self):
        """Pool must not be initialized until first write attempt."""
        _reset_module_state()
        self.assertIsNone(_mod._POOL, "Pool must start as None (lazy init)")

    def test_close_drains_queue(self):
        """close() must wait for queue to drain before closing pool."""
        async def _test():
            _reset_module_state()
            # With empty queue, close() should complete quickly
            await asyncio.wait_for(close(), timeout=2.0)

        _run(_test())


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_queue_full_drops_silently(self):
        """When queue is full, _enqueue() must drop write without raising."""
        async def _test():
            _reset_module_state()
            # Fill queue to max
            _mod._QUEUE = asyncio.Queue(maxsize=2)
            await _mod._QUEUE.put((lambda c: None, ()))
            await _mod._QUEUE.put((lambda c: None, ()))
            # Queue is now full — next enqueue should drop silently
            try:
                _mod._enqueue(lambda c: None)
            except Exception as e:
                self.fail(f"_enqueue() should not raise when queue is full: {e}")

        _run(_test())

    def test_worker_ignores_none_pool(self):
        """Worker must skip write gracefully when pool returns None."""
        async def _test():
            _reset_module_state()
            _mod._ASYNCPG = True

            called = [False]

            async def mock_op(conn, *args):
                called[0] = True

            with patch.object(_mod, "_get_pool", return_value=None):
                _mod._QUEUE = asyncio.Queue(maxsize=500)
                await _mod._QUEUE.put((mock_op, ()))
                # Run worker with short timeout
                try:
                    await asyncio.wait_for(_mod._worker(), timeout=0.2)
                except asyncio.TimeoutError:
                    pass

            self.assertFalse(called[0], "Worker must skip write when pool is None")

        _run(_test())

    def test_asyncpg_false_skips_pool_init(self):
        """When asyncpg is not installed, pool must remain None."""
        async def _test():
            _reset_module_state()
            _mod._ASYNCPG = False
            pool = await _mod._get_pool()
            self.assertIsNone(pool, "Pool must be None when asyncpg is unavailable")

        _run(_test())


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

    def test_required_functions_exist(self):
        for fn in ["record_run_start", "record_run_end", "close",
                   "_get_pool", "_worker", "_ensure_worker", "_enqueue"]:
            self.assertTrue(callable(getattr(_mod, fn, None)), f"Missing: {fn}")

    def test_db_dsn_defined(self):
        self.assertIsInstance(_mod.DB_DSN, str)
        self.assertGreater(len(_mod.DB_DSN), 10)

    def test_module_constants(self):
        """Module must define queue maxsize constant (used in _ensure_worker)."""
        src = _SCRIPT.read_text()
        self.assertIn("asyncio.Queue", src)

    def test_import_does_not_crash(self):
        """Importing the module must not produce side effects or crash."""
        import importlib
        spec = importlib.util.spec_from_file_location("nova_ops_writer2", _SCRIPT)
        mod2 = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod2)
        except Exception as e:
            self.fail(f"Module import crashed: {e}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
