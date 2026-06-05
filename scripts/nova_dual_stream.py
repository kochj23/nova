#!/usr/bin/env python3
"""
nova_dual_stream.py — Dual-stream processing (Frigate sub-stream/main-stream pattern).

Analyze the CHEAP version for routing decisions.
Store the FULL version for deep recall later.

Pattern:
  - Email: parse subject/sender/urgency for routing (free), store full body async
  - Logs: parse headers for anomaly detection (free), store full context for root-cause
  - Messages: extract intent from first 200 chars (cheap), store full message in memory

This prevents burning expensive inference on content that only needs metadata-level
processing for routing/triage decisions.

Usage:
  from nova_dual_stream import extract_metadata, store_full_async

  meta = extract_metadata(email_body, source="email")
  # meta = {"subject": "...", "sender": "...", "urgency": "low", "length": 1234}
  # Route based on meta — no LLM needed

  store_full_async(email_body, source="email_archive", metadata=meta)
  # Full content queued for async vectorization (P4 priority)

Written by Jordan Koch.
"""

import hashlib
import json
import re
import urllib.request
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────

MEMORY_URL = "http://192.168.1.6:18790/remember?async=1"
INFERENCE_QUEUE_URL = "http://127.0.0.1:37470/queue/submit"

# Urgency keywords (case-insensitive)
_URGENT_WORDS = re.compile(
    r"\b(urgent|critical|emergency|asap|immediately|down|outage|incident|"
    r"security|breach|failed|broken|blocked|deadline)\b", re.IGNORECASE
)
_LOW_PRIORITY_WORDS = re.compile(
    r"\b(newsletter|unsubscribe|promotional|no-?reply|automated|digest|"
    r"weekly update|monthly report|notification preferences)\b", re.IGNORECASE
)


# ── Metadata Extraction (FREE — no LLM) ──────────────────────────────────────

def extract_metadata(content: str, source: str = "unknown", extra: dict = None) -> dict:
    """Extract routing-relevant metadata from content without any LLM call.

    Returns dict with: subject, sender, urgency, length, word_count, has_code,
    language_hint, source, content_hash
    """
    extra = extra or {}
    lines = content.split("\n")
    first_line = lines[0].strip() if lines else ""

    # Subject extraction
    subject = extra.get("subject", "")
    if not subject:
        if source == "email" and lines:
            for line in lines[:5]:
                if line.lower().startswith("subject:"):
                    subject = line[8:].strip()
                    break
        if not subject:
            subject = first_line[:100]

    # Sender extraction
    sender = extra.get("sender", "")
    if not sender and source == "email":
        for line in lines[:10]:
            if line.lower().startswith("from:"):
                sender = line[5:].strip()
                break

    # Urgency classification
    urgency = "normal"
    if _URGENT_WORDS.search(content[:500]):
        urgency = "high"
    elif _LOW_PRIORITY_WORDS.search(content[:500]):
        urgency = "low"

    # Code detection
    has_code = bool(re.search(r"```|def |class |function |import |#include", content))

    # Language hint (very rough)
    language_hint = "en"
    if re.search(r"[àâäéèêëïîôùûüÿç]", content[:200]):
        language_hint = "fr"
    elif re.search(r"[äöüß]", content[:200]):
        language_hint = "de"

    return {
        "subject": subject[:200],
        "sender": sender[:100],
        "urgency": urgency,
        "length": len(content),
        "word_count": len(content.split()),
        "has_code": has_code,
        "language_hint": language_hint,
        "source": source,
        "content_hash": hashlib.md5(content[:1000].encode()).hexdigest()[:12],
        "preview": content[:200].replace("\n", " ").strip(),
    }


def should_process_immediately(metadata: dict) -> bool:
    """Based on metadata alone, does this need immediate LLM processing?"""
    if metadata["urgency"] == "high":
        return True
    if metadata["source"] in ("imessage", "slack_dm"):
        return True
    return False


def routing_priority(metadata: dict) -> int:
    """Determine inference queue priority from metadata. P1=highest, P4=lowest."""
    if metadata["urgency"] == "high":
        return 1
    if metadata["source"] in ("imessage", "slack_dm", "chatroom"):
        return 1
    if metadata["source"] in ("email", "slack"):
        return 2
    if metadata["urgency"] == "low":
        return 4
    return 3


# ── Full Content Storage (ASYNC — background vectorization) ───────────────────

def store_full_async(content: str, source: str, metadata: dict = None, tier: str = "standard"):
    """Queue full content for async vectorization. Never blocks the caller."""
    metadata = metadata or {}
    payload = json.dumps({
        "text": content[:5000],
        "source": source,
        "metadata": metadata,
        "tier": tier,
    }).encode()

    req = urllib.request.Request(
        MEMORY_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False


def process_dual_stream(content: str, source: str, extra: dict = None) -> dict:
    """Full dual-stream processing: extract metadata + queue full content.

    Returns metadata dict. Full content is queued asynchronously.
    Call this instead of directly ingesting — it handles both streams.
    """
    meta = extract_metadata(content, source, extra)
    store_full_async(content, source, meta)
    return meta
