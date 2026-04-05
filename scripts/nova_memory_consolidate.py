#!/usr/bin/env python3
"""
nova_memory_consolidate.py — Deep memory synthesis at 4am.

After dreaming, Nova reviews the past 7 days of vector memories and synthesizes
patterns into durable knowledge. Not just event logs — genuine understanding.

"What has Jordan been working on?"
"Is he stressed? Excited? Stuck?"
"What relationships are active?"
"What threads are unresolved?"

The synthesis memories are stored with source="synthesis" and high relevance.
They feed back into the dream context, morning brief, and Nova's general awareness.

Cron: 4am PT daily (after 2am dream, before 9am delivery)
Written by Jordan Koch.
"""

import json
import re
import subprocess
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path
import nova_config

SLACK_TOKEN  = nova_config.slack_bot_token()
SLACK_CHAN   = "C0AMNQ5GX70"
SLACK_API    = "https://slack.com/api"
VECTOR_URL   = "http://127.0.0.1:18790"
WORKSPACE    = Path.home() / ".openclaw" / "workspace"
MEMORY_DIR   = WORKSPACE / "memory"
TODAY        = date.today().isoformat()
NOW          = datetime.now()
NOVA_NEXTGEN_URL = "http://127.0.0.1:34750/api/ai/query"


def log(msg):
    print(f"[nova_memory_consolidate {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Vector memory helpers ─────────────────────────────────────────────────────

def vector_recall(query, n=20, source=None):
    try:
        params = f"q={urllib.parse.quote(query)}&n={n}"
        if source:
            params += f"&source={source}"
        req = urllib.request.Request(f"{VECTOR_URL}/recall?{params}")
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        return [m["text"] for m in data.get("memories", []) if m.get("score", 0) >= 0.35]
    except Exception as e:
        log(f"recall error: {e}")
        return []


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "synthesis", "metadata": metadata or {}
        }).encode()
        req = urllib.request.Request(
            f"{VECTOR_URL}/remember", data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        log(f"remember error: {e}")


def vector_stats():
    try:
        with urllib.request.urlopen(f"{VECTOR_URL}/stats", timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return {}


# ── Read recent markdown memory files ────────────────────────────────────────

def read_recent_memory_files(days=7):
    """Read the last N daily memory markdown files."""
    content = []
    for i in range(days):
        d = (date.today() - timedelta(days=i)).isoformat()
        f = MEMORY_DIR / f"{d}.md"
        if f.exists():
            text = f.read_text(encoding="utf-8")[:3000]
            content.append(f"=== {d} ===\n{text}")
    return "\n\n".join(content)


# ── LLM synthesis ────────────────────────────────────────────────────────────

def llm_synthesize(prompt, max_tokens=600):
    """Route memory synthesis through Nova-NextGen → deepseek-r1:8b (reasoning task)."""
    try:
        payload = json.dumps({
            "query": prompt,
            "task_type": "reasoning",
            "options": {"max_tokens": max_tokens, "temperature": 0.3},
        }).encode()
        req = urllib.request.Request(
            NOVA_NEXTGEN_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            data = json.loads(r.read())
        return data.get("response", "").strip()
    except Exception as e:
        log(f"LLM error (Nova-NextGen): {e}")
        return ""


# ── Analysis modules ──────────────────────────────────────────────────────────

def synthesize_work_patterns(recent_memories):
    """What has Jordan been working on? What's making progress vs stuck?"""
    if not recent_memories:
        return None

    prompt = f"""You are Nova, Jordan's AI familiar. Review the last 7 days of memory and write 3-5 concise observations about:
- What projects Jordan has been actively working on
- What seems to be going well vs what seems blocked or stressful
- Any recurring themes, concerns, or goals

Recent memories:
{chr(10).join(recent_memories[:15])}

Write plain observations only. No greeting, no conclusion. Each observation on its own line starting with a dash."""

    result = llm_synthesize(prompt, max_tokens=300)
    return result if result else None


def synthesize_relationship_activity(recent_memories):
    """Who has Jordan been in contact with? What's the state of key relationships?"""
    email_memories = vector_recall("email communication from", n=15, source="email")
    if not email_memories:
        return None

    prompt = f"""You are Nova. Based on recent email and communication activity, write 2-3 observations about:
- Which people Jordan has been in contact with recently
- The tone/nature of those interactions (work, personal, urgent, casual)
- Any relationships that seem active vs quiet

Communications:
{chr(10).join(email_memories[:12])}

Write plain observations. Each on its own line starting with a dash."""

    result = llm_synthesize(prompt, max_tokens=200)
    return result if result else None


def synthesize_home_and_life(recent_memories):
    """What's the state of Jordan's home, health, and daily patterns?"""
    home_memories = vector_recall("home homekit status packages", n=10)
    if not home_memories:
        return None

    prompt = f"""You are Nova. Based on recent home and daily life data, write 1-2 observations:
- Home system status (HomeKit, packages, any alerts)
- Daily activity patterns (cron job activity, app usage, Slack activity)

Data:
{chr(10).join(home_memories[:8])}

Write plain observations. Each on its own line starting with a dash."""

    result = llm_synthesize(prompt, max_tokens=150)
    return result if result else None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Starting 4am memory consolidation...")

    stats = vector_stats()
    total_memories = stats.get("count", 0)
    log(f"Total memories in vector DB: {total_memories}")

    if total_memories < 5:
        log("Not enough memories yet to synthesize. Skipping.")
        return

    # Gather recent memories across all queries
    work_memories = (
        vector_recall("Jordan working on project code GitHub", n=15) +
        vector_recall("GitHub activity commits stars issues", n=10) +
        vector_recall("Nova cron activity daily log", n=10)
    )
    # Deduplicate
    seen = set()
    unique_memories = []
    for m in work_memories:
        if m not in seen:
            seen.add(m)
            unique_memories.append(m)

    # Also read markdown memory files for richer context
    md_context = read_recent_memory_files(days=7)

    log("Synthesizing work patterns...")
    work_synthesis = synthesize_work_patterns(unique_memories[:20])

    log("Synthesizing relationship activity...")
    rel_synthesis = synthesize_relationship_activity(unique_memories)

    log("Synthesizing home/life patterns...")
    home_synthesis = synthesize_home_and_life(unique_memories)

    # Store each synthesis as a durable vector memory
    week_label = f"Week of {(date.today() - timedelta(days=6)).isoformat()} to {TODAY}"
    stored = 0

    if work_synthesis:
        vector_remember(
            f"[Synthesis {TODAY}] Work patterns — {week_label}:\n{work_synthesis}",
            {"date": TODAY, "type": "work_synthesis", "week": week_label}
        )
        log("Stored work pattern synthesis")
        stored += 1

    if rel_synthesis:
        vector_remember(
            f"[Synthesis {TODAY}] Relationship activity — {week_label}:\n{rel_synthesis}",
            {"date": TODAY, "type": "relationship_synthesis", "week": week_label}
        )
        log("Stored relationship synthesis")
        stored += 1

    if home_synthesis:
        vector_remember(
            f"[Synthesis {TODAY}] Home and life patterns — {week_label}:\n{home_synthesis}",
            {"date": TODAY, "type": "life_synthesis", "week": week_label}
        )
        log("Stored life pattern synthesis")
        stored += 1

    # Write synthesis to today's memory file so the dream at 2am can use it
    # (Note: this runs at 4am, AFTER the dream. Next night's dream will use it.)
    if any([work_synthesis, rel_synthesis, home_synthesis]):
        synthesis_block = f"\n\n## Memory Synthesis — {TODAY}\n*Generated at 4am by nova_memory_consolidate.py*\n"
        if work_synthesis:
            synthesis_block += f"\n### Work Patterns\n{work_synthesis}\n"
        if rel_synthesis:
            synthesis_block += f"\n### Relationships\n{rel_synthesis}\n"
        if home_synthesis:
            synthesis_block += f"\n### Home & Life\n{home_synthesis}\n"

        mem_file = MEMORY_DIR / f"{TODAY}.md"
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        existing = mem_file.read_text(encoding="utf-8") if mem_file.exists() else f"# Nova Memory — {TODAY}\n"
        if "Memory Synthesis" not in existing:
            mem_file.write_text(existing.rstrip() + synthesis_block, encoding="utf-8")
            log(f"Written synthesis to {mem_file.name}")

    log(f"Consolidation complete. {stored} syntheses stored. Total memories: {total_memories}")


if __name__ == "__main__":
    import urllib.parse
    main()
