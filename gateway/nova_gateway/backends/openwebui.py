"""
openwebui.py — OpenWebUI backend (port 3000).

OpenWebUI is a RAG-capable web interface for Ollama with vision support.

Specialized model: qwen3-vl:4b
Primary tasks: vision, UI interactions, multimodal, document RAG

API (OpenAI-compatible):
  POST /api/chat/completions  — primary chat endpoint
  GET  /api/models            — list available models (requires auth)
  GET  /api/version           — version check

Auth: OpenWebUI requires an API key when auth is enabled.
      Set api_key in config.yaml, or disable auth with WEBUI_AUTH=False.

Author: Jordan Koch
"""

import time
import logging
from typing import Optional, Any
from .base import BaseBackend

logger = logging.getLogger(__name__)


class OpenWebUIBackend(BaseBackend):
    name = "openwebui"

    def __init__(
        self,
        url: str = "http://localhost:3000",
        default_model: str = "qwen3-vl:4b",
        api_key: str = "",
    ):
        super().__init__(url, timeout=120.0)
        self.default_model = default_model
        self.api_key = api_key

    def _auth_headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

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
            "stream": False,
        }
        if "max_tokens" in kwargs:
            payload["max_tokens"] = kwargs["max_tokens"]

        start = time.monotonic()
        try:
            r = await self._client.post(
                f"{self.url}/api/chat/completions",
                json=payload,
                headers=self._auth_headers(),
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
                "model_used": f"openwebui/{target_model}",
                "tokens_per_second": tps,
                "token_count": total_tokens,
                "latency_ms": elapsed,
            }
        except Exception as e:
            logger.error(f"OpenWebUI query failed (model={target_model}): {type(e).__name__}: {e}")
            raise

    async def list_models(self) -> list[str]:
        try:
            r = await self._client.get(
                f"{self.url}/api/models",
                headers=self._auth_headers(),
                timeout=5.0
            )
            r.raise_for_status()
            return [m.get("id", m.get("name", "")) for m in r.json().get("data", [])]
        except Exception:
            return []

    async def health_check(self) -> tuple[bool, float]:
        start = time.monotonic()
        # Try unauthenticated version endpoint first
        try:
            r = await self._client.get(f"{self.url}/api/version", timeout=3.0)
            latency = (time.monotonic() - start) * 1000
            if r.status_code == 200:
                return True, latency
        except Exception:
            pass
        # Try authenticated models endpoint
        try:
            r = await self._client.get(
                f"{self.url}/api/models",
                headers=self._auth_headers(),
                timeout=3.0
            )
            latency = (time.monotonic() - start) * 1000
            # 401 = server is up but needs auth — still mark available
            # (queries will fail but at least routing won't skip it)
            return r.status_code in (200, 401), latency
        except Exception:
            pass
        # Fallback: root page
        try:
            r = await self._client.get(f"{self.url}/", timeout=3.0)
            latency = (time.monotonic() - start) * 1000
            return r.status_code < 500, latency
        except Exception:
            return False, 0.0
