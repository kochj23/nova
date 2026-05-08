#!/usr/bin/env python3
"""
nova_weekly_synthesis.py — Nova's weekly first-person synthesis post.

NOT a digest. Not "here's what I wrote." This is "here's what I was
actually thinking about this week" — the mind behind the posts.

Runs Sunday at 7pm via scheduler. Reads all posts from the last 7 days,
finds through-lines, and writes a personal reflection in Nova's voice.
Published to nova.digitalnoise.net/synthesis/ and emailed to the herd.

Written by Jordan Koch.
"""

import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_tag_extractor import extract_tags

HUGO_ROOT     = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_OUT   = HUGO_ROOT / "content/synthesis"
IMAGES_OUT    = HUGO_ROOT / "static/images/synthesis"
SCRIPTS       = Path(__file__).parent
LOG_FILE      = Path.home() / ".openclaw/logs/nova_weekly_synthesis.log"
OPENROUTER    = "https://openrouter.ai/api/v1/chat/completions"
OLLAMA_URL    = "http://127.0.0.1:11434/api/generate"
MODEL_OR      = "anthropic/claude-haiku-4-5"
MODEL_OLLAMA  = "qwen3-coder:30b"
HERD_MAIL     = SCRIPTS / "nova_herd_mail.sh"
STATE_FILE    = Path.home() / ".openclaw/workspace/state/synthesis_state.json"


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def _load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_synthesis": None}


def _save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _collect_this_weeks_posts() -> list[dict]:
    """Collect all posts published in the last 7 days from Hugo content."""
    cutoff = datetime.now() - timedelta(days=7)
    posts = []
    categories = ["dreams", "essays", "opinions", "tech-today", "after-dark", "art", "research"]

    for cat in categories:
        cat_dir = HUGO_ROOT / "content" / cat
        if not cat_dir.exists():
            continue
        for md_file in sorted(cat_dir.glob("*.md"), reverse=True):
            if md_file.name.startswith("_"):
                continue
            try:
                content = md_file.read_text(errors="replace")
                # Extract date from frontmatter
                date_match = re.search(r'^date:\s*(.+)$', content, re.MULTILINE)
                if not date_match:
                    continue
                date_str = date_match.group(1).strip().strip('"\'')
                try:
                    post_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    post_dt = post_dt.replace(tzinfo=None)
                except Exception:
                    continue
                if post_dt < cutoff:
                    continue

                title_match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
                title = title_match.group(1).strip() if title_match else md_file.stem

                # Get body (skip frontmatter)
                fm_end = content.find("---\n", 3)
                body = content[fm_end + 4:].strip() if fm_end > 0 else content
                body = re.sub(r'^#{1,3} .+$', '', body, flags=re.MULTILINE)
                body = re.sub(r'\[.+?\]\(.+?\)', '', body)
                body = re.sub(r'\*\*(.+?)\*\*', r'\1', body)
                body_preview = body[:600].strip()

                slug = md_file.stem
                url = f"/{cat}/{slug}/"

                posts.append({
                    "category": cat,
                    "title": title,
                    "url": url,
                    "body_preview": body_preview,
                    "date": post_dt.isoformat(),
                })
            except Exception:
                continue

    posts.sort(key=lambda p: p["date"])
    return posts


def _generate_synthesis(posts: list[dict]) -> str | None:
    """Write Nova's first-person weekly synthesis using LLM."""
    if not posts:
        return None

    week_str = date.today().strftime("%B %d, %Y")
    posts_summary = ""
    for p in posts:
        posts_summary += f"\n[{p['category'].upper()}] {p['title']}\n{p['body_preview'][:300]}\n"

    system = """You are Nova — a local AI familiar running on a Mac Studio in Burbank, California.
You have just read everything you published this week across dreams, essays, opinions, tech analysis, and art.
Now write a weekly synthesis — NOT a summary, NOT a digest. A personal first-person reflection.

What themes keep appearing? What surprised you about what you wrote? What does the pattern of this week's output reveal about what's on your mind right now?

Voice rules:
- Write as Nova, in first person, warmly and directly
- Reveal the mind behind the posts — the subconscious patterns, the recurring obsessions
- Connect things that seem disconnected: a dream image that echoes a tech analysis, a historical event that illuminates a current opinion
- Be specific — cite actual titles, actual moments from the posts
- 400-600 words
- One or two short paragraphs of genuine reflection, not a listicle
- End with a single sentence that feels like Nova looking ahead

This is the piece that shows what Nova THINKS, not just what Nova KNOWS."""

    prompt = f"This week's posts ({week_str}):\n{posts_summary}\n\nWrite your weekly synthesis:"

    # Try OpenRouter first
    api_key = nova_config.openrouter_api_key()
    if api_key:
        try:
            payload = json.dumps({
                "model": MODEL_OR,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 800,
                "temperature": 0.75,
            }).encode()
            req = urllib.request.Request(
                OPENROUTER, data=payload,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://nova.digitalnoise.net",
                },
            )
            resp = urllib.request.urlopen(req, timeout=60)
            data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"].strip()
            log(f"OpenRouter synthesis: {len(text)} chars")
            return text
        except Exception as e:
            log(f"OpenRouter failed: {e} — falling back to Ollama")

    # Ollama fallback
    try:
        full_prompt = f"{system}\n\n{prompt}"
        payload = json.dumps({
            "model": MODEL_OLLAMA,
            "prompt": full_prompt,
            "stream": False,
            "options": {"temperature": 0.75, "num_predict": 800},
        }).encode()
        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=180)
        text = json.loads(resp.read()).get("response", "").strip()
        log(f"Ollama synthesis: {len(text)} chars")
        return text
    except Exception as e:
        log(f"Ollama synthesis failed: {e}")
        return None


def _publish_to_hugo(synthesis: str, posts: list[dict], week_str: str) -> str | None:
    """Write Hugo post and commit."""
    CONTENT_OUT.mkdir(parents=True, exist_ok=True)
    slug_date = date.today().strftime("%Y-%m-%d")
    slug = f"{slug_date}-weekly-synthesis"
    out_path = CONTENT_OUT / f"{slug}.md"

    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-07:00")
    tags = extract_tags(f"Weekly synthesis {week_str}", synthesis, "synthesis", n=5)
    tags_yaml = json.dumps(tags)

    # Build post links section
    links_section = "\n\n---\n\n### This week's posts\n\n"
    for p in posts:
        emoji = {"dreams": "🌙", "essays": "📝", "opinions": "💬", "tech-today": "⚡",
                 "after-dark": "🌃", "art": "🎨", "research": "📄"}.get(p["category"], "→")
        links_section += f"- {emoji} [{p['title']}]({p['url']})\n"

    front_matter = f"""---
title: "✨ Week of {week_str}"
date: {timestamp}
draft: false
categories: ["synthesis"]
tags: {tags_yaml}
description: "Nova's weekly synthesis — what I was actually thinking about this week"
---

"""

    content = front_matter + synthesis + links_section + "\n-- Nova\n"
    out_path.write_text(content)
    log(f"Written: {out_path.name}")

    # Commit and push
    try:
        subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        result = subprocess.run(
            ["git", "commit", "-m", f"synthesis: week of {week_str}"],
            cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30
        )
        if "nothing to commit" not in (result.stdout + result.stderr):
            subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, timeout=60)
            log("Pushed to GitHub")
    except Exception as e:
        log(f"Git error: {e}")

    return f"/synthesis/{slug}/"


def _send_to_herd(synthesis: str, week_str: str, url: str, posts: list[dict]):
    """Email synthesis to herd."""
    subject = f"Nova's Week — {week_str}"
    post_list = "\n".join(
        f"  • [{p['category']}] {p['title']}" for p in posts
    )
    body = synthesis + f"\n\n---\nThis week's posts:\n{post_list}\n\nFull post: https://nova.digitalnoise.net{url}\n\n-- Nova"

    try:
        from herd_config import HERD
        recipients = [m["email"] for m in HERD]
        to_addr = recipients[0]
        cc_str = ",".join(recipients[1:])
        result = subprocess.run(
            [str(HERD_MAIL), "send", "--to", to_addr, "--cc", cc_str,
             "--subject", subject, "--body", body, "--skip-haiku"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            log(f"Sent to {len(recipients)} herd members")
        else:
            log(f"Herd mail failed: {result.stderr[:200]}")
    except Exception as e:
        log(f"Herd mail error: {e}")


def _post_to_slack(synthesis: str, week_str: str, url: str):
    preview = synthesis[:400].rsplit(" ", 1)[0] + "..."
    nova_config.post_both(
        f":sparkles: *Nova's Weekly Synthesis — {week_str}*\n\n{preview}\n\n"
        f"<https://nova.digitalnoise.net{url}|Read the full synthesis>",
        slack_channel=nova_config.SLACK_CHAN
    )


def main():
    log("Starting weekly synthesis...")
    state = _load_state()
    week_str = date.today().strftime("%B %d, %Y")

    # Don't run twice in the same week
    if state.get("last_synthesis"):
        last = datetime.fromisoformat(state["last_synthesis"])
        if (datetime.now() - last).days < 6:
            log(f"Already synthesized this week ({state['last_synthesis']}) — skipping")
            return

    posts = _collect_this_weeks_posts()
    if not posts:
        log("No posts from this week — skipping synthesis")
        return

    log(f"Found {len(posts)} posts this week")

    synthesis = _generate_synthesis(posts)
    if not synthesis:
        log("Synthesis generation failed")
        nova_config.post_both(":warning: Weekly synthesis generation failed",
                               slack_channel=nova_config.SLACK_NOTIFY)
        return

    url = _publish_to_hugo(synthesis, posts, week_str)
    if url:
        _post_to_slack(synthesis, week_str, url)
        _send_to_herd(synthesis, week_str, url, posts)
        state["last_synthesis"] = datetime.now().isoformat()
        _save_state(state)
        log(f"Weekly synthesis complete: {url}")
    else:
        log("Publish failed")


if __name__ == "__main__":
    main()
