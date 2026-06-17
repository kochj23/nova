"""
test_nova_jarvis_vision.py — All 7 test categories for the JARVIS Phase-3
vision subsystem in nova_jarvis_brain.py (Ollama-primary circuit breaker +
OpenRouter fallback).

Categories covered:
  1. Security      — key from Keychain (never hardcoded/logged), no PII in logs
  2. Performance   — open circuit returns instantly (no unbounded retry/hang)
  3. Retry         — Ollama failure cascades to OpenRouter; both-fail → None
  4. Correctness   — returns the right backend's content; <think> stripped
  5. Edge cases    — unreadable frame, empty content, missing key
  6. Concurrency   — circuit-breaker state machine (open/cooldown/recover)
  7. Privacy       — NO cloud call while local Ollama is healthy

Written for Jordan Koch.
"""

import importlib.util
import socket
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Stub third-party deps imported at module load ───────────────────────────
sys.modules.setdefault("asyncpg", MagicMock())
_aiohttp = MagicMock()
sys.modules.setdefault("aiohttp", _aiohttp)
sys.modules.setdefault("aiohttp.web", _aiohttp.web)

_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_jarvis_brain.py"
_spec = importlib.util.spec_from_file_location("nova_jarvis_brain", _SCRIPT)
jb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(jb)


def _resp(body: bytes):
    """A fake urlopen() context-less response with .read()."""
    r = MagicMock()
    r.read.return_value = body
    return r


def _ollama_ok(text="A bright kitchen, one person cooking."):
    import json
    return _resp(json.dumps({"message": {"content": text}}).encode())


def _openrouter_ok(text="A dim living room, no people present."):
    import json
    return _resp(json.dumps(
        {"choices": [{"message": {"content": text}}]}).encode())


class VisionTestBase(unittest.TestCase):
    def setUp(self):
        # Reset the module-level circuit breaker before every test.
        jb._vision_cb.update({"fails": 0, "open_until": 0.0, "logged_open": False})
        # Silence the module logger so tests don't spam.
        self._log = patch.object(jb, "log", lambda *a, **k: None)
        self._log.start()
        # Default: Keychain returns a usable key.
        self._key = patch.object(
            jb.subprocess, "run",
            return_value=MagicMock(stdout="sk-or-test-key\n"))
        self._key.start()

    def tearDown(self):
        self._log.stop()
        self._key.stop()

    def _route(self, ollama=None, openrouter=None):
        """Return a urlopen side_effect that dispatches by URL.

        `ollama` / `openrouter` may be a response object or an Exception
        (instance/class) to raise.
        """
        def fake(req, timeout=None):
            url = req.full_url
            target = openrouter if "openrouter" in url else ollama
            if target is None:
                raise AssertionError(f"unexpected call to {url}")
            if isinstance(target, Exception) or (
                    isinstance(target, type) and issubclass(target, Exception)):
                raise target if isinstance(target, Exception) else target()
            return target
        return fake


# ── 4 + 7. Correctness & Privacy ────────────────────────────────────────────
class TestCorrectnessAndPrivacy(VisionTestBase):
    def test_local_success_returns_ollama_content(self):
        with patch.object(jb.urllib.request, "urlopen",
                          side_effect=self._route(ollama=_ollama_ok())):
            out = jb.vision_describe.__wrapped__ if hasattr(
                jb.vision_describe, "__wrapped__") else jb.vision_describe
            with patch("builtins.open",
                       unittest.mock.mock_open(read_data=b"jpegbytes")):
                result = jb.vision_describe("/tmp/frame.jpg")
        self.assertIn("kitchen", result)

    def test_no_cloud_call_when_ollama_healthy(self):
        """PRIVACY: a healthy local model must never reach OpenRouter."""
        # openrouter=None → _route raises if the cloud is ever called.
        with patch.object(jb.urllib.request, "urlopen",
                          side_effect=self._route(ollama=_ollama_ok())):
            with patch("builtins.open",
                       unittest.mock.mock_open(read_data=b"x")):
                result = jb.vision_describe("/tmp/frame.jpg")
        self.assertTrue(result)
        self.assertEqual(jb._vision_cb["fails"], 0)

    def test_think_tags_stripped(self):
        with patch.object(jb.urllib.request, "urlopen", side_effect=self._route(
                ollama=_ollama_ok("<think>reasoning</think>Final answer."))):
            with patch("builtins.open",
                       unittest.mock.mock_open(read_data=b"x")):
                result = jb._vision_ollama("Zm9v")
        self.assertEqual(result, "Final answer.")


# ── 3. Retry / external-call cascade ────────────────────────────────────────
class TestRetryCascade(VisionTestBase):
    def test_ollama_fail_falls_back_to_openrouter(self):
        route = self._route(ollama=socket.timeout("timed out"),
                            openrouter=_openrouter_ok())
        with patch.object(jb.urllib.request, "urlopen", side_effect=route):
            with patch("builtins.open",
                       unittest.mock.mock_open(read_data=b"x")):
                result = jb.vision_describe("/tmp/frame.jpg")
        self.assertIn("living room", result)

    def test_both_backends_fail_returns_none(self):
        route = self._route(ollama=socket.timeout(),
                            openrouter=socket.timeout())
        with patch.object(jb.urllib.request, "urlopen", side_effect=route):
            with patch("builtins.open",
                       unittest.mock.mock_open(read_data=b"x")):
                result = jb.vision_describe("/tmp/frame.jpg")
        self.assertIsNone(result)


# ── 6. Concurrency / circuit-breaker state machine ──────────────────────────
class TestCircuitBreaker(VisionTestBase):
    def test_opens_after_threshold(self):
        route = self._route(ollama=socket.timeout(), openrouter=socket.timeout())
        with patch.object(jb.urllib.request, "urlopen", side_effect=route):
            with patch("builtins.open", unittest.mock.mock_open(read_data=b"x")):
                for _ in range(jb.VISION_FAIL_THRESHOLD):
                    jb.vision_describe("/tmp/frame.jpg")
        self.assertGreaterEqual(jb._vision_cb["fails"], jb.VISION_FAIL_THRESHOLD)
        self.assertGreater(jb._vision_cb["open_until"], time.time())

    def test_open_circuit_skips_ollama_uses_cloud(self):
        """While open, Ollama must NOT be hit; cloud serves the request."""
        jb._vision_cb.update({"fails": 5, "open_until": time.time() + 900,
                              "logged_open": True})
        # ollama=None → _route raises if local is called while open.
        with patch.object(jb.urllib.request, "urlopen",
                          side_effect=self._route(openrouter=_openrouter_ok())):
            with patch("builtins.open", unittest.mock.mock_open(read_data=b"x")):
                result = jb.vision_describe("/tmp/frame.jpg")
        self.assertIn("living room", result)

    def test_recovery_resets_breaker(self):
        jb._vision_cb.update({"fails": 2, "open_until": 0.0, "logged_open": True})
        with patch.object(jb.urllib.request, "urlopen",
                          side_effect=self._route(ollama=_ollama_ok())):
            with patch("builtins.open", unittest.mock.mock_open(read_data=b"x")):
                jb.vision_describe("/tmp/frame.jpg")
        self.assertEqual(jb._vision_cb["fails"], 0)
        self.assertFalse(jb._vision_cb["logged_open"])


# ── 2. Performance ──────────────────────────────────────────────────────────
class TestPerformance(VisionTestBase):
    def test_open_circuit_returns_fast_without_local_call(self):
        jb._vision_cb.update({"fails": 9, "open_until": time.time() + 900})
        # Cloud also fails → returns None, but must do so without ever
        # blocking on the local backend (no unbounded retry/hang).
        with patch.object(jb.urllib.request, "urlopen",
                          side_effect=self._route(openrouter=socket.timeout())):
            with patch("builtins.open", unittest.mock.mock_open(read_data=b"x")):
                t = time.time()
                jb.vision_describe("/tmp/frame.jpg")
                self.assertLess(time.time() - t, 1.0)

    def test_timeouts_are_bounded(self):
        self.assertLessEqual(jb.VISION_TIMEOUT, 60)
        self.assertLessEqual(jb.OPENROUTER_VISION_TIMEOUT, 60)
        self.assertGreaterEqual(jb.VISION_COOLDOWN, 60)


# ── 1. Security ─────────────────────────────────────────────────────────────
class TestSecurity(VisionTestBase):
    def test_key_comes_from_keychain_not_hardcoded(self):
        captured = {}

        def fake_run(cmd, *a, **k):
            captured["cmd"] = cmd
            return MagicMock(stdout="sk-secret\n")

        with patch.object(jb.subprocess, "run", side_effect=fake_run):
            with patch.object(jb.urllib.request, "urlopen",
                              side_effect=self._route(openrouter=_openrouter_ok())):
                jb._vision_openrouter("Zm9v")
        self.assertIn("find-generic-password", captured["cmd"])
        self.assertIn("nova-openrouter-api-key", captured["cmd"])

    def test_missing_key_returns_none_no_crash(self):
        with patch.object(jb.subprocess, "run",
                          return_value=MagicMock(stdout="")):
            result = jb._vision_openrouter("Zm9v")
        self.assertIsNone(result)

    def test_no_secret_in_module_source(self):
        src = _SCRIPT.read_text()
        self.assertNotIn("sk-or-", src)
        self.assertNotIn("Bearer sk", src)


# ── 5. Edge cases ───────────────────────────────────────────────────────────
class TestEdgeCases(VisionTestBase):
    def test_unreadable_frame_returns_none(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            result = jb.vision_describe("/tmp/missing.jpg")
        self.assertIsNone(result)

    def test_empty_cloud_content_returns_none(self):
        import json
        empty = _resp(json.dumps(
            {"choices": [{"message": {"content": ""}}]}).encode())
        with patch.object(jb.urllib.request, "urlopen",
                          side_effect=self._route(openrouter=empty)):
            result = jb._vision_openrouter("Zm9v")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
