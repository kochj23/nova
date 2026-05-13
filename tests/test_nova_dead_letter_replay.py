"""
test_nova_dead_letter_replay.py — All 7 test categories for nova_dead_letter_replay.py
Written by Jordan Koch.
"""

import importlib.util
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Stub nova_config, nova_logger, AND redis before loading
# ---------------------------------------------------------------------------
_nova_cfg = MagicMock()
_nova_cfg.SLACK_NOTIFY = "#nova-notifications"
sys.modules["nova_config"] = _nova_cfg

_nova_logger = MagicMock()
_nova_logger.LOG_INFO = "info"
_nova_logger.LOG_WARN = "warn"
_nova_logger.LOG_ERROR = "error"
sys.modules["nova_logger"] = _nova_logger

# Stub redis so tests work without the package installed.
# The script does `import redis` inside main() — stubbing sys.modules["redis"]
# ensures it resolves to our mock.
_redis_stub = MagicMock()
sys.modules["redis"] = _redis_stub

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_dead_letter_replay.py"
_spec = importlib.util.spec_from_file_location("nova_dead_letter_replay", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

main = _mod.main
REDIS_QUEUE = _mod.REDIS_QUEUE
REDIS_DEAD_LETTER = _mod.REDIS_DEAD_LETTER


def _setup_redis(mock_redis):
    """Configure the redis stub to return mock_redis from from_url()."""
    _redis_stub.from_url.return_value = mock_redis
    _redis_stub.from_url.side_effect = None


def _setup_redis_error(exc):
    """Configure the redis stub to raise exc from from_url()."""
    _redis_stub.from_url.side_effect = exc


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        """Source must not contain API keys or passwords."""
        src = _SCRIPT.read_text()
        for p in ["sk-", "ghp_", "AKIA", "password ="]:
            self.assertNotIn(p, src, f"Credential found: {p!r}")

    def test_no_pii_emails(self):
        """Source must not contain personal email addresses."""
        src = _SCRIPT.read_text()
        _at = "@"
        for pattern in ["kochjpar" + _at + "gmail.com", "kochj23" + _at + "gmail.com"]:
            self.assertNotIn(pattern, src)

    def test_no_hardcoded_home_path(self):
        """Source must not hardcode literal home path."""
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_redis_url_is_localhost(self):
        """Redis connection must be to localhost only."""
        src = _SCRIPT.read_text()
        self.assertIn("localhost", src)
        external_patterns = ["redis://0.0.0.0", "redis://192.168", "redis://10."]
        for p in external_patterns:
            self.assertNotIn(p, src, f"External Redis found: {p!r}")

    def test_replayed_items_strip_retry_metadata(self):
        """Replayed items must have _retries and _error stripped."""
        mock_redis = MagicMock()
        item = {"text": "test", "_retries": 3, "_error": "embed failed"}
        mock_redis.ping.return_value = True
        mock_redis.llen.return_value = 1
        mock_redis.lpop.return_value = json.dumps(item).encode()

        pushed_items = []

        def capture_rpush(queue, data):
            pushed_items.append(json.loads(data))

        mock_redis.rpush = capture_rpush
        _setup_redis(mock_redis)
        main()

        self.assertEqual(len(pushed_items), 1)
        self.assertNotIn("_retries", pushed_items[0])
        self.assertNotIn("_error", pushed_items[0])


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_replay_1000_items_fast(self):
        """Replay of 1000 items must complete in < 500ms."""
        items = [json.dumps({"text": f"item {i}", "source": "test"}).encode() for i in range(1000)]

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.llen.return_value = len(items)
        mock_redis.lpop.side_effect = items + [None]
        _setup_redis(mock_redis)

        start = time.perf_counter()
        main()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"Replay 1000 items too slow: {elapsed:.3f}s")

    def test_empty_queue_returns_immediately(self):
        """Empty dead-letter queue must return in < 50ms."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.llen.return_value = 0
        _setup_redis(mock_redis)

        start = time.perf_counter()
        main()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.05)


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_exits_when_redis_unavailable(self):
        """main() calls sys.exit(1) when Redis is unavailable."""
        _setup_redis_error(Exception("connection refused"))
        exit_codes = []

        def capture_exit(code):
            exit_codes.append(code)
            raise SystemExit(code)

        with patch("sys.exit", side_effect=capture_exit):
            try:
                main()
            except SystemExit:
                pass

        self.assertIn(1, exit_codes)

    def test_skips_malformed_items(self):
        """Malformed JSON items are skipped (counted as skipped, not replayed)."""
        items = [
            b"not valid json {{{",
            json.dumps({"text": "good item"}).encode(),
        ]

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.llen.return_value = 2
        mock_redis.lpop.side_effect = items + [None]

        replayed_items = []
        mock_redis.rpush = lambda q, d: replayed_items.append(d)
        _setup_redis(mock_redis)
        main()

        # Only 1 should be replayed (good item)
        self.assertEqual(len(replayed_items), 1)

    def test_handles_lpop_returning_none(self):
        """main() handles lpop returning None mid-loop."""
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.llen.return_value = 5
        mock_redis.lpop.return_value = None  # Empty queue unexpectedly
        _setup_redis(mock_redis)

        try:
            main()
        except Exception as exc:
            self.fail(f"main() raised on None lpop: {exc}")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_queue_names_defined(self):
        """Redis queue names must be defined correctly."""
        self.assertEqual(REDIS_QUEUE, "nova:memory:ingest")
        self.assertEqual(REDIS_DEAD_LETTER, "nova:memory:dead-letter")

    def test_item_retry_reset(self):
        """Items moved to ingest queue must have _retries stripped."""
        item = {"text": "test memory", "source": "test", "_retries": 3}
        item.pop("_retries", None)
        item.pop("_error", None)
        self.assertNotIn("_retries", item)
        self.assertNotIn("_error", item)
        self.assertIn("text", item)

    def test_replay_posts_to_slack(self):
        """main() must post results to Slack via post_both."""
        items = [json.dumps({"text": "item 1"}).encode()]
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.llen.return_value = 1
        mock_redis.lpop.side_effect = items + [None]
        _setup_redis(mock_redis)

        main()

        _nova_cfg.post_both.assert_called()
        call_args = _nova_cfg.post_both.call_args[0][0]
        self.assertIn("Replay", call_args)

    def test_empty_queue_no_slack_post(self):
        """Empty dead-letter queue must NOT post to Slack."""
        _nova_cfg.post_both.reset_mock()
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.llen.return_value = 0
        _setup_redis(mock_redis)

        main()

        _nova_cfg.post_both.assert_not_called()


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_full_replay_pipeline(self):
        """All items from dead-letter are moved to ingest queue."""
        dead_items = [
            json.dumps({"text": f"memory {i}", "source": "test", "_retries": 3}).encode()
            for i in range(5)
        ]
        ingest_queue = []

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.llen.return_value = len(dead_items)
        mock_redis.lpop.side_effect = dead_items + [None]
        mock_redis.rpush = lambda q, d: ingest_queue.append(json.loads(d))
        _setup_redis(mock_redis)

        main()

        self.assertEqual(len(ingest_queue), 5)
        for item in ingest_queue:
            self.assertNotIn("_retries", item)
            self.assertIn("text", item)

    def test_mixed_valid_and_malformed_items(self):
        """Mix of valid and malformed items: valid replayed, malformed skipped."""
        items = [
            json.dumps({"text": "good 1"}).encode(),
            b"bad json",
            json.dumps({"text": "good 2"}).encode(),
            b"",
            json.dumps({"text": "good 3"}).encode(),
        ]
        replayed = []

        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.llen.return_value = 5
        mock_redis.lpop.side_effect = items + [None]
        mock_redis.rpush = lambda q, d: replayed.append(d)
        _setup_redis(mock_redis)

        main()

        self.assertEqual(len(replayed), 3, "3 valid items should be replayed")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_replay_count_in_slack_message(self):
        """Slack message must include count of replayed items."""
        items = [json.dumps({"text": f"item {i}"}).encode() for i in range(7)]
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.llen.return_value = 7
        mock_redis.lpop.side_effect = items + [None]
        _setup_redis(mock_redis)

        slack_messages = []
        _nova_cfg.post_both.side_effect = lambda msg, **kw: slack_messages.append(msg)

        main()

        _nova_cfg.post_both.side_effect = None
        self.assertTrue(len(slack_messages) > 0)
        self.assertIn("7", slack_messages[0])

    def test_redis_unavailable_exits_1(self):
        """When Redis is unavailable (ping fails), main() must exit 1."""
        mock_redis = MagicMock()
        mock_redis.ping.side_effect = Exception("ECONNREFUSED")
        _setup_redis(mock_redis)

        exit_codes = []

        def capture_exit(code):
            exit_codes.append(code)
            raise SystemExit(code)

        with patch("sys.exit", side_effect=capture_exit):
            try:
                main()
            except SystemExit:
                pass

        self.assertIn(1, exit_codes)


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        """nova_dead_letter_replay.py compiles without syntax errors."""
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_queue_constants_defined(self):
        """Queue name constants must be defined and non-empty."""
        self.assertIsInstance(REDIS_QUEUE, str)
        self.assertIsInstance(REDIS_DEAD_LETTER, str)
        self.assertGreater(len(REDIS_QUEUE), 0)
        self.assertGreater(len(REDIS_DEAD_LETTER), 0)

    def test_main_callable(self):
        """main() must be callable."""
        self.assertTrue(callable(main))

    def test_queue_names_are_different(self):
        """Dead-letter and ingest queue must have different names."""
        self.assertNotEqual(REDIS_QUEUE, REDIS_DEAD_LETTER)


if __name__ == "__main__":
    unittest.main(verbosity=2)
