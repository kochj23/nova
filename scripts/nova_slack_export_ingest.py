#!/usr/bin/env python3
"""
nova_slack_export_ingest.py — Ingest a Slack workspace export into Nova's vector memory.

Reads the JSON export directory structure (one JSON file per day per channel)
and stores messages grouped by day into the vector store.

Skips nova-chat and nova-notifications (already in Nova's context).
Skips conversatiom (ingested separately via live API).

All data stays local — stored in PostgreSQL+pgvector on localhost:18790.

Usage:
    python3 nova_slack_export_ingest.py "/path/to/slack export dir"

Written by Jordan Koch.
"""

import json
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path

VECTOR_URL = "http://127.0.0.1:18790/remember"

# Skip channels that are already ingested or not useful
SKIP_CHANNELS = {"nova-chat", "nova-notifications"}

# Map channel names to source labels
SOURCE_MAP = {
    "general": "slack_general",
    "home-alerts": "slack_home_alerts",
    "jordan": "slack_jordan",
    "homerepair": "slack_homerepair",
    "things_for_a_new_house": "slack_house",
    "todo": "slack_todo",
    "random": "slack_random",
}

def log(msg):
    print(f"[export_ingest {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def load_users(export_dir):
    """Load user ID -> name mapping from users.json."""
    users_file = export_dir / "users.json"
    user_map = {}
    if users_file.exists():
        users = json.loads(users_file.read_text())
        for u in users:
            name = u.get("real_name") or u.get("name") or u.get("id")
            user_map[u["id"]] = name
    # Override with known names
    user_map["U049EPC2W"] = "Jordan"
    user_map["U04AS59BR"] = "Tricia"
    return user_map

def format_message(msg, user_map):
    """Format a single message."""
    uid = msg.get("user", msg.get("bot_id", "system"))
    user = user_map.get(uid, uid)
    text = msg.get("text", "").strip()

    if not text:
        files = msg.get("files", [])
        attachments = msg.get("attachments", [])
        if files:
            text = "[file: " + ", ".join(f.get("name", "file") for f in files) + "]"
        elif attachments:
            text = "[attachment]"
        else:
            return None

    # Replace user mentions
    for uid2, name in user_map.items():
        text = text.replace(f"<@{uid2}>", f"@{name}")

    return f"{user}: {text}"

def vector_remember(text, source, metadata):
    """Store a chunk in vector memory."""
    payload = json.dumps({
        "text": text,
        "source": source,
        "metadata": metadata,
    }).encode()
    try:
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            return True
    except Exception:
        return False

def ingest_channel(channel_dir, channel_name, user_map):
    """Ingest all messages from one channel directory."""
    source = SOURCE_MAP.get(channel_name, f"slack_{channel_name}")
    day_files = sorted(channel_dir.glob("*.json"))

    total_stored = 0
    total_chunks = 0
    total_messages = 0

    for day_file in day_files:
        day_str = day_file.stem  # e.g., "2015-04-06"
        try:
            messages = json.loads(day_file.read_text())
        except Exception:
            continue

        if not messages:
            continue

        # Format messages
        formatted = []
        for msg in messages:
            # Skip bot join/leave messages
            if msg.get("subtype") in ("channel_join", "channel_leave", "bot_add", "bot_remove"):
                continue
            line = format_message(msg, user_map)
            if line:
                formatted.append(line)

        if not formatted:
            continue

        total_messages += len(formatted)

        # Chunk (max ~1500 chars per chunk)
        chunks = []
        current = []
        current_len = 0
        MAX_CHUNK = 1500

        for line in formatted:
            if current_len + len(line) > MAX_CHUNK and current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            current.append(line)
            current_len += len(line) + 1

        if current:
            chunks.append("\n".join(current))

        # Store chunks
        for i, chunk in enumerate(chunks):
            header = f"Slack #{channel_name} ({day_str}):\n\n"
            metadata = {
                "date": day_str,
                "channel": channel_name,
                "chunk": i + 1,
                "total_chunks": len(chunks),
            }
            if vector_remember(header + chunk, source, metadata):
                total_stored += 1
            total_chunks += 1

        time.sleep(0.02)

    return total_messages, total_stored, total_chunks

def slack_status(token, msg):
    """Post status to nova-notifications."""
    try:
        payload = json.dumps({"channel": "C0ATAF7NZG9", "text": msg, "mrkdwn": True}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage", data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15)
    except Exception:
        pass

def main():
    if len(sys.argv) < 2:
        print("Usage: nova_slack_export_ingest.py /path/to/export/dir")
        sys.exit(1)

    export_dir = Path(sys.argv[1])
    if not export_dir.is_dir():
        print(f"Error: {export_dir} is not a directory")
        sys.exit(1)

    log(f"Starting Slack export ingest from: {export_dir}")

    # Get token for status updates
    import subprocess
    token = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-slack-bot-token", "-w"],
        capture_output=True, text=True
    ).stdout.strip()

    # Load users
    user_map = load_users(export_dir)
    log(f"Loaded {len(user_map)} users")

    # Find channel directories
    channel_dirs = [d for d in sorted(export_dir.iterdir())
                    if d.is_dir() and d.name not in SKIP_CHANNELS]

    log(f"Found {len(channel_dirs)} channels to ingest (skipping {SKIP_CHANNELS})")

    grand_total_msgs = 0
    grand_total_stored = 0
    grand_total_chunks = 0

    for i, channel_dir in enumerate(channel_dirs):
        channel_name = channel_dir.name
        log(f"Ingesting #{channel_name}...")

        msgs, stored, chunks = ingest_channel(channel_dir, channel_name, user_map)
        grand_total_msgs += msgs
        grand_total_stored += stored
        grand_total_chunks += chunks

        log(f"  #{channel_name}: {msgs} messages -> {stored}/{chunks} chunks stored")

        # Post status every 5 minutes worth of channels
        if (i + 1) % 3 == 0 or i == len(channel_dirs) - 1:
            status_msg = (
                f":file_folder: *Export Ingest Progress*\n"
                f"- Channels done: {i+1}/{len(channel_dirs)}\n"
                f"- Messages processed: {grand_total_msgs:,}\n"
                f"- Chunks stored: {grand_total_stored:,}/{grand_total_chunks:,}\n"
                f"- Current: #{channel_name}"
            )
            slack_status(token, status_msg)

    log(f"Complete! {grand_total_msgs:,} messages -> {grand_total_stored:,}/{grand_total_chunks:,} chunks")

    # Final status
    slack_status(token, (
        f":white_check_mark: *Slack Export Ingest Complete*\n"
        f"- Channels: {len(channel_dirs)}\n"
        f"- Total messages: {grand_total_msgs:,}\n"
        f"- Chunks stored: {grand_total_stored:,}\n"
        f"- All data local (localhost:18790)"
    ))

if __name__ == "__main__":
    main()
