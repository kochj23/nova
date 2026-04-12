"""
router.py — Intent-based AI routing engine for Nova-NextGen Gateway v2.

Routing decision order:
  1. Explicit preferred_backend in request → use it (availability checked)
  2. Explicit task_type in request → follow routing rules table
  3. task_type == "auto" → keyword detection on prompt text
  4. Availability check → if preferred backend is down, cascade to fallback(s)
  5. Last resort → first available backend

Backend strengths (why routing is designed this way):
  TinyChat  (8000)  — fastest round-trip, minimal overhead, qwen3:4b
  MLXCode   (37422) — Apple Neural Engine, Swift/code specialised
  MLXChat   (5000)  — Apple Neural Engine, fast general inference
  OpenWebUI (3000)  — RAG, document grounding, conversation history
  Ollama    (11434) — most models, deepseek-r1 reasoning, qwen3-vl vision
  SwarmUI   (7801)  — image generation (primary)
  ComfyUI   (8188)  — image generation workflows (fallback)

Author: Jordan Koch
"""

import logging
from typing import Optional
from . import config
from .backends.base import BaseBackend

logger = logging.getLogger(__name__)

# ── Keyword → task_type detection ────────────────────────────────────────────
# Checked in order; first match wins. More specific patterns first.

_KEYWORD_RULES: list[tuple[list[str], str]] = [

    # Image generation
    (["generate image", "create image", "draw me", "paint me", "render a",
      "dall-e", "midjourney", "stable diffusion", "make a picture",
      "create artwork", "generate art", "image of"], "image"),

    # Vision / image analysis
    (["what is in this image", "describe this image", "look at this image",
      "what do you see", "analyze this photo", "image shows", "picture shows"], "vision"),

    # Swift / Apple platform
    (["swift", ".swift", "swiftui", "uikit", "appkit", "xcode", "ios app",
      "macos app", "cocoa", "objective-c", ".m file", "viewcontroller",
      "spritekit", "scenekit", "arkit", "homekit", "watchos", "tvos"], "swift"),

    # Quick / classification — must match before generic coding
    (["yes or no", "true or false", "is it", "classify this", "tag this",
      "label this", "one word answer", "just say", "short answer only",
      "in one word", "quickly tell me", "just tell me if"], "quick"),

    # Coding / debugging
    (["write code", "write a function", "write a class", "debug this",
      "fix this bug", "refactor", "implement", "unit test", "write test",
      "algorithm for", "data structure", "time complexity",
      "python", "javascript", "typescript", "rust", "golang", "kotlin",
      "java ", "c++", "c#", ".py", ".js", ".ts", ".rs", ".go", ".kt",
      "async def", "func ", "fn ", "def "], "coding"),

    # Document / RAG
    (["in this document", "based on the file", "according to the pdf",
      "search through", "find in this", "this uploaded", "from the document",
      "retrieve from", "look up in", "the attached"], "document"),

    # Research
    (["research", "find information about", "look up", "what do you know about",
      "background on", "explain the history of", "what is the current state of"], "research"),

    # Deep reasoning
    (["why does", "explain why", "reason through", "think step by step",
      "walk me through", "what is the logical", "prove that", "disprove",
      "analyze the tradeoffs", "pros and cons of", "should i choose",
      "compare and contrast", "evaluate", "critique", "assess"], "reasoning"),

    # Analysis (slightly less deep than reasoning)
    (["analyze", "break down", "what patterns", "summarize the key",
      "identify the", "what is causing", "root cause", "diagnosis"], "analysis"),

    # Creative writing
    (["write a story", "write a poem", "fiction", "narrative",
      "blog post about", "marketing copy", "write an essay",
      "creative writing", "brainstorm ideas", "give me ideas for"], "creative"),

    # Long context
    (["summarize this entire", "full document", "complete transcript",
      "whole conversation", "entire codebase", "full text of"], "long_context"),

    # Summarize (lighter weight than long_context)
    (["summarize", "tldr", "tl;dr", "in brief", "give me the gist",
      "key points of", "main takeaways"], "summarize"),
]


def detect_task_type(prompt: str) -> str:
    """Detect task type from prompt keywords. Returns 'general' if no match."""
    lower = prompt.lower()
    for keywords, task_type in _KEYWORD_RULES:
        if any(kw in lower for kw in keywords):
            return task_type
    return "general"


class Router:
    def __init__(self, backends: dict[str, BaseBackend]):
        self._backends = backends

    async def resolve(
        self,
        prompt: str,
        task_type: str = "auto",
        preferred_backend: Optional[str] = None,
        model_override: Optional[str] = None,
    ) -> tuple[BaseBackend, Optional[str], str, bool]:
        """
        Returns (backend, model, resolved_task_type, fallback_used).
        """
        # Honour explicit backend override
        if preferred_backend and preferred_backend in self._backends:
            backend = self._backends[preferred_backend]
            available, _ = await backend.health_check()
            if available:
                return backend, model_override, task_type, False
            logger.warning(f"Router: forced backend '{preferred_backend}' is unavailable")

        # Auto-detect task type
        if task_type == "auto":
            task_type = detect_task_type(prompt)
            logger.debug(f"Router: auto-detected task_type='{task_type}'")

        rule = self._find_rule(task_type)

        if rule:
            # Try preferred backend — use rule's model unless caller overrides
            preferred_name = rule.get("preferred", config.default_backend())
            primary_model = model_override or rule.get("model")
            backend = self._backends.get(preferred_name)
            if backend:
                available, _ = await backend.health_check()
                if available:
                    logger.debug(
                        f"Router: {task_type} → {preferred_name}"
                        + (f" ({primary_model})" if primary_model else "")
                    )
                    return backend, primary_model, task_type, False

            # Try fallbacks in order (fallback, fallback2)
            for fallback_key in ("fallback", "fallback2"):
                fallback_name = rule.get(fallback_key)
                if not fallback_name or fallback_name not in self._backends:
                    continue
                fb_backend = self._backends[fallback_name]
                fb_available, _ = await fb_backend.health_check()
                if fb_available:
                    fb_model = model_override or rule.get(f"{fallback_key}_model")
                    logger.info(
                        f"Router: '{preferred_name}' unavailable → '{fallback_name}'"
                        + (f" ({fb_model})" if fb_model else "")
                    )
                    return fb_backend, fb_model, task_type, True

        # Last resort: first available backend in priority order
        priority = ["mlxchat", "tinychat", "ollama", "openwebui", "mlxcode", "comfyui", "swarmui"]
        for name in priority:
            if name not in self._backends:
                continue
            backend = self._backends[name]
            available, _ = await backend.health_check()
            if available:
                logger.warning(f"Router: all rules exhausted, using '{name}' as last resort")
                return backend, model_override, task_type, True

        raise RuntimeError(
            "No AI backends are currently available. "
            "Check that Ollama, MLXChat, or TinyChat is running."
        )

    def _find_rule(self, task_type: str) -> Optional[dict]:
        for rule in config.routing_rules():
            if rule.get("task_type") == task_type:
                return rule
        return None

    async def all_statuses(self) -> list[dict]:
        results = []
        for name, backend in self._backends.items():
            available, latency = await backend.health_check()
            results.append({
                "name": name,
                "available": available,
                "url": backend.url,
                "latency_ms": round(latency, 1) if available else None,
            })
        return results
