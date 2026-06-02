#!/usr/bin/env python3
"""
nova_journal_security.py — Daily security intelligence briefing + breaking alerts.

Daily (9am): PDB-style briefing covering all security events from the past 24 hours
  - Cyber threats (CVEs, APTs, exploits, breaches)
  - Military/geopolitical (US force posture, NATO, conflict zones)
  - Physical security (SoCal/LA area, critical infrastructure)

Breaking: Immediate article + Slack/chat alert on:
  - Any actively-exploited CVE
  - Nation-state APT campaigns
  - Critical infrastructure attacks
  - Military escalations involving US/NATO
  - Mass-exploitation events
  - Major physical security events in SoCal/LA

Tone: Presidential Daily Brief — terse, factual, bullet-pointed, confidence levels,
sources cited. No personality, no humor.

Written by Jordan Koch (via Claude).
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
from nova_image_utils import generate_image

# ── Config ────────────────────────────────────────────────────────────────────

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = HUGO_ROOT / "content/securities"
IMAGES_DIR = HUGO_ROOT / "static/images/securities"
LOG_FILE = Path.home() / ".openclaw/logs/nova_journal_security.log"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = "anthropic/claude-sonnet-4-6"
MEMORY_SERVER = f"http://{nova_config.NOVA_HOST}:18790"
SEARXNG_URL = "http://127.0.0.1:8888/search"

CONTENT_DIR.mkdir(parents=True, exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[security-journal {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ── API Key ───────────────────────────────────────────────────────────────────

def get_openrouter_key() -> str:
    return nova_config.openrouter_api_key()


def call_llm(system: str, user: str, max_tokens: int = 6000, temperature: float = 0.3) -> str:
    api_key = get_openrouter_key()
    body = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode()
    req = urllib.request.Request(OPENROUTER_URL, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://nova.digitalnoise.net",
        "X-Title": "Nova Security Journal",
    })
    resp = urllib.request.urlopen(req, timeout=300)
    data = json.loads(resp.read())
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    log(f"LLM [{MODEL}] in={usage.get('prompt_tokens','?')} out={usage.get('completion_tokens','?')}")
    return text


# ── Memory Fetching ───────────────────────────────────────────────────────────

def recall_memories(query: str, n: int = 30, source: str = None) -> list[dict]:
    params = {"q": query, "n": str(n)}
    if source:
        params["source"] = source
    url = f"{MEMORY_SERVER}/recall?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        return data if isinstance(data, list) else data.get("results", data.get("memories", []))
    except Exception as e:
        log(f"Memory recall failed: {e}")
        return []


def get_recent_security_memories(hours: int = 24) -> list[dict]:
    """Fetch recent memories from intelligence, military_history, law, and politics vectors."""
    memories = []
    for source in ["intelligence", "military_history", "law", "politics"]:
        try:
            result = subprocess.run(
                ["psql", "-h", "192.168.1.6", "-U", "kochj", "-d", "nova_memories", "-tA", "-c",
                 f"SELECT text, source, metadata::text FROM memories "
                 f"WHERE source = '{source}' "
                 f"AND created_at >= now() - interval '{hours} hours' "
                 f"AND LENGTH(text) > 60 "
                 f"ORDER BY created_at DESC LIMIT 40;"],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0:
                for line in result.stdout.strip().split("\n"):
                    if line.strip():
                        parts = line.split("|")
                        if parts[0].strip():
                            memories.append({
                                "text": parts[0].strip()[:500],
                                "source": parts[1].strip() if len(parts) > 1 else source,
                            })
        except Exception as e:
            log(f"PG query failed for {source}: {e}")
    return memories


def search_news(query: str, n: int = 5) -> list[dict]:
    """Search SearxNG for breaking news."""
    params = urllib.parse.urlencode({"q": query, "format": "json", "categories": "news", "time_range": "day"})
    url = f"{SEARXNG_URL}?{params}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read())
        results = []
        for r in data.get("results", [])[:n]:
            results.append({
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "content": r.get("content", "")[:300],
            })
        return results
    except Exception:
        return []


# ── Publishing ────────────────────────────────────────────────────────────────

def publish_hugo(title: str, body: str, tags: list[str], description: str,
                 image_path: str | None = None, is_breaking: bool = False) -> str:
    dt = time.strftime("%Y-%m-%d")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
    filename = f"{dt}-{slug}.md"

    hugo_image = ""
    if image_path and Path(image_path).exists():
        img_dest = IMAGES_DIR / f"{dt}-{slug}.webp"
        try:
            subprocess.run(
                ["cwebp", "-q", "82", "-resize", "1200", "0", image_path, "-o", str(img_dest)],
                capture_output=True, timeout=30
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            import shutil
            shutil.copy2(image_path, img_dest)
        hugo_image = f"/images/securities/{dt}-{slug}.webp"

    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-07:00")
    tags_yaml = json.dumps(tags)
    safe_title = title.replace('"', '')
    emoji = "🚨 🚨" if is_breaking else "🛡️"
    display_title = f"{emoji} {safe_title}"

    front_matter = f'''---
title: "{display_title}"
date: {timestamp}
draft: false
categories: ["securities"]
tags: {tags_yaml}
description: "{description.replace('"', "'")}"
'''
    if hugo_image:
        front_matter += f'cover:\n  image: "{hugo_image}"\n  alt: "{safe_title}"\n  relative: false\n'
    front_matter += "---\n\n"

    if hugo_image:
        body = f"![{safe_title}]({hugo_image})\n\n{body}"

    output = CONTENT_DIR / filename
    output.write_text(front_matter + body)
    log(f"Published: security/{filename}")

    # Git push
    try:
        subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        msg = f"security: {dt} — {title[:50]}"
        result = subprocess.run(["git", "commit", "-m", msg], cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, timeout=60)
            log("Pushed to GitHub")
    except Exception as e:
        log(f"Git error: {e}")

    return filename


def notify(title: str, preview: str, is_breaking: bool = False):
    emoji = ":rotating_light::rotating_light:" if is_breaking else ":shield:"
    prefix = "BREAKING" if is_breaking else "Daily Briefing"
    msg = f"{emoji} *Nova Securities — {prefix}*\n*{title}*\n_{preview[:250]}_"
    # Always post to notifications
    nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)
    # Breaking alerts also go to Nova's chat
    if is_breaking:
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_CHAT)


# ── Daily Briefing ────────────────────────────────────────────────────────────

def generate_daily_briefing():
    """Generate the daily PDB-style security briefing."""
    log("=== Generating daily security briefing ===")

    memories = get_recent_security_memories(24)
    if not memories:
        log("No security memories in last 24h — skipping")
        return

    # Also search for breaking news
    cyber_news = search_news("cybersecurity vulnerability exploit breach 2026")
    military_news = search_news("US military NATO deployment 2026")
    local_news = search_news("Los Angeles security crime emergency 2026")

    memory_block = "\n".join(f"- [{m.get('source','?')}] {m['text'][:300]}" for m in memories[:60])
    news_block = ""
    if cyber_news:
        news_block += "\nCYBER NEWS (last 24h):\n" + "\n".join(f"- {n['title']}: {n['content'][:150]}" for n in cyber_news)
    if military_news:
        news_block += "\nMILITARY NEWS (last 24h):\n" + "\n".join(f"- {n['title']}: {n['content'][:150]}" for n in military_news)
    if local_news:
        news_block += "\nLOCAL (LA/SoCal):\n" + "\n".join(f"- {n['title']}: {n['content'][:150]}" for n in local_news)

    system = """You write Presidential Daily Brief-style security intelligence summaries. Rules:

FORMAT:
- Start with a one-line BLUF (Bottom Line Up Front) — the single most important thing
- Then 3-5 sections: CYBER, MILITARY/GEOPOLITICAL, PHYSICAL/LOCAL, NUCLEAR/WMD (if applicable), ASSESSMENT
- Each section: 3-7 bullet points maximum
- Each bullet: one fact, one source attribution in brackets, one confidence level if uncertain
- End with KEY JUDGMENTS (2-3 sentences of analytical assessment)

STYLE:
- Terse. No filler words. No adjectives unless they convey information.
- "[HIGH CONFIDENCE]", "[MODERATE CONFIDENCE]", "[LOW CONFIDENCE]" where applicable
- Source attribution: [CISA], [NCSC-UK], [Krebs], [SANS], [Unit42], etc.
- "NOSIG" (no significant activity) for quiet sections — don't fabricate threats
- Dates in DD MMM format (02 JUN)
- Times in 24h Zulu (1400Z) where relevant
- No editorializing, no recommendations unless specifically about immediate action required

CONTENT PRIORITIES (for the reader — a senior SRE/infrastructure engineer in Los Angeles):
1. Actively-exploited vulnerabilities affecting production infrastructure
2. APT campaigns targeting US/allied organizations
3. Military posture changes involving US/NATO forces
4. Critical infrastructure threats (power, water, telecom, internet backbone)
5. Physical security events in Southern California
6. Supply chain attacks, dependency compromises
7. Nuclear/WMD developments (IAEA reports, test activity)

OUTPUT: Title line (no markdown header) + body. No preamble. ~1000-2000 words."""

    user = f"""Write today's security intelligence briefing ({time.strftime('%d %b %Y')}).

INTELLIGENCE FROM LAST 24 HOURS (Nova's ingested feeds — CISA, NCSC, FBI, Krebs, Talos, Unit42, Bellingcat, War on the Rocks, etc.):
{memory_block}

LIVE NEWS SEARCH RESULTS:
{news_block}

Write the PDB. If a section has no significant activity, mark it NOSIG and move on. Do not invent threats."""

    result = call_llm(system, user, max_tokens=4000)
    if not result or len(result) < 300:
        log("LLM generation failed or too short")
        return

    # Extract title
    lines = result.strip().split("\n")
    title = lines[0].strip().lstrip("#").strip()
    body = "\n".join(lines[1:]).strip()

    # Generate image
    img_prompt = "Satellite surveillance view of global threat map, dark blue tones, digital grid overlay, minimal, no text"
    try:
        img_path = generate_image(img_prompt, section="security")
    except Exception as e:
        log(f"Image gen failed: {e}")
        img_path = None

    # Publish
    tags = ["daily-briefing", "pdb", "cyber", "military", "osint"]
    description = f"Daily security intelligence briefing — {time.strftime('%d %b %Y')}"
    publish_hugo(title, body, tags, description, image_path=img_path)
    notify(title, body[:200])
    log(f"=== Daily briefing complete: {title} ===")


# ── Breaking Alert ────────────────────────────────────────────────────────────

def generate_breaking_alert(trigger: str, details: str):
    """Generate an immediate breaking security alert."""
    log(f"=== BREAKING ALERT: {trigger} ===")

    # Gather context
    context_memories = recall_memories(trigger, n=15, source="intelligence")
    context_block = "\n".join(f"- {m.get('text', '')[:200]}" for m in context_memories[:10])

    system = """You write BREAKING security alerts in PDB style. Rules:

FORMAT:
- BLUF first line — what happened, who is affected, what to do
- DETAILS section — 3-5 bullets of confirmed facts only
- IMPACT section — who/what is affected, scope
- RECOMMENDED ACTIONS — immediate steps (if any)
- SOURCES — attribution

STYLE:
- URGENT tone but factual — no speculation, no fear-mongering
- If details are uncertain, say so explicitly
- ~300-600 words maximum
- No preamble, no sign-off

OUTPUT: Title line + body."""

    user = f"""BREAKING security event. Generate an alert.

TRIGGER: {trigger}

DETAILS PROVIDED:
{details}

RELATED CONTEXT FROM NOVA'S MEMORY:
{context_block}

Write the breaking alert. Only include confirmed information. Flag uncertainty explicitly."""

    result = call_llm(system, user, max_tokens=2000, temperature=0.2)
    if not result or len(result) < 100:
        log("Breaking alert generation failed")
        return

    lines = result.strip().split("\n")
    title = lines[0].strip().lstrip("#").strip()
    body = "\n".join(lines[1:]).strip()

    # Generate alert image
    try:
        img_path = generate_image(
            "Red alert warning screen, cyber attack visualization, urgent dark red glow, network under attack, no text",
            section="security"
        )
    except Exception:
        img_path = None

    tags = ["breaking", "alert"] + [t.lower().replace(" ", "-") for t in trigger.split()[:3]]
    description = f"BREAKING: {trigger[:100]}"
    publish_hugo(title, body, tags, description, image_path=img_path, is_breaking=True)
    notify(title, body[:200], is_breaking=True)
    log(f"=== Breaking alert published: {title} ===")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "breaking":
        trigger = sys.argv[2] if len(sys.argv) > 2 else "Unknown security event"
        details = sys.argv[3] if len(sys.argv) > 3 else ""
        generate_breaking_alert(trigger, details)
    else:
        generate_daily_briefing()
