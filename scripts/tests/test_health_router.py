"""test_health_router.py — Tests for Nova's health monitoring, intent routing, and core utilities.
Written by Jordan Koch."""

import io
import json
import os
import statistics
import sys
import tempfile
import time
import urllib.error
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, mock_open, patch

import pytest

# Ensure scripts directory is on sys.path (conftest also does this)
SCRIPTS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# INTENT ROUTER — privacy enforcement, model routing, temperature, caching
# ---------------------------------------------------------------------------

class TestIntentRouterPrivacySets:
    """Verify the derived privacy sets match INTENT_MAP definitions exactly."""

    def test_cloud_intents_count_is_zero(self):
        """As of v4 (2026-04-28) ALL intents are local. Cloud count must be 0."""
        from nova_intent_router import CLOUD_INTENTS
        assert len(CLOUD_INTENTS) == 0, (
            f"Expected 0 cloud intents, found {len(CLOUD_INTENTS)}: {CLOUD_INTENTS}"
        )

    def test_private_intents_are_nonempty(self):
        from nova_intent_router import PRIVATE_INTENTS
        assert len(PRIVATE_INTENTS) >= 10, (
            f"Expected at least 10 private intents, found {len(PRIVATE_INTENTS)}"
        )

    def test_sensitive_intents_are_nonempty(self):
        from nova_intent_router import SENSITIVE_INTENTS
        assert len(SENSITIVE_INTENTS) >= 4

    def test_total_intent_count_at_least_67(self):
        from nova_intent_router import INTENT_MAP
        assert len(INTENT_MAP) >= 67, (
            f"Expected at least 67 intents, found {len(INTENT_MAP)}"
        )

    def test_private_intents_include_health(self):
        from nova_intent_router import PRIVATE_INTENTS
        health_private = {"health_query", "health_summary", "health_trend",
                          "health_alert", "health_ingest"}
        assert health_private.issubset(PRIVATE_INTENTS)

    def test_private_intents_include_memory(self):
        from nova_intent_router import PRIVATE_INTENTS
        memory_private = {"memory_recall", "memory_query", "memory_search",
                          "personal_memory", "memory_write", "memory_consolidation"}
        assert memory_private.issubset(PRIVATE_INTENTS)

    def test_private_intents_include_email(self):
        from nova_intent_router import PRIVATE_INTENTS
        email_private = {"email_recall", "email_memory", "email_reply",
                         "summarize_email_thread"}
        assert email_private.issubset(PRIVATE_INTENTS)

    def test_private_intents_include_face_imessage(self):
        from nova_intent_router import PRIVATE_INTENTS
        face_imsg = {"face_recognition", "face_identify",
                     "imessage_read", "imessage_compose"}
        assert face_imsg.issubset(PRIVATE_INTENTS)

    def test_sensitive_intents_set(self):
        from nova_intent_router import SENSITIVE_INTENTS
        expected = {"homekit_summary", "camera_analysis", "vision_analysis",
                    "slack_summary", "log_analysis", "relationship_tracker"}
        assert expected.issubset(SENSITIVE_INTENTS)

    def test_voice_intents_set(self):
        from nova_intent_router import VOICE_INTENTS
        expected = {"conversation", "realtime_chat", "slack_reply",
                    "slack_post", "herd_outreach"}
        assert VOICE_INTENTS == expected

    def test_all_intents_are_local_backend(self):
        """Every single intent must use Backend.LOCAL as of v4."""
        from nova_intent_router import INTENT_MAP, Backend
        for intent, (backend, _, _) in INTENT_MAP.items():
            assert backend == Backend.LOCAL, (
                f"Intent '{intent}' uses {backend}, expected LOCAL"
            )

    def test_no_cloud_privacy_level(self):
        """No intent should have privacy='cloud' in the current v4 config."""
        from nova_intent_router import INTENT_MAP
        for intent, (_, _, privacy) in INTENT_MAP.items():
            assert privacy != "cloud", (
                f"Intent '{intent}' has privacy='cloud' which violates v4 policy"
            )


class TestIntentRouterModelSelection:
    """Verify each intent maps to a valid model key."""

    def test_all_model_keys_exist_in_registry(self):
        from nova_intent_router import INTENT_MAP, MODELS
        for intent, (_, model_key, _) in INTENT_MAP.items():
            if model_key:  # image_generation has empty model key
                assert model_key in MODELS, (
                    f"Intent '{intent}' references unknown model key '{model_key}'"
                )

    def test_code_intents_use_coder_model(self):
        from nova_intent_router import INTENT_MAP
        code_intents = ["code_review", "code_generation", "debug",
                        "swift_code", "swift_review"]
        for intent in code_intents:
            _, model_key, _ = INTENT_MAP[intent]
            assert model_key == "coder", (
                f"Intent '{intent}' should use 'coder', uses '{model_key}'"
            )

    def test_reasoning_intents_use_reasoner_model(self):
        from nova_intent_router import INTENT_MAP
        reason_intents = ["architecture", "security_analysis",
                          "threat_analysis", "logic_check"]
        for intent in reason_intents:
            _, model_key, _ = INTENT_MAP[intent]
            assert model_key == "reasoner"

    def test_conversation_intents_use_conversation_model(self):
        from nova_intent_router import INTENT_MAP, VOICE_INTENTS
        for intent in VOICE_INTENTS:
            _, model_key, _ = INTENT_MAP[intent]
            assert model_key == "conversation"

    def test_health_intents_model_assignment(self):
        from nova_intent_router import INTENT_MAP
        # health_query and health_trend use reasoner (precise)
        assert INTENT_MAP["health_query"][1] == "reasoner"
        assert INTENT_MAP["health_trend"][1] == "reasoner"
        # health_summary and health_ingest use mlx_general (fast)
        assert INTENT_MAP["health_summary"][1] == "mlx_general"
        assert INTENT_MAP["health_ingest"][1] == "mlx_general"

    def test_rag_intents_use_rag_model(self):
        from nova_intent_router import INTENT_MAP
        rag_intents = ["document_query", "rag_lookup", "document_summary",
                       "research_topic", "long_document", "long_analysis"]
        for intent in rag_intents:
            _, model_key, _ = INTENT_MAP[intent]
            assert model_key == "rag"

    def test_vision_intents_use_vision_model(self):
        from nova_intent_router import INTENT_MAP
        assert INTENT_MAP["image_describe"][1] == "vision"
        assert INTENT_MAP["camera_analysis"][1] == "vision"


class TestIntentRouterTemperature:
    """Verify temperature tuning per intent."""

    def test_analytical_intents_have_low_temperature(self):
        from nova_intent_router import INTENT_TEMPERATURE
        analytical = ["code_review", "debug", "security_analysis",
                      "threat_analysis", "logic_check"]
        for intent in analytical:
            temp = INTENT_TEMPERATURE[intent]
            assert temp <= 0.40, (
                f"Analytical intent '{intent}' has temp {temp}, expected <= 0.40"
            )

    def test_creative_intents_have_high_temperature(self):
        from nova_intent_router import INTENT_TEMPERATURE
        creative = ["dream_journal", "creative_writing", "haiku_generate"]
        for intent in creative:
            temp = INTENT_TEMPERATURE[intent]
            assert temp >= 0.75, (
                f"Creative intent '{intent}' has temp {temp}, expected >= 0.75"
            )

    def test_health_intents_have_precise_temperature(self):
        from nova_intent_router import INTENT_TEMPERATURE
        health = ["health_query", "health_alert"]
        for intent in health:
            temp = INTENT_TEMPERATURE[intent]
            assert temp <= 0.30

    def test_default_temperature_is_moderate(self):
        from nova_intent_router import DEFAULT_TEMPERATURE
        assert 0.4 <= DEFAULT_TEMPERATURE <= 0.8

    def test_conversation_intents_are_warm(self):
        from nova_intent_router import INTENT_TEMPERATURE
        assert INTENT_TEMPERATURE["conversation"] >= 0.70
        assert INTENT_TEMPERATURE["slack_reply"] >= 0.65


class TestIntentRouterRouteFunction:
    """Test the route() function's privacy enforcement logic."""

    @patch("nova_intent_router.query_local")
    @patch("nova_intent_router._cache_get", return_value=None)
    @patch("nova_intent_router._cache_set")
    def test_private_intent_hard_fail_when_local_down(self, mock_set, mock_get, mock_local):
        from nova_intent_router import route
        mock_local.return_value = {"success": False, "error": "Ollama down", "source": "local"}
        result = route("memory_recall", "what did Jordan say?")
        assert result["success"] is False
        assert "NEVER" in result["error"]
        assert result["privacy"] == "private"

    @patch("nova_intent_router.query_local")
    @patch("nova_intent_router._cache_get", return_value=None)
    @patch("nova_intent_router._cache_set")
    def test_sensitive_intent_soft_fail_no_cloud(self, mock_set, mock_get, mock_local):
        from nova_intent_router import route
        mock_local.return_value = {"success": False, "error": "Ollama down", "source": "local"}
        result = route("homekit_summary", "how are the lights?")
        assert result["success"] is False
        assert "personal context" in result["error"]
        assert result["privacy"] == "sensitive"

    @patch("nova_intent_router.query_local")
    @patch("nova_intent_router._cache_get", return_value=None)
    @patch("nova_intent_router._cache_set")
    def test_normal_intent_no_cloud_fallback(self, mock_set, mock_get, mock_local):
        from nova_intent_router import route
        mock_local.return_value = {"success": False, "error": "down", "source": "local"}
        result = route("text_summary", "summarize this")
        assert result["success"] is False
        assert "Cloud fallback is disabled" in result["error"]

    @patch("nova_intent_router.query_local")
    @patch("nova_intent_router._cache_get", return_value=None)
    @patch("nova_intent_router._cache_set")
    def test_unknown_intent_routes_local(self, mock_set, mock_get, mock_local):
        from nova_intent_router import route
        mock_local.return_value = {"success": True, "response": "ok", "source": "local"}
        result = route("totally_unknown_intent", "hello")
        assert result["success"] is True
        assert result["privacy"] == "normal"
        mock_local.assert_called_once()

    @patch("nova_intent_router.query_local")
    @patch("nova_intent_router._cache_get", return_value=None)
    @patch("nova_intent_router._cache_set")
    def test_successful_route_returns_intent_and_privacy(self, mock_set, mock_get, mock_local):
        from nova_intent_router import route
        mock_local.return_value = {"success": True, "response": "analyzed", "source": "local"}
        result = route("code_review", "review this code")
        assert result["intent"] == "code_review"
        assert result["privacy"] == "normal"

    def test_image_generation_skips_llm(self):
        from nova_intent_router import route
        result = route("image_generation", "a cat in space")
        assert result["success"] is True
        assert result["backend"] == "swarmui"
        assert result["intent"] == "image_generation"

    def test_generate_image_skips_llm(self):
        from nova_intent_router import route
        result = route("generate_image", "a dog on mars")
        assert result["success"] is True
        assert result["backend"] == "swarmui"

    @patch("nova_intent_router.query_local")
    @patch("nova_intent_router._cache_get", return_value=None)
    @patch("nova_intent_router._cache_set")
    def test_nova_system_prompt_injected_for_voice(self, mock_set, mock_get, mock_local):
        from nova_intent_router import route, NOVA_SYSTEM_PROMPT
        mock_local.return_value = {"success": True, "response": "hi", "source": "local"}
        route("conversation", "good morning")
        args, kwargs = mock_local.call_args
        assert kwargs.get("system") == NOVA_SYSTEM_PROMPT or args[3] == NOVA_SYSTEM_PROMPT

    @patch("nova_intent_router.query_local")
    @patch("nova_intent_router._cache_get", return_value=None)
    @patch("nova_intent_router._cache_set")
    def test_all_health_private_intents_hard_fail(self, mock_set, mock_get, mock_local):
        """Every health intent marked private must hard-fail with NEVER in error."""
        from nova_intent_router import route, PRIVATE_INTENTS
        mock_local.return_value = {"success": False, "error": "down", "source": "local"}
        health_private = [i for i in PRIVATE_INTENTS if "health" in i]
        for intent in health_private:
            result = route(intent, "test")
            assert "NEVER" in result["error"], f"{intent} did not hard-fail"


class TestIntentRouterCaching:
    """Test Redis caching behavior."""

    def test_cache_key_deterministic(self):
        from nova_intent_router import _cache_key
        k1 = _cache_key("test", "hello")
        k2 = _cache_key("test", "hello")
        assert k1 == k2

    def test_cache_key_differs_for_different_input(self):
        from nova_intent_router import _cache_key
        k1 = _cache_key("test", "hello")
        k2 = _cache_key("test", "goodbye")
        assert k1 != k2

    def test_cache_key_prefix(self):
        from nova_intent_router import _cache_key, REDIS_RESPONSE_PREFIX
        key = _cache_key("code_review", "def foo(): pass")
        assert key.startswith(REDIS_RESPONSE_PREFIX)

    @patch("nova_intent_router.REDIS_AVAILABLE", False)
    def test_cache_get_returns_none_when_redis_unavailable(self):
        from nova_intent_router import _cache_get
        assert _cache_get("test", "hello") is None

    @patch("nova_intent_router.REDIS_AVAILABLE", False)
    def test_cache_set_noop_when_redis_unavailable(self):
        from nova_intent_router import _cache_set
        # Should not raise
        _cache_set("test", "hello", {"success": True, "response": "ok"})

    @patch("nova_intent_router.query_local")
    @patch("nova_intent_router._cache_get")
    @patch("nova_intent_router._cache_set")
    def test_cached_response_returned_for_cacheable_intent(self, mock_set, mock_get, mock_local):
        from nova_intent_router import route
        mock_get.return_value = {
            "success": True, "response": "cached!", "cached": True
        }
        result = route("code_review", "def foo(): pass")
        assert result["cached"] is True
        assert result["response"] == "cached!"
        mock_local.assert_not_called()

    @patch("nova_intent_router.query_local")
    @patch("nova_intent_router._cache_get", return_value=None)
    @patch("nova_intent_router._cache_set")
    def test_voice_intents_never_cached(self, mock_set, mock_get, mock_local):
        from nova_intent_router import route
        mock_local.return_value = {"success": True, "response": "hi", "source": "local"}
        route("conversation", "hello")
        # _cache_get should not have been called for voice intents
        # Actually it's skipped via the cacheable check, so _cache_set should not be called
        mock_set.assert_not_called()

    @patch("nova_intent_router.query_local")
    @patch("nova_intent_router._cache_get", return_value=None)
    @patch("nova_intent_router._cache_set")
    def test_private_intents_never_cached(self, mock_set, mock_get, mock_local):
        from nova_intent_router import route
        mock_local.return_value = {"success": True, "response": "data", "source": "local"}
        route("memory_recall", "what happened?")
        mock_set.assert_not_called()


class TestIntentRouterModels:
    """Test the LocalModel class and MODELS registry."""

    def test_model_registry_has_all_backends(self):
        from nova_intent_router import MODELS
        expected_keys = {"conversation", "mlx_general", "coder", "reasoner",
                         "vision", "quick", "rag"}
        assert set(MODELS.keys()) == expected_keys

    def test_conversation_model_has_large_context(self):
        from nova_intent_router import MODELS
        assert MODELS["conversation"].ctx >= 131072

    def test_mlx_model_name_prefix(self):
        from nova_intent_router import MODELS
        assert MODELS["mlx_general"].name.startswith("mlx:")

    def test_rag_model_name_prefix(self):
        from nova_intent_router import MODELS
        assert MODELS["rag"].name.startswith("openwebui:")


class TestIntentRouterQueryLocal:
    """Test query_local routing to the correct backend."""

    def test_unknown_model_key_returns_error(self):
        from nova_intent_router import query_local
        result = query_local("hello", "nonexistent_model")
        assert result["success"] is False
        assert "Unknown model key" in result["error"]

    @patch("nova_intent_router._query_mlx")
    def test_mlx_model_routes_to_mlx(self, mock_mlx):
        from nova_intent_router import query_local
        mock_mlx.return_value = {"success": True, "response": "ok"}
        query_local("hello", "mlx_general")
        mock_mlx.assert_called_once()

    @patch("nova_intent_router._query_ollama")
    def test_ollama_model_routes_to_ollama(self, mock_ollama):
        from nova_intent_router import query_local
        mock_ollama.return_value = {"success": True, "response": "ok"}
        query_local("hello", "coder")
        mock_ollama.assert_called_once()

    @patch("nova_intent_router._query_openwebui_rag")
    def test_rag_model_routes_to_openwebui(self, mock_rag):
        from nova_intent_router import query_local
        mock_rag.return_value = {"success": True, "response": "ok"}
        query_local("search docs", "rag")
        mock_rag.assert_called_once()


class TestIntentRouterCLI:
    """Test CLI flags --audit, --list-models, --list-intents."""

    def test_audit_flag_prints_privacy_sections(self, capsys):
        from nova_intent_router import INTENT_MAP, Backend
        import nova_intent_router
        with patch("sys.argv", ["prog", "--audit"]):
            nova_intent_router.main()
        output = capsys.readouterr().out
        assert "Privacy audit" in output
        assert "PRIVATE" in output
        assert "SENSITIVE" in output

    def test_list_models_prints_registry(self, capsys):
        import nova_intent_router
        with patch("sys.argv", ["prog", "--list-models"]):
            nova_intent_router.main()
        output = capsys.readouterr().out
        assert "conversation" in output
        assert "mlx_general" in output or "mlx:qwen2.5-32b" in output

    def test_list_intents_prints_table(self, capsys):
        import nova_intent_router
        with patch("sys.argv", ["prog", "--list-intents"]):
            nova_intent_router.main()
        output = capsys.readouterr().out
        assert "code_review" in output
        assert "LOCAL" in output


# ---------------------------------------------------------------------------
# MEMORY FIRST — query classification, recall, search, pipeline
# ---------------------------------------------------------------------------

class TestMemoryFirstClassify:
    """Test classify_query() for all source rule pattern groups."""

    def _classify(self, query):
        from nova_memory_first import classify_query
        return classify_query(query)

    def test_imessage_pattern(self):
        sources, labels, _ = self._classify("show me iMessage from Sam")
        assert "imessage" in sources
        assert "iMessage" in labels

    def test_slack_pattern(self):
        sources, labels, _ = self._classify("what did he say in slack?")
        assert "slack_general" in sources

    def test_security_camera_pattern(self):
        sources, labels, _ = self._classify("check the front door camera")
        assert "security" in sources

    def test_calendar_pattern(self):
        sources, labels, _ = self._classify("what meetings do I have today?")
        assert "calendar" in sources

    def test_email_pattern(self):
        sources, labels, _ = self._classify("find the email from Jason")
        assert "email_archive" in sources

    def test_health_pattern(self):
        sources, labels, _ = self._classify("what was my blood pressure?")
        assert "apple_health" in sources

    def test_music_rave_pattern(self):
        sources, labels, _ = self._classify("tell me about the raves in 2002")
        assert "music" in sources or "socal_rave" in sources

    def test_code_project_pattern(self):
        sources, labels, _ = self._classify("what's new with MLXCode?")
        assert "project_docs" in sources

    def test_people_pattern_prefers_search(self):
        sources, labels, prefer = self._classify("who is Sam?")
        assert prefer is True

    def test_corvette_pattern(self):
        sources, labels, _ = self._classify("what torque does the C6 make?")
        assert "corvette_workshop_manual" in sources

    def test_horror_pattern(self):
        sources, labels, _ = self._classify("tell me about Jason Voorhees")
        assert "horror" in sources

    def test_cooking_pattern(self):
        sources, labels, _ = self._classify("do you have a cocktail recipe?")
        assert "cooking" in sources or "cocktails" in sources

    def test_infrastructure_pattern(self):
        sources, labels, _ = self._classify("is the NAS responding?")
        assert "infrastructure" in sources

    def test_demonology_pattern(self):
        sources, labels, _ = self._classify("tell me about the Goetia")
        assert "demonology" in sources

    def test_punk_hardcore_pattern(self):
        sources, labels, _ = self._classify("tell me about Black Flag")
        assert "hardcore_punk" in sources

    def test_sre_pattern(self):
        sources, labels, _ = self._classify("what are error budgets in SRE?")
        assert "sre" in sources

    def test_appviewx_pattern(self):
        sources, labels, _ = self._classify("what's the AppViewX migration status?")
        assert "work_knowledge" in sources

    def test_default_sources_when_no_match(self):
        sources, labels, _ = self._classify("xyzzy blorp zagnuts")
        from nova_memory_first import DEFAULT_SOURCES
        assert sources == DEFAULT_SOURCES
        assert "general" in labels

    def test_multiple_rules_merge_sources(self):
        """A query matching both email and health should merge sources."""
        sources, labels, _ = self._classify("email about blood pressure reading")
        assert "email_archive" in sources or "email" in sources
        assert "apple_health" in sources or "health" in sources
        assert len(labels) >= 2

    def test_reddit_pattern(self):
        sources, labels, _ = self._classify("what was on reddit about this?")
        assert "reddit" in sources

    def test_comics_pattern(self):
        sources, labels, _ = self._classify("who would win, Hulk vs Superman?")
        assert "comic_books" in sources

    def test_history_pattern(self):
        sources, labels, _ = self._classify("tell me about ancient civilizations")
        assert "history" in sources

    def test_home_repair_pattern(self):
        sources, labels, _ = self._classify("how do I fix plumbing?")
        assert "home_repair" in sources

    def test_drag_racing_pattern(self):
        sources, labels, _ = self._classify("what is a quarter mile time?")
        assert "drag_racing" in sources

    def test_comedy_pattern(self):
        sources, labels, _ = self._classify("tell me about Dave Chappelle's stand-up")
        assert "comedy" in sources

    def test_vehicles_pattern(self):
        sources, labels, _ = self._classify("engine swap on the race car build")
        assert "vehicles" in sources

    def test_trivia_pattern(self):
        sources, labels, _ = self._classify("trivia: who invented the telephone?")
        assert "trivia" in sources

    def test_religion_pattern(self):
        sources, labels, _ = self._classify("what do you know about Christian theology?")
        assert "religion" in sources

    def test_gardening_pattern(self):
        sources, labels, _ = self._classify("when should I plant tomato seeds?")
        assert "gardening" in sources

    def test_world_knowledge_pattern(self):
        sources, labels, _ = self._classify("what country has the highest GDP?")
        assert "world_factbook" in sources

    def test_local_knowledge_pattern(self):
        sources, labels, _ = self._classify("tell me about gang activity in LA")
        assert "local_knowledge" in sources or "gang_data" in sources

    def test_lyrics_pattern(self):
        sources, labels, _ = self._classify("what are the lyrics to that song?")
        assert "music_lyrics" in sources

    def test_devo_music_pattern(self):
        sources, labels, _ = self._classify("tell me about Devo and Booji Boy")
        assert "music" in sources


class TestMemoryFirstRecall:
    """Test recall() and search() HTTP calls."""

    @patch("nova_memory_first.urllib.request.urlopen")
    def test_recall_returns_list(self, mock_urlopen):
        from nova_memory_first import recall
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            [{"text": "memory1", "source": "test"}]
        ).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response
        result = recall("test query")
        assert len(result) == 1
        assert result[0]["text"] == "memory1"

    @patch("nova_memory_first.urllib.request.urlopen")
    def test_recall_handles_dict_response(self, mock_urlopen):
        from nova_memory_first import recall
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"memories": [{"text": "m1"}]}
        ).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response
        result = recall("test")
        assert len(result) == 1

    @patch("nova_memory_first.urllib.request.urlopen", side_effect=Exception("timeout"))
    def test_recall_returns_empty_on_error(self, mock_urlopen):
        from nova_memory_first import recall
        result = recall("test")
        assert result == []

    @patch("nova_memory_first.urllib.request.urlopen")
    def test_search_returns_list(self, mock_urlopen):
        from nova_memory_first import search
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"results": [{"text": "found", "source": "test"}]}
        ).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response
        result = search("test query")
        assert len(result) == 1

    @patch("nova_memory_first.urllib.request.urlopen", side_effect=Exception("err"))
    def test_search_returns_empty_on_error(self, mock_urlopen):
        from nova_memory_first import search
        assert search("test") == []


class TestMemoryFirstBatchRecall:
    """Test batch_recall() including fallback behavior."""

    @patch("nova_memory_first.urllib.request.urlopen")
    def test_batch_recall_success(self, mock_urlopen):
        from nova_memory_first import batch_recall
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "results": [
                {"query": "q1", "memories": [{"text": "m1"}]},
                {"query": "q2", "memories": [{"text": "m2"}]},
            ]
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response
        results = batch_recall([{"q": "q1"}, {"q": "q2"}])
        assert len(results) == 2

    @patch("nova_memory_first.recall")
    @patch("nova_memory_first.urllib.request.urlopen", side_effect=Exception("err"))
    def test_batch_recall_fallback_to_individual(self, mock_urlopen, mock_recall):
        from nova_memory_first import batch_recall
        mock_recall.return_value = [{"text": "fallback"}]
        results = batch_recall([{"q": "q1"}])
        assert len(results) == 1
        assert results[0]["memories"] == [{"text": "fallback"}]
        mock_recall.assert_called_once()


class TestMemoryFirstFormatResult:
    """Test format_result() output formatting."""

    def test_format_result_basic(self):
        from nova_memory_first import format_result
        item = {"text": "Hello world", "source": "test", "score": 0.95}
        output = format_result(item, 1)
        assert "[1]" in output
        assert "(test" in output
        assert "0.95" in output
        assert "Hello world" in output

    def test_format_result_truncates_long_text(self):
        from nova_memory_first import format_result
        item = {"text": "x" * 500, "source": "test"}
        output = format_result(item, 1)
        # Text should be truncated to 400 chars
        text_part = output.split("\n", 1)[1]
        assert len(text_part) <= 400

    def test_format_result_no_score(self):
        from nova_memory_first import format_result
        item = {"text": "data", "source": "health"}
        output = format_result(item, 3)
        assert "[3]" in output
        assert "relevance" not in output

    def test_format_result_metadata_source_fallback(self):
        from nova_memory_first import format_result
        item = {"text": "data", "metadata": {"source": "email_archive"}}
        output = format_result(item, 1)
        assert "email_archive" in output


class TestMemoryFirstPipeline:
    """Test memory_lookup() full pipeline."""

    @patch("nova_memory_first.batch_recall")
    @patch("nova_memory_first.search")
    @patch("nova_memory_first.recall")
    def test_memory_lookup_returns_results(self, mock_recall, mock_search, mock_batch):
        from nova_memory_first import memory_lookup
        mock_batch.return_value = [
            {"query": "q", "memories": [{"text": "batch result", "source": "test"}]}
        ]
        mock_recall.return_value = [{"text": "broad result", "source": "general"}]
        mock_search.return_value = []
        results, sources_searched, labels = memory_lookup("hello world")
        assert len(results) >= 1

    @patch("nova_memory_first.batch_recall", return_value=[])
    @patch("nova_memory_first.search", return_value=[])
    @patch("nova_memory_first.recall", return_value=[])
    def test_memory_lookup_empty_results(self, mock_recall, mock_search, mock_batch):
        from nova_memory_first import memory_lookup
        results, sources_searched, labels = memory_lookup("xyzzy unknown thing")
        assert results == []


# ---------------------------------------------------------------------------
# NOVA CONFIG — keychain, constants, post_both
# ---------------------------------------------------------------------------

class TestNovaConfig:
    """Test nova_config.py functions and constants."""

    def test_slack_channel_constants_defined(self):
        import nova_config
        assert nova_config.SLACK_CHAN == "C0AMNQ5GX70"
        assert nova_config.SLACK_NOTIFY == "C0ATAF7NZG9"
        assert nova_config.JORDAN_DM == "D0AMPB3F4T0"

    def test_vector_url_defined(self):
        import nova_config
        assert nova_config.VECTOR_URL.startswith("http://")
        assert "18790" in nova_config.VECTOR_URL

    def test_discord_constants_defined(self):
        import nova_config
        assert nova_config.DISCORD_CHAT
        assert nova_config.DISCORD_NOTIFY

    def test_channel_map_maps_slack_to_discord(self):
        import nova_config
        assert nova_config.SLACK_CHAN in nova_config.CHANNEL_MAP
        assert nova_config.CHANNEL_MAP[nova_config.SLACK_CHAN] == nova_config.DISCORD_CHAT

    @patch("nova_config.subprocess.run")
    def test_keychain_returns_value_on_success(self, mock_run):
        import nova_config
        mock_run.return_value = MagicMock(returncode=0, stdout="test-secret\n")
        result = nova_config._keychain("test-service", "nova", required=False)
        assert result == "test-secret"

    @patch("nova_config.subprocess.run")
    def test_keychain_returns_empty_on_failure_non_required(self, mock_run):
        import nova_config
        mock_run.return_value = MagicMock(returncode=44, stdout="")
        result = nova_config._keychain("missing-service", "nova", required=False)
        assert result == ""

    @patch("nova_config.subprocess.run")
    def test_keychain_exits_on_failure_when_required(self, mock_run):
        import nova_config
        mock_run.return_value = MagicMock(returncode=44, stdout="")
        with pytest.raises(SystemExit):
            nova_config._keychain("missing-service", "nova", required=True)

    @patch("nova_config._keychain", return_value="xoxb-test")
    def test_slack_bot_token_from_keychain(self, mock_kc):
        import nova_config
        token = nova_config.slack_bot_token()
        assert token == "xoxb-test"

    @patch("nova_config._keychain", return_value="")
    def test_slack_bot_token_falls_back_to_env(self, mock_kc, monkeypatch):
        import nova_config
        monkeypatch.setenv("NOVA_SLACK_BOT_TOKEN", "xoxb-env-token")
        token = nova_config.slack_bot_token()
        assert token == "xoxb-env-token"

    @patch("nova_config._keychain", return_value="")
    def test_slack_bot_token_rejects_placeholder(self, mock_kc, monkeypatch):
        import nova_config
        monkeypatch.setenv("NOVA_SLACK_BOT_TOKEN", "${NOVA_SLACK_BOT_TOKEN}")
        token = nova_config.slack_bot_token()
        assert token == ""

    @patch("nova_config.post_discord")
    @patch("nova_config.slack_bot_token", return_value="xoxb-test")
    def test_post_both_calls_slack_and_discord(self, mock_token, mock_discord):
        import nova_config
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"ok": True}).encode()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
            nova_config.post_both("hello", slack_channel=nova_config.SLACK_CHAN)
            mock_urlopen.assert_called_once()
        mock_discord.assert_called_once()

    @patch("nova_config.post_discord")
    @patch("nova_config.slack_bot_token", return_value="")
    def test_post_both_skips_slack_without_token(self, mock_token, mock_discord):
        import nova_config
        nova_config.post_both("hello")
        # Discord should still be called
        mock_discord.assert_called_once()


# ---------------------------------------------------------------------------
# NOVA LOGGER — JSON-lines, rotation, level filtering
# ---------------------------------------------------------------------------

class TestNovaLogger:
    """Test nova_logger.py structured logging."""

    def test_log_writes_jsonl_format(self, tmp_path):
        import nova_logger
        orig_file = nova_logger.LOG_FILE
        orig_dir = nova_logger.LOG_DIR
        nova_logger.LOG_DIR = tmp_path
        nova_logger.LOG_FILE = tmp_path / "nova.jsonl"
        try:
            nova_logger.log("test message", level=nova_logger.LOG_INFO, source="test_suite")
            content = nova_logger.LOG_FILE.read_text()
            entry = json.loads(content.strip())
            assert entry["msg"] == "test message"
            assert entry["level"] == "info"
            assert entry["source"] == "test_suite"
            assert "ts" in entry
        finally:
            nova_logger.LOG_FILE = orig_file
            nova_logger.LOG_DIR = orig_dir

    def test_log_extra_dict_serialized(self, tmp_path):
        import nova_logger
        orig_file = nova_logger.LOG_FILE
        orig_dir = nova_logger.LOG_DIR
        nova_logger.LOG_DIR = tmp_path
        nova_logger.LOG_FILE = tmp_path / "nova.jsonl"
        try:
            nova_logger.log("with extra", source="test",
                            extra={"host": "localhost", "port": 5432})
            content = nova_logger.LOG_FILE.read_text()
            entry = json.loads(content.strip())
            assert entry["extra"]["host"] == "localhost"
            assert entry["extra"]["port"] == 5432
        finally:
            nova_logger.LOG_FILE = orig_file
            nova_logger.LOG_DIR = orig_dir

    def test_level_filtering_skips_debug_by_default(self, tmp_path):
        import nova_logger
        orig_file = nova_logger.LOG_FILE
        orig_dir = nova_logger.LOG_DIR
        orig_min = nova_logger.MIN_LEVEL
        nova_logger.LOG_DIR = tmp_path
        nova_logger.LOG_FILE = tmp_path / "nova.jsonl"
        nova_logger.MIN_LEVEL = nova_logger.LOG_INFO
        try:
            nova_logger.log("debug msg", level=nova_logger.LOG_DEBUG, source="test")
            if nova_logger.LOG_FILE.exists():
                content = nova_logger.LOG_FILE.read_text()
                assert content.strip() == ""
            # Debug messages should be filtered out
        finally:
            nova_logger.LOG_FILE = orig_file
            nova_logger.LOG_DIR = orig_dir
            nova_logger.MIN_LEVEL = orig_min

    def test_log_rotation_shifts_files(self, tmp_path):
        import nova_logger
        orig_file = nova_logger.LOG_FILE
        orig_dir = nova_logger.LOG_DIR
        orig_max = nova_logger.MAX_SIZE_BYTES
        nova_logger.LOG_DIR = tmp_path
        nova_logger.LOG_FILE = tmp_path / "nova.jsonl"
        nova_logger.MAX_SIZE_BYTES = 100  # tiny size to trigger rotation
        try:
            # Write enough to trigger rotation
            for i in range(20):
                nova_logger.log(f"message {i}" * 10, source="test")
            # Check that rotated file exists
            assert (tmp_path / "nova.jsonl.1").exists()
        finally:
            nova_logger.LOG_FILE = orig_file
            nova_logger.LOG_DIR = orig_dir
            nova_logger.MAX_SIZE_BYTES = orig_max

    def test_read_logs_returns_entries(self, tmp_path):
        import nova_logger
        orig_file = nova_logger.LOG_FILE
        orig_dir = nova_logger.LOG_DIR
        nova_logger.LOG_DIR = tmp_path
        nova_logger.LOG_FILE = tmp_path / "nova.jsonl"
        try:
            nova_logger.log("entry one", source="test", level=nova_logger.LOG_INFO)
            nova_logger.log("entry two", source="test", level=nova_logger.LOG_ERROR)
            entries = nova_logger.read_logs(n=10)
            assert len(entries) == 2
            # Newest first
            assert entries[0]["msg"] == "entry two"
        finally:
            nova_logger.LOG_FILE = orig_file
            nova_logger.LOG_DIR = orig_dir

    def test_read_logs_filters_by_level(self, tmp_path):
        import nova_logger
        orig_file = nova_logger.LOG_FILE
        orig_dir = nova_logger.LOG_DIR
        nova_logger.LOG_DIR = tmp_path
        nova_logger.LOG_FILE = tmp_path / "nova.jsonl"
        try:
            nova_logger.log("info msg", source="t", level=nova_logger.LOG_INFO)
            nova_logger.log("error msg", source="t", level=nova_logger.LOG_ERROR)
            entries = nova_logger.read_logs(n=10, level=nova_logger.LOG_ERROR)
            assert len(entries) == 1
            assert entries[0]["level"] == "error"
        finally:
            nova_logger.LOG_FILE = orig_file
            nova_logger.LOG_DIR = orig_dir

    def test_read_logs_filters_by_source(self, tmp_path):
        import nova_logger
        orig_file = nova_logger.LOG_FILE
        orig_dir = nova_logger.LOG_DIR
        nova_logger.LOG_DIR = tmp_path
        nova_logger.LOG_FILE = tmp_path / "nova.jsonl"
        try:
            nova_logger.log("a", source="alpha", level=nova_logger.LOG_INFO)
            nova_logger.log("b", source="beta", level=nova_logger.LOG_INFO)
            entries = nova_logger.read_logs(n=10, source="alpha")
            assert len(entries) == 1
            assert entries[0]["source"] == "alpha"
        finally:
            nova_logger.LOG_FILE = orig_file
            nova_logger.LOG_DIR = orig_dir


# ---------------------------------------------------------------------------
# HEALTH CHECK — job auditing, message formatting, slack delivery audit
# ---------------------------------------------------------------------------

class TestHealthCheck:
    """Test nova_health_check.py audit logic and message formatting."""

    def test_format_message_no_issues(self):
        from nova_health_check import format_message
        msg = format_message([])
        assert "All cron jobs running normally" in msg

    def test_format_message_with_errors(self):
        from nova_health_check import format_message
        issues = [
            {"severity": "error", "name": "broken_job", "reason": "5 failures"},
        ]
        msg = format_message(issues)
        assert "error" in msg.lower() or "broken_job" in msg

    def test_format_message_with_warnings(self):
        from nova_health_check import format_message
        issues = [
            {"severity": "warning", "name": "slow_job", "reason": "stale"},
        ]
        msg = format_message(issues)
        assert "warning" in msg.lower() or "slow_job" in msg

    def test_format_message_mixed_issues(self):
        from nova_health_check import format_message
        issues = [
            {"severity": "error", "name": "crash", "reason": "segfault"},
            {"severity": "warning", "name": "slow", "reason": "took 2h"},
            {"severity": "critical", "name": "db_down", "reason": "pg gone"},
        ]
        msg = format_message(issues)
        assert "crash" in msg
        assert "slow" in msg
        assert "db_down" in msg

    @patch("nova_health_check.urllib.request.urlopen")
    def test_audit_jobs_from_scheduler_api(self, mock_urlopen):
        from nova_health_check import audit_jobs
        now = datetime.now().timestamp()
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "healthy_task": {
                "enabled": True, "consecutive_failures": 0,
                "last_run": now - 3600, "last_duration": 5.0,
                "last_exit_code": 0, "schedule": "cron: */4 * * * *"
            },
            "broken_task": {
                "enabled": True, "consecutive_failures": 5,
                "last_run": now - 7200, "last_duration": 0.5,
                "last_exit_code": 1, "schedule": "cron: 0 8 * * *"
            },
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response
        issues = audit_jobs()
        error_names = [i["name"] for i in issues if i["severity"] == "error"]
        assert "broken_task" in error_names

    @patch("nova_health_check.urllib.request.urlopen")
    def test_audit_jobs_detects_fast_runs(self, mock_urlopen):
        from nova_health_check import audit_jobs
        now = datetime.now().timestamp()
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "empty_promise_task": {
                "enabled": True, "consecutive_failures": 0,
                "last_run": now - 3600, "last_duration": 0.05,
                "last_exit_code": 0, "schedule": "cron: */4 * * * *"
            },
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response
        issues = audit_jobs()
        assert any("empty promise" in i["reason"] for i in issues)


# ---------------------------------------------------------------------------
# HEALTH MONITOR — data reading, summarization, alerting
# ---------------------------------------------------------------------------

class TestHealthMonitor:
    """Test nova_health_monitor.py metric parsing, thresholds, and summaries."""

    def test_summarize_readings_basic(self):
        from nova_health_monitor import summarize_readings
        readings = {
            "heart_rate": [
                {"value": 72, "unit": "bpm"},
                {"value": 68, "unit": "bpm"},
                {"value": 75, "unit": "bpm"},
            ]
        }
        summaries = summarize_readings(readings)
        assert len(summaries) == 1
        assert "Heart Rate" in summaries[0]
        assert "75" in summaries[0]  # latest

    def test_summarize_readings_sleep(self):
        from nova_health_monitor import summarize_readings
        readings = {
            "sleep": [
                {"stage": "deep", "duration_min": 90},
                {"stage": "rem", "duration_min": 60},
                {"stage": "core", "duration_min": 180},
                {"stage": "awake", "duration_min": 30},
            ]
        }
        summaries = summarize_readings(readings)
        assert len(summaries) == 1
        assert "Sleep" in summaries[0]
        # Total should be 330 min = 5.5 hours (excluding awake)
        assert "5.5" in summaries[0]

    def test_summarize_readings_single_value(self):
        from nova_health_monitor import summarize_readings
        readings = {
            "blood_oxygen": [{"value": 97, "unit": "%"}]
        }
        summaries = summarize_readings(readings)
        assert "97" in summaries[0]
        assert "avg" not in summaries[0]  # single value, no average

    def test_check_alerts_high_bp(self):
        from nova_health_monitor import check_alerts
        readings = {
            "blood_pressure_sys": [{"value": 145, "unit": "mmHg"}]
        }
        alerts = check_alerts(readings)
        assert len(alerts) == 1
        assert "HIGH" in alerts[0]

    def test_check_alerts_low_oxygen(self):
        from nova_health_monitor import check_alerts
        readings = {
            "blood_oxygen": [{"value": 90, "unit": "%"}]
        }
        alerts = check_alerts(readings)
        assert len(alerts) == 1
        assert "LOW" in alerts[0]

    def test_check_alerts_normal_values(self):
        from nova_health_monitor import check_alerts
        readings = {
            "heart_rate": [{"value": 72, "unit": "bpm"}],
            "blood_oxygen": [{"value": 97, "unit": "%"}],
        }
        alerts = check_alerts(readings)
        assert len(alerts) == 0

    def test_check_alerts_high_heart_rate(self):
        from nova_health_monitor import check_alerts
        readings = {
            "heart_rate": [{"value": 125, "unit": "bpm"}]
        }
        alerts = check_alerts(readings)
        assert any("HIGH" in a for a in alerts)

    def test_check_alerts_low_heart_rate(self):
        from nova_health_monitor import check_alerts
        readings = {
            "heart_rate": [{"value": 45, "unit": "bpm"}]
        }
        alerts = check_alerts(readings)
        assert any("LOW" in a for a in alerts)

    def test_alert_thresholds_defined(self):
        from nova_health_monitor import ALERT_THRESHOLDS
        expected_keys = {"blood_pressure_sys", "blood_pressure_dia", "heart_rate",
                         "blood_oxygen", "blood_glucose", "resting_heart_rate",
                         "body_temperature"}
        assert expected_keys == set(ALERT_THRESHOLDS.keys())

    def test_state_load_default(self, tmp_path):
        from nova_health_monitor import load_state
        # Patch STATE_FILE to nonexistent
        with patch("nova_health_monitor.STATE_FILE", tmp_path / "missing.json"):
            state = load_state()
        assert state == {"last_ingest": "", "last_alert_date": ""}

    def test_state_save_and_load(self, tmp_path):
        import nova_health_monitor
        state_file = tmp_path / "state.json"
        with patch("nova_health_monitor.STATE_FILE", state_file):
            nova_health_monitor.save_state({"last_ingest": "2026-05-01", "last_alert_date": "2026-05-01"})
            state = nova_health_monitor.load_state()
        assert state["last_ingest"] == "2026-05-01"


# ---------------------------------------------------------------------------
# HEALTH INTELLIGENCE — trend detection, cross-referencing
# ---------------------------------------------------------------------------

class TestHealthIntelligence:
    """Test nova_health_intelligence.py trend detection and analysis."""

    def test_daily_averages_computation(self):
        from nova_health_intelligence import daily_averages
        daily_data = {
            "2026-04-28": {"heart_rate": [70, 72, 74]},
            "2026-04-29": {"heart_rate": [80, 82]},
        }
        avgs = daily_averages(daily_data, "heart_rate")
        assert abs(avgs["2026-04-28"] - 72.0) < 0.01
        assert abs(avgs["2026-04-29"] - 81.0) < 0.01

    def test_daily_averages_missing_type(self):
        from nova_health_intelligence import daily_averages
        daily_data = {"2026-04-28": {"steps": [5000]}}
        avgs = daily_averages(daily_data, "heart_rate")
        assert avgs == {}

    def test_detect_trends_rising_heart_rate(self):
        from nova_health_intelligence import detect_trends
        # Create 5 days of steadily rising resting HR
        daily_data = {}
        base_date = date(2026, 4, 24)
        for i in range(5):
            d = (base_date + timedelta(days=i)).isoformat()
            daily_data[d] = {"resting_heart_rate": [60 + i * 3]}  # 60, 63, 66, 69, 72 -- change=12 over threshold of 8
        alerts = detect_trends(daily_data)
        rising_alerts = [a for a in alerts if a["type"] == "resting_heart_rate" and a["pattern"] == "rising"]
        assert len(rising_alerts) >= 1

    def test_detect_trends_stable_values(self):
        from nova_health_intelligence import detect_trends
        daily_data = {}
        base_date = date(2026, 4, 24)
        for i in range(5):
            d = (base_date + timedelta(days=i)).isoformat()
            daily_data[d] = {"resting_heart_rate": [70]}  # stable
        alerts = detect_trends(daily_data)
        hr_alerts = [a for a in alerts if a["type"] == "resting_heart_rate"]
        rising_or_falling = [a for a in hr_alerts if a["pattern"] in ("rising", "falling")]
        assert len(rising_or_falling) == 0

    def test_detect_trends_sustained_high(self):
        from nova_health_intelligence import detect_trends
        daily_data = {}
        base_date = date(2026, 4, 24)
        for i in range(5):
            d = (base_date + timedelta(days=i)).isoformat()
            daily_data[d] = {"resting_heart_rate": [90]}  # above 85 threshold
        alerts = detect_trends(daily_data)
        sustained = [a for a in alerts if a["pattern"] == "sustained_high"]
        assert len(sustained) >= 1

    def test_detect_trends_falling_hrv(self):
        from nova_health_intelligence import detect_trends
        daily_data = {}
        base_date = date(2026, 4, 24)
        for i in range(5):
            d = (base_date + timedelta(days=i)).isoformat()
            daily_data[d] = {"hrv": [50 - i * 4]}  # 50, 46, 42, 38, 34 -- dropping
        alerts = detect_trends(daily_data)
        falling = [a for a in alerts if a["type"] == "hrv" and a["pattern"] == "falling"]
        assert len(falling) >= 1

    def test_detect_trends_insufficient_data(self):
        from nova_health_intelligence import detect_trends
        daily_data = {"2026-04-28": {"resting_heart_rate": [70]}}
        alerts = detect_trends(daily_data)
        assert len(alerts) == 0

    def test_trend_alerts_config_completeness(self):
        from nova_health_intelligence import TREND_ALERTS
        expected = {"resting_heart_rate", "blood_pressure_sys", "blood_pressure_dia",
                    "heart_rate", "hrv", "blood_oxygen", "weight"}
        assert set(TREND_ALERTS.keys()) == expected
        for key, cfg in TREND_ALERTS.items():
            assert "window_days" in cfg
            assert "label" in cfg
            assert "unit" in cfg
            assert "advice" in cfg


# ---------------------------------------------------------------------------
# HEALTH CORRELATION — cross-reference health with activity
# ---------------------------------------------------------------------------

class TestHealthCorrelation:
    """Test nova_health_correlation.py correlation functions."""

    def test_safe_avg_empty(self):
        from nova_health_correlation import _safe_avg
        assert _safe_avg([]) == 0.0

    def test_safe_avg_values(self):
        from nova_health_correlation import _safe_avg
        assert _safe_avg([10, 20, 30]) == 20.0

    def test_classify_day_weekday(self):
        from nova_health_correlation import _classify_day
        # 2026-05-01 is a Friday (weekday)
        assert _classify_day("2026-05-01") == "weekday"

    def test_classify_day_weekend(self):
        from nova_health_correlation import _classify_day
        # 2026-05-02 is a Saturday
        assert _classify_day("2026-05-02") == "weekend"

    def test_correlate_sleep_vs_meetings_significant_diff(self):
        from nova_health_correlation import correlate_sleep_vs_meetings
        health = {
            "2026-04-28": {"sleep_hours": 5.0},   # meeting-heavy day
            "2026-04-29": {"sleep_hours": 5.5},   # meeting-heavy day
            "2026-04-30": {"sleep_hours": 8.0},   # light day
            "2026-05-01": {"sleep_hours": 7.5},   # light day
        }
        calendar = {
            "2026-04-28": 5, "2026-04-29": 4,     # heavy
            "2026-04-30": 1, "2026-05-01": 1,     # light
        }
        result = correlate_sleep_vs_meetings(health, calendar)
        assert result is not None
        assert "Sleep" in result["title"]
        assert abs(result["diff_hours"]) > 0.3

    def test_correlate_sleep_vs_meetings_no_calendar(self):
        from nova_health_correlation import correlate_sleep_vs_meetings
        health = {"2026-04-28": {"sleep_hours": 7.0}}
        result = correlate_sleep_vs_meetings(health, {})
        assert result is None

    def test_correlate_hr_vs_meetings(self):
        from nova_health_correlation import correlate_hr_vs_meetings
        health = {
            "2026-04-28": {"resting_heart_rate": 75},
            "2026-04-29": {"resting_heart_rate": 78},
            "2026-04-30": {"resting_heart_rate": 65},
            "2026-05-01": {"resting_heart_rate": 63},
        }
        calendar = {"2026-04-28": 3, "2026-04-29": 4}  # only these days have meetings
        result = correlate_hr_vs_meetings(health, calendar)
        assert result is not None
        assert result["diff_bpm"] > 2.0

    def test_correlate_hrv_weekday_weekend(self):
        from nova_health_correlation import correlate_hrv_weekday_weekend
        health = {
            "2026-04-27": {"hrv": 45},   # Sunday (weekend)
            "2026-04-28": {"hrv": 30},   # Monday (weekday)
            "2026-05-02": {"hrv": 50},   # Saturday (weekend)
            "2026-05-01": {"hrv": 32},   # Friday (weekday)
        }
        result = correlate_hrv_weekday_weekend(health)
        assert result is not None
        assert "Weekend" in result["title"]
        assert result["diff_ms"] > 2.0

    def test_correlate_steps_vs_coding(self):
        from nova_health_correlation import correlate_steps_vs_coding
        health = {
            "2026-04-28": {"steps": 3000},
            "2026-04-29": {"steps": 2500},
            "2026-04-30": {"steps": 8000},
            "2026-05-01": {"steps": 9000},
        }
        coding = {"2026-04-28": 5, "2026-04-29": 3}  # only these are coding days
        result = correlate_steps_vs_coding(health, coding)
        assert result is not None
        assert "Steps" in result["title"]
        assert result["diff_steps"] < -500  # fewer steps on coding days

    def test_correlate_steps_vs_coding_no_diff(self):
        from nova_health_correlation import correlate_steps_vs_coding
        health = {
            "2026-04-28": {"steps": 5000},
            "2026-04-29": {"steps": 5100},
        }
        coding = {"2026-04-28": 1}
        result = correlate_steps_vs_coding(health, coding)
        # Diff is less than 500 so should be None
        assert result is None

    def test_compute_summaries(self):
        from nova_health_correlation import compute_summaries
        health = {
            "2026-04-28": {"sleep_hours": 7.0, "steps": 6000},
            "2026-04-29": {"sleep_hours": 6.5, "steps": 8000},
            "2026-04-30": {"sleep_hours": 7.5, "steps": 5000},
            "2026-05-01": {"sleep_hours": 8.0, "steps": 7000},
        }
        summaries = compute_summaries(health)
        assert len(summaries) == 2  # sleep_hours and steps
        text = " ".join(summaries)
        assert "Sleep Hours" in text
        assert "Steps" in text

    def test_load_health_data_missing_dir(self, tmp_path):
        from nova_health_correlation import load_health_data
        with patch("nova_health_correlation.HEALTH_DIR", tmp_path / "nonexistent"):
            result = load_health_data(7)
        assert result == {}

    def test_load_health_data_reads_json_files(self, tmp_path):
        from nova_health_correlation import load_health_data
        # Create a test health file
        today = date.today().isoformat()
        health_file = tmp_path / f"{today}.json"
        health_file.write_text(json.dumps({
            "sleep_hours": 7.5,
            "resting_heart_rate": 62,
            "steps": 8000,
        }))
        with patch("nova_health_correlation.HEALTH_DIR", tmp_path):
            result = load_health_data(7)
        assert today in result
        assert result[today]["sleep_hours"] == 7.5


# ---------------------------------------------------------------------------
# HEALTHKIT RECEIVER — HTTP handler logic
# ---------------------------------------------------------------------------

class TestHealthKitReceiver:
    """Test nova_healthkit_receiver.py handler logic."""

    def test_handler_rejects_non_health_path(self):
        """HealthHandler.do_POST sends 404 for non-/health paths."""
        from nova_healthkit_receiver import HealthHandler
        handler = MagicMock(spec=HealthHandler)
        handler.path = "/wrong"
        HealthHandler.do_POST(handler)
        handler.send_error.assert_called_with(404)

    def test_handler_rejects_invalid_json(self):
        from nova_healthkit_receiver import HealthHandler
        handler = MagicMock(spec=HealthHandler)
        handler.path = "/health"
        handler.headers = {"Content-Length": "11"}
        handler.rfile = io.BytesIO(b"not json!!!")
        HealthHandler.do_POST(handler)
        handler.send_error.assert_called()

    def test_handler_get_returns_no_data(self, tmp_path):
        from nova_healthkit_receiver import HealthHandler
        handler = MagicMock(spec=HealthHandler)
        handler.path = "/health"
        handler.wfile = io.BytesIO()
        with patch("nova_healthkit_receiver.HEALTH_DIR", tmp_path):
            HealthHandler.do_GET(handler)
        handler.send_response.assert_called_with(200)


# ---------------------------------------------------------------------------
# HEALTHKIT EXPORT — path and permission setup
# ---------------------------------------------------------------------------

class TestHealthKitExport:
    """Test nova_healthkit_export.py configuration."""

    def test_health_dir_path(self):
        from nova_healthkit_export import HEALTH_DIR
        assert ".openclaw/private/health" in str(HEALTH_DIR)

    def test_output_path(self):
        from nova_healthkit_export import OUTPUT_PATH
        assert OUTPUT_PATH.name == "latest.json"


# ---------------------------------------------------------------------------
# INTEGRATION TESTS (require live services)
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestIntentRouterIntegration:
    """Integration tests that require Ollama/MLX to be running."""

    def test_ollama_health_check(self):
        """Verify Ollama is reachable."""
        import urllib.request
        try:
            with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=5) as r:
                data = json.loads(r.read())
                assert "models" in data
        except Exception:
            pytest.skip("Ollama not running")

    def test_route_code_review_live(self):
        from nova_intent_router import route
        try:
            result = route("code_review", "def hello(): print('hi')")
        except Exception:
            pytest.skip("Local LLM not available")
        if not result["success"]:
            pytest.skip(f"Local LLM unavailable: {result.get('error')}")
        assert result["response"]
        assert result["backend"] in ("ollama", "mlx")

    def test_route_health_query_live(self):
        from nova_intent_router import route
        try:
            result = route("health_query", "What is a normal resting heart rate?")
        except Exception:
            pytest.skip("Local LLM not available")
        if not result["success"]:
            pytest.skip("Local LLM unavailable")
        assert result["privacy"] == "private"
        assert result["source"] == "local"


@pytest.mark.integration
class TestMemoryFirstIntegration:
    """Integration tests that require the memory server (port 18790)."""

    def test_vector_server_reachable(self):
        import urllib.request
        try:
            urllib.request.urlopen("http://127.0.0.1:18790/health", timeout=3)
        except Exception:
            pytest.skip("Vector memory server not running")

    def test_recall_real(self):
        from nova_memory_first import recall
        try:
            results = recall("test query", n=1)
        except Exception:
            pytest.skip("Memory server not available")
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# FUNCTIONAL TESTS (end-to-end workflows)
# ---------------------------------------------------------------------------

@pytest.mark.functional
class TestHealthMonitoringWorkflow:
    """End-to-end health monitoring workflow tests."""

    @patch("nova_health_monitor.slack_dm")
    @patch("nova_health_monitor.vector_remember")
    @patch("nova_health_monitor.vector_remember_async")
    @patch("nova_health_monitor.read_health_data")
    def test_ingest_workflow_stores_and_alerts(self, mock_read, mock_async, mock_remember, mock_dm):
        from nova_health_monitor import ingest
        mock_read.return_value = {
            "period_hours": 24,
            "start": "2026-05-01",
            "end": "2026-05-02",
            "readings": {
                "heart_rate": [
                    {"value": 72, "unit": "bpm"},
                    {"value": 130, "unit": "bpm"},  # HIGH alert
                ],
                "blood_oxygen": [{"value": 97, "unit": "%"}],
            }
        }
        with patch("nova_health_monitor.load_state", return_value={"last_ingest": "", "last_alert_date": ""}):
            with patch("nova_health_monitor.save_state"):
                ingest(hours=24)
        # Should store in vector memory
        mock_remember.assert_called_once()
        # Should alert on high heart rate
        mock_dm.assert_called_once()
        alert_text = mock_dm.call_args[0][0]
        assert "HIGH" in alert_text


@pytest.mark.functional
class TestIntentRouterWorkflow:
    """End-to-end intent routing workflow tests."""

    @patch("nova_intent_router.query_local")
    @patch("nova_intent_router._cache_get", return_value=None)
    @patch("nova_intent_router._cache_set")
    def test_full_route_lifecycle(self, mock_set, mock_get, mock_local):
        from nova_intent_router import route
        mock_local.return_value = {
            "success": True,
            "response": "Your code looks good.",
            "backend": "ollama",
            "model": "qwen3-coder:30b",
            "source": "local",
            "tokens": 42,
        }
        result = route("code_review", "def add(a, b): return a + b")
        assert result["success"] is True
        assert result["intent"] == "code_review"
        assert result["privacy"] == "normal"
        # Should attempt to cache since it's cacheable
        mock_set.assert_called_once()


# ---------------------------------------------------------------------------
# FRAME / OUTPUT FORMATTING TESTS
# ---------------------------------------------------------------------------

@pytest.mark.frame
class TestOutputFormatting:
    """Verify output formatting for CLI and Slack delivery."""

    def test_health_check_message_includes_date(self):
        from nova_health_check import format_message
        msg = format_message([])
        # Should include the day name
        today_name = datetime.now().strftime("%A")
        assert today_name in msg

    def test_health_check_error_count_label(self):
        from nova_health_check import format_message
        issues = [{"severity": "error", "name": "j1", "reason": "fail"}]
        msg = format_message(issues)
        assert "1 error" in msg
        # Plural check
        issues2 = [
            {"severity": "error", "name": "j1", "reason": "fail"},
            {"severity": "error", "name": "j2", "reason": "fail"},
        ]
        msg2 = format_message(issues2)
        assert "2 errors" in msg2

    def test_intent_router_audit_shows_zero_cloud(self, capsys):
        import nova_intent_router
        with patch("sys.argv", ["prog", "--audit"]):
            nova_intent_router.main()
        output = capsys.readouterr().out
        # Should show "CLOUD (0 intents)"
        assert "CLOUD (0 intents)" in output

    def test_memory_first_format_result_indexing(self):
        from nova_memory_first import format_result
        for i in range(1, 5):
            output = format_result({"text": f"item{i}", "source": "test"}, i)
            assert f"[{i}]" in output

    def test_health_monitor_summary_format(self):
        from nova_health_monitor import summarize_readings
        readings = {
            "blood_pressure_sys": [
                {"value": 120, "unit": "mmHg"},
                {"value": 125, "unit": "mmHg"},
                {"value": 118, "unit": "mmHg"},
            ]
        }
        summaries = summarize_readings(readings)
        assert len(summaries) == 1
        summary = summaries[0]
        assert "Blood Pressure Sys" in summary
        assert "118" in summary  # latest
        assert "120-125" in summary or "118-125" in summary  # range
