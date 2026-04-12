#!/usr/bin/env python3
"""
nova_context_bridge.py — Seamless context bridging across time.

Nova's own wish: "connecting dots across time — a reminder that echoes
an old conversation, a project idea that resurfaces when conditions align,
or recognizing when you're circling back to something you once let go."

This script:
  1. Reads today's activity (commits, meetings, emails, conversations)
  2. Searches vector memory for semantically similar past events
  3. When it finds a meaningful echo — something Jordan worked on months ago,
     a conversation thread that connects to today — it surfaces it gently
  4. Posts connections to Slack as a "thread from the past"

The goal is NOT to be a search engine. It's to be the friend who says
"hey, remember when you were thinking about this exact thing back in March?"

Cron: 10am and 4pm (twice daily — morning for setting context, afternoon for reflection)
Written by Jordan Koch.
"""

import json
import random
import subprocess
import sys
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_CHAN
SLACK_API = nova_config.SLACK_API
VECTOR_URL = "http://127.0.0.1:18790"
NOW = datetime.now()
TODAY = date.today().isoformat()
MEMORY_DIR = Path.home() / ".openclaw" / "workspace" / "memory"
JOURNAL_DIR = Path.home() / ".openclaw" / "workspace" / "journal"
STATE_FILE = Path("/tmp/nova_context_bridge_state.json")

# Minimum age (in days) for a memory to be considered an "echo"
# Too recent = not interesting. The magic is in the distant connections.
MIN_ECHO_AGE_DAYS = 14

# How many semantic neighbors to pull per query
RECALL_DEPTH = 8

# Similarity threshold — too low = noise, too high = obvious
# (vector DB returns similarity scores 0-1)
SIMILARITY_FLOOR = 0.45
SIMILARITY_CEILING = 0.85  # If it's THIS similar, it's probably the same event


def log(msg):
    print(f"[nova_context_bridge {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text, channel=None):
    data = json.dumps({
        "channel": channel or SLACK_CHAN, "text": text, "mrkdwn": True
    }).encode()
    req = urllib.request.Request(
        f"{SLACK_API}/chat.postMessage", data=data,
        headers={"Authorization": "Bearer " + SLACK_TOKEN,
                 "Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        log(f"Slack error: {e}")


def load_state():
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text())
            if state.get("date") != TODAY:
                return {"date": TODAY, "bridges_sent": [], "topics_used": []}
            return state
        except Exception:
            pass
    return {"date": TODAY, "bridges_sent": [], "topics_used": []}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Today's signals ──────────────────────────────────────────────────────────

def gather_today_signals():
    """Collect today's activity as short text fragments for semantic search."""
    signals = []

    # Recent commit messages
    try:
        r = subprocess.run(
            ["gh", "api", "/users/kochj23/events?per_page=20"],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            events = json.loads(r.stdout)
            for e in events:
                if TODAY in e.get("created_at", ""):
                    if e["type"] == "PushEvent":
                        for c in e.get("payload", {}).get("commits", []):
                            msg = c.get("message", "").split("\n")[0]
                            if msg:
                                signals.append(f"coding: {msg}")
                    elif e["type"] == "IssuesEvent":
                        title = e.get("payload", {}).get("issue", {}).get("title", "")
                        if title:
                            signals.append(f"issue: {title}")
    except Exception:
        pass

    # Meeting topics from OneOnOne
    try:
        r = subprocess.run(
            ["curl", "-s", "--connect-timeout", "2",
             "http://127.0.0.1:37421/api/oneonone/meetings?limit=5"],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout)
            meetings = data if isinstance(data, list) else data.get("meetings", [])
            for m in meetings:
                if TODAY in str(m.get("date", "")):
                    title = m.get("title") or m.get("name", "")
                    if title:
                        signals.append(f"meeting: {title}")
    except Exception:
        pass

    # Today's memory log topics
    mem_file = MEMORY_DIR / f"{TODAY}.md"
    if mem_file.exists():
        content = mem_file.read_text(encoding="utf-8")
        # Extract section headers and key phrases
        for line in content.splitlines():
            if line.startswith("## ") or line.startswith("- "):
                clean = line.lstrip("#- ").strip()
                if clean and len(clean) > 10:
                    signals.append(clean[:100])

    # Recent journal entries (last 3 days) for thematic continuity
    for days_back in range(1, 4):
        check_date = (date.today() - timedelta(days=days_back)).isoformat()
        for jf in JOURNAL_DIR.glob("*.md"):
            try:
                content = jf.read_text(encoding="utf-8")
                if check_date.replace("-", "") in content or check_date in content:
                    # Extract a snippet
                    for line in content.splitlines():
                        if check_date in line or (line.strip() and not line.startswith("#")):
                            clean = line.strip()
                            if len(clean) > 20:
                                signals.append(f"journal: {clean[:100]}")
                                break
            except Exception:
                continue

    return signals


# ── Vector memory search ─────────────────────────────────────────────────────

def recall(query, n=RECALL_DEPTH):
    """Search vector memory for semantically similar memories."""
    try:
        params = urllib.parse.urlencode({"q": query, "n": n})
        url = f"{VECTOR_URL}/recall?{params}"
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
            return data if isinstance(data, list) else data.get("results", [])
    except Exception as e:
        log(f"Recall error: {e}")
        return []


import urllib.parse


def filter_echoes(results):
    """Filter results to only interesting temporal echoes.

    An echo is interesting when:
      - It's old enough to be forgotten (>14 days)
      - It's similar enough to be relevant (>0.45)
      - It's not SO similar that it's the same event (<0.85)
    """
    cutoff_date = (date.today() - timedelta(days=MIN_ECHO_AGE_DAYS)).isoformat()
    echoes = []

    for r in results:
        metadata = r.get("metadata", {})
        mem_date = metadata.get("date", r.get("created_at", ""))[:10]
        similarity = r.get("similarity", r.get("score", 0))
        text = r.get("text", "")

        if not mem_date or mem_date > cutoff_date:
            continue  # Too recent
        if similarity < SIMILARITY_FLOOR or similarity > SIMILARITY_CEILING:
            continue  # Too weak or too identical
        if len(text) < 20:
            continue  # Too short to be meaningful

        echoes.append({
            "text": text[:300],
            "date": mem_date,
            "source": r.get("source", metadata.get("source", "unknown")),
            "similarity": similarity,
            "days_ago": (date.today() - date.fromisoformat(mem_date)).days,
        })

    # Sort by age (oldest first — the most surprising connections)
    echoes.sort(key=lambda e: e["days_ago"], reverse=True)
    return echoes


# ── Bridge construction ──────────────────────────────────────────────────────

BRIDGE_INTROS = [
    "This reminded me of something from {days_ago} days ago...",
    "There's an echo here — back on {date}, you were working on something similar:",
    "I noticed a thread: {days_ago} days ago, this came up:",
    "Circling back: on {date}, there was this:",
    "A connection across time — {days_ago} days ago:",
]


def build_bridge_message(signal, echo):
    """Build a gentle bridge message connecting today to the past."""
    intro = random.choice(BRIDGE_INTROS).format(
        days_ago=echo["days_ago"],
        date=echo["date"],
    )

    # Clean up the echo text
    echo_text = echo["text"].strip()
    if len(echo_text) > 200:
        echo_text = echo_text[:200] + "..."

    lines = [
        f"*Thread from the past*",
        f"_{intro}_",
        f"",
        f"> {echo_text}",
        f"",
        f"_Source: {echo['source']} ({echo['date']})_",
    ]
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("Looking for connections across time...")
    state = load_state()
    signals = gather_today_signals()

    if not signals:
        log("No signals today — nothing to bridge.")
        return

    log(f"Found {len(signals)} today signals")

    # Search for echoes across all signals
    best_bridge = None
    best_echo = None

    for signal in signals[:10]:  # Cap at 10 to be respectful of the memory server
        topic_key = signal[:40]
        if topic_key in state.get("topics_used", []):
            continue

        results = recall(signal)
        echoes = filter_echoes(results)

        if echoes:
            # Take the most distant (and therefore most surprising) echo
            echo = echoes[0]
            if best_echo is None or echo["days_ago"] > best_echo["days_ago"]:
                best_echo = echo
                best_bridge = (signal, echo)
                state.setdefault("topics_used", []).append(topic_key)

    if best_bridge and len(state.get("bridges_sent", [])) < 2:
        signal, echo = best_bridge
        message = build_bridge_message(signal, echo)
        slack_post(message)
        state.setdefault("bridges_sent", []).append({
            "signal": signal[:80],
            "echo_date": echo["date"],
            "echo_text": echo["text"][:100],
        })
        log(f"Bridge posted: today's '{signal[:40]}' ↔ {echo['date']} ({echo['days_ago']} days ago)")
    else:
        log("No compelling bridges found today — that's ok.")

    save_state(state)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Context Bridge")
    parser.add_argument("--run", action="store_true", help="Search for and post bridges (default)")
    parser.add_argument("--signals", action="store_true", help="Show today's signals (no posting)")
    parser.add_argument("--test", type=str, help="Test recall for a specific query")
    args = parser.parse_args()

    if args.signals:
        signals = gather_today_signals()
        for s in signals:
            print(f"  {s}")
        print(f"\n{len(signals)} signals found")
    elif args.test:
        results = recall(args.test)
        echoes = filter_echoes(results)
        for e in echoes:
            print(f"  [{e['date']} / {e['days_ago']}d / {e['source']}] {e['text'][:80]}")
    else:
        main()
