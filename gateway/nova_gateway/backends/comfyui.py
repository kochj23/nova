"""
comfyui.py — ComfyUI backend integration (port 8188).

ComfyUI provides node-based image generation workflows.
Used as fallback when SwarmUI is unavailable.

Author: Jordan Koch
"""

import time
import json
import uuid
import asyncio
import logging
from typing import Optional, Any
from .base import BaseBackend

logger = logging.getLogger(__name__)

# Minimal txt2img workflow for ComfyUI
_DEFAULT_WORKFLOW = {
    "3": {
        "inputs": {"seed": 0, "steps": 20, "cfg": 7, "sampler_name": "euler",
                   "scheduler": "normal", "denoise": 1, "model": ["4", 0],
                   "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0]},
        "class_type": "KSampler"
    },
    "4": {"inputs": {"ckpt_name": "v1-5-pruned-emaonly.ckpt"}, "class_type": "CheckpointLoaderSimple"},
    "5": {"inputs": {"width": 512, "height": 512, "batch_size": 1}, "class_type": "EmptyLatentImage"},
    "6": {"inputs": {"text": "", "clip": ["4", 1]}, "class_type": "CLIPTextEncode"},
    "7": {"inputs": {"text": "bad quality, blurry", "clip": ["4", 1]}, "class_type": "CLIPTextEncode"},
    "8": {"inputs": {"samples": ["3", 0], "vae": ["4", 2]}, "class_type": "VAEDecode"},
    "9": {"inputs": {"filename_prefix": "nova_gateway", "images": ["8", 0]}, "class_type": "SaveImage"},
}


class ComfyUIBackend(BaseBackend):
    name = "comfyui"

    def __init__(self, url: str = "http://localhost:8188"):
        super().__init__(url, timeout=120.0)
        self._client_id = str(uuid.uuid4())

    async def query(self, prompt: str, model: Optional[str] = None, **kwargs) -> dict[str, Any]:
        """Queue and wait for an image generation workflow."""
        import random
        workflow = json.loads(json.dumps(_DEFAULT_WORKFLOW))
        workflow["6"]["inputs"]["text"] = prompt
        workflow["3"]["inputs"]["seed"] = random.randint(0, 2**32)

        if model:
            workflow["4"]["inputs"]["ckpt_name"] = model

        payload = {"prompt": workflow, "client_id": self._client_id}
        start = time.monotonic()
        try:
            r = await self._client.post(f"{self.url}/prompt", json=payload, timeout=10.0)
            r.raise_for_status()
            prompt_id = r.json().get("prompt_id", "")

            # Poll for completion
            result = await self._poll_result(prompt_id, timeout=110.0)
            elapsed = (time.monotonic() - start) * 1000
            return {
                "response": f"Image queued (prompt_id: {prompt_id}). {result}",
                "model_used": model or "comfyui-default",
                "latency_ms": elapsed,
            }
        except Exception as e:
            logger.error(f"ComfyUI query failed: {e}")
            raise

    async def _poll_result(self, prompt_id: str, timeout: float = 110.0) -> str:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                r = await self._client.get(f"{self.url}/history/{prompt_id}", timeout=5.0)
                if r.status_code == 200:
                    history = r.json()
                    if prompt_id in history:
                        outputs = history[prompt_id].get("outputs", {})
                        images = []
                        for node_output in outputs.values():
                            for img in node_output.get("images", []):
                                images.append(img.get("filename", ""))
                        if images:
                            return f"Output images: {', '.join(images)}"
            except Exception:
                pass
            await asyncio.sleep(2.0)
        return "Generation timed out."

    async def health_check(self) -> tuple[bool, float]:
        start = time.monotonic()
        try:
            r = await self._client.get(f"{self.url}/system_stats", timeout=3.0)
            latency = (time.monotonic() - start) * 1000
            return r.status_code == 200, latency
        except Exception:
            return False, 0.0
