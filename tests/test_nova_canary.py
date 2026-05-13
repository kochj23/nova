"""
test_nova_canary.py — All 7 test categories for nova_canary.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# nova_canary does not import nova_config at module level — no stub needed
sys.modules.setdefault("nova_config", MagicMock())

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_canary.py"
_spec = importlib.util.spec_from_file_location("nova_canary", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)
# Stub redis
sys.modules["redis"] = MagicMock()
_spec.loader.exec_module(_mod)

_get_topic = _mod._get_topic
_quick_status = _mod._quick_status


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_no_hardcoded_credentials(self):
        src = _SCRIPT.read_text()
        for pat in ["sk-", "ghp_", "AKIA", "xoxb-"]:
            self.assertNotIn(pat, src, f"Credential found: {pat!r}")

    def test_no_hardcoded_home_path(self):
        src = _SCRIPT.read_text()
        home = str(Path.home()) + "/"
        self.assertNotIn(home, src)

    def test_no_pii_in_source(self):
        src = _SCRIPT.read_text()
        _at = "@"
        for p in ["kochjpar" + _at + "gmail.com", "jordan.koch" + _at + "disney.com"]:
            self.assertNotIn(p, src)

    def test_topic_loaded_from_keychain(self):
        """Topic must come from macOS Keychain, not be hardcoded."""
        src = _SCRIPT.read_text()
        # Confirm it calls 'security find-generic-password'
        self.assertIn("find-generic-password", src, "Keychain lookup not found in source")
        self.assertIn("nova-canary-topic", src)

    def test_no_topic_literal_in_source(self):
        """The ntfy topic value itself must never appear in source."""
        src = _SCRIPT.read_text()
        # Topic should be a UUID or random string — we just check it's loaded dynamically
        self.assertNotIn("ntfy.sh/nova-", src,
                         "ntfy topic URL should not be hardcoded — load from Keychain")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_quick_status_bounded_ports(self):
        """_quick_status checks exactly the 4 expected services."""
        import socket
        call_count = [0]
        original = socket.socket

        class FakeSocket:
            def __init__(self, *a): pass
            def settimeout(self, t): pass
            def connect_ex(self, addr): call_count[0] += 1; return 1
            def close(self): pass

        fake_redis = MagicMock()
        fake_redis.from_url.return_value.ping.side_effect = Exception("down")

        with patch("socket.socket", FakeSocket):
            with patch.dict(sys.modules, {"redis": fake_redis}):
                _quick_status()

        # Checks gateway, memory, scheduler = 3 socket calls
        self.assertEqual(call_count[0], 3, "Should check exactly 3 socket ports")

    def test_quick_status_completes_fast(self):
        """_quick_status must complete in <5 seconds."""
        import time
        import socket

        class FakeSocket:
            def __init__(self, *a): pass
            def settimeout(self, t): pass
            def connect_ex(self, addr): return 1
            def close(self): pass

        fake_redis = MagicMock()
        fake_redis.from_url.return_value.ping.side_effect = Exception("down")

        start = time.perf_counter()
        with patch("socket.socket", FakeSocket):
            with patch.dict(sys.modules, {"redis": fake_redis}):
                _quick_status()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 5.0, f"_quick_status took {elapsed:.2f}s")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_ntfy_failure_is_not_fatal(self):
        """If ntfy.sh is unreachable, main() should exit 0 (not raise)."""
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(stdout="test-topic\n")
            with patch("urllib.request.urlopen", side_effect=OSError("unreachable")):
                import socket

                class FakeSocket:
                    def __init__(self, *a): pass
                    def settimeout(self, t): pass
                    def connect_ex(self, addr): return 0
                    def close(self): pass

                fake_redis = MagicMock()
                fake_redis.from_url.return_value.ping.return_value = True

                with patch("socket.socket", FakeSocket):
                    with patch.dict(sys.modules, {"redis": fake_redis}):
                        with self.assertRaises(SystemExit) as ctx:
                            _mod.main()
                        # Should exit 0 (canary failure not fatal)
                        self.assertEqual(ctx.exception.code, 0)

    def test_missing_topic_exits_nonzero(self):
        """If Keychain topic is missing, main() should exit 1."""
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(stdout="\n")  # empty topic
            with self.assertRaises(SystemExit) as ctx:
                _mod.main()
            self.assertEqual(ctx.exception.code, 1)

    def test_get_topic_uses_security_command(self):
        """_get_topic must call `security find-generic-password`."""
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(stdout="my-topic\n")
            topic = _get_topic()
        cmd = mock_sub.call_args[0][0]
        self.assertIn("security", cmd)
        self.assertIn("find-generic-password", cmd)
        self.assertEqual(topic, "my-topic")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    def test_get_topic_strips_whitespace(self):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(stdout="  my-topic  \n")
            topic = _get_topic()
        self.assertEqual(topic, "my-topic")

    def test_get_topic_returns_empty_on_failure(self):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(stdout="")
            topic = _get_topic()
        self.assertEqual(topic, "")

    def test_quick_status_returns_dict(self):
        import socket

        class FakeSocket:
            def __init__(self, *a): pass
            def settimeout(self, t): pass
            def connect_ex(self, addr): return 1
            def close(self): pass

        fake_redis = MagicMock()
        fake_redis.from_url.return_value.ping.side_effect = Exception("down")

        with patch("socket.socket", FakeSocket):
            with patch.dict(sys.modules, {"redis": fake_redis}):
                result = _quick_status()
        self.assertIsInstance(result, dict)

    def test_quick_status_includes_expected_keys(self):
        import socket

        class FakeSocket:
            def __init__(self, *a): pass
            def settimeout(self, t): pass
            def connect_ex(self, addr): return 0
            def close(self): pass

        fake_redis = MagicMock()
        fake_redis.from_url.return_value.ping.return_value = True

        with patch("socket.socket", FakeSocket):
            with patch.dict(sys.modules, {"redis": fake_redis}):
                result = _quick_status()
        for key in ("gateway", "memory", "scheduler", "redis"):
            self.assertIn(key, result)

    def test_quick_status_up_when_all_connect(self):
        import socket

        class FakeSocket:
            def __init__(self, *a): pass
            def settimeout(self, t): pass
            def connect_ex(self, addr): return 0
            def close(self): pass

        fake_redis = MagicMock()
        fake_redis.from_url.return_value.ping.return_value = True

        with patch("socket.socket", FakeSocket):
            with patch.dict(sys.modules, {"redis": fake_redis}):
                result = _quick_status()
        self.assertTrue(all(v == "up" for v in result.values()))

    def test_quick_status_down_when_none_connect(self):
        import socket

        class FakeSocket:
            def __init__(self, *a): pass
            def settimeout(self, t): pass
            def connect_ex(self, addr): return 1
            def close(self): pass

        fake_redis = MagicMock()
        fake_redis.from_url.return_value.ping.side_effect = Exception("refused")

        with patch("socket.socket", FakeSocket):
            with patch.dict(sys.modules, {"redis": fake_redis}):
                result = _quick_status()
        self.assertTrue(all(v == "down" for v in result.values()))


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_main_sends_degraded_when_services_down(self):
        """When services are down, ntfy message should include DOWN."""
        posted = []

        def fake_urlopen(req, timeout=None):
            posted.append(req)
            return MagicMock(__enter__=lambda s: s, __exit__=MagicMock(return_value=False))

        import socket

        class FakeSocket:
            def __init__(self, *a): pass
            def settimeout(self, t): pass
            def connect_ex(self, addr): return 1
            def close(self): pass

        fake_redis = MagicMock()
        fake_redis.from_url.return_value.ping.side_effect = Exception("down")

        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(stdout="test-topic\n")
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with patch("socket.socket", FakeSocket):
                    with patch.dict(sys.modules, {"redis": fake_redis}):
                        _mod.main()

        self.assertEqual(len(posted), 1)
        req = posted[0]
        body = req.data.decode()
        self.assertIn("DOWN", body.upper())

    def test_main_sends_alive_when_all_up(self):
        """When all services up, ntfy should send silent alive ping."""
        posted = []

        def fake_urlopen(req, timeout=None):
            posted.append(req)
            return MagicMock(__enter__=lambda s: s, __exit__=MagicMock(return_value=False))

        import socket

        class FakeSocket:
            def __init__(self, *a): pass
            def settimeout(self, t): pass
            def connect_ex(self, addr): return 0
            def close(self): pass

        fake_redis = MagicMock()
        fake_redis.from_url.return_value.ping.return_value = True

        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(stdout="test-topic\n")
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with patch("socket.socket", FakeSocket):
                    with patch.dict(sys.modules, {"redis": fake_redis}):
                        _mod.main()

        self.assertEqual(len(posted), 1)
        req = posted[0]
        # Priority should be "min" for silent heartbeat
        self.assertEqual(req.headers.get("Priority"), "min")


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_priority_min_when_all_up(self):
        """Heartbeat priority must be 'min' when everything is healthy."""
        posted_headers = []

        def fake_urlopen(req, timeout=None):
            posted_headers.append(dict(req.headers))
            return MagicMock(__enter__=lambda s: s, __exit__=MagicMock(return_value=False))

        import socket

        class FakeSocket:
            def __init__(self, *a): pass
            def settimeout(self, t): pass
            def connect_ex(self, addr): return 0
            def close(self): pass

        fake_redis = MagicMock()
        fake_redis.from_url.return_value.ping.return_value = True

        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(stdout="my-topic\n")
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with patch("socket.socket", FakeSocket):
                    with patch.dict(sys.modules, {"redis": fake_redis}):
                        _mod.main()

        self.assertEqual(posted_headers[0].get("Priority"), "min")

    def test_priority_high_when_degraded(self):
        """Priority must be 'high' when services are degraded."""
        posted_headers = []

        def fake_urlopen(req, timeout=None):
            posted_headers.append(dict(req.headers))
            return MagicMock(__enter__=lambda s: s, __exit__=MagicMock(return_value=False))

        import socket

        class FakeSocket:
            def __init__(self, *a): pass
            def settimeout(self, t): pass
            def connect_ex(self, addr): return 1  # all down
            def close(self): pass

        fake_redis = MagicMock()
        fake_redis.from_url.return_value.ping.side_effect = Exception("down")

        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(stdout="my-topic\n")
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                with patch("socket.socket", FakeSocket):
                    with patch.dict(sys.modules, {"redis": fake_redis}):
                        _mod.main()

        self.assertEqual(posted_headers[0].get("Priority"), "high")


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles(self):
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Syntax error: {e}")

    def test_script_is_executable(self):
        self.assertTrue(os.access(_SCRIPT, os.X_OK))

    def test_module_has_main(self):
        self.assertTrue(callable(_mod.main))

    def test_module_has_quick_status(self):
        self.assertTrue(callable(_mod._quick_status))

    def test_module_has_get_topic(self):
        self.assertTrue(callable(_mod._get_topic))


if __name__ == "__main__":
    unittest.main(verbosity=2)
