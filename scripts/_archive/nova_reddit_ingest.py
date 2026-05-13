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
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN

# ── Config ────────────────────────────────────────────────────────────────────

VECTOR_URL = "http://192.168.1.6:18790/remember"
STATE_FILE = Path.home() / ".openclaw/workspace/state/reddit_ingest_state.json"
USER_AGENT = "Nova/1.0 nova_reddit_ingest.py (AI familiar, local use only)"
TODAY      = date.today().isoformat()

SUBREDDITS = {
    "burbank": {
        "source": "burbank",
        "label": "Burbank local",
        "limit": 15,
        "dream_weight": "high",
    },
    "glendale": {
        "source": "local",
        "label": "Glendale local",
        "limit": 15,
    },
    "Sovereigncitizen": {
        "source": "reddit",
        "label": "Sovereign citizen",
        "limit": 10,
        "dream_weight": "medium",
    },
    "SipsTea": {
        "source": "reddit",
        "label": "SipsTea humor",
        "limit": 10,
    },
    "lazerpig": {
        "source": "reddit",
        "label": "LazerPig military/history",
        "limit": 10,
    },
    "vibecoding": {
        "source": "reddit",
        "label": "Vibe coding",
        "limit": 10,
    },
    "3Dprinting": {
        "source": "reddit",
        "label": "3D printing",
        "limit": 10,
    },
    "avesLA": {
        "source": "socal_rave",
        "label": "LA rave/electronic music",
        "limit": 10,
    },
    "CarPlay": {
        "source": "automotive",
        "label": "Apple CarPlay",
        "limit": 10,
    },
    "chaoticgood": {
        "source": "reddit",
        "label": "Chaotic good",
        "limit": 10,
    },
    "ClaudeCode": {
        "source": "reddit",
        "label": "Claude Code",
        "limit": 10,
    },
}

# ── State ─────────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"seen_ids": []}

def save_state(state):
    # Prune IDs older than 30 days to prevent unbounded growth
    cutoff = (date.today() - timedelta(days=30)).isoformat()
    seen = state.get("seen_ids", [])
    # seen_ids are just strings — keep all (no timestamp), cap at 5000
    if len(seen) > 5000:
        seen = seen[-5000:]
    state["seen_ids"] = seen
    state["timestamp"] = TODAY
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Fetch helpers ─────────────────────────────────────────────────────────────

def fetch_subreddit(subreddit, config):
    """Fetch posts from a subreddit with full selftext."""
    limit = config.get("limit", 10)
    url = f"https://www.reddit.com/r/{subreddit}/hot.json?limit={limit}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        return data.get("data", {}).get("children", [])
    except Exception as e:
        log(f"Error fetching r/{subreddit}: {e}", level=LOG_ERROR, source="reddit_ingest")
        return []

def fetch_comments(subreddit, post_id):
    """Fetch top comments for a post."""
    url = f"https://www.reddit.com/r/{subreddit}/comments/{post_id}.json?limit=5&sort=best"
    comments = []
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        if len(data) > 1:
            for child in data[1].get("data", {}).get("children", []):
                cd = child.get("data", {})
                body = cd.get("body", "").strip()
                author = cd.get("author", "unknown")
                score = cd.get("score", 0)
                if body and body != "[deleted]" and len(body) > 10:
                    comments.append(f"  u/{author} (score:{score}): {body}")
    except Exception as e:
        log(f"Error fetching comments for {post_id}: {e}", level=LOG_WARN, source="reddit_ingest")
    return comments[:5]

# ── Vector memory ─────────────────────────────────────────────────────────────

def vector_remember(text, metadata=None):
    payload = json.dumps({
        "text": text,
        "source": "reddit_ingest",
        "metadata": metadata or {},
    }).encode()
    req = urllib.request.Request(
        VECTOR_URL + "?async=1",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=30)
    except Exception as e:
        log(f"vector_remember error: {e}", level=LOG_ERROR, source="reddit_ingest")

# ── Per-subreddit ingest ──────────────────────────────────────────────────────

def ingest_subreddit(subreddit, config, state):
    """Fetch and ingest posts from one subreddit."""
    ingested = set(state.get("seen_ids", []))
    posts = fetch_subreddit(subreddit, config)
    today_posts = []
    count = 0

    for child in posts:
        pd = child.get("data", {})
        post_id = pd.get("id", "")
        if not post_id or post_id in ingested or pd.get("stickied"):
            continue

        title    = pd.get("title", "").strip()
        selftext = pd.get("selftext", "").strip()
        flair    = pd.get("link_flair_text", "")
        score    = pd.get("score", 0)
        num_comments = pd.get("num_comments", 0)
        author   = pd.get("author", "unknown")
        permalink = pd.get("permalink", "")

        parts = [f"Reddit r/{subreddit}: {title}"]
        if flair:
            parts.append(f"Flair: {flair}")
        parts.append(f"Score: {score}, Comments: {num_comments}, Author: u/{author}")
        if selftext and selftext not in ("[removed]", "[deleted]"):
            parts.append(selftext[:1000])

        comments = fetch_comments(subreddit, post_id)
        if comments:
            parts.append("Top comments:")
            parts.extend(comments)

        full_text = "\n".join(parts)

        vector_remember(full_text, metadata={
            "type": "reddit_post",
            "subreddit": subreddit,
            "post_id": post_id,
            "flair": flair,
            "source": config.get("source", "reddit"),
            "sub": subreddit,
            "date": TODAY,
        })

        ingested.add(post_id)
        today_posts.append({"sub": subreddit, "title": title, "weight": config.get("dream_weight", "normal")})
        count += 1
        time.sleep(1)  # be polite to Reddit's API

    state["seen_ids"] = list(ingested)
    return count, today_posts

# ── Dream context ─────────────────────────────────────────────────────────────

def generate_dream_context(today_posts):
    """Write today's Reddit highlights to a file for dream_generate.py to use."""
    home = Path.home()
    dream_file = home / f".openclaw/workspace/memory/{TODAY}.reddit.md"
    dream_file.parent.mkdir(parents=True, exist_ok=True)

    lines = [f"## What Reddit is talking about ({TODAY})\n"]

    # Group by sub
    by_sub = {}
    for p in today_posts:
        by_sub.setdefault(p["sub"], []).append(p)

    for sub in sorted(by_sub):
        sub_config = SUBREDDITS.get(sub, {})
        weight = sub_config.get("dream_weight", "normal")
        label = sub_config.get("label", sub)
        titles = [p["title"] for p in by_sub[sub]]
        if weight == "high":
            lines.append(f"### r/{sub} (local/important):")
        else:
            lines.append(f"### r/{sub}:")
        for t in titles:
            lines.append(f"- {t}")
        lines.append("")

    dream_file.write_text("\n".join(lines))
    log(f"Dream context written: {dream_file}", level=LOG_INFO, source="reddit_ingest")

# ── Quiet hours ───────────────────────────────────────────────────────────────

def _is_quiet_hours():
    """Return True if current local time is between 22:00 and 07:00."""
    current_hour = datetime.now().hour
    return current_hour >= 22 or current_hour < 7

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    state = load_state()
    total = 0
    all_today = []

    log(f"Reddit ingest starting — {len(SUBREDDITS)} subreddits", level=LOG_INFO, source="reddit_ingest")

    for subreddit, config in SUBREDDITS.items():
        count, today_posts = ingest_subreddit(subreddit, config, state)
        if count:
            log(f"  r/{subreddit}: {count} new posts", level=LOG_INFO, source="reddit_ingest")
        total += count
        all_today.extend(today_posts)

    if all_today:
        generate_dream_context(all_today)

    save_state(state)

    log(f"Done. {total} new posts ingested across {len(SUBREDDITS)} subreddits", level=LOG_INFO, source="reddit_ingest")

    if total > 0 and not _is_quiet_hours():
        subs_with_posts = list({p["sub"] for p in all_today})
        msg = (
            f":globe_with_meridians: *Reddit Ingest* — "
            f"{total} new posts from {len(subs_with_posts)} subreddits\n"
            f"Sources: {', '.join(f'r/{s}' for s in sorted(subs_with_posts))}"
        )
        try:
            nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)
        except Exception:
            pass


if __name__ == "__main__":
    main()
