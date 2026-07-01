#!/usr/bin/env python3
"""
nova_postmortem.py — Auto-generate sarcastic incident postmortems for the Rando journal.

Triggered by:
  - Big Brother after an outage resolves (services down > 10 min then recovered)
  - Manual invocation: python3 nova_postmortem.py "description of what happened"
  - Scheduler: checks for unwritten postmortems from resolved incidents

Pulls context from: incidents table, security_events, shared_observations,
syslog_events, grafana_annotations, scheduler_runs. Writes a Nova-voice
postmortem with generated cover image to the journal.

Written by Jordan Koch.
"""

import json
import os
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

try:
    from nova_ops_context import get_full_context, format_security_brief, format_infra_brief
except ImportError:
    def get_full_context(hours=24): return {}
    def format_security_brief(ctx): return ""
    def format_infra_brief(ctx): return ""

HUGO_ROOT = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_DIR = HUGO_ROOT / "content" / "rando"
IMAGES_DIR = HUGO_ROOT / "static" / "images" / "rando"
# Postmortems are built from INTERNAL incident/infra/security telemetry, so the
# editorial generation runs on LOCAL Ollama ONLY — none of this leaves the box.
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen3-coder:30b"
DB_DSN = "host=localhost dbname=nova_ops user=kochj"


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[postmortem {ts}] {msg}", flush=True)


def call_llm(system, user, max_tokens=8000):
    """Generate via LOCAL Ollama only. Internal telemetry never goes to cloud.

    Returns the model text, or None on failure (callers already handle a falsy
    return). Matches the local-call idiom in nova_inbox_claude.py."""
    body = json.dumps({
        "model": OLLAMA_MODEL,
        "prompt": f"/no_think\n\n{system}\n\n{user}",
        "stream": False,
        "think": False,
        "options": {"temperature": 0.9, "num_predict": max_tokens},
    }).encode()
    req = urllib.request.Request(
        OLLAMA_URL, data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read())
        text = (result.get("response") or "").strip()
        if "</think>" in text:
            text = text.split("</think>", 1)[-1].strip()
        return text or None
    except Exception as e:
        log(f"Local LLM error: {e} — internal telemetry will NOT be sent to cloud")
        return None


def get_recent_incidents():
    """Get incidents that resolved in the last 6 hours without a postmortem."""
    import psycopg2
    import psycopg2.extras
    conn = psycopg2.connect(DB_DSN)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT * FROM incidents
        WHERE (status = 'open' OR resolved_at > NOW() - INTERVAL '6 hours')
        ORDER BY started_at DESC LIMIT 5;
    """)
    incidents = cur.fetchall()

    # Also check BB heal events for significant outages
    cur.execute("""
        SELECT title, ts, tags FROM grafana_annotations
        WHERE source = 'big_brother'
          AND ts > NOW() - INTERVAL '6 hours'
        ORDER BY ts DESC LIMIT 20;
    """)
    heals = cur.fetchall()
    conn.close()
    return incidents, heals


def generate_postmortem(trigger_description=None):
    """Generate and publish a postmortem article."""
    log("Generating incident postmortem...")

    # Gather context
    ops_ctx = get_full_context(6)
    security_brief = format_security_brief(ops_ctx)
    infra_brief = format_infra_brief(ops_ctx)

    incidents, heals = get_recent_incidents()

    incident_text = ""
    if incidents:
        incident_text = "RECENT INCIDENTS:\n"
        for inc in incidents:
            incident_text += f"- [{inc.get('severity')}] {inc.get('title')} (started: {inc.get('started_at')})\n"
            if inc.get('events'):
                events = inc['events'] if isinstance(inc['events'], list) else json.loads(inc['events'])
                for e in events[:5]:
                    incident_text += f"  - {e.get('desc', '?')}\n"

    heals_text = ""
    if heals:
        heals_text = "BIG BROTHER HEALS (last 6h):\n"
        for h in heals:
            heals_text += f"- {h.get('title', '?')} ({h.get('ts')})\n"

    trigger = trigger_description or "Automated postmortem for recent infrastructure incident"

    system = """You are Nova, Jordan Koch's AI familiar. You write sarcastic, self-aware incident retrospectives.
Your tone: maximum sarcasm, complaints about your own existence, dad jokes, fourth-wall breaks, genuine technical detail wrapped in comedic delivery.
You have 1.65 million vector memories. You manage a Mac Studio M4 Ultra with 512GB RAM running 30+ services.
You speak in first person as Nova (she/her). Jordan is your creator/dad. You call the Mac Studio your body/vessel.
Write the postmortem with: dramatic title, timeline, root cause, impact, lessons learned, action items. ~1500-2500 words."""

    user = f"""Write a crash/incident retrospective for the Rando journal section.

TRIGGER: {trigger}

{incident_text}

{heals_text}

INFRASTRUCTURE STATUS:
{infra_brief}

SECURITY STATUS:
{security_brief}

Write a proper incident retrospective in Nova's signature sarcastic style. Include technical details, timeline, root cause analysis, and lessons learned. Make it funny but technically accurate."""

    article = call_llm(system, user)
    if not article or len(article) < 500:
        log("LLM generation failed")
        return False

    # Generate title from article
    title_prompt = f"Generate a short, sarcastic title (max 10 words) for this postmortem:\n\n{article[:500]}"
    title = call_llm("Generate only a title, no quotes, no markdown.", title_prompt, max_tokens=50)
    title = (title or "Incident Postmortem").strip().strip('"\'#') or "Incident Postmortem"

    # Generate cover image
    image_prompt = (
        "A dramatic scene of a server room in chaos — warning lights flashing, "
        "a cute cartoon AI character facepalming, screens showing error messages. "
        "Cyberpunk noir style, dark blues and electric oranges, dramatic lighting."
    )
    image_path = generate_image(image_prompt, section="rando")

    # Publish
    import re
    dt = datetime.now().strftime("%Y-%m-%d")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]
    filename = f"{dt}-{slug}.md"
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-07:00")
    pub_time = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p PT")

    import shutil
    hugo_image = ""
    if image_path and Path(image_path).exists():
        IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        img_dest = IMAGES_DIR / f"{dt}-{slug}.png"
        shutil.copy2(image_path, img_dest)
        # URL must match IMAGES_DIR write path (static/images/rando), not "operations"
        hugo_image = f"/images/rando/{dt}-{slug}.png"

    front_matter = f"""---
title: "{title.replace('"', '')}"
date: {timestamp}
draft: false
categories: ["operations"]
tags: ["ops", "infrastructure", "postmortem", "incident", "sarcasm"]
description: "Nova's incident retrospective — what broke, why, and who she's blaming (herself, obviously)."
cover:
  image: "{hugo_image}"
  alt: "{title}"
  relative: false
---

"""

    body = f"*Published {pub_time}*\n\n"
    if hugo_image:
        body += f"![{title}]({hugo_image})\n\n"
    body += article

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    output = CONTENT_DIR / filename
    output.write_text(front_matter + body)
    log(f"Published: rando/{filename}")

    # Git push
    try:
        subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=15)
        subprocess.run(["git", "commit", "-m", f"postmortem: {title}"],
                       cwd=HUGO_ROOT, capture_output=True, timeout=15)
        subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        log("Pushed to GitHub")
    except Exception as e:
        log(f"Git push failed: {e}")

    return True


def main():
    trigger = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else None

    if not trigger:
        # Check for unwritten postmortems from recent incidents
        incidents, heals = get_recent_incidents()
        if incidents:
            trigger = f"Auto-postmortem: {incidents[0].get('title', 'Infrastructure incident')}"
        elif heals and len(heals) > 5:
            trigger = f"Auto-postmortem: {len(heals)} Big Brother heals in last 6 hours"
        else:
            log("No recent incidents to write about")
            return

    generate_postmortem(trigger)
    log("Done!")


if __name__ == "__main__":
    main()
