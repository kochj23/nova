"""
nova_gateway.router — ModelRouter class with multi-backend health-checked failover.

Also includes _build_tools_payload() for converting TOOL_REGISTRY to OpenAI format.

Written by Jordan Koch.
"""

import asyncio
import logging
import time

from nova_gateway.config import (
    OLLAMA_URL, MLX_URL, LLAMACPP_URL, OPENROUTER,
    is_private_content,
)

log = logging.getLogger("nova_gateway_v2")


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

        for name, base_url, health_path, is_local in self.BACKENDS:
            # Skip cloud backends for private queries
            if not is_local and private:
                continue

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
                result = await self._call_backend(
                    name, base_url, messages, system, max_tokens, tokens,
                    model_override, tools=tools, raw_response=raw_response,
                    ctx=ctx,
                )

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
            # Use Ollama's OpenAI-compatible endpoint for consistency
            model = model_override or "qwen3:30b-a3b"
            payload = {
                "model":      model,
                "messages":   msgs,
                "max_tokens": max_tokens,
                "temperature": 0.7,
                "stream":     False,
            }
            if tools:
                payload["tools"] = tools
            resp = await http.post(
                f"{base_url}/v1/chat/completions",
                json=payload,
                timeout=180,
            )
            resp.raise_for_status()
            data = resp.json()
            if raw_response:
                return data
            msg = data["choices"][0]["message"]
            return (msg.get("content") or msg.get("thinking") or "").strip()

        elif name == "mlx":
            # MLX LM Server — OpenAI-compatible
            payload = {
                "model":      model_override or "qwen2.5-32b",
                "messages":   msgs,
                "max_tokens": max_tokens,
                "temperature": 0.7,
            }
            if tools:
                payload["tools"] = tools
            resp = await http.post(
                f"{base_url}/v1/chat/completions",
                json=payload,
                timeout=180,
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
                timeout=180,
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

    @property
    def active_backend(self) -> str:
        return self._active_backend

    async def status(self, ctx=None) -> dict:
        """Return current health status of all backends (for health API)."""
        result = {}
        for name, base_url, health_path, is_local in self.BACKENDS:
            healthy = await self._check_health(name, base_url, health_path, ctx=ctx)
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
