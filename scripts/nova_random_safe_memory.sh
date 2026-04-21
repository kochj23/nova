#!/bin/bash
# nova_random_safe_memory.sh — Return a safe, non-PII memory for email footers.
#
# Tries semantic recall on the email topic first; returns a relevant, safe memory fragment from vector DB.
# Only consumes memories from pre-approved sources (SAFE_SOURCES) and filters PII (e.g., names, emails).
# No fallback to random needed — we always want topic match, even if score is lower.
#
# Usage:  nova_random_safe_memory.sh [topic text]
# Output: Formatted memory fragment, or empty string if unavailable.
#
# Written by Jordan Koch.

TOPIC="${*:-}"

python3 - "$TOPIC" <<'PYEOF'
import json, urllib.request, urllib.parse, random, re, sys

TOPIC = sys.argv[1] if len(sys.argv) > 1 else ""
VECTOR_URL = "http://127.0.0.1:18790"
MIN_SCORE = 0.45

# Sources safe to share with the herd (not personal/PII/security)
SAFE_SOURCES = [
    "world_factbook", "gardening", "health", "cooking", "astronomy",
    "music", "first_aid", "local", "swift_dev", "security",
    "document", "history", "corvette_workshop_manual",
    "nutrition", "fitness", "cbt", "home_repair", "california",
    "finance", "corvette", "entertainment", "philosophy", "film",
    "burbank", "disney",
]

SAFE_SOURCE_SET = set(SAFE_SOURCES)

PII_PATTERNS = [
    r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
    r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b',
    r'\b\d{3}-\d{2}-\d{4}\b',
    r'password|passwd|secret|token|api.?key|credential',
    r'jordan|kochj|digitalnoise|disney\.com',
    r'/Users/kochj|/Volumes/Data',
]

def is_safe(text):
    t = text.lower()
    for pat in PII_PATTERNS:
        if re.search(pat, t, re.IGNORECASE):
            return False
    return True

def trim(text, max_chars=300):
    if len(text) <= max_chars:
        return text
    sentences = re.split(r'(?<=[.!?])\s+', text)
    out = ""
    for s in sentences:
        if len(out) + len(s) > max_chars:
            break
        out = (out + " " + s).strip()
    # Add summary ellipsis
    if len(out) >= max_chars:
        out = out[:max_chars-3] + " +"  # " +" means it continues
    return out

def format_result(source, text):
    label = source.replace("_", " ").title()
    return f"\n---\n\n*Memory Fragment ({label}):* {trim(text)}"

# ── Step 1: Try semantic recall on the email topic only ────────────────────
# No fallback needed — we require a topic and return blank if no match is found
try:
    q = urllib.parse.quote(TOPIC[:200])
    url = f"{VECTOR_URL}/recall?q={q}&n=10&min_score={MIN_SCORE}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())

    candidates = [
        m for m in data.get("memories", [])
        if m.get("source") in SAFE_SOURCE_SET
        and is_safe(m.get("text", ""))
        and len(m.get("text", "")) > 60
        and m.get("score", 0) >= MIN_SCORE
    ]

    if candidates:
        # Pick top or from top 3 for variety
        pick = random.choice(candidates[:3]) if len(candidates) > 1 else candidates[0]
        print(format_result(pick["source"], pick["text"]))
        sys.exit(0)
    else:
        # No match above threshold
        sys.exit(0)

except Exception as e:
    print(f"Error in topic search: {e}", file=sys.stderr)
    sys.exit(0)

candidates = []
for source in SAFE_SOURCES[:6]:
    try:
        url = f"{VECTOR_URL}/random?source={source}&n=5"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as r:
            memories = json.loads(r.read()).get("memories", [])
        for m in memories:
            text = m.get("text", "").strip()
            if len(text) > 60 and is_safe(text):
                candidates.append((source, text))
    except Exception:
        continue
    if len(candidates) >= 3:
        break

if not candidates:
    sys.exit(0)

source, text = random.choice(candidates)
print(format_result(source, text))
PYEOF
