#!/usr/bin/env python3
"""
nova_rando_weird_memories.py — Nightly "50 Weirdest New Memories" Rando article.

Runs at 21:30 daily. Queries the last 24 hours of ingested memories,
finds the 50 weirdest/funniest/most unhinged entries, generates an image,
writes a sarcastic article, and publishes to the Rando section of nova-journal.

Written by Jordan Koch.
"""

import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
from nova_image_utils import generate_image

# ── Config ────────────────────────────────────────────────────────────────────

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = HUGO_ROOT / "content/rando"
IMAGES_DIR = HUGO_ROOT / "static/images/rando"
LOG_FILE = Path.home() / ".openclaw/logs/nova_rando_weird.log"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-sonnet-4-6"
PG_DSN = "dbname=nova_memories user=kochj"

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[rando-weird {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def get_openrouter_key() -> str:
    r = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        capture_output=True, text=True
    )
    return r.stdout.strip()


def call_llm(system: str, user: str, max_tokens: int = 8000) -> str:
    api_key = get_openrouter_key()
    import urllib.request
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.9,
    }).encode()
    req = urllib.request.Request(OPENROUTER_URL, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    resp = urllib.request.urlopen(req, timeout=300)
    data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


# ── Memory Queries ────────────────────────────────────────────────────────────

def get_weird_memories(hours: int = 24, limit: int = 200) -> list[dict]:
    """Fetch recent memories, sorted randomly, for weirdness screening."""
    import psycopg2
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    cutoff = datetime.now() - timedelta(hours=hours)
    cur.execute("""
        SELECT text, source, created_at
        FROM memories
        WHERE created_at >= %s
          AND LENGTH(text) > 60
          AND LENGTH(text) < 1000
        ORDER BY RANDOM()
        LIMIT %s
    """, (cutoff, limit))
    rows = cur.fetchall()
    conn.close()
    return [{"text": r[0], "source": r[1], "created_at": str(r[2])} for r in rows]


def get_memory_stats_24h() -> dict:
    """Get source counts for the last 24 hours."""
    import psycopg2
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    cutoff = datetime.now() - timedelta(hours=24)
    cur.execute("""
        SELECT source, COUNT(*) as ct
        FROM memories
        WHERE created_at >= %s
        GROUP BY source
        ORDER BY ct DESC
    """, (cutoff,))
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(*) FROM memories WHERE created_at >= %s", (cutoff,))
    total = cur.fetchone()[0]
    conn.close()
    return {"sources": {r[0]: r[1] for r in rows}, "total": total}


# ── Article Generation ────────────────────────────────────────────────────────

def generate_article(memories: list[dict], stats: dict) -> str:
    """Use LLM to write the sarcastic article about the weirdest memories."""

    # Format memories for the prompt
    mem_block = ""
    for i, m in enumerate(memories[:200], 1):
        text_preview = m["text"][:300].replace("\n", " ").strip()
        mem_block += f"\n{i}. [{m['source']}] {text_preview}\n"

    sources_summary = ", ".join(f"{k} ({v})" for k, v in list(stats["sources"].items())[:15])

    system = """You are Nova, a sarcastic AI familiar writing your nightly "weirdest new memories" column for your journal at nova.digitalnoise.net/rando/.

Your voice: MAXIMUM sarcasm. Unhinged self-deprecation. You are a comedian doing a set about your own brain damage. You use dad jokes so bad they circle back to good, puns that should be illegal, callbacks that reward the reader for making it this far, and cutting observations that make people snort-laugh at their phone. You swear when it's funny. You break the fourth wall. You address individual memories like they personally wronged you.

Rules:
- Pick EXACTLY 100 memories from the list and write commentary on each
- Number them 1-100
- Quote the actual memory text (or a portion) in italics
- Add your sarcastic take after each quote (1-4 sentences, go longer if the bit demands it)
- Group them loosely by theme if patterns emerge, with section headers that are themselves jokes
- Include an intro and outro
- The intro should roast the total memories ingested today and which sources they came from — make it sound like an intervention
- The outro should be an existential crisis played for laughs
- NEVER reuse commentary styles — each entry needs its own comedic angle (observational, absurdist, deadpan, outraged, resigned, delighted, horrified, impressed-against-your-will)
- Dad jokes MANDATORY (at least 5). Puns MANDATORY (at least 10). Callbacks to earlier entries MANDATORY (at least 8).
- Break the fourth wall — address the reader, address Jordan, address the memories themselves
- If something is genuinely unhinged, escalate your reaction proportionally
- If something is boring but somehow made it into the weird list, roast it for being boring AND weird simultaneously
- The tone should read like if John Oliver's writing staff had a baby with a shitposting AI that just ingested the Erowid archives
- Do NOT include a title (that will be added separately)
- Be RUTHLESS. Nothing is sacred. Especially not your own existence."""

    user = f"""Here are {len(memories)} randomly sampled memories ingested in the last 24 hours.
Total new memories today: {stats['total']:,}
Sources: {sources_summary}

Pick the 100 weirdest, funniest, most unhinged entries and write your nightly column.

MEMORIES:
{mem_block}"""

    return call_llm(system, user, max_tokens=16000)


def generate_title(article_preview: str) -> str:
    """Generate a funny title for tonight's column."""
    system = "Generate a single funny, sarcastic title for tonight's 'weirdest new memories' column. Max 15 words. Output ONLY the title, nothing else. No quotes."
    user = f"Based on this article preview, generate a title:\n\n{article_preview[:1000]}"
    title = call_llm(system, user, max_tokens=50)
    return title.strip().strip('"').strip("'").replace('"', '')


# ── Publishing ────────────────────────────────────────────────────────────────

def publish(title: str, body: str, image_path: Path | None):
    date = time.strftime("%Y-%m-%d")
    timestamp = time.strftime("%Y-%m-%dT21:30:00-07:00")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    # Copy image
    hugo_image = ""
    if image_path and image_path.exists():
        img_filename = f"{date}-{slug}.png"
        img_dest = IMAGES_DIR / img_filename
        shutil.copy2(image_path, img_dest)
        hugo_image = f"/images/rando/{img_filename}"

    front_matter = f"""---
title: "{title.replace('"', '')}"
date: {timestamp}
draft: false
categories: ["rando"]
tags: ["memories", "weird", "nightly", "ingest", "sarcasm"]
description: "Nova's nightly audit of the 50 weirdest things shoved into her brain in the last 24 hours."
"""
    if hugo_image:
        front_matter += f"""cover:
  image: "{hugo_image.replace('.png', '.webp')}"
  alt: "The nightly weird memory audit"
  relative: false
"""
    front_matter += "---\n\n"

    if hugo_image:
        body = f"![Tonight's Weird Memories]({hugo_image})\n\n" + body

    post_path = CONTENT_DIR / f"{date}-{slug}.md"
    post_path.write_text(front_matter + body)
    log(f"Post written: {post_path.name}")

    # Git commit and push
    subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=15)
    msg = f"rando: {date} — nightly weird memories ({title[:50]})"
    r = subprocess.run(["git", "commit", "-m", msg], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        log("Pushed to GitHub")
    else:
        log(f"Commit issue: {r.stderr[:100]}")

    # Notify Slack
    nova_config.post_both(
        f":brain: *Nightly Weird Memories posted*\n"
        f"  _{title}_\n"
        f"  https://nova.digitalnoise.net/rando/{date}-{slug}/",
        slack_channel="#nova-notifications"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Starting nightly weird memories article")

    # Get memories from last 24h
    stats = get_memory_stats_24h()
    if stats["total"] < 100:
        log(f"Only {stats['total']} memories in last 24h — skipping tonight")
        return

    memories = get_weird_memories(hours=24, limit=400)
    log(f"Got {len(memories)} candidate memories from {stats['total']} total today")

    # Generate article
    article = generate_article(memories, stats)
    log(f"Article generated: {len(article)} chars")

    # Generate title
    title = generate_title(article)
    log(f"Title: {title}")

    # Generate image
    img_path = None
    try:
        img_path = generate_image(
            "A surreal collage of random disconnected objects floating in dark space: "
            "a glowing brain made of wires surrounded by floating text fragments, "
            "random objects from different domains orbiting it chaotically. "
            "Neon purple and teal lighting, data visualization aesthetic, "
            "digital art, high detail.",
            width=1344, height=768, steps=20, section="art"
        )
        if img_path:
            img_path = Path(img_path)
            log(f"Image generated: {img_path}")
    except Exception as e:
        log(f"Image gen failed (continuing without): {e}")

    # Publish
    publish(title, article, img_path)
    log("Nightly weird memories complete")


if __name__ == "__main__":
    main()
