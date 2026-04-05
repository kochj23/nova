#!/usr/bin/env python3
"""
nova_slack_memory_ingest.py — Ingest #general Slack messages into memory and dream journal.

Pulls all messages from past 24 hours, stores in vector memory, and logs for dream journal.
"""

import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

SLACK_TOKEN = subprocess.run(
    ["security", "find-generic-password", "-a", "nova", "-s", "nova-slack-bot-token", "-w"],
    capture_output=True, text=True
).stdout.strip()

GENERAL_CHANNEL = "C049EPC32"
VECTOR_URL = "http://127.0.0.1:18790/remember"
MEMORY_DIR = Path.home() / ".openclaw/workspace/memory"
DREAM_LOG = Path.home() / ".openclaw/workspace/journal/slack_events.json"

def log(msg: str):
    print(f"[nova_slack_ingest {datetime.now().isoformat()}] {msg}")

def get_channel_history():
    """Fetch past 24 hours of messages from #general."""
    oldest = (datetime.now() - timedelta(days=1)).timestamp()
    
    try:
        url = f"https://slack.com/api/conversations.history"
        payload = {
            "channel": GENERAL_CHANNEL,
            "oldest": str(int(oldest)),
            "limit": 100,
        }
        
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {SLACK_TOKEN}",
                "Content-Type": "application/json",
            }
        )
        
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read())
            
            if result.get("ok"):
                return result.get("messages", [])
            else:
                log(f"Error: {result.get('error')}")
                return []
    except Exception as e:
        log(f"Error fetching history: {e}")
        return []

def remember(text: str, metadata: dict = None) -> str:
    """Store in vector memory."""
    payload = {
        "text": text,
        "source": "slack_general",
        "metadata": metadata or {}
    }
    
    try:
        req = urllib.request.Request(
            VECTOR_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            result = json.loads(response.read())
            return result.get("id")
    except Exception as e:
        log(f"Memory error: {e}")
    
    return None

def add_to_dream_log(event: dict):
    """Add event to dream journal log."""
    try:
        if DREAM_LOG.exists():
            with open(DREAM_LOG) as f:
                events = json.load(f)
        else:
            events = []
        
        events.append(event)
        
        with open(DREAM_LOG, "w") as f:
            json.dump(events, f, indent=2)
    except Exception as e:
        log(f"Dream log error: {e}")

def main():
    if not SLACK_TOKEN:
        log("ERROR: Slack token not found in Keychain")
        return 1
    
    log("Fetching past 24 hours from #general...")
    messages = get_channel_history()
    
    if not messages:
        log("No new messages")
        return 0
    
    log(f"Processing {len(messages)} messages...")
    
    stored = 0
    for msg in messages:
        # Skip bot messages and threads
        if msg.get("subtype") in ["bot_message", "thread_broadcast"]:
            continue
        
        user = msg.get("user", "unknown")
        text = msg.get("text", "").strip()
        ts = msg.get("ts")
        
        if not text or len(text) < 5:
            continue
        
        # Store in memory
        memory_id = remember(
            f"Slack #general from <@{user}>: {text}",
            {
                "user": user,
                "channel": "general",
                "timestamp": ts,
                "thread_ts": msg.get("thread_ts"),
            }
        )
        
        if memory_id:
            stored += 1
            
            # Log for dream journal
            add_to_dream_log({
                "timestamp": ts,
                "user": user,
                "text": text[:200],  # First 200 chars for dream
                "memory_id": memory_id,
            })
    
    log(f"✓ Stored {stored} messages in memory")
    return 0

if __name__ == "__main__":
    sys.exit(main())
