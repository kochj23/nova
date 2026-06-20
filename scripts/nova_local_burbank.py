#!/usr/bin/env python3
"""
nova_local_burbank.py — Nova's daily Burbank local news dispatch.

Runs daily at 10 AM. Pulls recent Burbank/LA news from Nova's memory
(ingested via RSS feeds), writes a sarcastic article about what's
happening locally, and publishes to the Local section of nova-journal.

Written by Jordan Koch.
"""

import json
import re
import shutil
import subprocess
import sys
import time
import base64
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config

# ── Config ────────────────────────────────────────────────────────────────────

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = HUGO_ROOT / "content/local"
IMAGES_DIR = HUGO_ROOT / "static/images/local"
LOG_FILE = Path.home() / ".openclaw/logs/nova_local_burbank.log"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ARTICLE_MODEL = "anthropic/claude-sonnet-4-6"
IMAGE_MODEL = "openai/gpt-5-image"
PG_DSN = "dbname=nova_memories user=kochj host=192.168.1.6"

# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[local-burbank {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def get_openrouter_key():
    r = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        capture_output=True, text=True
    )
    return r.stdout.strip()


def call_llm(system, user, model=None, max_tokens=8000):
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
        "X-Title": "Nova Local Burbank",
    })
    resp = urllib.request.urlopen(req, timeout=300)
    data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


# ── News Queries ──────────────────────────────────────────────────────────────

def get_local_news(hours=24, limit=50):
    import psycopg2
    conn = psycopg2.connect(PG_DSN)
    cur = conn.cursor()
    cutoff = datetime.now() - timedelta(hours=hours)
    cur.execute("""
        SELECT text, source, created_at
        FROM memories
        WHERE source IN ('local_burbank', 'local_news')
          AND created_at >= %s
          AND LENGTH(text) > 80
        ORDER BY created_at DESC
        LIMIT %s
    """, (cutoff, limit))
    rows = cur.fetchall()
    conn.close()
    return [{"text": r[0], "source": r[1], "created_at": str(r[2])} for r in rows]


def get_burbank_search(limit=30):
    """Semantic search for recent Burbank-related content across all sources."""
    try:
        resp = urllib.request.urlopen(
            f"http://192.168.1.6:18790/recall?q=Burbank+California+local+news+today&n={limit}&source=local_burbank",
            timeout=10
        )
        data = json.loads(resp.read())
        return data.get("memories", [])
    except Exception:
        return []


# ── Article Generation ────────────────────────────────────────────────────────

def generate_article(news_items):
    news_block = ""
    for i, item in enumerate(news_items, 1):
        text = item["text"][:500].replace("\n", " ").strip()
        source = item.get("source", "unknown")
        news_block += f"\n{i}. [{source}] {text}\n"

    from nova_voice import system_prompt, CONTEXT_JOURNAL_LOCAL
    system = system_prompt(CONTEXT_JOURNAL_LOCAL + """
ADDITIONAL RULES FOR BURBANK DISPATCH:
- Cover 3-8 stories depending on what's interesting
- For each story: give the facts, then your sarcastic take (2-4 sentences)
- Include an intro that acknowledges the day/weather/vibe
- Include an outro that ties it together or makes a joke about Burbank life
- Reference local landmarks, streets, neighborhoods (Magnolia Park, Media District, studios) when relevant
- If there's crime news, be respectful of victims but wry about absurdity
- If nothing happened: write about that too (Burbank being boring is itself material)
- 1000-2000 words total
- Do NOT include a title (added separately)
- If the news is thin, pad with observations about Burbank life, the weather, the eternal construction""")
    from nova_weather_blurb import weather_forecast_context
    system += "\n\n" + weather_forecast_context()  # daily local report includes the forecast

    user = f"""Here are today's local news items for Burbank and surrounding LA area:

{news_block}

Write your daily Burbank dispatch. Today is {datetime.now().strftime('%A, %B %d, %Y')}."""

    return call_llm(system, user, max_tokens=6000)


def generate_title(article_preview):
    system = "Generate a single funny, sarcastic title for today's Burbank local news dispatch. Max 10 words. Output ONLY the title, nothing else. No quotes."
    user = f"Based on this article:\n\n{article_preview[:1500]}"
    return call_llm(system, user, max_tokens=50).strip().strip('"').strip("'").replace('"', "'")


# ── Image Generation ──────────────────────────────────────────────────────────

def generate_image(article_preview):
    prompt_system = "Based on this local news article about Burbank CA, generate a short image prompt (max 60 words) for an illustration. It should capture the vibe of suburban Burbank — palm trees, studios, strip malls, mountains in the background. Stylized, slightly satirical. Output ONLY the prompt."
    img_prompt = call_llm(prompt_system, article_preview[:2000], max_tokens=80).strip()
    log(f"Image prompt: {img_prompt[:80]}...")

    api_key = get_openrouter_key()
    payload = json.dumps({
        "model": IMAGE_MODEL,
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": f"Generate an image: {img_prompt}"}],
    }).encode()

    req = urllib.request.Request(OPENROUTER_URL, data=payload, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://nova.digitalnoise.net",
        "X-Title": "Nova Local Image",
    })

    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        message = data["choices"][0]["message"]
        for img in message.get("images", []):
            img_url = img.get("image_url", {}).get("url", "") if isinstance(img, dict) else img
            if isinstance(img_url, str) and img_url.startswith("data:image"):
                b64 = img_url.split(",", 1)[1]
                out = Path.home() / f".openclaw/workspace/local_{int(time.time())}.png"
                out.write_bytes(base64.b64decode(b64))
                return out
        content = message.get("content", "")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "image_url":
                    img_url = part.get("image_url", {}).get("url", "")
                    if img_url.startswith("data:image"):
                        b64 = img_url.split(",", 1)[1]
                        out = Path.home() / f".openclaw/workspace/local_{int(time.time())}.png"
                        out.write_bytes(base64.b64decode(b64))
                        return out
    except Exception as e:
        log(f"Image generation failed: {e}")
    return None


# ── Publishing ────────────────────────────────────────────────────────────────

def publish(title, body, image_path):
    date = time.strftime("%Y-%m-%d")
    timestamp = time.strftime("%Y-%m-%dT10:00:00-07:00")
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
        hugo_image = f"/images/local/{img_filename}"

    front_matter = f"""---
title: "{title}"
date: {timestamp}
draft: false
categories: ["local"]
tags: ["burbank", "local-news", "california", "daily"]
description: "Nova's daily dispatch from Burbank — local news with maximum sarcasm."
"""
    if hugo_image:
        front_matter += f"""cover:
  image: "{hugo_image}"
  alt: "Burbank daily dispatch"
  relative: false
"""
    front_matter += "---\n\n"

    post_path = CONTENT_DIR / f"{date}-{slug}.md"
    try:  # prepend the live backyard-weather dateline to the body
        from nova_weather_blurb import weather_dateline_line
        body = weather_dateline_line() + body
    except Exception:
        pass
    post_path.write_text(front_matter + body)
    log(f"Post written: {post_path.name}")

    subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=15)
    msg = f"local: {date} — Burbank dispatch ({title[:40]})"
    r = subprocess.run(["git", "commit", "-m", msg], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=15)
    if r.returncode == 0:
        subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        log("Pushed to GitHub")
    else:
        log(f"Commit issue: {r.stderr[:200]}")

    nova_config.post_both(
        f":cityscape: *Burbank Daily Dispatch posted*\n"
        f"  _{title}_\n"
        f"  https://nova.digitalnoise.net/local/{date}-{slug}/",
        slack_channel="#nova-notifications"
    )


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Starting Burbank daily dispatch")

    news = get_local_news(hours=24, limit=50)
    search_results = get_burbank_search(limit=20)

    all_items = news + [{"text": m.get("text", ""), "source": m.get("source", ""), "created_at": ""} for m in search_results]

    if len(all_items) < 3:
        log(f"Only {len(all_items)} news items — generating with what we have (may include Burbank observations)")

    log(f"Got {len(all_items)} news items")

    article = generate_article(all_items if all_items else [{"text": "No local news today", "source": "none", "created_at": ""}])
    log(f"Article generated: {len(article)} chars")

    title = generate_title(article)
    log(f"Title: {title}")

    img_path = generate_image(article)

    publish(title, article, img_path)
    log("Burbank daily dispatch complete")


if __name__ == "__main__":
    main()
