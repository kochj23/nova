#!/usr/bin/env python3
"""
nova_slack_ingest.py — Watch #nova-chat for file shares and auto-ingest them.

Runs every 5 minutes via system cron alongside nova_mail_agent.py.
When Jordan (or anyone) shares a file in #nova-chat, this script:
  1. Downloads the file from Slack
  2. Extracts text (PDF, DOCX, XLSX, PPTX, TXT, etc.)
  3. Stores chunks in vector memory
  4. Replies in Slack with a confirmation + preview

Tracks processed file IDs in a local log to avoid re-processing.

Written by Jordan Koch.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPTS    = Path.home() / ".openclaw/scripts"
PROCESSED_LOG = Path.home() / ".openclaw/logs/slack_ingest_processed.json"
CHANNEL    = "C0AMNQ5GX70"   # #nova-chat

sys.path.insert(0, str(SCRIPTS))
import nova_config

_TOKEN_CACHE = Path.home() / ".openclaw/.slack_token_cache"

def _get_token() -> str:
    """Get Slack token from Keychain, falling back to cache file."""
    token = nova_config.slack_bot_token()
    if token:
        # Cache it for when Keychain is locked
        try:
            _TOKEN_CACHE.write_text(token)
            _TOKEN_CACHE.chmod(0o600)
        except Exception:
            pass
        return token
    # Keychain locked — try cache
    try:
        cached = _TOKEN_CACHE.read_text().strip()
        if cached:
            return cached
    except Exception:
        pass
    return ""

SLACK_TOKEN = _get_token()
SLACK_API   = nova_config.SLACK_API


def log(msg: str):
    print(f"[nova_slack_ingest {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_get(endpoint: str, params: dict = None) -> dict:
    url = f"{SLACK_API}/{endpoint}"
    if params:
        import urllib.parse
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {SLACK_TOKEN}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def slack_post(text: str, thread_ts: str = None):
    payload = {"channel": CHANNEL, "text": text, "mrkdwn": True}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{SLACK_API}/chat.postMessage", data=data,
        headers={"Authorization": f"Bearer {SLACK_TOKEN}",
                 "Content-Type": "application/json; charset=utf-8"}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def load_processed() -> set:
    try:
        return set(json.loads(PROCESSED_LOG.read_text()))
    except Exception:
        return set()


def save_processed(ids: set):
    PROCESSED_LOG.parent.mkdir(parents=True, exist_ok=True)
    # Keep last 1000 to avoid unbounded growth
    recent = list(ids)[-1000:]
    PROCESSED_LOG.write_text(json.dumps(recent))


def get_recent_files() -> list[dict]:
    """Get files shared in #nova-chat in the last 24 hours."""
    if not SLACK_TOKEN:
        return []
    try:
        # Get recent messages and look for file_share subtypes
        result = slack_get("conversations.history", {
            "channel": CHANNEL,
            "limit": 50
        })
        files = []
        for msg in result.get("messages", []):
            if msg.get("subtype") == "file_share" and msg.get("files"):
                for f in msg["files"]:
                    files.append({
                        "file": f,
                        "message_ts": msg.get("ts"),
                        "user": msg.get("user")
                    })
            # Also check messages with files attached (non-subtype)
            elif msg.get("files"):
                for f in msg["files"]:
                    files.append({
                        "file": f,
                        "message_ts": msg.get("ts"),
                        "user": msg.get("user")
                    })
        return files
    except Exception as e:
        log(f"Error fetching messages: {e}")
        return []


def main():
    if not SLACK_TOKEN:
        log("Slack token unavailable — skipping")
        return

    processed = load_processed()
    new_files = get_recent_files()

    if not new_files:
        return

    newly_processed = set()

    for item in new_files:
        f = item["file"]
        file_id = f.get("id")
        filename = f.get("name", "unknown")
        filetype = f.get("filetype", "")
        message_ts = item.get("message_ts")
        mimetype = f.get("mimetype", "")

        # Skip already processed
        if file_id in processed:
            continue

        # Skip files that aren't text-extractable
        skip_types = {"image", "video", "audio", "binary"}
        if any(filetype.startswith(t) for t in skip_types):
            log(f"Skipping non-text file: {filename} ({filetype})")
            newly_processed.add(file_id)
            continue

        log(f"Processing file: {filename} ({filetype})")

        try:
            # Use nova_ingest.py to download and process
            import subprocess
            result = subprocess.run(
                [sys.executable, str(SCRIPTS / "nova_ingest.py"),
                 "--slack-file-id", file_id,
                 "--filename", filename,
                 "--source", "slack"],
                capture_output=True, text=True, timeout=120
            )

            if result.returncode == 0:
                try:
                    output_lines = result.stdout.strip().split("\n")
                    # Find the JSON block
                    json_start = next((i for i, l in enumerate(output_lines) if l.startswith("{")), None)
                    if json_start is not None:
                        ingest_result = json.loads("\n".join(output_lines[json_start:]).split("\n✅")[0])
                    else:
                        ingest_result = {"ok": True, "stored": "?", "words": "?", "topic": filename}
                except Exception:
                    ingest_result = {"ok": True}

                stored = ingest_result.get("stored", "?")
                words = ingest_result.get("words", "?")
                topic = ingest_result.get("topic", filename)
                preview = ingest_result.get("preview", "")[:200]

                msg = (
                    f"📄 *Ingested: {filename}*\n"
                    f"Stored {stored} memory chunks ({words} words) under topic `{topic}`.\n"
                    f"_{preview}..._\n"
                    f"Ask me anything about it."
                )
                slack_post(msg, thread_ts=message_ts)
                log(f"Stored {stored} chunks from {filename}")
            else:
                log(f"Ingest failed for {filename}: {result.stderr[:200]}")
                slack_post(
                    f"⚠️ Couldn't extract text from `{filename}`. "
                    f"Format may not be supported or file may be corrupted.",
                    thread_ts=message_ts
                )

            newly_processed.add(file_id)

        except Exception as e:
            log(f"Error processing {filename}: {e}")
            newly_processed.add(file_id)  # Mark so we don't retry broken files endlessly

    if newly_processed:
        save_processed(processed | newly_processed)


if __name__ == "__main__":
    main()
