#!/usr/bin/env python3
"""
nova_inference_client.py — Client helper for the shared inference queue.

Provides simple functions to submit inference requests and wait for results.
Used by scripts that want priority-aware queuing instead of direct Ollama calls.

Usage:
  from nova_inference_client import queue_and_wait, queue_fire_and_forget

  # Synchronous (blocks until result)
  result = queue_and_wait("What is 2+2?", intent="quick", priority=1, timeout=30)

  # Fire-and-forget (returns immediately, result available via Redis)
  request_id = queue_fire_and_forget("Summarize this...", intent="summarize", priority=3)

Written by Jordan Koch.
"""

import json
import time
import urllib.request
from typing import Optional

QUEUE_URL = "http://127.0.0.1:37470"


def queue_inference(
    prompt: str,
    intent: str = "conversation",
    priority: int = 2,
    system: str = "",
    model: str = "",
    options: dict = None,
    callback_channel: str = "",
) -> dict:
    """Submit an inference request to the queue. Returns {"request_id": str, "queued": bool}."""
    payload = json.dumps({
        "prompt": prompt,
        "intent": intent,
        "priority": priority,
        "system": system,
        "model": model,
        "options": options or {},
        "callback_channel": callback_channel,
    }).encode()

    req = urllib.request.Request(
        f"{QUEUE_URL}/queue/submit",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e), "queued": False}


def get_result(request_id: str) -> Optional[dict]:
    """Poll for a result. Returns None if still pending."""
    try:
        with urllib.request.urlopen(f"{QUEUE_URL}/queue/result/{request_id}", timeout=5) as r:
            data = json.loads(r.read())
            if data.get("status") == "pending":
                return None
            return data
    except Exception:
        return None


def queue_and_wait(
    prompt: str,
    intent: str = "conversation",
    priority: int = 2,
    system: str = "",
    model: str = "",
    options: dict = None,
    timeout: float = 60,
) -> dict:
    """Submit request and block until result is available."""
    submission = queue_inference(prompt, intent, priority, system, model, options)
    if not submission.get("queued"):
        return submission

    request_id = submission["request_id"]
    deadline = time.time() + timeout

    while time.time() < deadline:
        result = get_result(request_id)
        if result is not None:
            return result
        time.sleep(0.2)

    return {"error": "timeout", "request_id": request_id}


def queue_fire_and_forget(
    prompt: str,
    intent: str = "conversation",
    priority: int = 3,
    system: str = "",
    model: str = "",
    options: dict = None,
    callback_channel: str = "",
) -> Optional[str]:
    """Submit and return immediately. Returns request_id or None on failure."""
    result = queue_inference(prompt, intent, priority, system, model, options, callback_channel)
    return result.get("request_id")


def queue_stats() -> dict:
    """Get current queue statistics."""
    try:
        with urllib.request.urlopen(f"{QUEUE_URL}/queue/stats", timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def queue_health() -> dict:
    """Get queue service health."""
    try:
        with urllib.request.urlopen(f"{QUEUE_URL}/health", timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e), "ok": False}
