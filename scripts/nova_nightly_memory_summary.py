#!/usr/bin/env python3
"""
nova_nightly_memory_summary.py — Generate and post nightly memory summary to Slack.

Runs at 9pm daily. Queries vector memory for key learnings from today,
compiles into a concise Slack message, and posts to #nova-chat.
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

WORKSPACE = Path.home() / ".openclaw/workspace"
MEMORY_DIR = WORKSPACE / "memory"
CHANNEL = "C0AMNQ5GX70"  # #nova-chat

def log(msg: str):
    print(f"[nova_nightly_summary {datetime.now().isoformat()}] {msg}")

def query_vector_memory(query: str, limit: int = 5) -> list:
    """Search vector memory for relevant memories."""
    try:
        result = subprocess.run(
            ["bash", f"{Path.home()}/.openclaw/scripts/nova_recall.sh", 
             query, str(limit), "", "0.6"],
            capture_output=True,
            text=True,
            timeout=30
        )
        return result.stdout.split('\n') if result.returncode == 0 else []
    except Exception as e:
        log(f"Memory query error: {e}")
        return []

def get_memory_stats() -> dict:
    """Get vector memory health stats."""
    try:
        result = subprocess.run(
            ["curl", "-s", "http://127.0.0.1:18790/health"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except Exception as e:
        log(f"Memory stats error: {e}")
    return {}

def get_today_logs() -> str:
    """Read today's memory flush file."""
    today = datetime.now().strftime('%Y-%m-%d')
    today_file = MEMORY_DIR / f"{today}.md"
    
    if today_file.exists():
        try:
            with open(today_file, 'r') as f:
                content = f.read()
                # Extract first 2KB of key sections
                lines = content.split('\n')
                return '\n'.join(lines[:40])
        except Exception as e:
            log(f"Error reading today's log: {e}")
    
    return "No new memories recorded today."

def generate_summary() -> str:
    """Generate nightly summary message."""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M %Z')
    
    # Get memory stats
    stats = get_memory_stats()
    memory_count = stats.get('count', 'unknown')
    
    # Get today's logs
    today_logs = get_today_logs()
    
    # Build message
    message = f"""🌙 **Nova Nightly Memory Summary** — {timestamp}

**Memory System Status:**
- Total memories indexed: {memory_count}
- Model: {stats.get('model', 'unknown')}
- Status: {stats.get('status', 'unknown')}

**Today's Work:**
{today_logs}

**Tomorrow's Priorities:**
- Monitor all new crons (GitHub, Git, Metrics monitors)
- Tune dream video generation (test full pipeline)
- Check #general channel memory ingest
- Verify MLX/OpenWebUI routing working

---
*Generated automatically at 9pm by nova_nightly_memory_summary.py*"""
    
    return message

def post_to_slack(message: str) -> bool:
    """Post message to Slack #nova-chat."""
    try:
        # Get Slack token from config
        config_path = Path.home() / ".openclaw/openclaw.json"
        if not config_path.exists():
            log("openclaw.json not found")
            return False
        
        with open(config_path, 'r') as f:
            config = json.load(f)
            bot_token = config.get('channels', {}).get('slack', {}).get('botToken')
        
        if not bot_token:
            log("Slack bot token not found in config")
            return False
        
        # Post to Slack
        result = subprocess.run(
            ["curl", "-s", "-X", "POST", "https://slack.com/api/chat.postMessage",
             "-H", "Authorization: Bearer " + bot_token,
             "-H", "Content-Type: application/json",
             "-d", json.dumps({
                 "channel": CHANNEL,
                 "text": message
             })],
            capture_output=True,
            text=True,
            timeout=10
        )
        
        if result.returncode == 0:
            response = json.loads(result.stdout)
            if response.get('ok'):
                log(f"✓ Posted to Slack: {response.get('ts')}")
                return True
            else:
                log(f"Slack error: {response.get('error')}")
                return False
    
    except Exception as e:
        log(f"Error posting to Slack: {e}")
    
    return False

def main():
    log("Starting nightly memory summary...")
    
    # Generate summary
    summary = generate_summary()
    
    # Post to Slack
    if post_to_slack(summary):
        log("✓ Nightly summary posted successfully")
        return 0
    else:
        log("✗ Failed to post nightly summary")
        return 1

if __name__ == "__main__":
    sys.exit(main())
