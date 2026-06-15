#!/usr/bin/env python3
"""
nova_rando_top10_weird.py — "Top 10 Weirdest Memories" article, every 12 hours.

Runs at 6 AM and 6 PM. Queries the last 12 hours of ingested memories,
has an LLM rank the weirdest 10, generates a sarcastic illustrated article
in Nova's voice, and publishes to the Rando section of nova-journal.

Written by Jordan Koch.
"""

import base64
import json
import os
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

# ── Config ────────────────────────────────────────────────────────────────────

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = HUGO_ROOT / "content/operations"
IMAGES_DIR = HUGO_ROOT / "static/images/operations"
LOG_FILE = Path.home() / ".openclaw/logs/nova_rando_top10.log"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ARTICLE_MODEL = "anthropic/claude-sonnet-4-6"
IMAGE_MODEL = "openai/gpt-5-image"
PG_DSN = "dbname=nova_memories user=kochj host=192.168.1.6"
HOURS_WINDOW = 12
TOP_N = 10

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[rando-top10 {ts}] {msg}"
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


def call_llm(system: str, user: str, model: str = None, max_tokens: int = 8000) -> str:
    import urllib.request
    api_key = get_openrouter_key()
    body = json.dumps({
        "model": model or ARTICLE_MODEL,
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
        "HTTP-Referer": "https://nova.digitalnoise.net",
        "X-Title": "Nova Rando Top 10",
    })
    resp = urllib.request.urlopen(req, timeout=300)
    data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


# ── Memory Queries ────────────────────────────────────────────────────────────

def get_recent_memories(hours: int = 12, limit: int = 150) -> list[dict]:
    import psycopg2
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    cutoff = datetime.now() - timedelta(hours=hours)
    cur.execute("""
        SELECT text, source, created_at
        FROM memories
        WHERE created_at >= %s
          AND LENGTH(text) > 80
          AND LENGTH(text) < 1200
        ORDER BY RANDOM()
        LIMIT %s
    """, (cutoff, limit))
    rows = cur.fetchall()
    conn.close()
    return [{"text": r[0], "source": r[1], "created_at": str(r[2])} for r in rows]


def get_memory_stats(hours: int = 12) -> dict:
    import psycopg2
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    cutoff = datetime.now() - timedelta(hours=hours)
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
    mem_block = ""
    for i, m in enumerate(memories, 1):
        text_preview = m["text"][:400].replace("\n", " ").strip()
        mem_block += f"\n{i}. [{m['source']}] {text_preview}\n"

    sources_summary = ", ".join(f"{k} ({v})" for k, v in list(stats["sources"].items())[:10])
    period = "morning" if datetime.now().hour < 12 else "evening"

    from nova_voice import system_prompt
    system = system_prompt(f"""
FORMAT: TOP 10 WEIRDEST MEMORIES — {period} edition.
- From the provided list, pick EXACTLY the 10 weirdest/funniest/most unhinged entries
- Number them 1-10 (countdown — save the weirdest for #1)
- Quote the actual memory text (or a juicy portion) in italics
- Add your sarcastic take after each (2-5 sentences, go long if the bit demands it)
- Include an intro roasting the ingestion period — make it sound like an intervention
- Include an outro (existential crisis played for laughs, or a callback to the most unhinged entry)
- Each entry needs its own comedic angle — never repeat a style
- Acknowledge the time of day in the intro
- Do NOT include a title (added separately)
- Total: ~1500-2500 words
""")

    user = f"""Here are {len(memories)} randomly sampled memories ingested in the last {HOURS_WINDOW} hours.
Total new memories this period: {stats['total']:,}
Sources: {sources_summary}

Pick the 10 weirdest and write your column.

MEMORIES:
{mem_block}"""

    return call_llm(system, user, max_tokens=8000)


def generate_title(article_preview: str) -> str:
    system = "Generate a single funny, sarcastic title for this 'Top 10 Weirdest Memories' column. Max 12 words. Output ONLY the title, nothing else. No quotes. No numbering."
    user = f"Based on this article:\n\n{article_preview[:1500]}"
    title = call_llm(system, user, max_tokens=50)
    return title.strip().strip('"').strip("'").replace('"', "'")


# ── Image Generation via OpenRouter ──────────────────────────────────────────

def generate_image_openrouter(article_preview: str) -> Path | None:
    import urllib.request

    prompt_system = "Based on this article about weird AI memories, generate a single short image prompt (max 80 words) for an illustration. The image should be surreal, funny, and capture the chaos of the article. Output ONLY the prompt, nothing else."
    img_prompt = call_llm(prompt_system, article_preview[:2000], max_tokens=100).strip()
    log(f"Image prompt: {img_prompt[:100]}...")

    api_key = get_openrouter_key()
    payload = json.dumps({
        "model": IMAGE_MODEL,
        "modalities": ["image", "text"],
        "messages": [
            {"role": "user", "content": f"Generate an image: {img_prompt}"}
        ],
    }).encode()

    req = urllib.request.Request(OPENROUTER_URL, data=payload, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://nova.digitalnoise.net",
        "X-Title": "Nova Rando Top 10 Image",
    })

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())

        choices = data.get("choices", [])
        if not choices:
            log("Image: no choices in response")
            return None

        message = choices[0].get("message", {})

        # Check images[] array
        for img in message.get("images", []):
            img_url = ""
            if isinstance(img, dict):
                img_url = img.get("image_url", {}).get("url", "") or img.get("url", "")
            elif isinstance(img, str):
                img_url = img
            if img_url.startswith("data:image"):
                b64 = img_url.split(",", 1)[1]
                out = Path.home() / f".openclaw/workspace/rando_top10_{int(time.time())}.png"
                out.write_bytes(base64.b64decode(b64))
                log(f"Image saved (base64): {out.name}")
                return out
            elif img_url.startswith("http"):
                out = Path.home() / f".openclaw/workspace/rando_top10_{int(time.time())}.png"
                urllib.request.urlretrieve(img_url, str(out))
                log(f"Image downloaded: {out.name}")
                return out

        # Check content array
        content = message.get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    img_url = part.get("image_url", {}).get("url", "")
                    if img_url.startswith("data:image"):
                        b64 = img_url.split(",", 1)[1]
                        out = Path.home() / f".openclaw/workspace/rando_top10_{int(time.time())}.png"
                        out.write_bytes(base64.b64decode(b64))
                        log(f"Image saved (content): {out.name}")
                        return out

        log(f"Image: no image in response (keys: {list(message.keys())})")
        return None

    except Exception as e:
        log(f"Image generation failed: {e}")
        return None


# ── Publishing ────────────────────────────────────────────────────────────────

def publish(title: str, body: str, image_path: Path | None):
    date = time.strftime("%Y-%m-%d")
    hour = datetime.now().hour
    time_label = "06:00:00" if hour < 12 else "18:00:00"
    timestamp = f"{date}T{time_label}-07:00"
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    hugo_image = ""
    if image_path and image_path.exists():
        img_filename = f"{date}-{slug}.webp"
        img_dest = IMAGES_DIR / img_filename
        try:
            subprocess.run(
                ["cwebp", "-q", "82", "-resize", "1200", "0", str(image_path), "-o", str(img_dest)],
                capture_output=True, timeout=30
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            shutil.copy2(image_path, img_dest)
        hugo_image = f"/images/operations/{img_filename}"

    front_matter = f"""---
title: "{title}"
date: {timestamp}
draft: false
categories: ["operations"]
tags: ["memories", "weird", "top10", "ingest", "sarcasm"]
description: "Nova's top 10 weirdest memories ingested in the last 12 hours."
"""
    if hugo_image:
        front_matter += f"""cover:
  image: "{hugo_image}"
  alt: "Top 10 weirdest memories"
  relative: false
"""
    front_matter += "---\n\n"

    post_path = CONTENT_DIR / f"{date}-{slug}.md"
    post_path.write_text(front_matter + body)
    log(f"Post written: {post_path.name}")

    # Git commit and push
    subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=15)
    msg = f"rando: {date} top 10 weirdest memories ({title[:40]})"
    r = subprocess.run(["git", "commit", "-m", msg], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        log("Pushed to GitHub")
    else:
        log(f"Commit issue: {r.stderr[:200]}")

    nova_config.post_both(
        f":brain: *Top 10 Weirdest Memories posted*\n"
        f"  _{title}_\n"
        f"  https://nova.digitalnoise.net/operations/{date}-{slug}/",
        slack_channel="#nova-notifications"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Starting Top 10 Weirdest Memories article")

    stats = get_memory_stats(hours=HOURS_WINDOW)
    if stats["total"] < 50:
        log(f"Only {stats['total']} memories in last {HOURS_WINDOW}h — skipping")
        return

    memories = get_recent_memories(hours=HOURS_WINDOW, limit=150)
    log(f"Got {len(memories)} candidate memories from {stats['total']} total")

    article = generate_article(memories, stats)
    log(f"Article generated: {len(article)} chars")

    title = generate_title(article)
    log(f"Title: {title}")

    img_path = generate_image_openrouter(article)

    publish(title, article, img_path)
    log("Top 10 Weirdest Memories complete")


if __name__ == "__main__":
    main()
