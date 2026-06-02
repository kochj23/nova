#!/usr/bin/env python3
"""
nova_security_weekly.py — Friday strategic intelligence rollup.

Synthesizes the week's daily security briefings into one "Week in Intelligence" article:
- What escalated this week
- What resolved
- Trends (recurring threat actors, vulnerability classes, geopolitical shifts)
- What to watch next week
- Patch status summary

Runs Friday at 4pm.

Written by Jordan Koch (via Claude).
"""

import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
from nova_image_utils import generate_image

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = HUGO_ROOT / "content/securities"
IMAGES_DIR = HUGO_ROOT / "static/images/securities"
LOG_FILE = Path.home() / ".openclaw/logs/nova_security_weekly.log"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-sonnet-4-6"

CONTENT_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[sec-weekly {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def call_llm(system: str, user: str, max_tokens: int = 6000) -> str:
    api_key = nova_config.openrouter_api_key()
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(OPENROUTER_URL, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    })
    resp = urllib.request.urlopen(req, timeout=300)
    data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def get_week_memories() -> str:
    """Get all security-relevant memories from the past 7 days."""
    result = subprocess.run(
        ["psql", "-h", "192.168.1.6", "-U", "kochj", "-d", "nova_memories", "-tA", "-c",
         "SELECT text FROM memories "
         "WHERE source IN ('intelligence', 'military_history', 'law') "
         "AND created_at >= now() - interval '7 days' "
         "AND LENGTH(text) > 60 "
         "ORDER BY created_at DESC LIMIT 200;"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return ""
    return "\n".join(f"- {line.strip()[:250]}" for line in result.stdout.strip().split("\n") if line.strip())


def get_week_articles() -> str:
    """Read this week's daily security briefings from Hugo."""
    articles = []
    today = datetime.now()
    for i in range(7):
        day = today - timedelta(days=i)
        pattern = day.strftime("%Y-%m-%d")
        for f in CONTENT_DIR.glob(f"{pattern}*.md"):
            content = f.read_text()
            # Strip frontmatter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    content = parts[2].strip()
            articles.append(content[:2000])
    return "\n\n---\n\n".join(articles[:7])


def run():
    log("=== Generating weekly security rollup ===")

    memories = get_week_memories()
    articles = get_week_articles()

    if not memories and not articles:
        log("No content for weekly rollup")
        return

    system = """You write weekly intelligence strategic summaries. Format:

# WEEK IN INTELLIGENCE — [date range]

## BLUF
One paragraph: the single most important trend or development of the week.

## ESCALATIONS
What got worse this week. Threat actors that became more active, vulnerability classes that expanded, geopolitical tensions that increased.

## RESOLUTIONS
What was patched, contained, or de-escalated. Successful operations, diplomatic wins.

## TRENDS
Patterns across the week's events. Recurring TTPs, common target sectors, emerging threat actor behaviors.

## PATCH STATUS SUMMARY
Table of critical CVEs from the week: CVE | Product | Status | Priority

## WATCH LIST (NEXT WEEK)
3-5 things to monitor in the coming week based on trajectory.

## ASSESSMENT
2-3 paragraph strategic assessment. What does this week mean for the reader's security posture?

STYLE: PDB-adjacent but allowed to be slightly more analytical. Still terse, still sourced, but synthesis is the goal here rather than raw reporting. ~2000-3000 words."""

    user = f"""Write the weekly intelligence rollup for the week ending {time.strftime('%d %b %Y')}.

THIS WEEK'S DAILY BRIEFINGS:
{articles[:8000]}

RAW INTELLIGENCE FROM THE WEEK:
{memories[:6000]}

Synthesize into the weekly strategic rollup."""

    result = call_llm(system, user, max_tokens=5000)
    if not result or len(result) < 500:
        log("Weekly generation failed")
        return

    lines = result.strip().split("\n")
    title = lines[0].strip().lstrip("#").strip()
    body = "\n".join(lines[1:]).strip()

    # Image
    try:
        img_path = generate_image(
            "Strategic intelligence briefing room with world map, weekly report charts, dark professional tones, blue accents, no text",
            section="security"
        )
    except Exception:
        img_path = None

    # Publish
    dt = time.strftime("%Y-%m-%d")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
    filename = f"{dt}-{slug}.md"

    hugo_image = ""
    if img_path and Path(img_path).exists():
        img_dest = IMAGES_DIR / f"{dt}-{slug}.webp"
        subprocess.run(["cwebp", "-q", "82", "-resize", "1200", "0", img_path, "-o", str(img_dest)],
                       capture_output=True, timeout=30)
        hugo_image = f"/images/securities/{dt}-{slug}.webp"

    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-07:00")
    front_matter = f'''---
title: "📊 {title.replace('"', '')}"
date: {timestamp}
draft: false
categories: ["securities"]
tags: ["weekly", "strategic", "rollup", "trends"]
description: "Weekly intelligence strategic rollup — {time.strftime('%d %b %Y')}"
'''
    if hugo_image:
        front_matter += f'cover:\n  image: "{hugo_image}"\n  alt: "{title}"\n  relative: false\n'
    front_matter += "---\n\n"
    if hugo_image:
        body = f"![{title}]({hugo_image})\n\n{body}"

    output = CONTENT_DIR / filename
    output.write_text(front_matter + body)
    log(f"Published: security/{filename}")

    subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
    subprocess.run(["git", "commit", "-m", f"security: weekly rollup {dt}"],
                   cwd=HUGO_ROOT, capture_output=True, timeout=30)
    subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, timeout=60)

    nova_config.post_both(
        f":bar_chart: *Nova Security — Week in Intelligence*\n*{title}*",
        slack_channel=nova_config.SLACK_NOTIFY
    )
    log(f"=== Weekly rollup complete: {title} ===")


if __name__ == "__main__":
    run()
