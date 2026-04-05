#!/usr/bin/env python3
"""
nova_inbox_simple.py — Minimal herd email watcher
Uses herd-mail library directly. No custom complexity.

Cron: every 5 minutes
"""

import subprocess
import json
import sys
import os
from datetime import datetime

# Ensure PYTHONPATH is set for waggle module
os.environ["PYTHONPATH"] = "/Volumes/Data/AI/python_packages:" + os.environ.get("PYTHONPATH", "")

HERD_MAIL = str(Path.home() / ".openclaw/scripts/nova_herd_mail.sh")

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def run_herd(args):
    """Run herd-mail command."""
    try:
        result = subprocess.run(
            [HERD_MAIL] + args,
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ
        )
        return result.returncode, result.stdout.strip()
    except Exception as e:
        log(f"Error: {e}")
        return 1, ""

def main():
    log("Checking inbox...")
    
    # Get unread messages
    code, output = run_herd(["list", "--unread"])
    
    if code != 0:
        log(f"Failed to list unread: {output}")
        return
    
    if not output:
        log("No unread messages")
        return
    
    try:
        data = json.loads(output)
        # herd_mail returns {"messages": [...]}
        messages = data.get("messages", []) if isinstance(data, dict) else data
    except json.JSONDecodeError:
        log(f"Failed to parse JSON: {output[:100]}")
        return
    
    if not messages:
        log("No unread messages")
        return
    
    log(f"Found {len(messages)} unread messages")
    
    for msg in messages[:10]:  # Limit to 10 to avoid overwhelming
        try:
            msg_id = msg.get("uid")
            sender = msg.get("from_addr", "Unknown")
            subject = msg.get("subject", "(no subject)")
            
            log(f"Processing: {sender} | {subject[:50]}")
            
            # Read full message using uid (positional argument)
            code, full_msg = run_herd(["read", str(msg_id)])
            
            if code != 0:
                log(f"  Failed to read message: {full_msg[:50]}")
                continue
            
            # For now, just log that we saw it
            # (Actual reply logic goes here when integrated with Nova's reasoning)
            log(f"  ✓ Read message uid={msg_id}")
            
        except Exception as e:
            log(f"  Error processing message: {e}")
            continue
    
    log("Inbox check complete")

if __name__ == "__main__":
    main()
