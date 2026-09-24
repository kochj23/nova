"""
nova_gateway.router — ModelRouter class with multi-backend health-checked failover.

Also includes _build_tools_payload() for converting TOOL_REGISTRY to OpenAI format.

Written by Jordan Koch.
"""

import asyncio
import logging
import re
import sys
import time
from pathlib import Path

from nova_gateway.config import (
    OLLAMA_URL, MLX_URL, LLAMACPP_URL, OPENROUTER,
    is_private_content,
)

log = logging.getLogger("nova_gateway_v2")

# nova_lb lives in the shared scripts dir, not the nova_gateway package.
sys.path.insert(0, str(Path.home() / ".openclaw" / "scripts"))
try:
    import nova_lb
except Exception:
    nova_lb = None
    log.warning("ModelRouter: nova_lb unavailable — falling back to static single-host URLs")

# Protocols nova_lb actually load-balances across multiple nodes for.
_LB_PROTOCOLS = {"ollama", "mlx"}

_THINK_RE = re.compile(r"^.*?</think>\s*", re.DOTALL)
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


def _strip_thinking(text: str) -> str:
    """Remove thinking content — handles both <think>...</think> and bare ...></think> patterns."""
    if "</think>" in text:
        cleaned = _THINK_RE.sub("", text).strip()
        if cleaned:
            return cleaned
    cleaned = _THINK_BLOCK_RE.sub("", text).strip()
    if cleaned:
        return cleaned
    return text


class ModelRouter:
    """Routes LLM requests through a priority chain of backends with health checking.

    Priority order:
      1. Ollama (localhost:11434) — fastest, GPU-accelerated
      2. MLX LM (localhost:5050) — hot standby, Apple Silicon native
      3. llama.cpp (localhost:11435) — secondary standby
      4. OpenRouter (cloud) — fallback for non-private queries only

    Health is cached for 30 seconds. Failed mid-request calls automatically
    retry on the next backend in the chain.
    """

    # Backend definitions: (name, base_url, health_path, is_local)
    BACKENDS = [
        ("ollama",    OLLAMA_URL,   "/api/tags",           True),
        ("mlx",       MLX_URL,      "/v1/models",          True),
        ("llamacpp",  LLAMACPP_URL, "/v1/models",          True),
        ("openrouter", OPENROUTER,  "/models",             False),
    ]

    # Health cache TTL in seconds
    HEALTH_TTL = 30.0

    def __init__(self):
        # Backend name -> (is_healthy: bool, last_checked: float)
        self._health_cache: dict[str, tuple[bool, float]] = {}
        # Track which backend is currently active for logging
        self._active_backend: str = "unknown"
        # Track transitions for logging
        self._last_logged_backend: str = ""
        # protocol name -> node name currently resolved for it (nova_lb-backed protocols)
        self._last_picked_node: dict[str, str] = {}

    async def _check_health(self, name: str, base_url: str, health_path: str,
                            ctx=None) -> bool:
        """Check backend health via a lightweight HTTP GET. Cached for HEALTH_TTL seconds."""
        now = time.time()
        cached = self._health_cache.get(name)
        if cached and (now - cached[1]) < self.HEALTH_TTL:
            return cached[0]

        # Remember previous state for transition logging
        was_healthy = cached[0] if cached else None

        healthy = False
        try:
            if name == "openrouter":
                # OpenRouter is always "healthy" if we have an API key — just mark True
                # Actual availability is tested when we make the call
                healthy = True
            else:
                http = ctx.http if ctx else None
                if http is None:
                    healthy = False
                else:
                    resp = await http.get(f"{base_url}{health_path}", timeout=5.0)
                    healthy = resp.status_code == 200
        except Exception:
            healthy = False

        self._health_cache[name] = (healthy, now)

        # Log health transitions
        if was_healthy is not None and was_healthy != healthy:
            status = "UP" if healthy else "DOWN"
            log.warning(f"ModelRouter: backend '{name}' transitioned to {status}")

        return healthy

    def invalidate_health(self, name: str):
        """Force re-check on next request (call after a mid-request failure)."""
        self._health_cache.pop(name, None)

    def _resolve_backend(self, name: str, fallback_url: str) -> tuple[str, str | None]:
        """For load-balanced protocols (ollama/mlx), ask nova_lb for the current
        fastest healthy node instead of a hardcoded single host. Falls back to the
        static config URL if nova_lb has nothing (unreachable, no healthy node,
        or this protocol isn't multi-node). Returns (base_url, picked_node_name).
        """
        if name not in _LB_PROTOCOLS or nova_lb is None:
            return fallback_url, None
        try:
            pick = nova_lb.pick_node_shared(protocol=name)
        except Exception as e:
            log.warning(f"ModelRouter: nova_lb pick failed for '{name}': {e}")
            pick = None
        if not pick:
            return fallback_url, None
        return f"http://{pick['ip']}:{pick['port']}", pick["name"]

    async def route(self, messages: list, system: str = "", max_tokens: int = 1024,
                    private: bool = False, tokens: dict = None,
                    model_override: str = "",
                    tools: list = None, raw_response: bool = False,
                    ctx=None) -> str | dict:
        """Route a chat completion request through the priority chain.

        Args:
            messages: Conversation messages in OpenAI format [{role, content}, ...]
            system: System prompt (prepended as system message)
            max_tokens: Maximum response tokens
            private: If True, never route to OpenRouter (cloud)
            tokens: Dict with API keys (needs 'openrouter' key)
            model_override: Force a specific model name (for Ollama/OpenRouter)
            tools: Optional list of tool definitions in OpenAI function-calling format.
            raw_response: If True, return the full response JSON dict (for tool_calls inspection).
            ctx: GatewayContext instance for accessing shared state.

        Returns:
            The assistant's response text (str), or full response dict if raw_response=True.

        Raises:
            RuntimeError: If all backends fail.
        """
        tokens = tokens or {}
        errors = []

        for name, static_base_url, health_path, is_local in self.BACKENDS:
            # Skip cloud backends for private queries
            if not is_local and private:
                continue

            # F5-style: for ollama/mlx, ask nova_lb for the CURRENT fastest healthy
            # node instead of a hardcoded single host. This is the whole point of
            # not depending on one box — a dead .6 no longer means dead inference.
            base_url, picked_node = self._resolve_backend(name, static_base_url)
            if picked_node and picked_node != self._last_picked_node.get(name):
                log.info(f"ModelRouter: '{name}' now routing to node '{picked_node}' ({base_url})")
                self._last_picked_node[name] = picked_node
                # New node for this protocol — its health hasn't been checked yet,
                # don't trust a cached "healthy" that was measured against the OLD host.
                self.invalidate_health(name)

            # Privacy policy enforcement: hard block OpenRouter for sensitive content
            if name == "openrouter" and is_private_content(messages):
                log.warning("Privacy policy: blocked OpenRouter for private content")
                errors.append((name, "privacy policy blocked"))
                # Log to PG for auditing (fire-and-forget)
                if ctx:
                    from nova_gateway.session import log_privacy_block
                    asyncio.create_task(log_privacy_block(ctx, messages))
                continue

            # Skip OpenRouter if no API key
            if name == "openrouter" and not tokens.get("openrouter"):
                continue

            # Check health before attempting
            healthy = await self._check_health(name, base_url, health_path, ctx=ctx)
            if not healthy:
                errors.append((name, "health check failed"))
                continue

            # Attempt the request
            try:
                import time as _time
                _t0 = _time.time()
                result = await self._call_backend(
                    name, base_url, messages, system, max_tokens, tokens,
                    model_override, tools=tools, raw_response=raw_response,
                    ctx=ctx,
                )
                _elapsed_ms = int((_time.time() - _t0) * 1000)

                # Log backend transition
                if name != self._last_logged_backend:
                    if self._last_logged_backend:
                        log.info(
                            f"ModelRouter: routed to '{name}' "
                            f"(was: '{self._last_logged_backend}')"
                        )
                    else:
                        log.info(f"ModelRouter: using backend '{name}'")
                    self._last_logged_backend = name

                self._active_backend = name

                # Log inference to PG (fire-and-forget)
                asyncio.ensure_future(self._log_inference(
                    name, model_override, _elapsed_ms, messages, result, ctx
                ))

                return result

            except Exception as e:
                # Mid-request failure — invalidate health and try next
                self.invalidate_health(name)
                errors.append((name, str(e)))
                log.warning(f"ModelRouter: backend '{name}' failed mid-request: {e}")
                continue

        # All backends failed
        error_summary = "; ".join(f"{n}: {e}" for n, e in errors)
        log.error(f"ModelRouter: ALL backends failed — {error_summary}")
        raise RuntimeError(f"All LLM backends unavailable: {error_summary}")

    async def _call_backend(self, name: str, base_url: str, messages: list,
                            system: str, max_tokens: int, tokens: dict,
                            model_override: str, tools: list = None,
                            raw_response: bool = False, ctx=None) -> str | dict:
        """Call a specific backend. All use OpenAI-compatible format.

        Args:
            tools: Optional tool definitions (OpenAI function-calling format).
            raw_response: If True, return the full JSON response dict.
            ctx: GatewayContext for accessing http client.
        """
        http = ctx.http if ctx else None
        if http is None:
            raise RuntimeError(f"HTTP client not available for backend '{name}'")

        msgs = messages
        if system:
            msgs = [{"role": "system", "content": system}] + messages

        if name == "ollama":
            # Use Ollama's native API with think:true — thinking goes to
            # separate field, we only return content.
            model = model_override or "qwen3:8b"   # 2026-09-18: 30b only on wedged .6 / cold .77; 8b is reliable on the working nodes
            payload = {
                "model":   model,
                "messages": msgs,
                "options": {"num_predict": max_tokens + 2048, "temperature": 0.4},
                "think":   True,
                "stream":  False,
            }
            if tools:
                payload["tools"] = tools
            resp = await http.post(
                f"{base_url}/api/chat",
                json=payload,
                timeout=45,
            )
            resp.raise_for_status()
            data = resp.json()
            msg = data.get("message", {})
            content = (msg.get("content") or "").strip()
            if raw_response:
                tool_calls = msg.get("tool_calls")
                oai_msg = {"role": "assistant", "content": content}
                if tool_calls:
                    oai_msg["tool_calls"] = tool_calls
                return {"choices": [{"message": oai_msg, "finish_reason": "stop"}]}
            return content

        elif name == "mlx":
            # MLX LM Server — OpenAI-compatible
            payload = {
                "model":      model_override or "/Volumes/Data/mlx-models/qwen2.5-32b-4bit",
                "messages":   msgs,
                "max_tokens": max_tokens,
                "temperature": 0.7,
            }
            if tools:
                payload["tools"] = tools
            resp = await http.post(
                f"{base_url}/v1/chat/completions",
                json=payload,
                timeout=45,
            )
            resp.raise_for_status()
            data = resp.json()
            if raw_response:
                return data
            msg = data["choices"][0]["message"]
            return (msg.get("content") or msg.get("thinking") or "").strip()

        elif name == "llamacpp":
            # llama.cpp server — OpenAI-compatible
            payload = {
                "messages":   msgs,
                "max_tokens": max_tokens,
                "temperature": 0.7,
            }
            if tools:
                payload["tools"] = tools
            resp = await http.post(
                f"{base_url}/v1/chat/completions",
                json=payload,
                timeout=45,
            )
            resp.raise_for_status()
            data = resp.json()
            if raw_response:
                return data
            return data["choices"][0]["message"]["content"].strip()

        elif name == "openrouter":
            api_key = tokens.get("openrouter", "")
            model = model_override or "qwen/qwen3-235b-a22b-2507"
            payload = {
                "model":      model,
                "messages":   msgs,
                "max_tokens": max_tokens,
                "temperature": 0.7,
            }
            if tools:
                payload["tools"] = tools
            resp = await http.post(
                f"{base_url}/chat/completions",
                json=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "HTTP-Referer": "https://nova.digitalnoise.net",
                },
                timeout=120,
            )
            resp.raise_for_status()
            data = resp.json()
            if raw_response:
                return data
            return data["choices"][0]["message"]["content"].strip()

        else:
            raise ValueError(f"Unknown backend: {name}")

    # Default model name per backend, mirrors the literals in _call_backend.
    # Lets inference_latency.model carry a real, groupable name for Grafana
    # instead of the placeholder "default".
    _DEFAULT_MODELS = {
        "ollama":     "qwen3:8b",
        "mlx":        "/Volumes/Data/mlx-models/qwen2.5-32b-4bit",
        "llamacpp":   "llamacpp",
        "openrouter": "qwen/qwen3-235b-a22b-2507",
    }

    async def _log_inference(self, backend: str, model: str, total_ms: int,
                             messages: list, result, ctx=None):
        """Log inference request to PG inference_latency table (fire-and-forget)."""
        try:
            import psycopg2
            # Resolve the real model name: explicit override > backend default.
            model_name = model or self._DEFAULT_MODELS.get(backend, "default")
            # Prefer exact token usage from a raw API response; else estimate.
            prompt_tokens = completion_tokens = None
            if isinstance(result, dict):
                usage = result.get("usage") or {}
                prompt_tokens = usage.get("prompt_tokens")
                completion_tokens = usage.get("completion_tokens")
            if prompt_tokens is None:
                prompt_tokens = sum(len(m.get("content", "").split()) for m in messages) * 1.3
            if completion_tokens is None:
                completion_tokens = len(str(result).split()) * 1.3 if result else 0
            conn = psycopg2.connect("dbname=nova_ops user=kochj host=pg-primary.digitalnoise.net", connect_timeout=3)
            conn.autocommit = True
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO inference_latency ("timestamp", backend, model, prompt_tokens, completion_tokens, ttft_ms, total_ms, status)
                VALUES (NOW(), %s, %s, %s, %s, NULL, %s, 'ok')
            """, (backend, model_name, int(prompt_tokens), int(completion_tokens), total_ms))
            conn.close()
        except Exception:
            pass

    @property
    def active_backend(self) -> str:
        return self._active_backend

    async def status(self, ctx=None) -> dict:
        """Return current health status of all backends (for health API)."""
        # 2026-09-24: /health must never block on backend probes. The fleet checker times out
        # at 5s and this used to await up to 4 serial 5s probes on every cache expiry, so the
        # gateway was logged "down" ~25% of the time while answering chat fine. Serve the cache;
        # refresh stale entries in the background (the chat path still probes inline as before).
        result = {}
        for name, base_url, health_path, is_local in self.BACKENDS:
            cached = self._health_cache.get(name)
            if cached and (time.time() - cached[1]) < self.HEALTH_TTL:
                healthy = cached[0]
            else:
                asyncio.ensure_future(self._check_health(name, base_url, health_path, ctx=ctx))
                healthy = cached[0] if cached else False
            cached = self._health_cache.get(name)
            result[name] = {
                "healthy": healthy,
                "is_local": is_local,
                "last_checked": cached[1] if cached else None,
            }
        result["active"] = self._active_backend
        return result


def build_tools_payload(tool_registry: dict) -> list[dict]:
    """Convert TOOL_REGISTRY into OpenAI function-calling format for LLM requests."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": defn["description"],
                "parameters": {
                    "type": "object",
                    "properties": defn["parameters"],
                    "required": defn.get("required", []),
                },
            },
        }
        for name, defn in tool_registry.items()
    ]
