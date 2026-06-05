#!/usr/bin/env python3
"""
nova_filter_pipeline.py — Progressive filtering pipeline (Frigate pattern).

Four-stage classification cascade — each stage is a cheap gate that prevents
the expensive next stage from running:

  Stage 1 (FREE):    Regex/keyword rules → resolves ~50% of intents instantly
  Stage 2 (CHEAP):   Small model triage (qwen3-coder:30b, 20 tokens) → ambiguous cases
  Stage 3 (FULL):    Full intent_router.route() → confirmed complex queries
  Stage 4 (ENRICH):  Memory recall → only when personal context is needed

Most queries never hit Stage 3 or 4, saving significant inference cost.

Usage:
  from nova_filter_pipeline import classify_and_route
  result = classify_and_route(prompt, source="slack", session_id="abc123")
  # result = {"intent": "greeting", "stage": 1, "needs_memory": False, ...}

Written by Jordan Koch.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))

try:
    import redis
    _rc = redis.from_url("redis://192.168.1.6:6379", decode_responses=True)
except Exception:
    _rc = None

# ── Stage 1: Regex/Keyword Rules (FREE — no LLM) ─────────────────────────────
# Adapted from gateway/nova_gateway/router.py _KEYWORD_RULES
# Returns intent name or None (pass to Stage 2)

STAGE1_RULES: list[tuple[list[str], str, bool]] = [
    # (keywords, intent, needs_memory)

    # Memory recall — always needs memory, skip LLM for routing (BEFORE greetings — "hello" is substring of "do you remember what I said hello")
    (["do you remember", "what do you know about", "recall ",
      "search your memory", "in your memories", "remember when"], "memory_recall", True),

    # Greetings — never need memory or LLM
    (["hello", "hi nova", "hey nova", "good morning", "good evening",
      "howdy", "what's up", "sup", "yo"], "greeting", False),

    # Acknowledgements — no processing needed
    (["thanks", "thank you", "got it", "ok", "okay", "cool", "nice",
      "awesome", "great", "perfect", "sounds good", "will do"], "acknowledgement", False),

    # Commands — direct actions, no reasoning needed
    (["turn on", "turn off", "set the", "play ", "stop ", "pause ",
      "lights on", "lights off", "lock the", "unlock the"], "command", False),

    # Quick yes/no — tiny model sufficient
    (["yes or no", "true or false", "is it", "just say", "one word",
      "quickly tell me", "just tell me if", "short answer"], "quick", False),

    # Image generation — routes to SwarmUI, not LLM
    (["generate image", "create image", "draw me", "paint me", "render a",
      "make a picture", "create artwork", "image of"], "image_generation", False),

    # Classification/tagging — cheap model
    (["classify this", "tag this", "label this", "categorize"], "classify", False),

    # Time/date queries — no LLM needed
    (["what time", "what day", "what date", "what's today"], "time_query", False),

    # System status — direct API call, no LLM
    (["system status", "how are you running", "are you up",
      "health check", "status report"], "system_status", False),

    # Personal/emotional — needs memory for context
    (["how am i", "my health", "my schedule", "my calendar",
      "about me", "my preferences", "my "], "personal_query", True),

    # Creative writing — full LLM, no memory needed
    (["write a story", "write a poem", "blog post", "essay about",
      "creative writing", "brainstorm"], "creative_writing", False),

    # Code — full LLM, no memory needed
    (["write code", "write a function", "debug this", "fix this bug",
      "refactor", "implement", "unit test", "write test",
      "python", "javascript", "typescript", "rust", "golang",
      "async def", "func ", "fn ", "def "], "coding", False),

    # Research — full LLM + memory
    (["research", "find information", "look up", "background on",
      "explain the history", "what is the current state"], "research", True),

    # Reasoning — full LLM, sometimes memory
    (["why does", "explain why", "reason through", "think step by step",
      "walk me through", "prove that", "analyze the tradeoffs",
      "pros and cons", "compare and contrast"], "reasoning", False),
]

# Intents that ALWAYS need memory enrichment
MEMORY_INTENTS = {
    "memory_recall", "personal_query", "research", "email_recall",
    "health_query", "conversation",
}

# Intents that NEVER need memory (even if classified that way by LLM)
NO_MEMORY_INTENTS = {
    "greeting", "acknowledgement", "command", "quick", "classify",
    "time_query", "system_status", "image_generation", "coding",
}

# ── Stage 2: Fast LLM Classification (~20 tokens, <500ms) ────────────────────

FAST_CLASSIFY_PROMPT = """Classify this user message into one intent category.
Categories: greeting, command, quick, coding, creative_writing, reasoning, research, personal_query, memory_recall, conversation, other
Reply with ONLY the category name, nothing else.

Message: {prompt}
Category:"""

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
FAST_MODEL = "qwen3-coder:30b"


def _fast_classify(prompt: str) -> Optional[str]:
    """Stage 2: Use small model for 20-token classification. Returns intent or None."""
    import urllib.request
    payload = json.dumps({
        "model": FAST_MODEL,
        "prompt": FAST_CLASSIFY_PROMPT.format(prompt=prompt[:200]),
        "stream": False,
        "options": {"num_predict": 10, "temperature": 0.1},
    }).encode()
    try:
        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
            response = data.get("response", "").strip().lower()
            # Strip thinking tags if present
            if "<think>" in response:
                response = re.sub(r"<think>.*?</think>", "", response, flags=re.DOTALL).strip()
            # Extract just the category word
            words = response.split()
            if words:
                category = words[0].strip(".,!:;\"'")
                return category if len(category) > 2 else None
    except Exception:
        pass
    return None


# ── Pipeline Stats ────────────────────────────────────────────────────────────

def _record_stage(stage: int):
    """Record which stage resolved this query (for metrics)."""
    if _rc:
        try:
            _rc.hincrby("nova:pipeline:stats", f"stage{stage}", 1)
            _rc.hincrby("nova:pipeline:stats", "total", 1)
        except Exception:
            pass


def get_pipeline_stats() -> dict:
    """Return stage resolution distribution."""
    if not _rc:
        return {}
    try:
        raw = _rc.hgetall("nova:pipeline:stats")
        return {k: int(v) for k, v in raw.items()}
    except Exception:
        return {}


# ── Main Pipeline ─────────────────────────────────────────────────────────────

def classify_and_route(
    prompt: str,
    source: str = "unknown",
    session_id: str = "",
) -> dict:
    """Progressive filtering: classify intent through staged pipeline.

    Returns:
        {
            "intent": str,          # resolved intent name
            "stage": int,           # which stage resolved it (1-4)
            "needs_memory": bool,   # whether memory recall should run
            "needs_full_llm": bool, # whether full inference is needed
            "confidence": float,    # 0.0-1.0
        }
    """
    lower = prompt.lower().strip()

    # ── Stage 1: Regex/keyword (FREE) ──────────────────────────────────────
    for keywords, intent, needs_mem in STAGE1_RULES:
        if any(kw in lower for kw in keywords):
            _record_stage(1)
            return {
                "intent": intent,
                "stage": 1,
                "needs_memory": needs_mem,
                "needs_full_llm": intent in ("coding", "creative_writing", "reasoning", "research"),
                "confidence": 0.9,
            }

    # Short messages that aren't commands are likely conversational
    if len(lower.split()) <= 3:
        _record_stage(1)
        return {
            "intent": "conversation",
            "stage": 1,
            "needs_memory": True,
            "needs_full_llm": True,
            "confidence": 0.6,
        }

    # ── Stage 2: Fast LLM classification (CHEAP, ~500ms) ──────────────────
    fast_intent = _fast_classify(prompt)
    if fast_intent and fast_intent != "other":
        needs_mem = fast_intent in MEMORY_INTENTS
        needs_llm = fast_intent not in ("greeting", "acknowledgement", "command", "quick", "classify")
        _record_stage(2)
        return {
            "intent": fast_intent,
            "stage": 2,
            "needs_memory": needs_mem,
            "needs_full_llm": needs_llm,
            "confidence": 0.75,
        }

    # ── Stage 3: Default to conversation (FULL inference) ──────────────────
    _record_stage(3)
    return {
        "intent": "conversation",
        "stage": 3,
        "needs_memory": True,
        "needs_full_llm": True,
        "confidence": 0.5,
    }


def should_recall_memory(classification: dict) -> bool:
    """Given a classification result, should we run memory recall?"""
    intent = classification.get("intent", "")
    if intent in NO_MEMORY_INTENTS:
        return False
    if intent in MEMORY_INTENTS:
        return True
    return classification.get("needs_memory", True)
