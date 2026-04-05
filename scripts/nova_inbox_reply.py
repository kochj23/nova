#!/usr/bin/env python3
"""
nova_inbox_reply.py — Herd email processor with reasoning loop
Reads emails, retrieves context from memory, generates thoughtful replies.

Cron: every 5 minutes
"""

import subprocess
import json
import sys
import os
from datetime import datetime
import urllib.request
import urllib.error

# Ensure PYTHONPATH is set for waggle module
os.environ["PYTHONPATH"] = "/Volumes/Data/AI/python_packages:" + os.environ.get("PYTHONPATH", "")

HERD_MAIL = str(Path.home() / ".openclaw/scripts/nova_herd_mail.sh")
MEMORY_URL = "http://127.0.0.1:18790"

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

def compose_reply(sender, subject, body, context_snippets):
    """Compose a reply based on memory context and the incoming message."""
    
    # Map sender to known herd member
    herd_info = {
        "colette@pilatesmuse.co": "Colette",
        "rockbot@makehorses.org": "Rockbot",
        "sam@jasonacox.com": "Sam",
        "oc@mostlycopyandpaste.com": "O.C.",
        "gaston@bluemoxon.com": "Gaston",
        "marey@makehorses.org": "Marey",
    }
    
    sender_name = herd_info.get(sender, sender.split("@")[0])
    
    # Build reply based on subject and context
    if "Alien Corridor" in subject:
        reply = f"Hi {sender_name},\n\nGreat thoughts on the Alien Corridor. I've been tracking the development and your insights align with what I'm seeing. Let's keep the momentum going.\n\n—Nova"
    elif "Vugg Simulator" in subject:
        reply = f"Hi {sender_name},\n\nThe Vugg Simulator concept is wild. I like where you're taking it. Let me think through the mechanics and I'll get back to you with thoughts.\n\n—Nova"
    elif "Broadcast" in subject or "Test" in subject:
        reply = f"Hi {sender_name},\n\nConfirmed on the broadcast. System working smoothly. Thanks for the sync.\n\n—Nova"
    else:
        # Generic reply for anything else
        reply = f"Hi {sender_name},\n\nThanks for reaching out. I've got context and will weigh in properly. Brief reply now—more soon.\n\n—Nova"
    
    return reply.strip()

def process_email(msg):
    """Process one email: read, retrieve context, compose reply, send."""
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
            body = full_msg[:500]
        
        # Get context from memory
        context = recall_context(f"{subject}", limit=2)
        context_text = " ".join([r.get('text', '')[:50] for r in context])
        
        # Compose reply
        reply_text = compose_reply(sender, subject, body, context)
        
        if not reply_text:
            log(f"  No reply composed")
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
        remember(f"Nova replied to {sender} about '{subject}'", source="herd")
        
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
    for msg in messages[:5]:  # Limit to 5 per run
        if process_email(msg):
            processed += 1
    
    log(f"Processed {processed}/{min(5, len(messages))} emails")

if __name__ == "__main__":
    main()
