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

VECTOR_URL = nova_config.VECTOR_URL
JORDAN_DM = nova_config.JORDAN_DM
NOW = datetime.now()
TODAY = date.today().isoformat()

MESSAGES_DB = Path.home() / "Library/Messages/chat.db"
STATE_FILE = Path.home() / ".openclaw/workspace/state/nova_imessage_state.json"
CONTACTS_CACHE = Path.home() / ".openclaw/workspace/state/contacts_cache.json"

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


# ── Contact resolution ───────────────────────────────────────────────────────

CONTACTS_SWIFT = r'''
import Contacts
import Foundation
let store = CNContactStore()
let sem = DispatchSemaphore(value: 0)
store.requestAccess(for: .contacts) { _, _ in sem.signal() }
_ = sem.wait(timeout: .now() + 5)
let keys = [CNContactGivenNameKey, CNContactFamilyNameKey,
            CNContactOrganizationNameKey, CNContactPhoneNumbersKey,
            CNContactEmailAddressesKey] as [CNKeyDescriptor]
let request = CNContactFetchRequest(keysToFetch: keys)
var entries: [[String: String]] = []
try? store.enumerateContacts(with: request) { contact, _ in
    let name = [contact.givenName, contact.familyName].filter { !$0.isEmpty }.joined(separator: " ")
    let displayName = name.isEmpty ? contact.organizationName : name
    guard !displayName.isEmpty else { return }
    for phone in contact.phoneNumbers {
        let digits = phone.value.stringValue.filter { $0.isNumber || $0 == "+" }
        entries.append(["phone": digits, "name": displayName])
    }
    for email in contact.emailAddresses {
        entries.append(["email": (email.value as String).lowercased(), "name": displayName])
    }
}
if let data = try? JSONSerialization.data(withJSONObject: entries),
   let str = String(data: data, encoding: .utf8) { print(str) }
'''

_contact_lookup = None


def _normalize_phone(number):
    """Normalize a phone number for matching: strip everything except digits, drop leading 1."""
    digits = re.sub(r'[^\d]', '', str(number))
    if digits.startswith('1') and len(digits) == 11:
        digits = digits[1:]
    return digits[-10:] if len(digits) >= 10 else digits


def _build_contact_cache():
    """Build or load the phone/email → name lookup cache."""
    # Use cache if < 24h old
    if CONTACTS_CACHE.exists():
        try:
            age = time.time() - CONTACTS_CACHE.stat().st_mtime
            if age < 86400:
                return json.loads(CONTACTS_CACHE.read_text())
        except Exception:
            pass

    # Dump contacts via Swift
    swift_file = Path.home() / ".openclaw/workspace/state/contacts_dump.swift"
    swift_file.write_text(CONTACTS_SWIFT)
    try:
        result = subprocess.run(["swift", str(swift_file)],
                                capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            log(f"Contacts dump failed: {result.stderr[:100]}")
            return {}
        entries = json.loads(result.stdout)
    except Exception as e:
        log(f"Contacts error: {e}")
        return {}

    # Build lookup: normalized_phone → name, email → name
    lookup = {}
    for entry in entries:
        name = entry.get("name", "").strip()
        if not name:
            continue
        if "phone" in entry:
            normalized = _normalize_phone(entry["phone"])
            if normalized:
                lookup[normalized] = name
        if "email" in entry:
            lookup[entry["email"].lower()] = name

    # Cache to disk
    CONTACTS_CACHE.parent.mkdir(parents=True, exist_ok=True)
    CONTACTS_CACHE.write_text(json.dumps(lookup))
    log(f"Contact cache built: {len(lookup)} entries")
    return lookup


def resolve_contact(handle):
    """Resolve a phone number or email to a contact name."""
    global _contact_lookup
    if _contact_lookup is None:
        _contact_lookup = _build_contact_cache()

    if not handle:
        return "Unknown"

    # Try exact match (email addresses)
    if handle.lower() in _contact_lookup:
        return _contact_lookup[handle.lower()]

    # Try normalized phone match
    normalized = _normalize_phone(handle)
    if normalized in _contact_lookup:
        return _contact_lookup[normalized]

    # No match — return the raw handle
    return handle


def slack_post(text, channel=None):
    nova_config.post_both(text, slack_channel=channel or nova_config.SLACK_NOTIFY)


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

def get_all_new_messages():
    """Get ALL new messages (incoming AND outgoing) since last check for memory storage."""
    state = load_state()
    last_check_ts = state.get("last_check_ts", 0)

    # If first run, start from 24h ago to avoid flooding
    if last_check_ts == 0:
        first_run_cutoff = datetime.now() - timedelta(hours=24)
        last_check_ts = int((first_run_cutoff.timestamp() - 978307200) * 1_000_000_000)

    if not MESSAGES_DB.exists():
        return [], last_check_ts

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
              AND m.date > ?
              AND m.item_type = 0
            ORDER BY m.date ASC
            LIMIT 200
        """
        cursor = conn.execute(query, [last_check_ts])
        messages = []

        max_ts = last_check_ts
        for row in cursor:
            dt = _mac_timestamp_to_datetime(row["date"])
            messages.append({
                "sender": "Jordan" if row["is_from_me"] else (row["handle_id"] or "Unknown"),
                "text": row["text"][:500] if row["text"] else "",
                "date": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
                "is_from_me": bool(row["is_from_me"]),
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

        return messages, max_ts

    except Exception as e:
        log(f"DB read error: {e}")
        return [], last_check_ts


def is_spam(msg):
    """Filter out spam/noise messages."""
    text = msg.get("text", "")
    sender = msg.get("sender", "")
    if not text or len(text) < 2:
        return True
    if "@" in sender and "gmail" not in sender and "icloud" not in sender:
        return True  # Email-address senders are usually RCS spam
    if sender.isdigit() and len(sender) <= 6:
        return True  # Short codes (verification, alerts) — still store but don't alert
    return False


def watch():
    """Check for new iMessages, store ALL in memory, alert on real conversations."""
    messages, _ = get_all_new_messages()

    if not messages:
        log("No new messages.")
        return

    # Store ALL messages in vector memory with resolved contact names
    stored = 0
    for m in messages:
        direction = "to" if m["is_from_me"] else "from"
        raw_handle = m.get("handle", "")
        contact_name = resolve_contact(raw_handle)

        text = f"iMessage {direction} {contact_name} on {m['date']}: {m['text'][:300]}"
        vector_remember(text, {
            "date": TODAY,
            "type": "imessage",
            "direction": direction,
            "contact": contact_name,
            "handle": raw_handle,
        })
        stored += 1

    # Post notable incoming messages to Slack DM (not spam, not from Jordan)
    incoming = [m for m in messages if not m["is_from_me"] and not is_spam(m)]

    if incoming:
        lines = [f"*iMessage — {len(incoming)} new*"]
        for m in incoming[:10]:
            contact_name = resolve_contact(m.get("handle", ""))
            text_preview = m["text"][:100]
            if len(m["text"]) > 100:
                text_preview += "..."
            lines.append(f"  *{contact_name}* ({m['date']}): {text_preview}")

        slack_post("\n".join(lines), channel=JORDAN_DM)
        log(f"Posted {len(incoming)} incoming messages to Slack")

    log(f"Stored {stored} messages in memory ({len(incoming)} incoming alerts)")


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
