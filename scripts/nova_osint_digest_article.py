#!/usr/bin/env python3
"""nova_osint_digest_article.py — turns fresh OSINT findings into a published
nova-journal article in Nova's voice, with a cover image.

Runs weekly (after osint_amass/osint_theharvester's Sunday jobs and the daily
osint_hibp check). Looks at osint_findings for anything the individual scanner
scripts already flagged as new (severity 'warning' or 'critical' — they do
their own diffing against prior known findings before insert, so this script
doesn't need its own state tracking). If nothing new turned up, it skips
publishing entirely rather than writing a "nothing happened" article.

Reuses the existing publish pipeline (nova_voice for tone, nova_image_utils
for the cover image, git commit/push to nova-journal) — same shape as
nova_rando_daily_ops.py, just fed OSINT data instead of ops telemetry.

Written by Jordan Koch (via Claude).
"""
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import psycopg2
import psycopg2.extras

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.home() / ".openclaw"))

import nova_config
import nova_journal
from nova_image_utils import generate_image
from nova_notify import notify as nova_notify
from nova_voice import system_prompt, CONTEXT_JOURNAL_SECURITY


def call_llm(system: str, user: str, max_tokens: int = 8000) -> str:
    # Prefer Claude Code Max (sonnet, flat-rate) -- matches nova_rando_daily_ops.py.
    # call_openrouter's default CLI model is haiku, which under-delivers on the
    # mandated snark/profanity; sonnet holds the voice instructions properly.
    try:
        import nova_claude_code
        return nova_claude_code.claude_generate(user, system=system)
    except Exception as e:
        log(f"claude_generate (sonnet) failed, falling back to haiku: {e}")
        return nova_journal.call_openrouter(system, user, max_tokens=max_tokens)

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
HUGO_ROOT = Path.home() / "nova-journal"
CONTENT_DIR = HUGO_ROOT / "content/operations"
IMAGES_DIR = HUGO_ROOT / "static/images/operations"
LOG_FILE = Path.home() / ".openclaw/logs/osint_digest_article.log"
LOOKBACK_DAYS = 7


def log(msg):
    line = f"[osint-digest {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def gather_new_findings():
    conn = psycopg2.connect(DSN)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT tool, target, finding_type, finding, severity, ts
        FROM osint_findings
        WHERE severity IN ('warning', 'critical')
        AND ts > now() - interval '%s days'
        ORDER BY severity DESC, ts DESC
        LIMIT 200
    """, (LOOKBACK_DAYS,))
    rows = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return rows


def generate_article(findings):
    by_tool = {}
    for f in findings:
        by_tool.setdefault(f["tool"], []).append(f)

    data_block = "\n".join(
        f"[{f['severity'].upper()}] {f['tool']} / {f['target']} — {f['finding_type']}: {f['finding']}"
        for f in findings
    )

    system = system_prompt(CONTEXT_JOURNAL_SECURITY + """
ADDITIONAL CONTEXT FOR THIS COLUMN:
- This is Nova's own OSINT self-recon column: what Amass, theHarvester, and
  HaveIBeenPwned found this week when pointed at Jordan's own public-facing
  domains and accounts. It is attack-surface awareness, not a real intrusion.
- Lead with the most consequential findings (breach exposures beat a new
  subdomain). If it's all routine new-subdomain noise, say so and roast it.
- Explain WHY each finding matters in plain terms — a stray subdomain is a
  bigger deal than it sounds, a breach hit is worse than it sounds.
- Do NOT include a title (added separately).
- Length: 800-1800 words.
""")
    user = f"""New OSINT findings from the past {LOOKBACK_DAYS} days, across {len(by_tool)} tool(s):

{data_block}

Write this week's OSINT self-recon column."""
    return call_llm(system, user, max_tokens=8000)


def generate_title(article_preview):
    system = ("Generate a single funny, sarcastic, profane-if-it-lands title for an "
              "OSINT/attack-surface findings column. Max 15 words. Output ONLY the title, no quotes.")
    title = call_llm(system, article_preview[:1000], max_tokens=50)
    return (title or "This Week In Nova Stalking Herself").strip().strip('"').strip("'").replace('"', '')


def publish(title, body, image_path, severity_max):
    date = time.strftime("%Y-%m-%d")
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:60]

    CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    hugo_image = ""
    if image_path and Path(image_path).exists():
        img_filename = f"{date}-{slug}.webp"
        img_dest = IMAGES_DIR / img_filename
        try:
            subprocess.run(["cwebp", "-q", "82", "-resize", "1200", "0", str(image_path), "-o", str(img_dest)],
                           capture_output=True, timeout=30)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            shutil.copy2(image_path, img_dest)
        if img_dest.exists():
            hugo_image = f"/images/operations/{img_filename}"

    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-07:00")
    front_matter = f'''---
title: "{title.replace('"', '')}"
date: {timestamp}
draft: false
categories: ["operations"]
tags: ["osint", "security", "attack-surface", "sarcasm"]
description: "Nova's weekly OSINT self-recon — what Amass, theHarvester, and HIBP found pointed at her own house."
'''
    if hugo_image:
        front_matter += f'cover:\n  image: "{hugo_image}"\n  alt: "{title}"\n  relative: false\n'
    front_matter += "---\n\n"

    pub_time = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p PT")
    post_path = CONTENT_DIR / f"{date}-{slug}.md"
    post_path.write_text(front_matter + f"*Published {pub_time}*\n\n" + body)
    log(f"Post written: {post_path.name}")

    # Hardened commit + push (PG advisory lock, rebase-on-reject, retry, alert-on-failure).
    nova_journal.git_push("osint", title)

    url = f"https://nova.digitalnoise.net/operations/{date}-{slug}/"
    nova_config.post_both(f"OSINT digest posted — {title}\n{url}", slack_channel=nova_config.SLACK_FEED)
    if severity_max == "critical":
        nova_notify("OSINT digest: new breach exposure found", body=f"{title}\n{url}",
                    level="critical", category="security", dedup_key=None)


def main():
    findings = gather_new_findings()
    if not findings:
        log(f"No new (warning/critical) OSINT findings in last {LOOKBACK_DAYS} days — skipping article")
        return
    log(f"{len(findings)} new finding(s) across "
        f"{len({f['tool'] for f in findings})} tool(s) — writing article")

    severity_max = "critical" if any(f["severity"] == "critical" for f in findings) else "warning"

    article = generate_article(findings)
    if not article or len(article) < 200:
        log("Article generation failed or too short — aborting")
        return
    title = generate_title(article)
    log(f"Title: {title}")

    image_prompt = (
        "A sarcastic AI robot detective examining a digital surveillance board covered in "
        "subdomains, email addresses, and breach alerts, magnifying glass in hand, dramatic "
        "noir lighting, cyberpunk detective office. Digital art style."
    )
    try:
        image_path = generate_image(image_prompt, section="osint_digest")
    except Exception as e:
        log(f"Image generation failed: {e}")
        image_path = None

    publish(title, article, image_path, severity_max)
    log("Done!")


if __name__ == "__main__":
    main()
