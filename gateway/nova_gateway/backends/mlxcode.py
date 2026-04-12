"""
mlxcode.py — MLXCode backend integration (port 37422).

MLXCode is Jordan's local Apple Neural Engine–optimized LLM app.
It runs models via MLX on Apple Silicon — lowest latency for coding tasks.

Endpoints (from NovaAPIServer.swift):
  GET  /api/status          — app status, model loaded, tokens/sec
  POST /api/chat            — send message {"message": "..."}
  GET  /api/model           — current loaded model info
  GET  /api/metrics         — performance metrics
  GET  /api/conversations   — list conversations
  POST /api/conversations   — new conversation
  POST /api/cancel          — cancel generation

Author: Jordan Koch
"""

import time
import logging
from typing import Optional, Any
from .base import BaseBackend

logger = logging.getLogger(__name__)


class MLXCodeBackend(BaseBackend):
    name = "mlxcode"

    def __init__(self, url: str = "http://localhost:37422"):
        super().__init__(url, timeout=60.0)

    async def query(self, prompt: str, model: Optional[str] = None, **kwargs) -> dict[str, Any]:
        payload = {"message": prompt}
        start = time.monotonic()
        try:
            r = await self._client.post(
                f"{self.url}/api/chat",
                json=payload,
                timeout=60.0
            )
            r.raise_for_status()
            data = r.json()
            elapsed = (time.monotonic() - start) * 1000

            return {
                "response": data.get("response", ""),
                "model_used": await self._current_model(),
                "tokens_per_second": data.get("tokensPerSecond"),
                "token_count": data.get("tokenCount"),
                "latency_ms": elapsed,
            }
        except Exception as e:
            logger.error(f"MLXCode query failed: {e}")
            raise

    async def _current_model(self) -> str:
        try:
            data = await self._get("/api/model")
            return data.get("currentModel", "mlx-local")
        except Exception:
            return "mlx-local"

    async def status(self) -> dict:
        try:
            return await self._get("/api/status")
        except Exception as e:
            return {"error": str(e)}

    async def metrics(self) -> dict:
        try:
            return await self._get("/api/metrics")
        except Exception as e:
            return {"error": str(e)}

    async def health_check(self) -> tuple[bool, float]:
        start = time.monotonic()
        try:
            r = await self._client.get(f"{self.url}/api/status", timeout=3.0)
            latency = (time.monotonic() - start) * 1000
            if r.status_code == 200:
                data = r.json()
                is_ready = data.get("modelLoaded", False)
                return is_ready, latency
            return False, latency
        except Exception:
            return False, 0.0
