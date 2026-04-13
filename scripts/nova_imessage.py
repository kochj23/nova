#!/usr/bin/env python3
"""
nova_imessage.py — iMessage integration for Nova.

Send and receive iMessages via macOS Messages.app. Sends as Jordan's
Apple ID, signed "— Nova" so recipients know it's her.

Send: AppleScript → Messages.app
Receive: Read from ~/Library/Messages/chat.db (SQLite)

Usage:
  # Send an iMessage
  python3 nova_imessage.py --send "+15551234567" "Hey, it's Nova checking in!"

  # Check for new messages from a contact
  python3 nova_imessage.py --check "+15551234567"

  # Check all recent incoming messages (last N hours)
  python3 nova_imessage.py --recent 4

  # Watch for new messages and post to Slack (cron mode)
  python3 nova_imessage.py --watch

Written by Jordan Koch.
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_CHAN
SLACK_API = nova_config.SLACK_API
VECTOR_URL = nova_config.VECTOR_URL
JORDAN_DM = nova_config.JORDAN_DM
NOW = datetime.now()
TODAY = date.today().isoformat()

MESSAGES_DB = Path.home() / "Library/Messages/chat.db"
STATE_FILE = Path.home() / ".openclaw/workspace/state/nova_imessage_state.json"

# Nova's signature — appended to all outgoing messages
NOVA_SIGNATURE = "\n— Nova"

# Contacts Nova is allowed to message (herd members + Jordan's family)
# Phone numbers or email addresses used in iMessage
# Loaded from herd_config.py if available
ALLOWED_CONTACTS = {}
try:
    sys.path.insert(0, str(Path.home() / ".openclaw"))
    from herd_config import HERD
    for member in HERD:
        if member.get("imessage"):
            ALLOWED_CONTACTS[member["name"]] = member["imessage"]
        elif member.get("email"):
            ALLOWED_CONTACTS[member["name"]] = member["email"]
except ImportError:
    pass


def log(msg):
    print(f"[nova_imessage {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text, channel=None):
    data = json.dumps({
        "channel": channel or SLACK_CHAN, "text": text, "mrkdwn": True
    }).encode()
    req = urllib.request.Request(
        f"{SLACK_API}/chat.postMessage", data=data,
        headers={"Authorization": "Bearer " + SLACK_TOKEN,
                 "Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        log(f"Slack error: {e}")


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "imessage", "metadata": metadata or {}
        }).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── Send via AppleScript ─────────────────────────────────────────────────────

def send_imessage(recipient, text, sign=True):
    """Send an iMessage via Messages.app AppleScript.

    recipient: phone number (+1...) or email address
    text: message body
    sign: if True, append Nova's signature
    """
    if sign and NOVA_SIGNATURE not in text:
        text = text.rstrip() + NOVA_SIGNATURE

    # Escape for AppleScript
    escaped_text = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped_recipient = recipient.replace("\\", "\\\\").replace('"', '\\"')

    # Determine service — phone numbers use iMessage, emails try iMessage first
    if recipient.startswith("+") or recipient[0].isdigit():
        service = "iMessage"
    else:
        service = "iMessage"

    script = f'''
tell application "Messages"
    set targetService to 1st account whose service type = {service}
    set targetBuddy to participant "{escaped_recipient}" of targetService
    send "{escaped_text}" to targetBuddy
end tell
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            log(f"Sent iMessage to {recipient}")
            return True
        else:
            log(f"Send failed: {result.stderr.strip()[:200]}")
            # Try alternate approach
            return send_imessage_alternate(recipient, text)
    except Exception as e:
        log(f"Send error: {e}")
        return False


def send_imessage_alternate(recipient, text):
    """Alternate send method using 'send' to chat directly."""
    escaped_text = text.replace("\\", "\\\\").replace('"', '\\"')
    escaped_recipient = recipient.replace("\\", "\\\\").replace('"', '\\"')

    script = f'''
tell application "Messages"
    set targetBuddy to buddy "{escaped_recipient}" of service "iMessage"
    send "{escaped_text}" to targetBuddy
end tell
'''
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0:
            log(f"Sent iMessage (alt method) to {recipient}")
            return True
        log(f"Alt send failed: {result.stderr.strip()[:200]}")
        return False
    except Exception as e:
        log(f"Alt send error: {e}")
        return False


# ── Read from Messages database ──────────────────────────────────────────────

def _mac_timestamp_to_datetime(mac_ts):
    """Convert macOS Messages timestamp to datetime."""
    if mac_ts is None or mac_ts == 0:
        return None
    # Messages uses nanoseconds since 2001-01-01
    unix_ts = mac_ts / 1_000_000_000 + 978307200
    return datetime.fromtimestamp(unix_ts)


def get_recent_messages(hours=4, contact=None):
    """Read recent messages from the Messages database.

    Returns list of dicts: {sender, text, date, is_from_me, service}
    """
    if not MESSAGES_DB.exists():
        log("Messages database not found")
        return []

    cutoff = datetime.now() - timedelta(hours=hours)
    cutoff_mac = int((cutoff.timestamp() - 978307200) * 1_000_000_000)

    try:
        conn = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        query = """
            SELECT
                m.text,
                m.is_from_me,
                m.date,
                m.service,
                h.id as handle_id,
                m.date_read
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.text IS NOT NULL
              AND m.date > ?
              AND m.item_type = 0
        """
        params = [cutoff_mac]

        if contact:
            query += " AND h.id LIKE ?"
            params.append(f"%{contact}%")

        query += " ORDER BY m.date DESC LIMIT 100"

        cursor = conn.execute(query, params)
        messages = []

        for row in cursor:
            dt = _mac_timestamp_to_datetime(row["date"])
            messages.append({
                "sender": "Jordan" if row["is_from_me"] else row["handle_id"],
                "text": row["text"][:500] if row["text"] else "",
                "date": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
                "is_from_me": bool(row["is_from_me"]),
                "service": row["service"] or "iMessage",
                "handle": row["handle_id"] or "",
            })

        conn.close()
        return messages

    except Exception as e:
        log(f"DB read error: {e}")
        return []


def get_unread_messages():
    """Get messages received since last check."""
    state = load_state()
    last_check_ts = state.get("last_check_ts", 0)

    if not MESSAGES_DB.exists():
        return []

    try:
        conn = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row

        query = """
            SELECT
                m.text,
                m.is_from_me,
                m.date,
                m.service,
                h.id as handle_id
            FROM message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            WHERE m.text IS NOT NULL
              AND m.is_from_me = 0
              AND m.date > ?
              AND m.item_type = 0
            ORDER BY m.date ASC
        """
        cursor = conn.execute(query, [last_check_ts])
        messages = []

        max_ts = last_check_ts
        for row in cursor:
            dt = _mac_timestamp_to_datetime(row["date"])
            messages.append({
                "sender": row["handle_id"] or "Unknown",
                "text": row["text"][:500] if row["text"] else "",
                "date": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
                "service": row["service"] or "iMessage",
                "handle": row["handle_id"] or "",
                "raw_date": row["date"],
            })
            if row["date"] > max_ts:
                max_ts = row["date"]

        conn.close()

        # Update state
        state["last_check_ts"] = max_ts
        save_state(state)

        return messages

    except Exception as e:
        log(f"Unread check error: {e}")
        return []


# ── State ────────────────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_check_ts": 0}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


# ── Watch mode (cron) ────────────────────────────────────────────────────────

def watch():
    """Check for new incoming iMessages and post to Slack."""
    messages = get_unread_messages()

    if not messages:
        log("No new messages.")
        return

    # Filter out spam/noise
    real_messages = [m for m in messages if
                     m["text"] and
                     len(m["text"]) > 1 and
                     not m["text"].startswith("http") and
                     "@" not in m.get("sender", "")]  # Skip email-address senders (usually spam)

    if not real_messages:
        log(f"Filtered {len(messages)} messages (all noise).")
        return

    lines = [f"*iMessage — {len(real_messages)} new*"]
    for m in real_messages[:10]:
        sender = m["sender"]
        # Try to match to a known contact name
        for name, contact_id in ALLOWED_CONTACTS.items():
            if contact_id in sender:
                sender = name
                break
        text_preview = m["text"][:100]
        if len(m["text"]) > 100:
            text_preview += "..."
        lines.append(f"  *{sender}* ({m['date']}): {text_preview}")

    slack_post("\n".join(lines), channel=JORDAN_DM)

    # Store in vector memory
    for m in real_messages[:5]:
        vector_remember(
            f"iMessage from {m['sender']} on {m['date']}: {m['text'][:200]}",
            {"date": TODAY, "type": "imessage_received", "sender": m["sender"]}
        )

    log(f"Posted {len(real_messages)} new messages to Slack")


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova iMessage")
    parser.add_argument("--send", nargs=2, metavar=("RECIPIENT", "TEXT"),
                        help="Send an iMessage")
    parser.add_argument("--check", type=str, metavar="CONTACT",
                        help="Check recent messages from a contact")
    parser.add_argument("--recent", type=int, nargs="?", const=4,
                        help="Show recent messages (last N hours)")
    parser.add_argument("--watch", action="store_true",
                        help="Check for new messages since last run (cron mode)")
    parser.add_argument("--contacts", action="store_true",
                        help="List configured contacts")
    args = parser.parse_args()

    if args.send:
        recipient, text = args.send
        success = send_imessage(recipient, text)
        if success:
            print(f"Sent to {recipient}")
        else:
            print(f"Failed to send to {recipient}")
            sys.exit(1)
    elif args.check:
        messages = get_recent_messages(hours=24, contact=args.check)
        for m in messages:
            direction = "→" if m["is_from_me"] else "←"
            print(f"  {direction} [{m['date']}] {m['sender']}: {m['text'][:80]}")
        print(f"\n{len(messages)} messages")
    elif args.recent is not None:
        messages = get_recent_messages(hours=args.recent)
        for m in messages[:20]:
            direction = "→" if m["is_from_me"] else "←"
            print(f"  {direction} [{m['date']}] {m['sender']}: {m['text'][:80]}")
        print(f"\n{len(messages)} messages (showing first 20)")
    elif args.contacts:
        if ALLOWED_CONTACTS:
            for name, contact in ALLOWED_CONTACTS.items():
                print(f"  {name}: {contact}")
        else:
            print("  No contacts configured.")
            print("  Add 'imessage' field to herd members in herd_config.py")
    elif args.watch:
        watch()
    else:
        watch()
