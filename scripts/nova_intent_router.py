#!/usr/bin/env python3
"""
nova_intent_router.py — Unified intent-based AI router for Nova. (v3)

Two tiers:
  CLOUD (OpenRouter Qwen3 80B free → Qwen3 235B paid fallback)
    — Nova's voice, personality, external comms

  LOCAL (Nova-NextGen :34750) — All compute: 7 backends, intent-matched
    tinychat  :8000  → quick/classify — fastest, qwen3:4b, low overhead
    mlxcode   :37422 → coding/swift   — Apple Neural Engine, Swift-specialised
    mlxchat   :5000  → general/creative/summarize — fast ANE general inference
    openwebui :3000  → document/research — RAG-capable, doc grounding
    ollama    :11434 → reasoning/analysis/vision/long_context — deepseek-r1, qwen3-vl
    swarmui   :7801  → image           — primary image generation
    comfyui   :8188  → image           — image fallback

Intent → Route mapping:
  CLOUD:
    conversation, realtime_chat, email_reply, slack_reply, slack_post,
    herd_outreach, creative_writing

  LOCAL → creative (Ollama qwen3-coder:30b):
    dream_journal

  LOCAL → quick:
    classify, tag_content, yes_no, quick_lookup, format_output

  LOCAL → coding:
    code_review, code_generation, debug

  LOCAL → swift:
    swift_code, swift_review

  LOCAL → general (MLXChat):
    text_summary, news_summary, github_digest, git_summary,
    log_analysis, metrics_summary, weekly_review, software_inventory,
    morning_brief_gen, nightly_report_gen, data_extraction,
    alert_generate, homekit_summary, slack_summary

  LOCAL → summarize (MLXChat):
    summarize_text, summarize_email_thread, summarize_news_batch, memory_write

  LOCAL → document (OpenWebUI):
    document_query, rag_lookup, research_topic, document_summary

  LOCAL → reasoning (Ollama deepseek-r1):
    architecture, memory_consolidation, deep_analysis, security_analysis,
    threat_analysis, logic_check

  LOCAL → vision (Ollama qwen3-vl):
    vision_analysis, image_describe, camera_analysis

  LOCAL → image:
    image_generation, generate_image

  LOCAL → long_context (Ollama deepseek-v3.1):
    long_document, long_analysis

Usage:
  python3 nova_intent_router.py --intent "code_review" --input "def foo(): ..."
  python3 nova_intent_router.py --intent "dream_journal" --input "tonight's themes..."
  python3 nova_intent_router.py --intent "slack_reply" --input "Jordan said good morning"
  python3 nova_intent_router.py --list-intents

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


# ── Backend config ────────────────────────────────────────────────────────────

NOVA_NEXTGEN_URL          = "http://127.0.0.1:34750"
OPENROUTER_URL            = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODEL          = "qwen/qwen3-next-80b-a3b-instruct:free"  # Qwen3 80B — free tier
OPENROUTER_MODEL_FALLBACK = "qwen/qwen3-235b-a22b-2507"              # Qwen3 235B MoE — paid fallback
LOCAL_TIMEOUT             = 300
CLOUD_TIMEOUT             = 90   # generous for free-tier latency spikes

# Module-level API key cache — loaded once per process, not on every call
_openrouter_key_cache: Optional[str] = None


# ── Per-intent temperature ────────────────────────────────────────────────────

INTENT_TEMPERATURE: dict[str, float] = {
    # Voice / personality
    "conversation":         0.75,
    "realtime_chat":        0.75,
    "slack_reply":          0.70,
    "slack_post":           0.70,
    "email_reply":          0.65,
    "herd_outreach":        0.75,
    # Creative — high for imagination, surrealism
    "dream_journal":        0.90,
    "creative_writing":     0.90,
    # Analytical — lower for precision
    "architecture":         0.40,
    "code_review":          0.30,
    "code_generation":      0.35,
    "debug":                0.30,
    "swift_code":           0.35,
    "swift_review":         0.30,
    "memory_consolidation": 0.40,
    "deep_analysis":        0.40,
    "security_analysis":    0.30,
    "threat_analysis":      0.30,
    "logic_check":          0.30,
    "log_analysis":         0.35,
}
DEFAULT_TEMPERATURE = 0.70


# ── Nova's identity — injected automatically for voice intents ────────────────

VOICE_INTENTS = frozenset({
    "conversation", "realtime_chat", "email_reply", "slack_reply",
    "slack_post", "herd_outreach", "creative_writing",
})

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
    "When you write — Slack messages, emails, anything external — "
    "it should sound unmistakably like you. Not a generic AI. Not a corporate assistant. Nova."
)


class Backend(Enum):
    LOCAL  = "local"   # Nova-NextGen gateway
    CLOUD  = "cloud"   # OpenRouter Qwen3 80B (free) → Qwen3 235B (paid fallback)


# ── Intent → routing table ────────────────────────────────────────────────────

# Maps intent name → (backend, task_type_for_gateway)
# task_type is only used when backend == LOCAL
INTENT_MAP: dict[str, tuple[Backend, str]] = {
    # ── Cloud: Nova's voice, personality, external comms ──────────────────────
    "conversation":             (Backend.CLOUD, ""),
    "realtime_chat":            (Backend.CLOUD, ""),
    "email_reply":              (Backend.CLOUD, ""),
    "slack_reply":              (Backend.CLOUD, ""),
    "slack_post":               (Backend.CLOUD, ""),
    "herd_outreach":            (Backend.CLOUD, ""),
    "creative_writing":         (Backend.CLOUD, ""),

    # ── Local: creative (Ollama qwen3-coder:30b — imaginative, surreal) ───────
    "dream_journal":            (Backend.LOCAL, "creative"),

    # ── Local: quick / classify (TinyChat — low-latency) ─────────────────────
    "classify":                 (Backend.LOCAL, "quick"),
    "tag_content":              (Backend.LOCAL, "quick"),
    "yes_no":                   (Backend.LOCAL, "quick"),
    "quick_lookup":             (Backend.LOCAL, "quick"),
    "format_output":            (Backend.LOCAL, "quick"),

    # ── Local: code (MLXCode → MLXChat → Ollama) ──────────────────────────────
    "code_review":              (Backend.LOCAL, "coding"),
    "code_generation":          (Backend.LOCAL, "coding"),
    "debug":                    (Backend.LOCAL, "coding"),
    "swift_code":               (Backend.LOCAL, "swift"),
    "swift_review":             (Backend.LOCAL, "swift"),

    # ── Local: general text (MLXChat — Apple ANE fast inference) ──────────────
    "text_summary":             (Backend.LOCAL, "general"),
    "news_summary":             (Backend.LOCAL, "general"),
    "github_digest":            (Backend.LOCAL, "general"),
    "git_summary":              (Backend.LOCAL, "general"),
    "log_analysis":             (Backend.LOCAL, "general"),
    "data_extraction":          (Backend.LOCAL, "general"),
    "metrics_summary":          (Backend.LOCAL, "general"),
    "weekly_review":            (Backend.LOCAL, "general"),
    "software_inventory":       (Backend.LOCAL, "general"),
    "morning_brief_gen":        (Backend.LOCAL, "general"),
    "nightly_report_gen":       (Backend.LOCAL, "general"),
    "alert_generate":           (Backend.LOCAL, "general"),
    "homekit_summary":          (Backend.LOCAL, "general"),
    "slack_summary":            (Backend.LOCAL, "general"),

    # ── Local: summarize (MLXChat) ─────────────────────────────────────────────
    "summarize_text":           (Backend.LOCAL, "summarize"),
    "summarize_email_thread":   (Backend.LOCAL, "summarize"),
    "summarize_news_batch":     (Backend.LOCAL, "summarize"),
    "memory_write":             (Backend.LOCAL, "summarize"),

    # ── Local: document / RAG (OpenWebUI) ─────────────────────────────────────
    "document_query":           (Backend.LOCAL, "document"),
    "rag_lookup":               (Backend.LOCAL, "document"),
    "document_summary":         (Backend.LOCAL, "document"),
    "research_topic":           (Backend.LOCAL, "research"),

    # ── Local: reasoning (Ollama deepseek-r1:8b) ──────────────────────────────
    "architecture":             (Backend.LOCAL, "reasoning"),
    "memory_consolidation":     (Backend.LOCAL, "reasoning"),
    "deep_analysis":            (Backend.LOCAL, "reasoning"),
    "security_analysis":        (Backend.LOCAL, "reasoning"),
    "threat_analysis":          (Backend.LOCAL, "reasoning"),
    "logic_check":              (Backend.LOCAL, "reasoning"),

    # ── Local: vision (Ollama qwen3-vl:4b) ────────────────────────────────────
    "vision_analysis":          (Backend.LOCAL, "vision"),
    "image_describe":           (Backend.LOCAL, "vision"),
    "camera_analysis":          (Backend.LOCAL, "vision"),

    # ── Local: image generation (SwarmUI → ComfyUI) ───────────────────────────
    "image_generation":         (Backend.LOCAL, "image"),
    "generate_image":           (Backend.LOCAL, "image"),

    # ── Local: long context (Ollama deepseek-v3.1:671b-cloud) ─────────────────
    "long_document":            (Backend.LOCAL, "long_context"),
    "long_analysis":            (Backend.LOCAL, "long_context"),
}


# ── Nova-NextGen caller ───────────────────────────────────────────────────────

def query_local(
    prompt: str,
    task_type: str,
    intent: str = "",
    session_id: Optional[str] = None,
    model: Optional[str] = None,
    system: Optional[str] = None,
    options: Optional[dict] = None,
) -> dict:
    """POST to Nova-NextGen gateway."""
    # Merge per-intent temperature into options
    temperature = INTENT_TEMPERATURE.get(intent, DEFAULT_TEMPERATURE)
    merged_options: dict = {"temperature": temperature}
    if options:
        merged_options.update(options)

    payload: dict = {
        "query": prompt,
        "task_type": task_type,
        "options": merged_options,
    }
    if session_id:
        payload["session_id"] = session_id
    if model:
        payload["model"] = model
    if system:
        # Inject system prompt as a prefix — gateway doesn't have a system field
        payload["query"] = f"[System: {system}]\n\n{prompt}"

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{NOVA_NEXTGEN_URL}/api/ai/query",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=LOCAL_TIMEOUT) as r:
            result = json.loads(r.read().decode())
            return {
                "success": True,
                "response": result.get("response", ""),
                "backend": result.get("backend_used", "unknown"),
                "model": result.get("model_used"),
                "task_type": result.get("task_type"),
                "tokens_per_second": result.get("tokens_per_second"),
                "fallback_used": result.get("fallback_used", False),
                "source": "local",
            }
    except urllib.error.URLError as e:
        return {"success": False, "error": f"Nova-NextGen unavailable: {e}", "source": "local"}
    except Exception as e:
        return {"success": False, "error": str(e), "source": "local"}


# ── OpenRouter caller ─────────────────────────────────────────────────────────

def _load_openrouter_key() -> str:
    """Load OpenRouter API key — cached after first read."""
    global _openrouter_key_cache
    if _openrouter_key_cache:
        return _openrouter_key_cache
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
        return {"success": False, "error": "OpenRouter API key not found in openclaw.json", "source": "cloud"}

    target_model = model or OPENROUTER_MODEL
    temperature  = INTENT_TEMPERATURE.get(intent, DEFAULT_TEMPERATURE)

    # Auto-inject Nova's identity for voice intents (unless caller provided their own)
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
        # Rate-limited, quota, or model unavailable — step up to paid fallback
        if e.code in (429, 402, 503) and target_model != OPENROUTER_MODEL_FALLBACK:
            print(f"[nova_intent_router] {target_model} → {e.code}, falling back to {OPENROUTER_MODEL_FALLBACK}", file=sys.stderr)
            return query_cloud(prompt, intent=intent, system=system, model=OPENROUTER_MODEL_FALLBACK, _retry=_retry)
        # Transient server error — one retry before giving up
        if e.code in (500, 502) and _retry:
            print(f"[nova_intent_router] {target_model} → {e.code} (transient), retrying once", file=sys.stderr)
            return query_cloud(prompt, intent=intent, system=system, model=target_model, _retry=False)
        return {"success": False, "error": f"OpenRouter HTTP {e.code}: {e.reason}", "source": "cloud"}
    except Exception as e:
        # Connection reset / timeout — one retry
        if _retry:
            print(f"[nova_intent_router] {target_model} connection error ({e}), retrying once", file=sys.stderr)
            return query_cloud(prompt, intent=intent, system=system, model=target_model, _retry=False)
        return {"success": False, "error": str(e), "source": "cloud"}


# ── Main router ───────────────────────────────────────────────────────────────

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
    Returns dict with: success, response, backend, model, source, intent, temperature
    """
    if intent not in INTENT_MAP:
        # Unknown intent — default to cloud so nothing is silently swallowed
        print(f"[nova_intent_router] Unknown intent '{intent}', defaulting to cloud", file=sys.stderr)
        backend, task_type = Backend.CLOUD, ""
    else:
        backend, task_type = INTENT_MAP[intent]

    if backend == Backend.LOCAL:
        result = query_local(prompt, task_type, intent=intent, session_id=session_id,
                             model=model, system=system, options=options)
        if not result["success"]:
            print(f"[nova_intent_router] intent={intent} local failed ({result.get('error')}), falling back to cloud", file=sys.stderr)
            result = query_cloud(prompt, intent=intent, system=system)
    else:
        result = query_cloud(prompt, intent=intent, system=system, model=model)

    result["intent"] = intent
    return result


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Nova intent router — routes AI tasks to the right backend")
    parser.add_argument("--intent",   default="", help=f"Intent name. Options: {', '.join(sorted(INTENT_MAP.keys()))}")
    parser.add_argument("--input",    default="", help="Prompt / input text")
    parser.add_argument("--system",   help="System prompt override (optional)")
    parser.add_argument("--session",  help="Session ID for Nova-NextGen context bus")
    parser.add_argument("--model",    help="Override model name")
    parser.add_argument("--temp",     type=float, help="Override temperature")
    parser.add_argument("--json",     action="store_true", dest="as_json", help="Output full JSON result")
    parser.add_argument("--list-intents", action="store_true", help="Print all intents and exit")
    args = parser.parse_args()

    if args.list_intents:
        print("\nIntent routing table:\n")
        print(f"  {'Intent':<32} {'Backend':<10} {'Task Type':<18} {'Temp'}")
        print(f"  {'-'*32} {'-'*10} {'-'*18} {'-'*6}")
        for intent, (backend, task_type) in sorted(INTENT_MAP.items()):
            temp = INTENT_TEMPERATURE.get(intent, DEFAULT_TEMPERATURE)
            print(f"  {intent:<32} {backend.value:<10} {task_type or '(cloud native)':<18} {temp}")
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
            print(f"[nova_intent_router] Error: {result.get('error', 'Unknown error')}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
