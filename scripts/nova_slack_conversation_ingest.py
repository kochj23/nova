#!/usr/bin/env python3
"""
nova_slack_conversation_ingest.py — One-shot ingest of the #conversatiom channel
(C0FHEB9D4) into Nova's vector memory.

This is the Jordan/Tricia Slack conversation spanning 2015-2026.
Messages are chunked by day and stored with source "slack_conversation".

All data stays local — stored in PostgreSQL+pgvector on localhost:18790.

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, date
from pathlib import Path

CHANNEL = "C0FHEB9D4"
VECTOR_URL = "http://127.0.0.1:18790/remember"
SOURCE = "slack_conversation"

USER_MAP = {
    "U049EPC2W": "Jordan",
    "U04AS59BR": "Tricia",
}

def get_token():
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-slack-bot-token", "-w"],
        capture_output=True, text=True
    )
    return result.stdout.strip()

def log(msg):
    print(f"[slack_convo_ingest {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def fetch_all_messages(token):
    """Paginate through all channel history."""
    messages = []
    cursor = ""
    pages = 0

    while True:
        params = f"channel={CHANNEL}&limit=200"
        if cursor:
            params += f"&cursor={cursor}"
        url = f"https://slack.com/api/conversations.history?{params}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())

        if not data.get("ok"):
            log(f"Error on page {pages}: {data.get('error')}")
            break

        msgs = data.get("messages", [])
        messages.extend(msgs)
        pages += 1

        if pages % 20 == 0:
            log(f"  Fetched {len(messages)} messages ({pages} pages)...")

        cursor = data.get("response_metadata", {}).get("next_cursor", "")
        if not cursor:
            break
        time.sleep(0.3)

    log(f"Fetched {len(messages)} total messages in {pages} pages")
    return messages

def resolve_user(uid):
    return USER_MAP.get(uid, uid)

def format_message(msg):
    """Format a single Slack message into readable text."""
    user = resolve_user(msg.get("user", "unknown"))
    text = msg.get("text", "").strip()

    if not text:
        # Check for attachments/files
        attachments = msg.get("attachments", [])
        files = msg.get("files", [])
        if files:
            text = "[shared file: " + ", ".join(f.get("name", "file") for f in files) + "]"
        elif attachments:
            text = "[attachment: " + ", ".join(a.get("fallback", "item") for a in attachments) + "]"
        else:
            return None

    # Replace user mentions with names
    for uid, name in USER_MAP.items():
        text = text.replace(f"<@{uid}>", f"@{name}")

    return f"{user}: {text}"

def chunk_by_day(messages):
    """Group messages by date."""
    days = defaultdict(list)
    for msg in messages:
        ts = float(msg.get("ts", 0))
        day = date.fromtimestamp(ts).isoformat()
        formatted = format_message(msg)
        if formatted:
            days[day].append((ts, formatted))

    # Sort messages within each day by timestamp
    for day in days:
        days[day].sort(key=lambda x: x[0])

    return days

def vector_remember(text, metadata):
    """Store a chunk in vector memory."""
    payload = json.dumps({
        "text": text,
        "source": SOURCE,
        "metadata": metadata,
    }).encode()
    try:
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return True
    except Exception as e:
        return False

def ingest_day(day_str, messages_with_ts):
    """Ingest one day's messages as chunks (max ~1500 chars per chunk)."""
    messages = [m[1] for m in messages_with_ts]
    chunks = []
    current_chunk = []
    current_len = 0
    MAX_CHUNK = 1500

    for msg in messages:
        if current_len + len(msg) > MAX_CHUNK and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = []
            current_len = 0
        current_chunk.append(msg)
        current_len += len(msg) + 1

    if current_chunk:
        chunks.append("\n".join(current_chunk))

    stored = 0
    for i, chunk in enumerate(chunks):
        header = f"Slack conversation between Jordan and Tricia ({day_str}):\n\n"
        metadata = {
            "date": day_str,
            "channel": "conversatiom",
            "participants": "Jordan Koch, Tricia Riordan",
            "chunk": i + 1,
            "total_chunks": len(chunks),
            "message_count": len(messages),
        }
        if vector_remember(header + chunk, metadata):
            stored += 1

    return stored, len(chunks)

def main():
    log("Starting Slack conversation ingest (Jordan/Tricia #conversatiom)")
    log("Channel: C0FHEB9D4 | Source: slack_conversation | All local.")

    token = get_token()
    if not token:
        log("ERROR: No Slack token available")
        sys.exit(1)

    # Fetch all messages
    log("Fetching all messages from Slack API...")
    messages = fetch_all_messages(token)

    if not messages:
        log("No messages found. Exiting.")
        return

    # Group by day
    log("Grouping messages by day...")
    days = chunk_by_day(messages)
    log(f"Found messages across {len(days)} days")

    # Ingest day by day
    total_stored = 0
    total_chunks = 0
    days_processed = 0

    for day_str in sorted(days.keys()):
        stored, chunks = ingest_day(day_str, days[day_str])
        total_stored += stored
        total_chunks += chunks
        days_processed += 1

        if days_processed % 100 == 0:
            log(f"  Progress: {days_processed}/{len(days)} days, {total_stored}/{total_chunks} chunks stored")

        time.sleep(0.05)  # Gentle on the vector store

    log(f"Complete! {total_stored}/{total_chunks} chunks stored across {days_processed} days")
    log(f"Source: '{SOURCE}' — Nova can recall with: recall?q=...&source=slack_conversation")

if __name__ == "__main__":
    main()
