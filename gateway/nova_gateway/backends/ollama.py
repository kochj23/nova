"""
ollama.py — Ollama backend integration.

Available models (as of April 2026):
  qwen3-vl:4b            — Vision/multimodal (fast)
  deepseek-r1:8b         — Reasoning, analysis
  deepseek-v3.1:671b-cloud — Long context, complex tasks
  gpt-oss:120b           — Large general purpose
  qwen3-coder:480b-cloud — Large coding (cloud-routed)
  qwen3-coder:30b        — Local coding (primary)
  gpt-oss:20b            — Fast general purpose

Endpoints used:
  POST /api/generate      — Text generation
  POST /api/chat          — Chat completion
  GET  /api/tags          — List models
  POST /api/show          — Model info

Author: Jordan Koch
"""

import time
import logging
from typing import Optional, Any
from .base import BaseBackend

logger = logging.getLogger(__name__)


class OllamaBackend(BaseBackend):
    name = "ollama"

    def __init__(self, url: str = "http://localhost:11434", default_model: str = "qwen3-coder:30b"):
        super().__init__(url, timeout=300.0)
        self.default_model = default_model

    async def query(self, prompt: str, model: Optional[str] = None, **kwargs) -> dict[str, Any]:
        target_model = model or self.default_model
        stream = kwargs.get("stream", False)

        payload = {
            "model": target_model,
            "prompt": prompt,
            "stream": stream,
            "options": {
                "temperature": kwargs.get("temperature", 0.7),
                "num_predict": kwargs.get("max_tokens", 2048),
            }
        }

        if "system" in kwargs:
            payload["system"] = kwargs["system"]

        start = time.monotonic()
        try:
            r = await self._client.post(
                f"{self.url}/api/generate",
                json=payload,
                timeout=300.0
            )
            r.raise_for_status()
            data = r.json()
            elapsed = (time.monotonic() - start) * 1000

            response_text = data.get("response", "")
            eval_count = data.get("eval_count", 0)
            eval_duration_ns = data.get("eval_duration", 1)
            tokens_per_second = (eval_count / (eval_duration_ns / 1e9)) if eval_duration_ns > 0 else None

            return {
                "response": response_text,
                "model_used": target_model,
                "tokens_per_second": tokens_per_second,
                "token_count": eval_count,
                "latency_ms": elapsed,
                "done": data.get("done", True),
            }
        except Exception as e:
            logger.error(f"Ollama query failed (model={target_model}): {type(e).__name__}: {e}")
            raise

    async def chat(self, messages: list[dict], model: Optional[str] = None, **kwargs) -> dict[str, Any]:
        """Chat-style completion using /api/chat."""
        target_model = model or self.default_model
        payload = {
            "model": target_model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": kwargs.get("temperature", 0.7),
            }
        }
        start = time.monotonic()
        r = await self._client.post(f"{self.url}/api/chat", json=payload, timeout=120.0)
        r.raise_for_status()
        data = r.json()
        elapsed = (time.monotonic() - start) * 1000
        return {
            "response": data.get("message", {}).get("content", ""),
            "model_used": target_model,
            "latency_ms": elapsed,
        }

    async def list_models(self) -> list[str]:
        try:
            data = await self._get("/api/tags")
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    async def health_check(self) -> tuple[bool, float]:
        start = time.monotonic()
        try:
            r = await self._client.get(f"{self.url}/api/tags", timeout=3.0)
            latency = (time.monotonic() - start) * 1000
            return r.status_code == 200, latency
        except Exception:
            return False, 0.0
