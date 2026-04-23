#!/usr/bin/env python3
"""
nova_subagent.py — Base framework for Nova's local LLM subagents.

Provides:
  - Redis pub/sub message bus (subscribe by capability, publish results)
  - Subagent registry (register/deregister in subagents/runs.json)
  - Health heartbeat (periodic Redis key update)
  - Slack notification helpers (flag-and-report to Jordan)
  - LLM inference wrapper (Ollama + MLX backends)
  - Structured logging via nova_logger

Usage:
    from nova_subagent import SubAgent

    class MyAgent(SubAgent):
        name = "analyst"
        model = "deepseek-r1:8b"
        channels = ["email", "meeting"]

        async def handle(self, task):
            result = await self.infer(task["prompt"])
            await self.report(result)

    if __name__ == "__main__":
        MyAgent().run()

Written by Jordan Koch.
"""

import asyncio
import json
import os
import signal
import sys
import time
import traceback
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Add scripts dir to path for nova_config/nova_logger
sys.path.insert(0, str(Path(__file__).parent))

try:
    import redis
except ImportError:
    print("[nova_subagent] ERROR: redis package required. Run: pip3 install redis", file=sys.stderr)
    sys.exit(1)

import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN, LOG_DEBUG

REDIS_URL = os.environ.get("NOVA_REDIS_URL", "redis://localhost:6379")
REGISTRY_PATH = Path.home() / ".openclaw" / "subagents" / "runs.json"
OLLAMA_URL = "http://127.0.0.1:11434"
MLX_URL = "http://127.0.0.1:5050"
SLACK_NOTIFY = nova_config.SLACK_NOTIFY   # C0ATAF7NZG9 #nova-notifications
SLACK_CHAT = nova_config.SLACK_CHAN        # C0AMNQ5GX70 #nova-chat
HEARTBEAT_INTERVAL = 30  # seconds


class SubAgent(ABC):
    """Base class for all Nova subagents."""

    name: str = "unnamed"
    model: str = "deepseek-r1:8b"
    backend: str = "ollama"  # "ollama" or "mlx"
    channels: list[str] = []
    description: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096

    def __init__(self):
        self._redis = redis.from_url(REDIS_URL, decode_responses=True)
        self._pubsub = self._redis.pubsub()
        self._running = False
        self._task_count = 0
        self._start_time = None
        self._last_error = None
        log(f"SubAgent '{self.name}' initialized (model={self.model}, channels={self.channels})",
            level=LOG_INFO, source=f"subagent.{self.name}")

    # ── Abstract ─────────────────────────────────────────────────────────────

    @abstractmethod
    async def handle(self, task: dict) -> Optional[dict]:
        """Process a task message. Return result dict or None."""
        ...

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def run(self):
        """Start the subagent event loop."""
        signal.signal(signal.SIGINT, self._shutdown)
        signal.signal(signal.SIGTERM, self._shutdown)
        try:
            asyncio.run(self._main_loop())
        except KeyboardInterrupt:
            pass
        finally:
            self._deregister()

    async def _main_loop(self):
        self._running = True
        self._start_time = datetime.now(timezone.utc)
        self._register()

        # Subscribe to channels
        for ch in self.channels:
            self._pubsub.subscribe(f"nova:task:{ch}")
        self._pubsub.subscribe(f"nova:task:{self.name}")  # direct addressing
        self._pubsub.subscribe("nova:task:broadcast")       # all agents

        log(f"SubAgent '{self.name}' listening on {self.channels}",
            level=LOG_INFO, source=f"subagent.{self.name}")

        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            while self._running:
                msg = self._pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg and msg["type"] == "message":
                    try:
                        task = json.loads(msg["data"])
                        task["_channel"] = msg["channel"]
                        task["_received_at"] = datetime.now(timezone.utc).isoformat()
                        self._task_count += 1
                        log(f"Task received: {task.get('type', 'unknown')} via {msg['channel']}",
                            level=LOG_INFO, source=f"subagent.{self.name}")
                        result = await self.handle(task)
                        if result:
                            await self._publish_result(task, result)
                    except Exception as e:
                        self._last_error = str(e)
                        log(f"Task handler error: {e}\n{traceback.format_exc()}",
                            level=LOG_ERROR, source=f"subagent.{self.name}")
                else:
                    await asyncio.sleep(0.1)
        finally:
            heartbeat_task.cancel()
            self._pubsub.unsubscribe()

    def _shutdown(self, *_):
        log(f"SubAgent '{self.name}' shutting down", level=LOG_INFO, source=f"subagent.{self.name}")
        self._running = False

    # ── Registry ─────────────────────────────────────────────────────────────

    def _register(self):
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        registry = self._load_registry()
        registry["runs"][self.name] = {
            "status": "running",
            "model": self.model,
            "backend": self.backend,
            "channels": self.channels,
            "description": self.description,
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
            "task_count": 0,
        }
        self._save_registry(registry)
        self._redis.set(f"nova:agent:{self.name}:status", "running", ex=HEARTBEAT_INTERVAL * 3)
        log(f"Registered in {REGISTRY_PATH}", level=LOG_DEBUG, source=f"subagent.{self.name}")

    def _deregister(self):
        try:
            registry = self._load_registry()
            if self.name in registry["runs"]:
                registry["runs"][self.name]["status"] = "stopped"
                registry["runs"][self.name]["stopped_at"] = datetime.now(timezone.utc).isoformat()
                registry["runs"][self.name]["task_count"] = self._task_count
                self._save_registry(registry)
            self._redis.delete(f"nova:agent:{self.name}:status")
        except Exception:
            pass

    def _load_registry(self) -> dict:
        if REGISTRY_PATH.exists():
            try:
                return json.loads(REGISTRY_PATH.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {"version": 2, "runs": {}}

    def _save_registry(self, registry: dict):
        REGISTRY_PATH.write_text(json.dumps(registry, indent=2, default=str))

    # ── Heartbeat ────────────────────────────────────────────────────────────

    async def _heartbeat_loop(self):
        while self._running:
            try:
                self._redis.set(
                    f"nova:agent:{self.name}:status", "running",
                    ex=HEARTBEAT_INTERVAL * 3
                )
                self._redis.hset(f"nova:agent:{self.name}:meta", mapping={
                    "model": self.model,
                    "tasks_completed": str(self._task_count),
                    "uptime_s": str(int((datetime.now(timezone.utc) - self._start_time).total_seconds())),
                    "last_error": self._last_error or "",
                })
            except Exception:
                pass
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    # ── LLM Inference ────────────────────────────────────────────────────────

    async def infer(self, prompt: str, system: str = "", model: str = None,
                    temperature: float = None, max_tokens: int = None) -> str:
        """Send a prompt to the configured LLM backend and return the response text."""
        model = model or self.model
        temp = temperature if temperature is not None else self.temperature
        tokens = max_tokens or self.max_tokens

        if self.backend == "ollama":
            return await self._infer_ollama(prompt, system, model, temp, tokens)
        elif self.backend == "mlx":
            return await self._infer_mlx(prompt, system, model, temp, tokens)
        else:
            raise ValueError(f"Unknown backend: {self.backend}")

    async def _infer_ollama(self, prompt, system, model, temp, tokens) -> str:
        payload = json.dumps({
            "model": model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {"temperature": temp, "num_predict": tokens},
        }).encode()
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            data = json.loads(resp.read())
            return data.get("response", "")
        except Exception as e:
            log(f"Ollama inference failed: {e}", level=LOG_ERROR, source=f"subagent.{self.name}")
            raise

    async def _infer_mlx(self, prompt, system, model, temp, tokens) -> str:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        payload = json.dumps({
            "model": model,
            "messages": messages,
            "temperature": temp,
            "max_tokens": tokens,
            "stream": False,
        }).encode()
        req = urllib.request.Request(
            f"{MLX_URL}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=300)
            data = json.loads(resp.read())
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            log(f"MLX inference failed: {e}", level=LOG_ERROR, source=f"subagent.{self.name}")
            raise

    # ── Memory ───────────────────────────────────────────────────────────────

    async def recall(self, query: str, n: int = 5, source: str = None) -> list[dict]:
        """Search Nova's vector memory."""
        url = f"http://127.0.0.1:18790/recall?q={urllib.parse.quote(query)}&n={n}"
        if source:
            url += f"&source={source}"
        try:
            resp = urllib.request.urlopen(url, timeout=10)
            data = json.loads(resp.read())
            return data.get("memories", [])
        except Exception:
            return []

    async def remember(self, text: str, source: str = "", metadata: dict = None):
        """Store a fact in Nova's vector memory."""
        payload = json.dumps({
            "text": text,
            "source": source or f"subagent.{self.name}",
            "metadata": metadata or {},
        }).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:18790/remember",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f"Memory write failed: {e}", level=LOG_ERROR, source=f"subagent.{self.name}")

    # ── Slack ────────────────────────────────────────────────────────────────

    async def notify(self, message: str, channel: str = None):
        """Post to #nova-notifications."""
        await self._slack_post(message, channel or SLACK_NOTIFY)

    async def report_to_jordan(self, message: str):
        """Post to #nova-chat for Jordan's attention (flag-and-report pattern)."""
        await self._slack_post(message, SLACK_CHAT)

    async def _slack_post(self, message: str, channel: str = None):
        import asyncio
        ch = channel or nova_config.SLACK_NOTIFY
        await asyncio.to_thread(nova_config.post_both, message, ch)

    # ── Task Publishing ──────────────────────────────────────────────────────

    async def _publish_result(self, task: dict, result: dict):
        """Publish result back to Redis for the orchestrator or other agents."""
        result["_agent"] = self.name
        result["_task_id"] = task.get("id", "")
        result["_completed_at"] = datetime.now(timezone.utc).isoformat()
        self._redis.publish(f"nova:result:{self.name}", json.dumps(result, default=str))

    @staticmethod
    def dispatch(channel: str, task: dict, redis_url: str = REDIS_URL):
        """Class method to dispatch a task to a channel from any script."""
        r = redis.from_url(redis_url, decode_responses=True)
        task["_dispatched_at"] = datetime.now(timezone.utc).isoformat()
        task["id"] = task.get("id", f"{channel}-{int(time.time())}")
        r.publish(f"nova:task:{channel}", json.dumps(task, default=str))

    # ── Health Check ─────────────────────────────────────────────────────────

    def is_backend_healthy(self) -> bool:
        """Check if the LLM backend is reachable."""
        url = f"{OLLAMA_URL}/api/tags" if self.backend == "ollama" else f"{MLX_URL}/v1/models"
        try:
            urllib.request.urlopen(url, timeout=5)
            return True
        except Exception:
            return False


# ── Convenience: import urllib.parse for recall ──────────────────────────────
import urllib.parse
