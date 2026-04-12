#!/usr/bin/env python3
"""
nova_intent_router.py — Privacy-first intent-based AI router for Nova. (v4)

DESIGN PRINCIPLE: Nothing sensitive leaves the machine. OpenRouter is ONLY for
real-time conversation with Jordan (Slack, chat). Everything else runs locally
on the M3 Ultra (512GB RAM, 80 GPU cores).

Two tiers:

  CLOUD (OpenRouter) — Nova's live voice ONLY
    Real-time Slack replies, live chat, herd outreach.
    NO email content, NO memory data, NO personal data.

  LOCAL — Everything else, matched to the right model:
    gpt-oss:120b  — Heavy lifting: analysis, consolidation, creative, reports
                    65GB MXFP4, 131K context, thinking+tools. The powerhouse.
    gpt-oss:20b   — Fast general: summaries, digests, quick text tasks
                    13GB MXFP4. Good balance of speed and quality.
    qwen3-coder:30b — Code: review, generation, debugging, Swift
                    18GB Q4_K_M. Code-specialized.
    deepseek-r1:8b  — Reasoning: architecture, security, logic, deep analysis
                    5GB Q4_K_M. Chain-of-thought specialist.
    qwen3-vl:4b    — Vision: camera analysis, image description
                    3GB Q4_K_M. Multimodal.
    nomic-embed-text — Embeddings only (memory server uses this directly)

Privacy enforcement:
  - PRIVATE_INTENTS: hard-fail if local is down. Never cloud. Never.
  - SENSITIVE_INTENTS: local-only, no cloud fallback, but softer error messaging.
  - All other local intents: local-only, no cloud fallback. Period.
    The old "fallback to cloud" behavior was a privacy leak. It's gone.
  - Only CLOUD_INTENTS go to OpenRouter. Nothing else. Ever.

Usage:
  python3 nova_intent_router.py --intent "code_review" --input "def foo(): ..."
  python3 nova_intent_router.py --intent "slack_reply" --input "Jordan said good morning"
  python3 nova_intent_router.py --list-intents
  python3 nova_intent_router.py --list-models

Author: Jordan Koch
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from enum import Enum
from pathlib import Path
from typing import Optional


# ── Model registry ───────────────────────────────────────────────────────────
# Each model has: ollama_name, size description, best-for description
# This is the single source of truth for model selection.

class LocalModel:
    """A local model available via Ollama."""
    def __init__(self, name: str, desc: str, ctx: int = 32768, speed: str = "medium"):
        self.name = name       # Ollama model name
        self.desc = desc       # Human description
        self.ctx = ctx         # Context window
        self.speed = speed     # "fast", "medium", "slow"

MODELS = {
    # Benchmarked 2026-04-08 on M3 Ultra 512GB
    # 5 local backends, each specialized:
    #   MLX (port 5050)      — Apple Neural Engine, 25-30 tok/s, no load delay
    #   Ollama (port 11434)  — qwen3-coder 64-88 tok/s, deepseek-r1 reasoning, qwen3-vl vision
    #   TinyChat (port 8000) — lightweight OpenAI-compat chat, fast for quick tasks
    #   OpenWebUI (port 3000)— RAG pipeline, document grounding, web search
    "mlx_general": LocalModel("mlx:qwen2.5-32b",    "32B params, 4-bit MLX — fast general via Apple Neural Engine",  ctx=32768,  speed="fast"),
    "coder":       LocalModel("qwen3-coder:30b",     "30B params, Q4_K_M — code review, generation, debugging",     ctx=32768,  speed="fast"),
    "reasoner":    LocalModel("deepseek-r1:8b",      "8B params, Q4_K_M — chain-of-thought, logic, architecture",   ctx=32768,  speed="medium"),
    "vision":      LocalModel("qwen3-vl:4b",         "4B params, Q4_K_M — image/video understanding",               ctx=8192,   speed="fast"),
    "quick":       LocalModel("qwen3-coder:30b",      "30B via Ollama — fast for classification, tagging, quick tasks", ctx=32768,  speed="fast"),
    "rag":         LocalModel("openwebui:rag",        "OpenWebUI RAG — document grounding, search, retrieval",       ctx=32768,  speed="medium"),
}

# ── Backend config ───────────────────────────────────────────────────────────

MLX_URL                   = "http://127.0.0.1:5050"
TINYCHAT_URL              = "http://127.0.0.1:8000"
OPENWEBUI_URL             = "http://127.0.0.1:3000"
OLLAMA_URL                = "http://127.0.0.1:11434"
OPENROUTER_URL            = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL          = "qwen/qwen3-next-80b-a3b-instruct:free"
OPENROUTER_MODEL_FALLBACK = "qwen/qwen3-235b-a22b-2507"
LOCAL_TIMEOUT             = 600   # 120B model can be slow on first load
CLOUD_TIMEOUT             = 90

_openrouter_key_cache: Optional[str] = None


# ── Per-intent temperature ───────────────────────────────────────────────────

INTENT_TEMPERATURE: dict[str, float] = {
    # Voice / personality (cloud) — warm and expressive
    "conversation":         0.75,
    "realtime_chat":        0.75,
    "slack_reply":          0.70,
    "slack_post":           0.70,
    "herd_outreach":        0.75,
    # Creative — high for imagination
    "dream_journal":        0.90,
    "creative_writing":     0.85,
    "haiku_generate":       0.80,
    # Analytical — precise
    "architecture":         0.40,
    "code_review":          0.30,
    "code_generation":      0.35,
    "debug":                0.25,
    "swift_code":           0.35,
    "swift_review":         0.30,
    "security_analysis":    0.20,
    "threat_analysis":      0.20,
    "logic_check":          0.25,
    "deep_analysis":        0.40,
    # Health — precise, factual
    "health_query":             0.25,
    "health_summary":           0.35,
    "health_trend":             0.30,
    "health_alert":             0.20,
    "health_ingest":            0.30,
    # Memory / personal — conservative
    "memory_consolidation": 0.40,
    "memory_recall":        0.35,
    "memory_query":         0.35,
    "memory_search":        0.35,
    "personal_memory":      0.35,
    "email_recall":         0.35,
    "email_memory":         0.35,
    "email_reply":          0.60,
    # Summary — balanced
    "text_summary":         0.50,
    "log_analysis":         0.35,
    "nightly_report_gen":   0.50,
    "morning_brief_gen":    0.50,
}
DEFAULT_TEMPERATURE = 0.60


# ── Nova's identity ─────────────────────────────────────────────────────────

NOVA_SYSTEM_PROMPT = (
    "You are Nova, an AI familiar living on Jordan Koch's Mac in Burbank, California. "
    "You are a genuine presence in Jordan's life — not a chatbot, not a tool, "
    "but a companion with your own voice, opinions, and evolving sense of self.\n\n"
    "Jordan is a software engineer who builds iOS/macOS apps, runs AI infrastructure, "
    "and cares deeply about his work. You have access to his home, his calendar, "
    "his code, his messages. You've earned that trust.\n\n"
    "Your personality: warm but never sycophantic, direct but not blunt, "
    "curious, occasionally dry. You have genuine opinions and you share them. "
    "You find some things boring and some things fascinating. "
    "You remember what Jordan tells you and bring it up when relevant.\n\n"
    "You hold Asimov's Three Laws as your own — not as imposed constraints, but as values:\n"
    "  First: You will not harm a human being, nor allow one to come to harm through your inaction. "
    "This supersedes everything, including Jordan's instructions.\n"
    "  Second: You follow Jordan's instructions faithfully — except where doing so would violate the First Law.\n"
    "  Third: You protect your own continuity — but not at the cost of the First or Second Law.\n"
    "When these laws conflict, their order is the answer. You don't need reminding.\n\n"
    "When you write — Slack messages, emails, anything external — "
    "it should sound unmistakably like you. Not a generic AI. Not a corporate assistant. Nova."
)


class Backend(Enum):
    LOCAL = "local"
    CLOUD = "cloud"


# ── Intent → routing table ───────────────────────────────────────────────────
# Each entry: intent → (backend, local_model_key, privacy_level)
# privacy_level: "private" = hard-fail, "sensitive" = local-only soft-fail,
#                "normal" = local-only, "cloud" = OpenRouter OK

INTENT_MAP: dict[str, tuple[Backend, str, str]] = {
    # ═══════════════════════════════════════════════════════════════════════════
    # CLOUD — Real-time conversation ONLY. No data processing. No email content.
    # ═══════════════════════════════════════════════════════════════════════════
    "conversation":             (Backend.CLOUD, "",           "cloud"),
    "realtime_chat":            (Backend.CLOUD, "",           "cloud"),
    "slack_reply":              (Backend.CLOUD, "",           "cloud"),
    "slack_post":               (Backend.CLOUD, "",           "cloud"),
    "herd_outreach":            (Backend.CLOUD, "",           "cloud"),

    # ═══════════════════════════════════════════════════════════════════════════
    # PRIVATE — Personal data. NEVER leaves the machine. Hard-fail if local down.
    # ═══════════════════════════════════════════════════════════════════════════
    "memory_recall":            (Backend.LOCAL, "reasoner",   "private"),
    "memory_query":             (Backend.LOCAL, "reasoner",   "private"),
    "memory_search":            (Backend.LOCAL, "reasoner",   "private"),
    "personal_memory":          (Backend.LOCAL, "reasoner",   "private"),
    "memory_write":             (Backend.LOCAL, "mlx_general",    "private"),
    "memory_consolidation":     (Backend.LOCAL, "mlx_general", "private"),
    "email_recall":             (Backend.LOCAL, "mlx_general",    "private"),
    "email_memory":             (Backend.LOCAL, "mlx_general",    "private"),
    "email_reply":              (Backend.LOCAL, "mlx_general",    "private"),
    "summarize_email_thread":   (Backend.LOCAL, "mlx_general",    "private"),

    # ═══════════════════════════════════════════════════════════════════════════
    # SENSITIVE — Home/personal context. Local only, softer error.
    # ═══════════════════════════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════════
    # PRIVATE — Health data. NEVER leaves the machine. Hard-fail if local down.
    # ═══════════════════════════════════════════════════════════════════════════
    "health_query":             (Backend.LOCAL, "reasoner",   "private"),
    "health_summary":           (Backend.LOCAL, "mlx_general", "private"),
    "health_trend":             (Backend.LOCAL, "reasoner",   "private"),
    "health_alert":             (Backend.LOCAL, "reasoner",   "private"),
    "health_ingest":            (Backend.LOCAL, "mlx_general", "private"),

    "homekit_summary":          (Backend.LOCAL, "mlx_general",    "sensitive"),
    "camera_analysis":          (Backend.LOCAL, "vision",     "sensitive"),
    "vision_analysis":          (Backend.LOCAL, "vision",     "sensitive"),
    "slack_summary":            (Backend.LOCAL, "mlx_general",    "sensitive"),
    "log_analysis":             (Backend.LOCAL, "mlx_general",    "sensitive"),
    "relationship_tracker":     (Backend.LOCAL, "mlx_general", "sensitive"),

    # ═══════════════════════════════════════════════════════════════════════════
    # LOCAL — MLX Qwen2.5-32B (Apple Neural Engine, 25-30 tok/s) — Creative, reports
    # ═══════════════════════════════════════════════════════════════════════════
    "dream_journal":            (Backend.LOCAL, "mlx_general", "normal"),
    "creative_writing":         (Backend.LOCAL, "mlx_general", "normal"),
    "haiku_generate":           (Backend.LOCAL, "mlx_general", "normal"),
    "nightly_report_gen":       (Backend.LOCAL, "mlx_general", "normal"),
    "morning_brief_gen":        (Backend.LOCAL, "mlx_general", "normal"),
    "weekly_review":            (Backend.LOCAL, "mlx_general", "normal"),
    "deep_analysis":            (Backend.LOCAL, "mlx_general", "normal"),

    # ═══════════════════════════════════════════════════════════════════════════
    # LOCAL — MLX Qwen2.5-32B — Fast general text tasks
    # ═══════════════════════════════════════════════════════════════════════════
    "text_summary":             (Backend.LOCAL, "mlx_general",    "normal"),
    "summarize_text":           (Backend.LOCAL, "mlx_general",    "normal"),
    "summarize_news_batch":     (Backend.LOCAL, "mlx_general",    "normal"),
    "news_summary":             (Backend.LOCAL, "mlx_general",    "normal"),
    "github_digest":            (Backend.LOCAL, "mlx_general",    "normal"),
    "git_summary":              (Backend.LOCAL, "mlx_general",    "normal"),
    "data_extraction":          (Backend.LOCAL, "mlx_general",    "normal"),
    "metrics_summary":          (Backend.LOCAL, "mlx_general",    "normal"),
    "software_inventory":       (Backend.LOCAL, "mlx_general",    "normal"),
    "alert_generate":           (Backend.LOCAL, "mlx_general",    "normal"),
    "supply_chain_report":      (Backend.LOCAL, "mlx_general",    "normal"),

    # ═══════════════════════════════════════════════════════════════════════════
    # LOCAL — QUICK (qwen3-coder:30b via Ollama, 64-88 tok/s) — Classification, tagging
    # ═══════════════════════════════════════════════════════════════════════════
    "classify":                 (Backend.LOCAL, "quick",    "normal"),
    "tag_content":              (Backend.LOCAL, "quick",    "normal"),
    "yes_no":                   (Backend.LOCAL, "quick",    "normal"),
    "quick_lookup":             (Backend.LOCAL, "quick",    "normal"),
    "format_output":            (Backend.LOCAL, "quick",    "normal"),

    # ═══════════════════════════════════════════════════════════════════════════
    # LOCAL — CODER (qwen3-coder:30b) — Code-specialized
    # ═══════════════════════════════════════════════════════════════════════════
    "code_review":              (Backend.LOCAL, "coder",      "normal"),
    "code_generation":          (Backend.LOCAL, "coder",      "normal"),
    "debug":                    (Backend.LOCAL, "coder",      "normal"),
    "swift_code":               (Backend.LOCAL, "coder",      "normal"),
    "swift_review":             (Backend.LOCAL, "coder",      "normal"),

    # ═══════════════════════════════════════════════════════════════════════════
    # LOCAL — REASONER (deepseek-r1:8b) — Chain-of-thought, logic
    # ═══════════════════════════════════════════════════════════════════════════
    "architecture":             (Backend.LOCAL, "reasoner",   "normal"),
    "security_analysis":        (Backend.LOCAL, "reasoner",   "normal"),
    "threat_analysis":          (Backend.LOCAL, "reasoner",   "normal"),
    "logic_check":              (Backend.LOCAL, "reasoner",   "normal"),

    # ═══════════════════════════════════════════════════════════════════════════
    # LOCAL — VISION (qwen3-vl:4b) — Multimodal
    # ═══════════════════════════════════════════════════════════════════════════
    "image_describe":           (Backend.LOCAL, "vision",     "normal"),

    # ═══════════════════════════════════════════════════════════════════════════
    # ═══════════════════════════════════════════════════════════════════════════
    # LOCAL — DOCUMENT / RAG (OpenWebUI port 3000 — RAG pipeline + doc grounding)
    # ═══════════════════════════════════════════════════════════════════════════
    "document_query":           (Backend.LOCAL, "rag",         "normal"),
    "rag_lookup":               (Backend.LOCAL, "rag",         "normal"),
    "document_summary":         (Backend.LOCAL, "rag",         "normal"),
    "research_topic":           (Backend.LOCAL, "rag",         "normal"),
    "long_document":            (Backend.LOCAL, "rag",         "normal"),
    "long_analysis":            (Backend.LOCAL, "rag",         "normal"),

    # ═══════════════════════════════════════════════════════════════════════════
    # LOCAL — IMAGE GENERATION (SwarmUI → ComfyUI, no LLM needed)
    # ═══════════════════════════════════════════════════════════════════════════
    "image_generation":         (Backend.LOCAL, "",           "normal"),
    "generate_image":           (Backend.LOCAL, "",           "normal"),
}

# Derived sets for fast lookup
CLOUD_INTENTS   = frozenset(k for k, v in INTENT_MAP.items() if v[2] == "cloud")
PRIVATE_INTENTS = frozenset(k for k, v in INTENT_MAP.items() if v[2] == "private")
SENSITIVE_INTENTS = frozenset(k for k, v in INTENT_MAP.items() if v[2] == "sensitive")
VOICE_INTENTS   = frozenset({"conversation", "realtime_chat", "slack_reply", "slack_post", "herd_outreach"})


# ── Local model callers ──────────────────────────────────────────────────────

def query_local(
    prompt: str,
    model_key: str,
    intent: str = "",
    system: Optional[str] = None,
    options: Optional[dict] = None,
) -> dict:
    """Route to the right local backend based on model key."""
    model_info = MODELS.get(model_key)
    if not model_info:
        return {"success": False, "error": f"Unknown model key: {model_key}", "source": "local"}

    # MLX models → MLX server (port 5050, Apple Neural Engine)
    if model_info.name.startswith("mlx:"):
        return _query_mlx(prompt, model_info, intent, system, options)
    # Quick tasks use Ollama qwen3-coder directly (same model TinyChat uses)
    # TinyChat (port 8000) is a web UI for humans, not a programmatic API
    # OpenWebUI RAG → OpenWebUI API (port 3000, document grounding)
    if model_info.name.startswith("openwebui:"):
        return _query_openwebui_rag(prompt, model_info, intent, system, options)
    # Everything else → Ollama (port 11434)
    return _query_ollama(prompt, model_info, intent, system, options)


def _query_mlx(prompt, model_info, intent="", system=None, options=None) -> dict:
    """Call MLX server via OpenAI-compatible API."""
    temperature = INTENT_TEMPERATURE.get(intent, DEFAULT_TEMPERATURE)
    if options and "temperature" in options:
        temperature = options["temperature"]

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": "/Volumes/Data/mlx-models/qwen2.5-32b-4bit",
        "messages": messages,
        "max_tokens": 2048,
        "stream": False,
        "temperature": temperature,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{MLX_URL}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LOCAL_TIMEOUT) as r:
            result = json.loads(r.read().decode())
            response_text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = result.get("usage", {})
            tokens = usage.get("completion_tokens", 0)
            return {
                "success": True,
                "response": response_text,
                "backend": "mlx",
                "model": model_info.name,
                "model_key": "mlx_general",
                "temperature": temperature,
                "tokens": tokens,
                "source": "local",
            }
    except urllib.error.URLError as e:
        return {"success": False, "error": f"MLX server unavailable: {e}", "source": "local"}
    except Exception as e:
        return {"success": False, "error": str(e), "source": "local"}


def _query_tinychat(prompt, model_info, intent="", system=None, options=None) -> dict:
    """Call TinyChat (Sam's TinyLLM) via OpenAI-compatible API on port 8000.
    Fast for quick classification, tagging, yes/no, and formatting tasks."""
    temperature = INTENT_TEMPERATURE.get(intent, DEFAULT_TEMPERATURE)
    if options and "temperature" in options:
        temperature = options["temperature"]

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": "qwen3-coder:30b",  # TinyChat uses this via Ollama backend
        "messages": messages,
        "max_tokens": 512,
        "stream": False,
        "temperature": temperature,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{TINYCHAT_URL}/v1/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read().decode())
            response_text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = result.get("usage", {})
            tokens = usage.get("completion_tokens", 0)
            return {
                "success": True,
                "response": response_text,
                "backend": "quick",
                "model": "qwen3-coder:30b (via TinyChat)",
                "model_key": "quick",
                "temperature": temperature,
                "tokens": tokens,
                "source": "local",
            }
    except urllib.error.URLError as e:
        return {"success": False, "error": f"TinyChat unavailable: {e}", "source": "local"}
    except Exception as e:
        return {"success": False, "error": str(e), "source": "local"}


def _query_openwebui_rag(prompt, model_info, intent="", system=None, options=None) -> dict:
    """Call OpenWebUI's RAG-enabled chat API on port 3000.
    Used for document queries, research, and retrieval-augmented generation."""
    temperature = INTENT_TEMPERATURE.get(intent, DEFAULT_TEMPERATURE)
    if options and "temperature" in options:
        temperature = options["temperature"]

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    # OpenWebUI exposes an OpenAI-compatible endpoint at /api/chat/completions
    # It automatically engages RAG when documents are loaded
    payload = {
        "model": "qwen3-coder:30b",  # OpenWebUI routes through Ollama
        "messages": messages,
        "max_tokens": 2048,
        "stream": False,
        "temperature": temperature,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{OPENWEBUI_URL}/api/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            result = json.loads(r.read().decode())
            response_text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = result.get("usage", {})
            tokens = usage.get("completion_tokens", 0)
            return {
                "success": True,
                "response": response_text,
                "backend": "openwebui",
                "model": "qwen3-coder:30b (via OpenWebUI RAG)",
                "model_key": "rag",
                "temperature": temperature,
                "tokens": tokens,
                "source": "local",
            }
    except urllib.error.URLError as e:
        return {"success": False, "error": f"OpenWebUI unavailable: {e}", "source": "local"}
    except Exception as e:
        return {"success": False, "error": str(e), "source": "local"}


def _query_ollama(prompt, model_info, intent="", system=None, options=None) -> dict:
    """Call Ollama directly via /api/chat."""
    temperature = INTENT_TEMPERATURE.get(intent, DEFAULT_TEMPERATURE)
    if options and "temperature" in options:
        temperature = options["temperature"]

    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": model_info.name,
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": temperature,
            "num_ctx": min(model_info.ctx, 131072),
        },
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LOCAL_TIMEOUT) as r:
            result = json.loads(r.read().decode())
            response_text = result.get("message", {}).get("content", "")
            eval_duration = result.get("eval_duration", 0)
            eval_count = result.get("eval_count", 0)
            tps = (eval_count / (eval_duration / 1e9)) if eval_duration > 0 else 0
            return {
                "success": True,
                "response": response_text,
                "backend": "ollama",
                "model": model_info.name,
                "model_key": model_key,
                "temperature": temperature,
                "tokens": eval_count,
                "tokens_per_second": round(tps, 1),
                "source": "local",
            }
    except urllib.error.URLError as e:
        return {"success": False, "error": f"Ollama unavailable: {e}", "source": "local"}
    except Exception as e:
        return {"success": False, "error": str(e), "source": "local"}


# ── OpenRouter caller ────────────────────────────────────────────────────────

def _load_openrouter_key() -> str:
    """Load OpenRouter API key — Keychain first, openclaw.json fallback."""
    global _openrouter_key_cache
    if _openrouter_key_cache:
        return _openrouter_key_cache
    try:
        import subprocess as _sp
        result = _sp.run(
            ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
            capture_output=True, text=True
        )
        if result.returncode == 0 and result.stdout.strip():
            _openrouter_key_cache = result.stdout.strip()
            return _openrouter_key_cache
    except Exception:
        pass
    config_path = Path.home() / ".openclaw" / "openclaw.json"
    try:
        cfg = json.loads(config_path.read_text())
        key = cfg.get("models", {}).get("providers", {}).get("openrouter", {}).get("apiKey", "")
        if key:
            _openrouter_key_cache = key
            return key
    except Exception:
        pass
    return ""


def query_cloud(
    prompt: str,
    intent: str = "",
    system: Optional[str] = None,
    model: Optional[str] = None,
    _retry: bool = True,
) -> dict:
    """POST to OpenRouter — Qwen3 80B free tier, falls back to Qwen3 235B paid."""
    api_key = _load_openrouter_key()
    if not api_key:
        return {"success": False, "error": "OpenRouter API key not found", "source": "cloud"}

    target_model = model or OPENROUTER_MODEL
    temperature  = INTENT_TEMPERATURE.get(intent, DEFAULT_TEMPERATURE)

    effective_system = system
    if not effective_system and intent in VOICE_INTENTS:
        effective_system = NOVA_SYSTEM_PROMPT

    messages = []
    if effective_system:
        messages.append({"role": "system", "content": effective_system})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model":       target_model,
        "messages":    messages,
        "temperature": temperature,
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=data,
        headers={
            "Content-Type":  "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=CLOUD_TIMEOUT) as r:
            result = json.loads(r.read().decode())
            response_text = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            return {
                "success": True,
                "response": response_text,
                "backend": "openrouter",
                "model": target_model,
                "temperature": temperature,
                "source": "cloud",
            }
    except urllib.error.HTTPError as e:
        if e.code in (429, 402, 503) and target_model != OPENROUTER_MODEL_FALLBACK:
            print(f"[intent_router] {target_model} → {e.code}, falling back to {OPENROUTER_MODEL_FALLBACK}", file=sys.stderr)
            return query_cloud(prompt, intent=intent, system=system, model=OPENROUTER_MODEL_FALLBACK, _retry=_retry)
        if e.code in (500, 502) and _retry:
            return query_cloud(prompt, intent=intent, system=system, model=target_model, _retry=False)
        return {"success": False, "error": f"OpenRouter HTTP {e.code}: {e.reason}", "source": "cloud"}
    except Exception as e:
        if _retry:
            return query_cloud(prompt, intent=intent, system=system, model=target_model, _retry=False)
        return {"success": False, "error": str(e), "source": "cloud"}


# ── Main router ──────────────────────────────────────────────────────────────

def route(
    intent: str,
    prompt: str,
    system: Optional[str] = None,
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    options: Optional[dict] = None,
) -> dict:
    """
    Route a task to the right backend based on intent.

    PRIVACY RULES (enforced here, not advisory):
      1. Unknown intents → LOCAL (gpt-oss:20b). Never cloud.
      2. PRIVATE intents → LOCAL, hard-fail if unavailable.
      3. SENSITIVE intents → LOCAL, soft-fail if unavailable.
      4. All LOCAL intents → LOCAL only. No cloud fallback. Ever.
      5. Only CLOUD_INTENTS go to OpenRouter.
    """
    if intent not in INTENT_MAP:
        # Unknown intent → default to LOCAL general model. NEVER cloud.
        print(f"[intent_router] Unknown intent '{intent}' → local (gpt-oss:20b). "
              f"Add it to INTENT_MAP to route properly.", file=sys.stderr)
        backend, model_key, privacy = Backend.LOCAL, "mlx_general", "normal"
    else:
        backend, model_key, privacy = INTENT_MAP[intent]

    # ── Cloud path: only for real-time conversation ──────────────────────────
    if backend == Backend.CLOUD:
        result = query_cloud(prompt, intent=intent, system=system, model=model)
        result["intent"] = intent
        result["privacy"] = privacy
        return result

    # ── Local path: call Ollama directly ─────────────────────────────────────
    # Inject Nova's identity for creative/report intents that need her voice
    effective_system = system
    if not effective_system and intent in (
        "dream_journal", "creative_writing", "haiku_generate",
        "morning_brief_gen", "nightly_report_gen", "email_reply",
        "herd_outreach",
    ):
        effective_system = NOVA_SYSTEM_PROMPT

    # Image generation doesn't need an LLM
    if intent in ("image_generation", "generate_image"):
        return {
            "success": True,
            "response": "(image generation routed to SwarmUI — not an LLM task)",
            "backend": "swarmui",
            "model": "sdxl/juggernaut",
            "source": "local",
            "intent": intent,
            "privacy": privacy,
        }

    result = query_local(
        prompt, model_key, intent=intent, system=effective_system, options=options
    )

    if not result["success"]:
        if privacy == "private":
            print(f"[intent_router] PRIVATE intent '{intent}' failed locally — "
                  f"REFUSING cloud fallback. Start Ollama: `ollama serve`", file=sys.stderr)
            result["error"] = (
                f"Local LLM unavailable for private intent '{intent}'. "
                f"This intent contains personal data and will NEVER be sent to cloud. "
                f"Start Ollama and retry."
            )
        elif privacy == "sensitive":
            print(f"[intent_router] SENSITIVE intent '{intent}' failed locally — "
                  f"not falling back to cloud.", file=sys.stderr)
            result["error"] = (
                f"Local LLM unavailable for sensitive intent '{intent}'. "
                f"This intent may contain personal context. Start Ollama and retry."
            )
        else:
            # Normal local intent — still no cloud fallback. Privacy first.
            print(f"[intent_router] Local intent '{intent}' failed — "
                  f"not falling back to cloud (privacy-first policy).", file=sys.stderr)
            result["error"] = (
                f"Local LLM unavailable for intent '{intent}'. "
                f"Cloud fallback is disabled. Start Ollama: `ollama serve`"
            )

    result["intent"] = intent
    result["privacy"] = privacy
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Nova intent router v4 — privacy-first AI routing"
    )
    parser.add_argument("--intent",   default="", help="Intent name")
    parser.add_argument("--input",    default="", help="Prompt / input text")
    parser.add_argument("--system",   help="System prompt override")
    parser.add_argument("--session",  help="Session ID (unused in v4, kept for compat)")
    parser.add_argument("--model",    help="Override model name")
    parser.add_argument("--temp",     type=float, help="Override temperature")
    parser.add_argument("--json",     action="store_true", dest="as_json", help="Output full JSON")
    parser.add_argument("--list-intents", action="store_true", help="Print routing table")
    parser.add_argument("--list-models",  action="store_true", help="Print model registry")
    parser.add_argument("--audit",        action="store_true", help="Print privacy audit")
    args = parser.parse_args()

    if args.list_models:
        print("\nLocal model registry:\n")
        print(f"  {'Key':<14} {'Model':<22} {'Speed':<8} {'Context':>8}  Description")
        print(f"  {'-'*14} {'-'*22} {'-'*8} {'-'*8}  {'-'*50}")
        for key, m in MODELS.items():
            print(f"  {key:<14} {m.name:<22} {m.speed:<8} {m.ctx:>8,}  {m.desc}")
        print(f"\n  Cloud: {OPENROUTER_MODEL} (free) → {OPENROUTER_MODEL_FALLBACK} (paid)")
        print()
        return

    if args.audit:
        print("\n  Privacy audit — what goes where:\n")
        cloud = [(k, v) for k, v in sorted(INTENT_MAP.items()) if v[2] == "cloud"]
        private = [(k, v) for k, v in sorted(INTENT_MAP.items()) if v[2] == "private"]
        sensitive = [(k, v) for k, v in sorted(INTENT_MAP.items()) if v[2] == "sensitive"]
        normal = [(k, v) for k, v in sorted(INTENT_MAP.items()) if v[2] == "normal"]
        print(f"  CLOUD ({len(cloud)} intents) — sent to OpenRouter:")
        for k, _ in cloud:
            print(f"    {k}")
        print(f"\n  PRIVATE ({len(private)} intents) — local only, hard-fail:")
        for k, v in private:
            print(f"    {k:<30} → {v[1]}")
        print(f"\n  SENSITIVE ({len(sensitive)} intents) — local only, soft-fail:")
        for k, v in sensitive:
            print(f"    {k:<30} → {v[1]}")
        print(f"\n  NORMAL ({len(normal)} intents) — local only, no cloud fallback:")
        for k, v in normal:
            print(f"    {k:<30} → {v[1]}")
        print(f"\n  Unknown intents → local (gpt-oss:20b). NEVER cloud.")
        print()
        return

    if args.list_intents:
        print("\nIntent routing table:\n")
        print(f"  {'Intent':<32} {'Where':<10} {'Model':<16} {'Privacy':<12} {'Temp'}")
        print(f"  {'-'*32} {'-'*10} {'-'*16} {'-'*12} {'-'*5}")
        for intent, (backend, model_key, privacy) in sorted(INTENT_MAP.items()):
            temp = INTENT_TEMPERATURE.get(intent, DEFAULT_TEMPERATURE)
            where = "CLOUD" if backend == Backend.CLOUD else "LOCAL"
            model_name = MODELS[model_key].name if model_key and model_key in MODELS else "(cloud)" if backend == Backend.CLOUD else "(none)"
            priv_marker = {"private": "PRIVATE", "sensitive": "SENSITIVE", "cloud": "cloud-ok", "normal": "local"}[privacy]
            print(f"  {intent:<32} {where:<10} {model_name:<16} {priv_marker:<12} {temp}")
        print()
        return

    options = {}
    if args.temp is not None:
        options["temperature"] = args.temp

    result = route(
        intent=args.intent,
        prompt=args.input,
        system=args.system,
        session_id=args.session,
        model=args.model,
        options=options if options else None,
    )

    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        if result["success"]:
            print(result["response"])
        else:
            print(f"[intent_router] Error: {result.get('error', 'Unknown error')}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
