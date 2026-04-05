#!/usr/bin/env python3
"""
Nova General Channel Monitor — Ingest relevant local content into memory
Similar to Burbank subreddit summary job
"""

import subprocess
import os
from datetime import datetime
import json

def get_general_channel_messages():
    """Get recent messages from #general (C049EPC32)"""
    # Use message tool to read channel
    result = subprocess.run([
        "python3", "-c",
        """
import sys
sys.path.insert(0, '/opt/homebrew/lib/node_modules/openclaw')
from tools import message
result = message.read(action='read', channel='slack', target='C049EPC32', limit=50)
print(result)
"""
    ], capture_output=True, text=True)
    
    return result.stdout

def ingest_to_memory(content):
    """Store relevant content in memory"""
    if not content or len(content.strip()) < 10:
        return None
    
    # Ingest via nova_remember script
    cmd = [
        "bash",
        str(Path.home() / ".openclaw/scripts/nova_remember.sh"),
        content,
        "slack",
        json.dumps({"topic": "general_channel", "source": "kochfamily_slack"})
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.stdout

if __name__ == "__main__":
    print(f"[{datetime.now().isoformat()}] Monitoring #general for local content...")
    
    # Get recent messages
    messages = get_general_channel_messages()
    
    if messages:
        # Ingest relevant ones
        memory_result = ingest_to_memory(messages)
        print(f"✓ Ingested: {memory_result}")
    else:
        print("No new messages in #general")
