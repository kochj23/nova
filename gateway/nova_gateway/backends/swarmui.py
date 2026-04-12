"""
swarmui.py — SwarmUI backend (port 7801).

SwarmUI handles Stable Diffusion image generation.

Specialized model: Juggernaut XL
Primary tasks: image generation (photo-realistic, high quality)

The model name must match the exact filename in your SwarmUI models directory.
Common variants: "Juggernaut XL", "juggernautXL_version6Rundiffusion", etc.
Set the exact name in config.yaml: backends.swarmui.default_model

API:
  POST /API/GetNewSession         — create session
  POST /API/GenerateText2Image    — generate image
  GET  /API/GetServerStatus       — health check

Author: Jordan Koch
"""

import time
import logging
import asyncio
from typing import Optional, Any
from .base import BaseBackend

logger = logging.getLogger(__name__)


class SwarmUIBackend(BaseBackend):
    name = "swarmui"

    def __init__(self, url: str = "http://localhost:7801", default_model: str = "Juggernaut XL"):
        super().__init__(url, timeout=120.0)
        self.default_model = default_model
        self._session_id: Optional[str] = None

    async def _get_session(self) -> str:
        if self._session_id:
            return self._session_id
        r = await self._client.post(f"{self.url}/API/GetNewSession", json={}, timeout=10.0)
        r.raise_for_status()
        self._session_id = r.json().get("session_id", "")
        return self._session_id

    async def query(self, prompt: str, model: Optional[str] = None, **kwargs) -> dict[str, Any]:
        """Generate an image from a text prompt using Juggernaut XL."""
        target_model = model or self.default_model
        try:
            session_id = await self._get_session()
            payload = {
                "session_id": session_id,
                "prompt": prompt,
                "negativeprompt": kwargs.get("negative_prompt", "blurry, low quality, watermark"),
                "images": kwargs.get("count", 1),
                "width": kwargs.get("width", 1024),
                "height": kwargs.get("height", 1024),
                "steps": kwargs.get("steps", 25),
                "cfgscale": kwargs.get("cfg_scale", 7.0),
                "model": target_model,
            }
            start = time.monotonic()
            r = await self._client.post(
                f"{self.url}/API/GenerateText2Image",
                json=payload,
                timeout=120.0
            )
            r.raise_for_status()
            data = r.json()
            elapsed = (time.monotonic() - start) * 1000
            images = data.get("images", [])
            return {
                "response": f"Image generated via {target_model}. URLs: {', '.join(images)}",
                "images": images,
                "model_used": target_model,
                "latency_ms": elapsed,
            }
        except Exception as e:
            logger.error(f"SwarmUI query failed (model={target_model}): {type(e).__name__}: {e}")
            raise

    async def health_check(self) -> tuple[bool, float]:
        start = time.monotonic()
        try:
            r = await self._client.get(f"{self.url}/API/GetServerStatus", timeout=3.0)
            latency = (time.monotonic() - start) * 1000
            return r.status_code == 200, latency
        except Exception:
            return False, 0.0
