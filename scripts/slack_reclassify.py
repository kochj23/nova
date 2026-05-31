#!/usr/bin/env python3
"""
slack_reclassify.py — Reclassify Slack vector memories into proper subject vectors.

Identifies three categories:
1. Actual Slack messages (keep in 'slack')
2. Video/podcast transcripts (reclassify by topic via Haiku)
3. Garbage (repeated text, empty attachments) → delete

Uses OpenRouter Claude Haiku for classification.
Processes in batches of 20, with progress logging.

Written by Jordan Koch (via Claude).
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

DB = "nova_memories"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-haiku-4.5"
BATCH_SIZE = 20
LOG_FILE = Path.home() / ".openclaw/logs/slack_reclassify.log"
SLACK_NOTIFY_INTERVAL = 300  # 5 minutes

# Valid target vectors for reclassification
VALID_VECTORS = [
    "music", "metal", "jazz", "rap", "edm", "newwave", "nowave", "hardcore_punk",
    "automotive", "home_improvement", "cooking", "gardening",
    "climate", "science", "physics", "chemistry", "biology", "geology", "astronomy",
    "psychology", "medicine", "pharmacology", "neuroscience", "nutrition",
    "politics", "economics", "law", "sociology",
    "technology_general", "computing", "programming", "security",
    "history", "military_history", "ww2", "american_civil_war",
    "religion", "philosophy", "occult", "mythology_folklore",
    "sports", "fitness",
    "art", "architecture", "fashion", "horology",
    "film_criticism", "television", "comedy", "horror", "drama", "documentary",
    "education", "linguistics", "literature", "mathematics",
    "burbank_local", "geography",
    "sexuality", "gang_culture",
    "cocktails", "food_science",
    "leadership_core", "management_core", "operations",
]

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


# ── Database ──────────────────────────────────────────────────────────────────

def db_query(sql: str, params: list = None) -> list:
    """Run a query and return rows."""
    import subprocess
    cmd = ["psql", "-h", "localhost", "-U", "kochj", "-d", DB, "-t", "-A", "-F", "\t"]
    if params:
        for i, p in enumerate(params):
            sql = sql.replace(f"${i+1}", f"'{p}'")
    cmd.extend(["-c", sql])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        log(f"DB ERROR: {result.stderr[:200]}")
        return []
    rows = [line.split("\t") for line in result.stdout.strip().split("\n") if line.strip()]
    return rows


def db_exec(sql: str):
    """Execute a statement (no return)."""
    import subprocess
    cmd = ["psql", "-h", "localhost", "-U", "kochj", "-d", DB, "-c", sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        log(f"DB EXEC ERROR: {result.stderr[:200]}")


# ── Garbage Detection ─────────────────────────────────────────────────────────

def is_garbage(text: str) -> bool:
    """Detect low-quality memories that should be deleted."""
    stripped = text.strip()
    if len(stripped) < 30:
        return True
    # Repeated phrases (Whisper hallucinations)
    words = stripped.split()
    if len(words) > 10:
        unique_phrases = set()
        for i in range(0, len(words) - 3):
            phrase = " ".join(words[i:i+3])
            unique_phrases.add(phrase)
        if len(unique_phrases) < len(words) * 0.15:
            return True
    # Only attachments/system messages
    lines = [l.strip() for l in stripped.split("\n") if l.strip()]
    attachment_lines = sum(1 for l in lines if l in ("[attachment]", "system: [attachment]"))
    if attachment_lines > 0 and attachment_lines >= len(lines) * 0.8:
        return True
    return False


def is_actual_slack(text: str) -> bool:
    """Detect actual Slack messages (not transcripts)."""
    if text.startswith("Slack #") or text.startswith("Slack conversation between"):
        return True
    if "B06RSQYQY:" in text or "B0FH" in text:
        return True
    if "<http" in text and "|" in text:
        return True
    return False


# ── LLM Classification ───────────────────────────────────────────────────────

def classify_batch(memories: list[tuple]) -> list[dict]:
    """Classify a batch of memories using Haiku. Returns [{id, action, target}]."""
    api_key = nova_config.openrouter_api_key()
    if not api_key:
        log("ERROR: No OpenRouter API key")
        return []

    vector_list = ", ".join(sorted(VALID_VECTORS))

    items = []
    for mem_id, text in memories:
        preview = text[:300].replace('"', "'").replace("\n", " ")
        items.append(f'  {{"id": "{mem_id}", "text": "{preview}"}}')

    items_json = ",\n".join(items)

    system_prompt = f"""You are a content classifier. Given text snippets, classify each into the most appropriate subject vector.

Available vectors: {vector_list}

Rules:
- If the text is a video/podcast transcript about a specific topic, pick the best matching vector
- If you're unsure between two vectors, pick the more specific one
- If nothing fits well, use "education" as a catch-all for informational content
- For music gear/production content, use "music"
- For car/vehicle content, use "automotive"
- For political commentary/news, use "politics"
- For parenting/child development, use "psychology"
- For health/diet content, use "nutrition" or "medicine"

Output ONLY valid JSON array. No markdown, no explanation."""

    user_prompt = f"""Classify each memory into a target vector. Return JSON array:
[{{"id": "...", "vector": "..."}}]

Memories:
[
{items_json}
]"""

    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 2000,
    })

    req = urllib.request.Request(
        OPENROUTER_URL,
        data=payload.encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://nova.digitalnoise.net",
            "X-Title": "Nova Slack Reclassify",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        content = data["choices"][0]["message"]["content"].strip()
        # Strip markdown fences if present
        if content.startswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        results = json.loads(content)
        return results
    except Exception as e:
        log(f"LLM classify error: {e}")
        return []


# ── Main Pipeline ────────────────────────────────────────────────────────────

def main():
    log("=" * 60)
    log("Slack Reclassify — starting")
    log("=" * 60)

    # Count total
    rows = db_query("SELECT count(*) FROM memories WHERE source = 'slack'")
    total = int(rows[0][0]) if rows else 0
    log(f"Total slack memories: {total}")

    # Stats
    stats = {"kept_slack": 0, "reclassified": 0, "deleted": 0, "errors": 0}
    last_notify = time.time()

    offset = 0
    while True:
        # Fetch batch
        rows = db_query(
            f"SELECT id, LEFT(text, 500) FROM memories WHERE source = 'slack' "
            f"ORDER BY created_at ASC LIMIT {BATCH_SIZE} OFFSET {offset}"
        )
        if not rows:
            break

        to_classify = []
        for row in rows:
            if len(row) < 2:
                continue
            mem_id, text = row[0], row[1]

            # Phase 1: garbage detection
            if is_garbage(text):
                db_exec(f"DELETE FROM memories WHERE id = '{mem_id}'")
                stats["deleted"] += 1
                continue

            # Phase 2: actual slack detection
            if is_actual_slack(text):
                stats["kept_slack"] += 1
                continue

            # Phase 3: needs LLM classification
            to_classify.append((mem_id, text))

        # Classify transcripts via Haiku
        if to_classify:
            results = classify_batch(to_classify)
            for r in results:
                rid = r.get("id", "")
                vector = r.get("vector", "")
                if vector and vector in VALID_VECTORS and rid:
                    db_exec(f"UPDATE memories SET source = '{vector}' WHERE id = '{rid}'")
                    stats["reclassified"] += 1
                elif rid:
                    stats["errors"] += 1

        offset += BATCH_SIZE
        processed = stats["kept_slack"] + stats["reclassified"] + stats["deleted"] + stats["errors"]

        # Progress log every 5 batches
        if (offset // BATCH_SIZE) % 5 == 0:
            log(f"Progress: {processed}/{total} — kept={stats['kept_slack']}, "
                f"moved={stats['reclassified']}, deleted={stats['deleted']}, errors={stats['errors']}")

        # Slack notify every 5 minutes
        if time.time() - last_notify > SLACK_NOTIFY_INTERVAL:
            pct = int(processed / total * 100) if total else 0
            nova_config.post_both(
                f":recycle: *Slack Reclassify* — {pct}% ({processed}/{total})\n"
                f"Kept: {stats['kept_slack']} | Moved: {stats['reclassified']} | "
                f"Deleted: {stats['deleted']} | Errors: {stats['errors']}",
                slack_channel=nova_config.SLACK_NOTIFY
            )
            last_notify = time.time()

        # Rate limit (avoid hammering OpenRouter)
        time.sleep(1)

    # Final report
    log("=" * 60)
    log(f"COMPLETE — kept={stats['kept_slack']}, moved={stats['reclassified']}, "
        f"deleted={stats['deleted']}, errors={stats['errors']}")
    log("=" * 60)

    nova_config.post_both(
        f":white_check_mark: *Slack Reclassify Complete*\n"
        f"• Kept in slack: {stats['kept_slack']}\n"
        f"• Reclassified: {stats['reclassified']}\n"
        f"• Deleted (garbage): {stats['deleted']}\n"
        f"• Errors: {stats['errors']}",
        slack_channel=nova_config.SLACK_NOTIFY
    )


if __name__ == "__main__":
    main()
