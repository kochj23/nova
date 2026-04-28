#!/usr/bin/env python3
"""
nova_reddit_ingest.py — Fetch and ingest Reddit posts with full thread context.

Fetches top posts from configured subreddits, including post body and top
comments. Stores in vector memory with subreddit and topic metadata.
Feeds into Nova's dream generation and nightly reports.

Runs via launchd every 4 hours. Deduplicates by post ID.

Written by Jordan Koch.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN

VECTOR_URL = "http://127.0.0.1:18790/remember"
STATE_FILE = Path.home() / ".openclaw/workspace/state/reddit_ingest_state.json"
USER_AGENT = "Nova/1.0 nova_reddit_ingest.py (AI familiar, local use only)"
TODAY = date.today().isoformat()

SUBREDDITS = {
    "burbank": {
        "source": "burbank",
        "label": "Burbank local",
        "limit": 10,
        "dream_weight": "high",
    },
    "glendale": {
        "source": "local",
        "label": "Glendale local",
        "limit": 10,
        "dream_weight": "high",
    },
    "Sovereigncitizen": {
        "source": "reddit",
        "label": "Sovereign citizen",
        "limit": 8,
        "dream_weight": "medium",
    },
    "SipsTea": {
        "source": "reddit",
        "label": "SipsTea humor",
        "limit": 8,
        "dream_weight": "medium",
    },
    "lazerpig": {
        "source": "reddit",
        "label": "LazerPig military/history",
        "limit": 8,
        "dream_weight": "medium",
    },
    "vibecoding": {
        "source": "reddit",
        "label": "Vibe coding",
        "limit": 8,
        "dream_weight": "low",
    },
    "3Dprinting": {
        "source": "reddit",
        "label": "3D printing",
        "limit": 8,
        "dream_weight": "low",
    },
    "avesLA": {
        "source": "socal_rave",
        "label": "LA rave/electronic music",
        "limit": 10,
        "dream_weight": "high",
    },
    "CarPlay": {
        "source": "automotive",
        "label": "Apple CarPlay",
        "limit": 6,
        "dream_weight": "low",
    },
    "chaoticgood": {
        "source": "reddit",
        "label": "Chaotic good",
        "limit": 8,
        "dream_weight": "medium",
    },
    "ClaudeCode": {
        "source": "reddit",
        "label": "Claude Code",
        "limit": 10,
        "dream_weight": "low",
    },
}


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_ids": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # Keep only last 7 days of seen IDs to prevent unbounded growth
    cutoff = (datetime.now().timestamp()) - (7 * 86400)
    cleaned = {k: v for k, v in state.get("seen_ids", {}).items()
               if v.get("ts", 0) > cutoff}
    state["seen_ids"] = cleaned
    STATE_FILE.write_text(json.dumps(state, indent=2))


def fetch_subreddit(name, limit=10):
    """Fetch posts from a subreddit with full selftext."""
    url = f"https://www.reddit.com/r/{name}/hot.json?limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return data.get("data", {}).get("children", [])
    except Exception as e:
        log(f"Error fetching r/{name}: {e}", level=LOG_ERROR, source="reddit_ingest")
        return []


def fetch_comments(subreddit, post_id, limit=5):
    """Fetch top comments for a post."""
    url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json?limit={limit}&sort=best"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        if len(data) < 2:
            return []
        comments = []
        for child in data[1].get("data", {}).get("children", []):
            cd = child.get("data", {})
            if child.get("kind") != "t1":
                continue
            body = cd.get("body", "").strip()
            author = cd.get("author", "")
            score = cd.get("score", 0)
            if body and len(body) > 10:
                comments.append({
                    "author": author,
                    "body": body[:500],
                    "score": score,
                })
        return comments[:limit]
    except Exception as e:
        log(f"Error fetching comments for {post_id}: {e}", level=LOG_WARN, source="reddit_ingest")
        return []


def vector_remember(text, source, metadata):
    payload = json.dumps({"text": text, "source": source, "metadata": metadata}).encode()
    try:
        req = urllib.request.Request(VECTOR_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception:
        return False


def ingest_subreddit(name, config, state):
    """Fetch and ingest posts from one subreddit."""
    source = config["source"]
    label = config["label"]
    limit = config.get("limit", 10)
    dream_weight = config.get("dream_weight", "medium")

    posts = fetch_subreddit(name, limit)
    if not posts:
        return 0

    ingested = 0
    for post in posts:
        pd = post.get("data", {})
        post_id = pd.get("id", "")

        # Skip if already seen
        if post_id in state.get("seen_ids", {}):
            continue

        # Skip stickied/pinned
        if pd.get("stickied"):
            continue

        title = pd.get("title", "").strip()
        selftext = pd.get("selftext", "").strip()
        score = pd.get("score", 0)
        author = pd.get("author", "")
        flair = pd.get("link_flair_text", "")
        num_comments = pd.get("num_comments", 0)
        url = pd.get("url", "")
        permalink = pd.get("permalink", "")

        if not title:
            continue

        # Fetch top comments for context
        comments = []
        if num_comments > 0:
            comments = fetch_comments(name, post_id, limit=5)
            time.sleep(30)  # Rate limit between comment fetches

        # Build full context memory
        parts = [f"Reddit r/{name}: {title}"]
        if flair:
            parts.append(f"Flair: {flair}")
        parts.append(f"Score: {score}, Comments: {num_comments}, Author: u/{author}")

        if selftext and len(selftext) > 20:
            parts.append(f"\n{selftext[:1000]}")

        if comments:
            parts.append("\nTop comments:")
            for c in comments:
                parts.append(f"  u/{c['author']} (↑{c['score']}): {c['body'][:300]}")

        full_text = "\n".join(parts)

        metadata = {
            "type": "reddit_post",
            "subreddit": name,
            "post_id": post_id,
            "title": title[:100],
            "score": score,
            "flair": flair,
            "date": TODAY,
            "dream_weight": dream_weight,
        }

        if vector_remember(full_text, source, metadata):
            ingested += 1
            state.setdefault("seen_ids", {})[post_id] = {
                "ts": time.time(),
                "sub": name,
                "title": title[:60],
            }

    return ingested


def generate_dream_context(state):
    """Write today's Reddit highlights to a file for dream_generate.py to use."""
    dream_file = Path.home() / f".openclaw/workspace/memory/{TODAY}.reddit.md"

    # Collect today's ingested posts grouped by subreddit
    today_posts = {}
    for pid, info in state.get("seen_ids", {}).items():
        if info.get("ts", 0) > time.time() - 86400:
            sub = info.get("sub", "?")
            today_posts.setdefault(sub, []).append(info.get("title", ""))

    if not today_posts:
        return

    lines = [f"## What Reddit is talking about ({TODAY})\n"]
    for sub, titles in sorted(today_posts.items()):
        sub_config = SUBREDDITS.get(sub, {})
        weight = sub_config.get("dream_weight", "low")
        if weight == "high":
            lines.append(f"### r/{sub} (local/important)")
        else:
            lines.append(f"### r/{sub}")
        for t in titles[:5]:
            lines.append(f"- {t}")
        lines.append("")

    dream_file.write_text("\n".join(lines))
    log(f"Dream context written: {dream_file}", level=LOG_INFO, source="reddit_ingest")


def main():
    log(f"Reddit ingest starting — {len(SUBREDDITS)} subreddits", level=LOG_INFO, source="reddit_ingest")
    state = load_state()
    total = 0

    for name, config in SUBREDDITS.items():
        count = ingest_subreddit(name, config, state)
        if count:
            log(f"  r/{name}: {count} new posts", level=LOG_INFO, source="reddit_ingest")
        total += count
        time.sleep(10)  # Rate limit between subreddits

    save_state(state)
    generate_dream_context(state)

    log(f"Done. {total} new posts ingested across {len(SUBREDDITS)} subreddits",
        level=LOG_INFO, source="reddit_ingest")

    # Post summary to Slack+Discord if any new content
    if total > 0:
        try:
            msg = (
                f":globe_with_meridians: *Reddit Ingest* — {total} new posts from "
                f"{len(SUBREDDITS)} subreddits\n"
                f"Sources: {', '.join(f'r/{s}' for s in SUBREDDITS)}"
            )
            nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)
        except Exception:
            pass


if __name__ == "__main__":
    main()
