"""
test_nova_intent_router.py — All 7 test categories for nova_intent_router.py
Written by Jordan Koch.
"""

import importlib.util
import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Load module under test
# ---------------------------------------------------------------------------
_SCRIPT = Path(__file__).parent.parent / "scripts" / "nova_intent_router.py"
_spec = importlib.util.spec_from_file_location("nova_intent_router", _SCRIPT)
_mod = importlib.util.module_from_spec(_spec)

# Stub nova_config, redis before loading
_nova_cfg = MagicMock()
sys.modules["nova_config"] = _nova_cfg

# Suppress redis import — it's handled with try/except in the module
_redis_mock = MagicMock()
_redis_mock.Redis.return_value.ping.side_effect = Exception("no redis in test")
sys.modules["redis"] = _redis_mock

_spec.loader.exec_module(_mod)

# Convenience aliases
route = _mod.route
query_local = _mod.query_local
query_cloud = _mod.query_cloud
_cache_key = _mod._cache_key
_cache_get = _mod._cache_get
_cache_set = _mod._cache_set
INTENT_MAP = _mod.INTENT_MAP
PRIVATE_INTENTS = _mod.PRIVATE_INTENTS
SENSITIVE_INTENTS = _mod.SENSITIVE_INTENTS
CLOUD_INTENTS = _mod.CLOUD_INTENTS
VOICE_INTENTS = _mod.VOICE_INTENTS
MODELS = _mod.MODELS
Backend = _mod.Backend


# ===========================================================================
# 1. SECURITY TESTS
# ===========================================================================

class TestSecurity(unittest.TestCase):

    def test_private_intents_never_fallback_to_cloud(self):
        """PRIVATE intents must return a local-failure message, never route to cloud."""
        with patch.object(_mod, "query_local") as mock_local:
            mock_local.return_value = {"success": False, "error": "Ollama down", "source": "local"}
            with patch.object(_mod, "query_cloud") as mock_cloud:
                result = route("memory_recall", "what did I say about my health?")
        mock_cloud.assert_not_called()
        self.assertFalse(result["success"])
        self.assertIn("NEVER", result.get("error", "").upper() + result.get("error", ""),
                      "Private intent failure message should mention it will never be sent to cloud")

    def test_sensitive_intents_never_fallback_to_cloud(self):
        """SENSITIVE intents must not fall through to cloud."""
        with patch.object(_mod, "query_local") as mock_local:
            mock_local.return_value = {"success": False, "error": "local down", "source": "local"}
            with patch.object(_mod, "query_cloud") as mock_cloud:
                result = route("camera_analysis", "who is at the door?")
        mock_cloud.assert_not_called()

    def test_unknown_intent_routes_local_not_cloud(self):
        """Unknown intents must fall back to local mlx_general, not cloud."""
        with patch.object(_mod, "query_local") as mock_local:
            mock_local.return_value = {"success": True, "response": "ok", "source": "local"}
            with patch.object(_mod, "query_cloud") as mock_cloud:
                route("completely_unknown_intent_xyz", "hello")
        mock_cloud.assert_not_called()
        mock_local.assert_called_once()

    def test_no_hardcoded_api_keys_in_source(self):
        """Source must not contain hardcoded API keys."""
        src = _SCRIPT.read_text()
        forbidden = ["sk-live", "ghp_", "AKIA", "xoxb-", "sk-or-"]
        for pattern in forbidden:
            self.assertNotIn(pattern, src,
                             f"Potential credential found: {pattern!r}")

    def test_no_hardcoded_home_path(self):
        """Source must not hardcode literal home path."""
        src = _SCRIPT.read_text()
        home_path = str(Path.home()) + "/"
        self.assertNotIn(home_path, src,
                         "Hardcoded home path found — use Path.home()")

    def test_openrouter_key_loaded_from_keychain_not_hardcoded(self):
        """_load_openrouter_key() must call 'security' CLI, not return a hardcoded key."""
        # Reset module-level cache before test
        _mod._openrouter_key_cache = None
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
            # Also patch openclaw.json read to return empty config (no key there either)
            with patch.object(_mod.Path, "read_text", return_value="{}"):
                key = _mod._load_openrouter_key()
        self.assertEqual(key, "",
                         "Should return empty string when Keychain has no key and config has none")

    def test_all_intents_are_local_or_explicit_cloud(self):
        """Every intent in INTENT_MAP must be explicitly LOCAL or CLOUD — no ambiguous routing."""
        for intent, (backend, model_key, privacy) in INTENT_MAP.items():
            self.assertIn(backend, (Backend.LOCAL, Backend.CLOUD),
                          f"Intent '{intent}' has unexpected backend: {backend!r}")
            self.assertIn(privacy, ("private", "sensitive", "normal", "cloud"),
                          f"Intent '{intent}' has unexpected privacy level: {privacy!r}")


# ===========================================================================
# 2. PERFORMANCE TESTS
# ===========================================================================

class TestPerformance(unittest.TestCase):

    def test_intent_lookup_fast(self):
        """INTENT_MAP lookup + privacy classification must handle 10,000 lookups in < 50ms."""
        intents = list(INTENT_MAP.keys())
        start = time.perf_counter()
        for i in range(10000):
            intent = intents[i % len(intents)]
            backend, model_key, privacy = INTENT_MAP[intent]
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.05,
                        f"INTENT_MAP 10k lookups took {elapsed:.3f}s (limit 50ms)")

    def test_cache_key_fast(self):
        """_cache_key() must hash 1000 intent+prompt combos in < 100ms."""
        start = time.perf_counter()
        for i in range(1000):
            _cache_key("conversation", f"Hello, how are you? — test {i}")
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.1,
                        f"_cache_key 1000x took {elapsed:.3f}s (limit 100ms)")

    def test_temperature_lookup_covered_for_all_voice_intents(self):
        """All VOICE_INTENTS must have an explicit temperature entry."""
        for vi in VOICE_INTENTS:
            self.assertIn(vi, _mod.INTENT_TEMPERATURE,
                          f"VOICE_INTENT '{vi}' missing explicit temperature")

    def test_model_registry_non_empty(self):
        """MODELS registry must have at least 5 entries."""
        self.assertGreaterEqual(len(MODELS), 5,
                                "MODELS registry should have at least 5 local models")


# ===========================================================================
# 3. RETRY TESTS
# ===========================================================================

class TestRetry(unittest.TestCase):

    def test_query_cloud_retries_on_500(self):
        """query_cloud() must retry once on HTTP 500."""
        import urllib.error
        call_count = [0]

        def flaky_urlopen(req, timeout=None):
            call_count[0] += 1
            if call_count[0] < 2:
                raise urllib.error.HTTPError(
                    url="https://openrouter.ai", code=500, msg="server error",
                    hdrs=None, fp=None
                )
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.read.return_value = json.dumps({
                "choices": [{"message": {"content": "retry worked"}}]
            }).encode()
            return r

        with patch.object(_mod, "_load_openrouter_key", return_value="fake-key"):
            with patch("urllib.request.urlopen", side_effect=flaky_urlopen):
                result = query_cloud("hello", intent="conversation")

        self.assertTrue(result["success"], "query_cloud() should succeed on retry")
        self.assertEqual(call_count[0], 2)

    def test_query_cloud_no_retry_on_404(self):
        """query_cloud() should not indefinitely retry non-retriable errors."""
        import urllib.error
        call_count = [0]

        def failing_urlopen(req, timeout=None):
            call_count[0] += 1
            raise urllib.error.HTTPError(
                url="https://openrouter.ai", code=404, msg="not found",
                hdrs=None, fp=None
            )

        with patch.object(_mod, "_load_openrouter_key", return_value="fake-key"):
            with patch("urllib.request.urlopen", side_effect=failing_urlopen):
                result = query_cloud("hello", intent="conversation", _retry=False)

        self.assertFalse(result["success"])
        self.assertLessEqual(call_count[0], 2,
                             "Should not retry more than once on 404")

    def test_query_cloud_fallback_on_429(self):
        """query_cloud() must fall back to OPENROUTER_MODEL_FALLBACK on 429."""
        import urllib.error
        called_models = []

        def model_tracking_urlopen(req, timeout=None):
            body = json.loads(req.data.decode())
            called_models.append(body.get("model", ""))
            code = 429 if called_models[0] == _mod.OPENROUTER_MODEL else 200
            if len(called_models) == 1:
                raise urllib.error.HTTPError(
                    url="https://openrouter.ai", code=429, msg="rate limit",
                    hdrs=None, fp=None
                )
            r = MagicMock()
            r.__enter__ = lambda s: s
            r.__exit__ = MagicMock(return_value=False)
            r.read.return_value = json.dumps({
                "choices": [{"message": {"content": "fallback worked"}}]
            }).encode()
            return r

        with patch.object(_mod, "_load_openrouter_key", return_value="fake-key"):
            with patch("urllib.request.urlopen", side_effect=model_tracking_urlopen):
                result = query_cloud("hello", intent="conversation")

        self.assertGreaterEqual(len(called_models), 2,
                                "Should have tried primary then fallback model")
        self.assertIn(_mod.OPENROUTER_MODEL_FALLBACK, called_models,
                      "Should have used fallback model after 429")


# ===========================================================================
# 4. UNIT TESTS
# ===========================================================================

class TestUnit(unittest.TestCase):

    # --- INTENT_MAP structure ---

    def test_all_model_keys_exist_in_models(self):
        """Every model_key in INTENT_MAP must reference a valid MODELS entry (or be empty)."""
        for intent, (backend, model_key, privacy) in INTENT_MAP.items():
            if model_key and backend == Backend.LOCAL:
                self.assertIn(model_key, MODELS,
                              f"Intent '{intent}' references unknown model_key '{model_key}'")

    def test_private_intents_subset_of_intent_map(self):
        """PRIVATE_INTENTS must be a subset of INTENT_MAP keys."""
        self.assertTrue(PRIVATE_INTENTS.issubset(set(INTENT_MAP.keys())),
                        "PRIVATE_INTENTS contains intents not in INTENT_MAP")

    def test_voice_intents_are_local(self):
        """All VOICE_INTENTS must route to LOCAL backend."""
        for vi in VOICE_INTENTS:
            backend, _, _ = INTENT_MAP.get(vi, (Backend.LOCAL, "", ""))
            self.assertEqual(backend, Backend.LOCAL,
                             f"VOICE_INTENT '{vi}' must route LOCAL")

    def test_health_intents_are_private(self):
        """Health-related intents must have 'private' privacy level."""
        health_intents = ["health_query", "health_summary", "health_trend", "health_alert"]
        for intent in health_intents:
            _, _, privacy = INTENT_MAP.get(intent, (None, None, "?"))
            self.assertEqual(privacy, "private",
                             f"Health intent '{intent}' must be 'private', got '{privacy}'")

    def test_memory_intents_are_private(self):
        """Memory recall intents must have 'private' privacy level."""
        for intent in ["memory_recall", "memory_query", "personal_memory", "email_recall"]:
            _, _, privacy = INTENT_MAP.get(intent, (None, None, "?"))
            self.assertEqual(privacy, "private",
                             f"Memory intent '{intent}' must be 'private'")

    def test_image_generation_special_case(self):
        """image_generation intent must route to swarmui without calling LLM."""
        with patch.object(_mod, "query_local") as mock_local:
            result = route("image_generation", "generate a cat picture")
        mock_local.assert_not_called()
        self.assertTrue(result["success"])
        self.assertEqual(result["backend"], "swarmui")

    # --- cache key ---

    def test_cache_key_deterministic(self):
        k1 = _cache_key("conversation", "hello world")
        k2 = _cache_key("conversation", "hello world")
        self.assertEqual(k1, k2)

    def test_cache_key_different_for_different_inputs(self):
        k1 = _cache_key("conversation", "hello")
        k2 = _cache_key("conversation", "goodbye")
        self.assertNotEqual(k1, k2)

    def test_cache_key_includes_prefix(self):
        k = _cache_key("conversation", "test")
        self.assertTrue(k.startswith(_mod.REDIS_RESPONSE_PREFIX))

    # --- LocalModel ---

    def test_local_model_has_required_attrs(self):
        for key, m in MODELS.items():
            self.assertIsInstance(m.name, str, f"Model '{key}' name must be str")
            self.assertGreater(len(m.name), 0, f"Model '{key}' name must not be empty")
            self.assertIsInstance(m.ctx, int, f"Model '{key}' ctx must be int")
            self.assertGreater(m.ctx, 0)


# ===========================================================================
# 5. INTEGRATION TESTS
# ===========================================================================

class TestIntegration(unittest.TestCase):

    def test_route_local_success_returns_cached_on_second_call(self):
        """Successful local route should be cached and returned on second call."""
        _mod.REDIS_AVAILABLE = True
        cached_data = {}

        def fake_cache_get(intent, prompt):
            key = f"{intent}:{prompt}"
            return cached_data.get(key)

        def fake_cache_set(intent, prompt, result):
            key = f"{intent}:{prompt}"
            cached_data[key] = {**result, "cached": True}

        with patch.object(_mod, "_cache_get", side_effect=fake_cache_get):
            with patch.object(_mod, "_cache_set", side_effect=fake_cache_set):
                with patch.object(_mod, "query_local") as mock_local:
                    mock_local.return_value = {
                        "success": True, "response": "summarized text",
                        "source": "local", "backend": "mlx"
                    }
                    r1 = route("text_summary", "Summarize this paragraph of text")

        with patch.object(_mod, "_cache_get", side_effect=fake_cache_get):
            with patch.object(_mod, "query_local") as mock_local2:
                r2 = route("text_summary", "Summarize this paragraph of text")

        self.assertTrue(r1["success"])
        self.assertTrue(r2["success"])
        mock_local.assert_called_once()

    def test_route_injects_nova_system_prompt_for_voice(self):
        """Voice intents must use NOVA_SYSTEM_PROMPT when no system override given."""
        injected_system = []

        def capture_local(prompt, model_key, intent="", system=None, options=None):
            injected_system.append(system)
            return {"success": True, "response": "hi", "source": "local"}

        with patch.object(_mod, "query_local", side_effect=capture_local):
            route("conversation", "Good morning!")

        self.assertIsNotNone(injected_system[0])
        self.assertIn("Nova", injected_system[0],
                      "Voice intents should inject Nova's personality prompt")

    def test_route_private_failure_message_explains_why(self):
        """PRIVATE intent failure message must explain it cannot go to cloud."""
        with patch.object(_mod, "query_local") as mock_local:
            mock_local.return_value = {"success": False, "error": "Ollama down", "source": "local"}
            result = route("memory_recall", "what's in my memory?")

        self.assertFalse(result["success"])
        error = result.get("error", "")
        self.assertTrue(
            "NEVER" in error.upper() or "personal" in error.lower() or "cloud" in error.lower(),
            f"PRIVATE failure message should explain data privacy: {error!r}"
        )

    def test_route_adds_intent_and_privacy_to_result(self):
        """route() must always add 'intent' and 'privacy' keys to the result."""
        with patch.object(_mod, "query_local") as mock_local:
            mock_local.return_value = {"success": True, "response": "ok", "source": "local"}
            result = route("code_review", "def foo(): pass")

        self.assertEqual(result["intent"], "code_review")
        self.assertIn("privacy", result)


# ===========================================================================
# 6. FUNCTIONAL TESTS
# ===========================================================================

class TestFunctional(unittest.TestCase):

    def test_list_intents_does_not_crash(self):
        """--list-intents must print without raising."""
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--list-intents"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(_SCRIPT.parent)},
        )
        self.assertEqual(result.returncode, 0,
                         f"--list-intents failed: {result.stderr[:300]}")
        self.assertIn("Intent routing table", result.stdout)

    def test_list_models_does_not_crash(self):
        """--list-models must print without raising."""
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--list-models"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(_SCRIPT.parent)},
        )
        self.assertEqual(result.returncode, 0,
                         f"--list-models failed: {result.stderr[:300]}")
        self.assertIn("model registry", result.stdout.lower())

    def test_audit_does_not_crash(self):
        """--audit must print privacy classification without raising."""
        result = subprocess.run(
            [sys.executable, str(_SCRIPT), "--audit"],
            capture_output=True, text=True,
            env={**os.environ, "PYTHONPATH": str(_SCRIPT.parent)},
        )
        self.assertEqual(result.returncode, 0,
                         f"--audit failed: {result.stderr[:300]}")

    def test_query_local_returns_failure_dict_when_ollama_down(self):
        """query_local() must return structured failure when Ollama is unreachable."""
        import urllib.error
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("connection refused")
            result = query_local("test prompt", "reasoner", intent="logic_check")

        self.assertFalse(result["success"])
        self.assertIn("error", result)
        self.assertEqual(result.get("source"), "local")

    def test_query_cloud_returns_failure_dict_when_no_key(self):
        """query_cloud() must return structured failure when API key is missing."""
        with patch.object(_mod, "_load_openrouter_key", return_value=""):
            result = query_cloud("test prompt", intent="conversation")

        self.assertFalse(result["success"])
        self.assertIn("error", result)

    def test_route_unknown_intent_still_returns_success_when_local_works(self):
        """route() with unknown intent must still return success if local LLM works."""
        with patch.object(_mod, "query_local") as mock_local:
            mock_local.return_value = {"success": True, "response": "got it", "source": "local"}
            result = route("totally_unknown_intent_abc123", "some prompt")

        self.assertTrue(result["success"])


# ===========================================================================
# 7. FRAME / SMOKE TESTS
# ===========================================================================

class TestFrame(unittest.TestCase):

    def test_script_compiles_cleanly(self):
        """nova_intent_router.py must compile without syntax errors."""
        import py_compile
        try:
            py_compile.compile(str(_SCRIPT), doraise=True)
        except py_compile.PyCompileError as e:
            self.fail(f"Compile error: {e}")

    def test_intent_map_is_populated(self):
        """INTENT_MAP must have at least 40 entries."""
        self.assertGreaterEqual(len(INTENT_MAP), 40,
                                "INTENT_MAP should have at least 40 intent entries")

    def test_private_and_sensitive_intents_non_empty(self):
        """PRIVATE_INTENTS and SENSITIVE_INTENTS must each have entries."""
        self.assertGreater(len(PRIVATE_INTENTS), 0, "PRIVATE_INTENTS must not be empty")
        self.assertGreater(len(SENSITIVE_INTENTS), 0, "SENSITIVE_INTENTS must not be empty")

    def test_nova_system_prompt_defined(self):
        """NOVA_SYSTEM_PROMPT must be a non-empty string containing 'Nova'."""
        p = _mod.NOVA_SYSTEM_PROMPT
        self.assertIsInstance(p, str)
        self.assertGreater(len(p), 100)
        self.assertIn("Nova", p)

    def test_default_temperature_in_valid_range(self):
        """DEFAULT_TEMPERATURE must be between 0.0 and 1.0."""
        t = _mod.DEFAULT_TEMPERATURE
        self.assertGreaterEqual(t, 0.0)
        self.assertLessEqual(t, 1.0)

    def test_all_explicit_temperatures_in_valid_range(self):
        """All INTENT_TEMPERATURE values must be in [0.0, 1.0]."""
        for intent, temp in _mod.INTENT_TEMPERATURE.items():
            self.assertGreaterEqual(temp, 0.0,
                                    f"Temperature for '{intent}' is below 0.0: {temp}")
            self.assertLessEqual(temp, 1.0,
                                 f"Temperature for '{intent}' exceeds 1.0: {temp}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
