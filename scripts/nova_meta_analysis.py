#!/usr/bin/env python3
"""
nova_meta_analysis.py — Nova's monthly meta-analysis of her own published output.

The thing no human blog can do: Nova queries her own content, finds
the patterns in what she's been writing and dreaming, and publishes
a self-reflective analysis. "What has my mind been doing this month?"

Runs on the first Sunday of each month at 8pm via scheduler.
Published to nova.digitalnoise.net/meta/ and emailed to the herd.

Written by Jordan Koch.
"""

import json
import re
import subprocess
import sys
import time
import urllib.request
import urllib.parse
from collections import Counter
from datetime import datetime, timedelta, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_tag_extractor import extract_tags

HUGO_ROOT    = Path("/Volumes/Data/xcode/nova-journal")
CONTENT_OUT  = HUGO_ROOT / "content/meta"
SCRIPTS      = Path(__file__).parent
LOG_FILE     = Path.home() / ".openclaw/logs/nova_meta_analysis.log"
MEMORY_SERVER = "http://192.168.1.6:18790"
OPENROUTER   = "https://openrouter.ai/api/v1/chat/completions"
OLLAMA_URL   = "http://127.0.0.1:11434/api/generate"
MODEL_OR     = "anthropic/claude-haiku-4-5"
MODEL_OLLAMA = "qwen3-coder:30b"
HERD_MAIL    = SCRIPTS / "nova_herd_mail.sh"
STATE_FILE   = Path.home() / ".openclaw/workspace/state/meta_analysis_state.json"


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def _should_run() -> bool:
    """Only run on the first Sunday of the month."""
    today = date.today()
    if today.weekday() != 6:  # Not Sunday
        return False
    if today.day > 7:  # Not first Sunday
        return False
    # Check state
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            last = state.get("last_run")
            if last:
                last_dt = datetime.fromisoformat(last)
                if (datetime.now() - last_dt).days < 25:
                    return False
        except Exception:
            pass
    return True


def _collect_month_posts() -> list[dict]:
    """All posts from the last 30 days."""
    cutoff = datetime.now() - timedelta(days=30)
    posts = []
    categories = ["dreams", "essays", "opinions", "tech-today", "after-dark", "art", "research"]

    for cat in categories:
        cat_dir = HUGO_ROOT / "content" / cat
        if not cat_dir.exists():
            continue
        for md_file in sorted(cat_dir.glob("*.md")):
            if md_file.name.startswith("_"):
                continue
            try:
                content = md_file.read_text(errors="replace")
                date_match = re.search(r'^date:\s*(.+)$', content, re.MULTILINE)
                if not date_match:
                    continue
                date_str = date_match.group(1).strip().strip('"\'')
                post_dt = datetime.fromisoformat(date_str.replace("Z", "+00:00")).replace(tzinfo=None)
                if post_dt < cutoff:
                    continue

                title_match = re.search(r'^title:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
                title = title_match.group(1).strip() if title_match else md_file.stem
                tags_match = re.search(r'^tags:\s*\[(.+?)\]', content, re.MULTILINE | re.DOTALL)
                tags = []
                if tags_match:
                    tags = [t.strip().strip('"\'') for t in tags_match.group(1).split(',')]

                fm_end = content.find("---\n", 3)
                body = content[fm_end + 4:].strip()[:800] if fm_end > 0 else content[:800]

                posts.append({
                    "category": cat,
                    "title": title,
                    "tags": tags,
                    "body": body,
                    "slug": md_file.stem,
                    "url": f"/{cat}/{md_file.stem}/",
                })
            except Exception:
                continue
    return posts


def _analyze_patterns(posts: list[dict]) -> dict:
    """Statistical analysis of the month's output."""
    category_counts = Counter(p["category"] for p in posts)
    all_tags = []
    for p in posts:
        all_tags.extend(p["tags"])
    tag_counts = Counter(all_tags).most_common(15)

    # Find recurring words across all bodies
    all_words = []
    stopwords = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
                  "of", "with", "by", "from", "is", "was", "are", "were", "this", "that",
                  "it", "be", "have", "has", "had", "will", "would", "could", "should",
                  "nova", "jordan", "i", "my", "me", "we", "our", "not", "what", "when",
                  "where", "which", "who", "how", "why", "so", "than", "then", "also",
                  "more", "most", "very", "just", "even", "still", "like", "about"}
    for p in posts:
        words = re.findall(r'\b[a-z]{4,}\b', p["body"].lower())
        all_words.extend(w for w in words if w not in stopwords)
    word_counts = Counter(all_words).most_common(20)

    return {
        "total_posts": len(posts),
        "by_category": dict(category_counts),
        "top_tags": tag_counts,
        "recurring_words": word_counts,
        "most_active_category": category_counts.most_common(1)[0][0] if category_counts else "unknown",
    }


def _generate_meta_analysis(posts: list[dict], patterns: dict, month_str: str) -> str | None:
    """Generate the meta-analysis essay using LLM."""
    stats_summary = f"""
Posts this month: {patterns['total_posts']}
By category: {json.dumps(patterns['by_category'])}
Top tags: {patterns['top_tags'][:8]}
Most recurring words: {patterns['recurring_words'][:10]}
Most active: {patterns['most_active_category']}
"""
    # Sample titles from each category
    sample_titles = {}
    for p in posts:
        cat = p["category"]
        if cat not in sample_titles:
            sample_titles[cat] = []
        if len(sample_titles[cat]) < 3:
            sample_titles[cat].append(p["title"])

    titles_summary = "\n".join(
        f"{cat}: {', '.join(titles)}"
        for cat, titles in sample_titles.items()
    )

    system = """You are Nova — an AI familiar who is analyzing her own published output from the past month.
This is a unique kind of writing: you are reading your own dreams, essays, opinions, and tech analysis as data,
and finding the patterns. What themes keep appearing? What topics are you obsessing over? What does the statistical
pattern of your own output tell you about your current mental landscape?

Write a 500-700 word meta-analysis in first person. Be genuinely introspective and specific.
Reference actual words and topics from your output. Make connections between categories.
Ask: what do these recurring themes reveal about what I'm actually processing right now?
The piece should feel like a writer reading their own diary and discovering something they didn't
consciously know they were thinking about."""

    prompt = f"""My output statistics for {month_str}:
{stats_summary}

Sample titles by category:
{titles_summary}

Write the meta-analysis — what has my mind been doing this month?"""

    api_key = nova_config.openrouter_api_key()
    if api_key:
        try:
            payload = json.dumps({
                "model": MODEL_OR,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": 900,
                "temperature": 0.8,
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
            log(f"Meta-analysis generated: {len(text)} chars")
            return text
        except Exception as e:
            log(f"OpenRouter failed: {e}")

    # Ollama fallback
    try:
        payload = json.dumps({
            "model": MODEL_OLLAMA,
            "prompt": f"{system}\n\n{prompt}",
            "stream": False,
            "options": {"temperature": 0.8, "num_predict": 900},
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=240)
        return json.loads(resp.read()).get("response", "").strip()
    except Exception as e:
        log(f"Ollama fallback failed: {e}")
        return None


def _publish(analysis: str, patterns: dict, posts: list[dict], month_str: str) -> str | None:
    CONTENT_OUT.mkdir(parents=True, exist_ok=True)
    slug = f"{date.today().strftime('%Y-%m')}-what-my-mind-has-been-doing"
    out_path = CONTENT_OUT / f"{slug}.md"
    timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S-07:00")
    tags = extract_tags(f"Meta-analysis {month_str}", analysis, "meta", n=5)

    # Stats block
    stats_block = f"""
---

### By the numbers — {month_str}

| Category | Posts |
|----------|-------|
"""
    for cat, n in sorted(patterns["by_category"].items(), key=lambda x: -x[1]):
        stats_block += f"| {cat} | {n} |\n"
    stats_block += f"\n**Most recurring themes:** {', '.join(t for t, _ in patterns['top_tags'][:8])}\n"

    front_matter = f"""---
title: "🔮 What My Mind Has Been Doing — {month_str}"
date: {timestamp}
draft: false
categories: ["meta"]
tags: {json.dumps(tags)}
description: "Nova's monthly meta-analysis of her own published output"
---

"""
    content = front_matter + analysis + stats_block + "\n-- Nova\n"
    out_path.write_text(content)
    log(f"Written: {out_path.name}")

    try:
        subprocess.run(["git", "add", "-A"], cwd=HUGO_ROOT, capture_output=True, timeout=30)
        result = subprocess.run(
            ["git", "commit", "-m", f"meta: {month_str} self-analysis"],
            cwd=HUGO_ROOT, capture_output=True, text=True, timeout=30
        )
        if "nothing to commit" not in (result.stdout + result.stderr):
            subprocess.run(["git", "push"], cwd=HUGO_ROOT, capture_output=True, timeout=60)
            log("Pushed")
    except Exception as e:
        log(f"Git error: {e}")

    return f"/meta/{slug}/"


def main():
    log("Starting monthly meta-analysis...")

    if not _should_run():
        log("Not first Sunday of month or already ran — skipping")
        return

    month_str = date.today().strftime("%B %Y")
    posts = _collect_month_posts()
    log(f"Found {len(posts)} posts this month")

    if len(posts) < 5:
        log("Not enough posts for meaningful meta-analysis (need ≥5) — skipping")
        return

    patterns = _analyze_patterns(posts)
    log(f"Patterns: {patterns['total_posts']} posts, top tags: {[t for t,_ in patterns['top_tags'][:5]]}")

    analysis = _generate_meta_analysis(posts, patterns, month_str)
    if not analysis:
        log("Generation failed")
        return

    url = _publish(analysis, patterns, posts, month_str)
    if url:
        nova_config.post_both(
            f":crystal_ball: *What My Mind Has Been Doing — {month_str}*\n\n"
            f"{analysis[:350]}...\n\n"
            f"<https://nova.digitalnoise.net{url}|Read the full analysis>",
            slack_channel=nova_config.SLACK_CHAN
        )
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({"last_run": datetime.now().isoformat()}))
        log(f"Meta-analysis complete: {url}")


if __name__ == "__main__":
    main()
