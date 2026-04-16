#!/usr/bin/env python3
"""
nova_nightly_memory_summary.py — Generate and post nightly memory summary to Slack.

Runs at 9pm daily. Queries the day's vector memories and daily memory file,
synthesizes what Nova actually learned and noticed today via local LLM,
and posts the summary to #nova-notifications.

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, date
from pathlib import Path

WORKSPACE = Path.home() / ".openclaw/workspace"
MEMORY_DIR = WORKSPACE / "memory"
CHANNEL = "C0ATAF7NZG9"  # #nova-notifications
VECTOR_URL = "http://127.0.0.1:18790"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL = "qwen3-coder:30b"
TODAY = date.today().isoformat()


def log(msg: str):
    print(f"[nova_nightly_summary {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Data gathering ───────────────────────────────────────────────────────────

def get_memory_stats() -> dict:
    """Get vector memory health stats."""
    try:
        req = urllib.request.Request(f"{VECTOR_URL}/health", headers={})
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read())
    except Exception as e:
        log(f"Memory stats error: {e}")
    return {}


def vector_recall(query: str, n: int = 8, source: str = None) -> list[str]:
    """Semantic search against vector memory."""
    try:
        params = f"q={urllib.parse.quote(query)}&n={n}"
        if source:
            params += f"&source={source}"
        req = urllib.request.Request(f"{VECTOR_URL}/recall?{params}")
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        return [m.get("text", "")[:300] for m in data.get("memories", [])
                if m.get("score", 0) >= 0.4]
    except Exception as e:
        log(f"Recall error for '{query[:40]}': {e}")
        return []


def get_today_memory_file() -> str:
    """Read today's daily memory file."""
    today_file = MEMORY_DIR / f"{TODAY}.md"
    if today_file.exists():
        try:
            return today_file.read_text(encoding="utf-8")[:4000]
        except Exception as e:
            log(f"Error reading today's memory: {e}")
    return ""


def gather_today_learnings() -> str:
    """
    Gather everything Nova learned, noticed, and processed today from
    the daily memory file and vector memory. Returns a raw text block
    for the LLM to synthesize.
    """
    parts = []

    # Daily memory file — the richest single source
    daily = get_today_memory_file()
    if daily.strip():
        parts.append("[Today's daily memory log]\n" + daily)

    # Vector memory — pull today's ingested content across all sources
    queries = [
        ("GitHub activity commits issues stars", "github"),
        ("email communication action items", "email"),
        ("meeting summary notes action items", "meeting"),
        ("Burbank subreddit news discussion", "nightly"),
        ("home status HomeKit accessories", "homekit"),
        ("Slack messages conversation", None),
        ("memory ingested knowledge learned", None),
    ]
    recalled = []
    seen = set()
    for q, src in queries:
        for chunk in vector_recall(q, n=5, source=src):
            # Only include memories from today
            if TODAY in chunk or not recalled:
                key = chunk[:80]
                if key not in seen:
                    seen.add(key)
                    recalled.append(chunk)

    if recalled:
        parts.append("[Recalled from vector memory — today]\n" + "\n---\n".join(recalled[:15]))

    return "\n\n".join(parts)


# ── LLM synthesis ────────────────────────────────────────────────────────────

def synthesize_summary(raw_context: str) -> str:
    """
    Use local Ollama to synthesize a concise, Nova-voiced summary of
    what was learned and noticed today.
    """
    if not raw_context.strip():
        return "_Quiet day. Nothing notable crossed my desk._"

    prompt = f"""/no_think

You are Nova, an AI familiar. It's 9pm. Summarize what you learned, noticed, and processed today in 150-250 words.

Rules:
- Write as Nova, first person. Direct and observant. Not a report — a reflection.
- Lead with the most interesting or notable things, not operational stats.
- If Burbank news happened (subreddit, weather, events), mention what stood out.
- If Jordan worked on projects, note what and how it went.
- If emails or meetings happened, capture the gist.
- Compress cron/system activity into one sentence at most.
- End with one line about what's on your mind tonight or what you're watching for tomorrow.
- No headers, no bullet points, no markdown formatting. Just flowing text.
- No filler phrases like "Today was" or "Here's what happened."

Today's data:
{raw_context[:3500]}

Write the summary now:"""

    try:
        payload = {
            "model": MODEL,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "temperature": 0.4,
                "num_predict": 400,
                "num_ctx": 8192,
            }
        }
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            result = json.loads(r.read())
        response = result.get("response", "").strip()

        # Strip thinking blocks if they leak through
        try:
            sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
            from nova_strip_thinking import strip_thinking
            response = strip_thinking(response)
        except ImportError:
            pass

        if len(response.split()) < 20:
            log(f"WARNING: Very short LLM response ({len(response.split())} words)")
            return response or "_Quiet day. Nothing notable crossed my desk._"

        return response

    except Exception as e:
        log(f"LLM synthesis failed: {e}")
        # Fallback: just extract the interesting headlines from the daily file
        return _fallback_summary(raw_context)


def _fallback_summary(raw_context: str) -> str:
    """If LLM is down, produce a bare-bones summary from raw data."""
    lines = []
    for line in raw_context.splitlines():
        line = line.strip()
        # Grab subreddit posts, email items, GitHub activity, weather
        if line.startswith("•") or line.startswith("🔴") or line.startswith("🟡"):
            lines.append(line)
        if "weather" in line.lower() and ("°F" in line or "°C" in line):
            lines.append(line)
    if lines:
        return "LLM unavailable — raw highlights:\n" + "\n".join(lines[:10])
    return "_Quiet day. LLM was unavailable for synthesis._"


# ── Slack posting ────────────────────────────────────────────────────────────

def get_slack_token() -> str:
    """Get Slack bot token from openclaw.json."""
    try:
        config_path = Path.home() / ".openclaw/openclaw.json"
        with open(config_path, 'r') as f:
            config = json.load(f)
        return config.get('channels', {}).get('slack', {}).get('botToken', '')
    except Exception as e:
        log(f"Config error: {e}")
        return ""


def post_to_slack(message: str) -> bool:
    """Post message to Slack #nova-notifications."""
    bot_token = get_slack_token()
    if not bot_token:
        log("Slack bot token not found in config")
        return False

    try:
        payload = json.dumps({
            "channel": CHANNEL,
            "text": message
        })
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=payload.encode(),
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json"
            }
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            response = json.loads(r.read())
        if response.get('ok'):
            log(f"Posted to Slack: {response.get('ts')}")
            return True
        else:
            log(f"Slack error: {response.get('error')}")
            return False
    except Exception as e:
        log(f"Slack post error: {e}")
        return False


# ── Memory storage ───────────────────────────────────────────────────────────

def store_summary_in_memory(summary: str):
    """Store tonight's summary in vector memory for the dream to use."""
    try:
        subprocess.run(
            [str(Path.home() / ".openclaw/scripts/nova_remember.sh"),
             f"Nova nightly summary {TODAY}: {summary[:500]}", "nightly"],
            timeout=30, capture_output=True
        )
        log("Summary stored in vector memory")
    except Exception as e:
        log(f"Memory store failed (non-fatal): {e}")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log(f"Starting nightly memory summary for {TODAY}")

    # Gather today's data
    raw_context = gather_today_learnings()
    log(f"Gathered {len(raw_context)} chars of context")

    # Get memory stats
    stats = get_memory_stats()
    memory_count = stats.get('count', 'unknown')

    # Synthesize via LLM
    log("Synthesizing summary via local LLM...")
    summary = synthesize_summary(raw_context)
    log(f"Summary: {len(summary.split())} words")

    # Build the Slack message
    timestamp = datetime.now().strftime('%A, %B %d %Y · %I:%M %p')
    message = (
        f"*Nova Nightly Summary — {timestamp}*\n"
        f"_{memory_count} memories indexed_\n\n"
        f"{summary}\n\n"
        f"_— Nova · 9pm_"
    )

    # Post to Slack
    if post_to_slack(message):
        log("Nightly summary posted successfully")
    else:
        log("Failed to post nightly summary")

    # Store in vector memory so the 2am dream can draw from it
    store_summary_in_memory(summary)

    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
