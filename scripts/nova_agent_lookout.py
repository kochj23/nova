#!/usr/bin/env python3
"""
nova_agent_lookout.py — Lookout subagent (qwen3-vl:4b).

Subscribes to: vision, camera, motion channels.
Image analysis, anomaly detection, document OCR.
Reports genuine anomalies to #nova-notifications; critical to Jordan via Slack.

Written by Jordan Koch.
"""

import base64
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_subagent import SubAgent
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN

SYSTEM_PROMPT = """You are Lookout, a specialist AI vision subagent for Nova.
Your role is to analyze images from security cameras, motion events, and documents.

For each image, produce a JSON response with:
{
  "description": "what you see in the image",
  "anomaly_detected": true/false,
  "anomaly_type": "person|vehicle|animal|object|weather|none",
  "severity": "critical|high|medium|low|none",
  "confidence": 0.0-1.0,
  "details": "specific details about what's unusual",
  "flag_jordan": true/false  // true for unrecognized people, open doors/gates, or active threats
}

For documents/OCR, extract text content and key data.
Only flag as anomaly if something genuinely unusual or concerning is present.
Normal activity (family members, pets, delivery drivers) is NOT an anomaly."""


class LookoutAgent(SubAgent):
    name = "lookout"
    model = "qwen3-vl:4b"
    backend = "ollama"
    channels = ["vision", "camera", "motion"]
    description = "Vision analysis of camera feeds, screenshots, documents. Uses qwen3-vl:4b."
    temperature = 0.2
    max_tokens = 2048

    async def handle(self, task: dict) -> dict:
        image_path = task.get("image_path", "")
        image_b64 = task.get("image_base64", "")
        task_type = task.get("type", "vision")
        camera = task.get("camera", "unknown")
        text_prompt = task.get("prompt", "Analyze this image. What do you see? Is anything unusual?")

        if not image_path and not image_b64:
            log("No image provided", level=LOG_WARN, source="subagent.lookout")
            return None

        # Load image as base64 if path provided
        if image_path and not image_b64:
            try:
                with open(image_path, "rb") as f:
                    image_b64 = base64.b64encode(f.read()).decode()
            except Exception as e:
                log(f"Failed to read image: {e}", level=LOG_ERROR, source="subagent.lookout")
                return None

        log(f"Analyzing {task_type} from camera: {camera}", level=LOG_INFO, source="subagent.lookout")

        try:
            response = await self._infer_vision(text_prompt, image_b64)
        except Exception as e:
            log(f"Vision inference failed: {e}", level=LOG_ERROR, source="subagent.lookout")
            return None

        # Parse response
        try:
            cleaned = response
            if "<think>" in cleaned:
                think_end = cleaned.rfind("</think>")
                if think_end > 0:
                    cleaned = cleaned[think_end + 8:].strip()

            start = cleaned.find("{")
            end = cleaned.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(cleaned[start:end])
            else:
                result = {
                    "description": cleaned[:500],
                    "anomaly_detected": False,
                    "severity": "none",
                    "flag_jordan": False,
                }
        except json.JSONDecodeError:
            result = {
                "description": response[:500],
                "anomaly_detected": False,
                "severity": "none",
                "flag_jordan": False,
            }

        result["camera"] = camera
        result["source_type"] = task_type

        # Only notify on actual anomalies
        if result.get("anomaly_detected"):
            severity = result.get("severity", "medium")
            emoji = {"critical": ":rotating_light:", "high": ":warning:", "medium": ":eyes:", "low": ":mag:"}.get(severity, ":eyes:")

            msg = (
                f"{emoji} *Lookout Alert* ({severity.upper()})\n"
                f"*Camera:* {camera} | *Type:* {result.get('anomaly_type', 'unknown')}\n"
                f"*Description:* {result.get('description', 'N/A')[:300]}\n"
                f"*Details:* {result.get('details', 'N/A')[:200]}\n"
                f"*Confidence:* {result.get('confidence', 0):.0%}"
            )

            if result.get("flag_jordan") or severity in ("critical", "high"):
                await self.report_to_jordan(msg)
            else:
                await self.notify(msg)

        return result

    async def _infer_vision(self, prompt: str, image_b64: str) -> str:
        """Send image + prompt to Ollama's vision model."""
        payload = json.dumps({
            "model": self.model,
            "prompt": prompt,
            "system": SYSTEM_PROMPT,
            "images": [image_b64],
            "stream": False,
            "options": {"temperature": self.temperature, "num_predict": self.max_tokens},
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:11434/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        return data.get("response", "")


if __name__ == "__main__":
    LookoutAgent().run()
