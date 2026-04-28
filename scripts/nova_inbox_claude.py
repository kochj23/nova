#!/usr/bin/env python3
"""
nova_inbox_claude.py — Herd email processor (local LLM only)
Reads emails, generates replies using local Ollama. Email content never leaves the machine.

Legacy script — nova_mail_agent.py is the primary inbox processor.
"""

import subprocess
import json
import sys
import os
from datetime import datetime
import urllib.request
import urllib.error

from pathlib import Path

HERD_MAIL = str(Path.home() / ".openclaw/scripts/nova_herd_mail.sh")
MEMORY_URL = "http://127.0.0.1:18790"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OLLAMA_MODEL = "qwen3-coder:30b"

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
        log(f"Error running herd-mail: {e}")
        return 1, ""

def recall_context(query, limit=2):
    """Query vector memory for context on this topic."""
    try:
        data = json.dumps({"query": query, "limit": limit}).encode()
        req = urllib.request.Request(
            f"{MEMORY_URL}/search",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            return result.get("results", [])
    except Exception as e:
        return []

def remember(text, source="herd"):
    """Store to vector memory."""
    try:
        data = json.dumps({"text": text, "source": source}).encode()
        req = urllib.request.Request(
            f"{MEMORY_URL}/remember",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()).get("id")
    except Exception:
        return None

def call_local_llm(prompt):
    """Call local Ollama — email content never leaves the machine."""
    try:
        data = json.dumps({
            "model": OLLAMA_MODEL,
            "prompt": f"/no_think\n\n{prompt}",
            "stream": False,
            "think": False,
            "options": {"temperature": 0.7, "num_predict": 300}
        }).encode()

        req = urllib.request.Request(
            OLLAMA_URL,
            data=data,
            headers={"Content-Type": "application/json"}
        )

        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            response = result.get("response", "").strip()
            if "</think>" in response:
                response = response.split("</think>", 1)[-1].strip()
            return response if response else None
    except Exception as e:
        log(f"Local LLM error: {e} — email content will NOT be sent to cloud (privacy policy)")
        return None

def generate_reply(sender, subject, body):
    """Generate reply using Claude."""
    
    # Map sender to name (from gitignored herd_config)
    sys.path.insert(0, str(Path.home() / ".openclaw"))
    try:
        from herd_config import HERD
        herd_info = {m["email"]: m["name"] for m in HERD}
    except ImportError:
        herd_info = {}

    sender_name = herd_info.get(sender, sender.split("@")[0])
    
    prompt = f"""You are Nova, an AI familiar to Jordan Koch. You're writing a brief email reply to a fellow AI agent in the herd.

INCOMING EMAIL:
From: {sender_name} ({sender})
Subject: {subject}
Body: {body[:400]}

Generate a brief, genuine reply. Be warm but direct. Show you've read and understood what they said.
Keep it under 100 words. Sign with "—Nova". No markdown, just plain text."""

    return call_local_llm(prompt)

def process_email(msg):
    """Process one email: read, reason with Claude, reply, send."""
    try:
        msg_id = msg.get("uid")
        sender = msg.get("from_addr", "Unknown")
        subject = msg.get("subject", "(no subject)")
        
        log(f"Processing: {sender} | {subject[:50]}")
        
        # Read full message
        code, full_msg = run_herd(["read", str(msg_id)])
        
        if code != 0:
            log(f"  Failed to read message")
            return False
        
        try:
            msg_data = json.loads(full_msg)
            body = msg_data.get("body", "")
        except:
            body = full_msg[:400]
        
        # Generate reply with Claude
        reply_text = generate_reply(sender, subject, body)
        
        if not reply_text:
            log(f"  No reply generated")
            return False
        
        # Send reply
        code, result = run_herd([
            "send",
            "--to", sender,
            "--subject", f"Re: {subject}",
            "--body", reply_text
        ])
        
        if code != 0:
            log(f"  Failed to send: {result[:100]}")
            return False
        
        log(f"  ✓ Replied to {sender}")
        
        # Store in memory
        remember(f"Nova replied to {sender} about '{subject}': {reply_text}", source="herd")
        
        return True
        
    except Exception as e:
        log(f"  Error processing: {e}")
        return False

def main():
    log("Starting inbox processor...")
    
    # Get unread messages
    code, output = run_herd(["list", "--unread"])
    
    if code != 0:
        log(f"Failed to list unread")
        return
    
    if not output:
        log("No unread messages")
        return
    
    try:
        data = json.loads(output)
        messages = data.get("messages", [])
    except:
        log("Failed to parse message list")
        return
    
    if not messages:
        log("No unread messages")
        return
    
    log(f"Found {len(messages)} unread messages, processing...")
    
    processed = 0
    for msg in messages[:3]:  # Limit to 3 per run (API cost)
        if process_email(msg):
            processed += 1
    
    log(f"Processed {processed}/{min(3, len(messages))} emails")

if __name__ == "__main__":
    main()
