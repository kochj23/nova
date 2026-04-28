"""
tinychat.py — TinyChat backend (port 8000).

TinyChat is a lightweight OpenAI-compatible proxy to local Ollama models,
running as a Docker container.

Specialized model: gpt-oss:20b
Primary tasks: quick responses, lightweight chat, low-latency queries

To enable gpt-oss:20b in TinyChat's Docker:
  docker exec -it tinychat ollama pull gpt-oss:20b

Until then, the backend falls back to qwen3:8b (already available in Docker).
The gateway also falls back to Ollama with gpt-oss:20b if TinyChat can't serve it.

API:
  POST /api/chat/stream  — SSE stream, {"messages": [...], "model"?: "..."}
  GET  /api/config       — available_models, default_model
  GET  /api/health       — liveness check

Author: Jordan Koch
"""

import time
import json
import logging
from typing import Optional, Any
from .base import BaseBackend

logger = logging.getLogger(__name__)

# Preference order for TinyChat models — use best available
_MODEL_PREFERENCE = ["gpt-oss:20b", "qwen3:30b", "qwen3:8b", "qwen3-vl:4b", "mistral:latest"]


class TinyChatBackend(BaseBackend):
    name = "tinychat"

    def __init__(self, url: str = "http://192.168.1.6:8000", default_model: str = "gpt-oss:20b"):
        super().__init__(url, timeout=60.0)
        self.default_model = default_model
        self._available_models: list[str] = []
        self._config_fetched = False

    async def _resolve_model(self, requested: Optional[str]) -> str:
        """Pick the best available model from TinyChat's Docker Ollama."""
        if not self._config_fetched:
            try:
                data = await self._get("/api/config")
                self._available_models = data.get("available_models", [])
                self._config_fetched = True
            except Exception:
                pass

        if requested and requested in self._available_models:
            return requested

        # Walk preference list — use first model TinyChat actually has
        for model in _MODEL_PREFERENCE:
            if model in self._available_models:
                if model != self.default_model:
                    logger.info(
                        f"TinyChat: {self.default_model} not in Docker — using {model}. "
                        f"Run: docker exec -it tinychat ollama pull {self.default_model}"
                    )
                return model

        return self._available_models[0] if self._available_models else self.default_model

    async def query(self, prompt: str, model: Optional[str] = None, **kwargs) -> dict[str, Any]:
        target_model = await self._resolve_model(model)

        messages = []
        if "system" in kwargs:
            messages.append({"role": "system", "content": kwargs["system"]})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {"messages": messages, "model": target_model}
        if "temperature" in kwargs:
            payload["temperature"] = kwargs["temperature"]

        start = time.monotonic()
        try:
            r = await self._client.post(
                f"{self.url}/api/chat/stream",
                json=payload,
                timeout=60.0
            )
            r.raise_for_status()
            elapsed = (time.monotonic() - start) * 1000

            response_text = ""
            error_msg = None
            for line in r.text.splitlines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    chunk = json.loads(raw)
                    if "error" in chunk:
                        err = chunk["error"]
                        if isinstance(err, str):
                            try:
                                err = json.loads(err)
                            except Exception:
                                pass
                        error_msg = err.get("error", {}).get("message", str(err)) if isinstance(err, dict) else str(err)
                        break
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content", "") or chunk.get("content", "")
                    response_text += content
                except json.JSONDecodeError:
                    pass

            if error_msg:
                logger.error(f"TinyChat model error ({target_model}): {error_msg}")
                raise RuntimeError(f"TinyChat: {error_msg}")

            return {
                "response": response_text.strip(),
                "model_used": target_model,
                "latency_ms": elapsed,
            }
        except RuntimeError:
            raise
        except Exception as e:
            logger.error(f"TinyChat query failed ({target_model}): {type(e).__name__}: {e}")
            raise

    async def health_check(self) -> tuple[bool, float]:
        start = time.monotonic()
        try:
            r = await self._client.get(f"{self.url}/api/health", timeout=3.0)
            latency = (time.monotonic() - start) * 1000
            if r.status_code == 200:
                # Refresh model cache on health check
                try:
                    cfg = await self._get("/api/config")
                    self._available_models = cfg.get("available_models", [])
                    self._config_fetched = True
                except Exception:
                    pass
                return True, latency
        except Exception:
            pass
        try:
            r = await self._client.get(f"{self.url}/", timeout=3.0)
            latency = (time.monotonic() - start) * 1000
            return r.status_code < 500, latency
        except Exception:
            return False, 0.0
