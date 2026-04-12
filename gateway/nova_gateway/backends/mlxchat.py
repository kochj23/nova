"""
mlxchat.py — MLX Chat backend (port 5000).

MLX Chat runs Apple-optimized models directly on the Apple Neural Engine
via the MLX framework. It exposes an OpenAI-compatible API and is the
fastest backend for general text generation on Apple Silicon.

Distinct from MLXCode (port 37422) — MLX Chat is a general-purpose
inference server. MLXCode is Jordan's custom app with Swift/coding focus.

Best for:
  - Fast general-purpose text generation
  - Medium-complexity tasks that need Apple Silicon speed
  - Creative writing that doesn't need a cloud model
  - Routing: task_type "general", "creative", "summarize"

API (OpenAI-compatible):
  POST /v1/chat/completions  — standard chat
  GET  /v1/models            — list loaded models
  GET  /health               — liveness check

Author: Jordan Koch
"""

import time
import logging
from typing import Optional, Any
from .base import BaseBackend

logger = logging.getLogger(__name__)


class MLXChatBackend(BaseBackend):
    name = "mlxchat"

    def __init__(self, url: str = "http://localhost:5000", default_model: str = "mlx-community/Qwen2.5-7B-Instruct-4bit"):
        super().__init__(url, timeout=120.0)
        self.default_model = default_model

    async def query(self, prompt: str, model: Optional[str] = None, **kwargs) -> dict[str, Any]:
        target_model = model or self.default_model

        messages = []
        if "system" in kwargs:
            messages.append({"role": "system", "content": kwargs["system"]})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": target_model,
            "messages": messages,
            "temperature": kwargs.get("temperature", 0.7),
            "max_tokens": kwargs.get("max_tokens", 2048),
            "stream": False,
        }

        start = time.monotonic()
        try:
            r = await self._client.post(
                f"{self.url}/v1/chat/completions",
                json=payload,
                timeout=120.0
            )
            r.raise_for_status()
            data = r.json()
            elapsed = (time.monotonic() - start) * 1000

            response_text = (
                data.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            ).strip()

            usage = data.get("usage", {})
            total_tokens = usage.get("completion_tokens", 0)
            tps = (total_tokens / (elapsed / 1000)) if elapsed > 0 and total_tokens else None

            return {
                "response": response_text,
                "model_used": data.get("model", target_model),
                "tokens_per_second": tps,
                "token_count": total_tokens,
                "latency_ms": elapsed,
            }
        except Exception as e:
            logger.error(f"MLXChat query failed (model={target_model}): {type(e).__name__}: {e}")
            raise

    async def current_model(self) -> str:
        try:
            data = await self._get("/v1/models")
            models = data.get("data", [])
            if models:
                return models[0].get("id", self.default_model)
        except Exception:
            pass
        return self.default_model

    async def health_check(self) -> tuple[bool, float]:
        start = time.monotonic()
        for path in ("/health", "/v1/models"):
            try:
                r = await self._client.get(f"{self.url}{path}", timeout=3.0)
                latency = (time.monotonic() - start) * 1000
                if r.status_code < 500:
                    return True, latency
            except Exception:
                continue
        return False, 0.0
