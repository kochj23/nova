#!/usr/bin/env python3
"""
nova_mail_agent.py — Nova's email agent (v2).

Rewritten 2026-04-14. Fixes: duplicate sends, re-processing loops,
missing Sent Items, and missing CC to Jordan.

For each unread herd email:
  1. Read full content via IMAP (single connection for entire run)
  2. Generate a reply using local Ollama
  3. Send ONE reply to ALL herd members + CC Jordan
  4. Save to Sent Items via IMAP APPEND
  5. Move original to Trash (so it's not re-processed)
  6. Post summary to #nova-notifications
  7. Store in vector memory

Key design decisions:
  - Single IMAP connection for the entire run (no connection mismatch)
  - Delete from Inbox after processing (move to Trash, not just mark read)
  - One reply per thread to all recipients (not per-recipient)
  - CC Jordan's work email on all outgoing (loaded from known_senders.py)
  - Jules LaPlante (jules@laplante.dev) is a herd member

Cron: disabled (will be re-enabled via launchd after verification)
Written by Jordan Koch.
"""

import imaplib
import email
import email.utils
import json
import re
import subprocess
import sys
import urllib.request
from datetime import datetime, date
from email.message import EmailMessage
from email.utils import formatdate, parseaddr
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

SCRIPTS      = Path.home() / ".openclaw/scripts"
WORKSPACE    = Path.home() / ".openclaw/workspace"
OLLAMA_URL   = "http://127.0.0.1:11434/api/generate"
MODEL        = "qwen3-coder:30b"
VECTOR_URL   = "http://127.0.0.1:18790/remember"
TODAY        = date.today().isoformat()

NOVA_EMAIL   = "nova@digitalnoise.net"
IMAP_HOST    = "imap.gmail.com"
IMAP_PORT    = 993
SMTP_HOST    = "smtp.gmail.com"
SMTP_PORT    = 587

# Gmail folder names
SENT_FOLDER  = "[Gmail]/Sent Mail"
TRASH_FOLDER = "[Gmail]/Trash"

sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(Path.home() / ".openclaw"))
import nova_config

SLACK_TOKEN  = None  # loaded lazily
SLACK_CHAN   = nova_config.SLACK_NOTIFY
SLACK_API    = nova_config.SLACK_API

# Load herd config
try:
    from herd_config import HERD, HERD_EMAILS
except ImportError:
    HERD = []
    HERD_EMAILS = set()

# Load known senders + Jordan's CC address (gitignored — contains PII)
try:
    from known_senders import KNOWN_SENDERS, JORDAN_EMAILS, JORDAN_CC_ADDR as JORDAN_CC
except ImportError:
    KNOWN_SENDERS = set()
    JORDAN_EMAILS = set()
    JORDAN_CC = ""

SYSTEM_SENDER_PATTERNS = [
    "mailer-daemon", "postmaster", "mail delivery", "noreply", "no-reply",
    "donotreply", "do-not-reply", "delivery status", "undeliverable",
]

# All herd emails + Jules (who is a herd member but not in HERD config yet)
ALL_HERD_EMAILS = list(HERD_EMAILS | {"jules@laplante.dev"})
# All recipients for a herd reply: all herd members + Nova herself (for threading)
HERD_REPLY_TO = [e for e in ALL_HERD_EMAILS if e != NOVA_EMAIL]


def log(msg: str):
    print(f"[nova_mail_agent {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── IMAP helpers (single connection) ─────────────────────────────────────────

def _get_app_password() -> str:
    """Get Nova's email password from Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-a", NOVA_EMAIL,
         "-s", "nova-smtp-app-password", "-w"],
        capture_output=True, text=True
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def imap_connect(app_pass: str) -> imaplib.IMAP4_SSL:
    """Open and authenticate an IMAP connection."""
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    conn.login(NOVA_EMAIL, app_pass)
    return conn


def imap_list_unread(conn: imaplib.IMAP4_SSL) -> list[bytes]:
    """Return list of UIDs for unread messages in INBOX."""
    conn.select("INBOX")
    status, data = conn.uid("SEARCH", None, "UNSEEN")
    if status != "OK" or not data or not data[0]:
        return []
    return data[0].split()


def imap_fetch_message(conn: imaplib.IMAP4_SSL, uid: bytes) -> dict:
    """Fetch and parse a single message by UID."""
    status, data = conn.uid("FETCH", uid, "(RFC822)")
    if status != "OK" or not data or not data[0]:
        return {}
    raw = data[0][1]
    msg = email.message_from_bytes(raw)

    # Extract sender
    from_raw = msg.get("From", "")
    from_name, from_addr = parseaddr(from_raw)
    from_addr = from_addr.lower()

    # Extract subject
    subject = msg.get("Subject", "(no subject)")
    # Decode encoded subject
    decoded_parts = email.header.decode_header(subject)
    subject = "".join(
        part.decode(enc or "utf-8") if isinstance(part, bytes) else part
        for part, enc in decoded_parts
    )

    # Extract body (plain text preferred)
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode("utf-8", errors="replace")

    # Extract message-id and references for threading
    message_id = msg.get("Message-ID", "")
    references = msg.get("References", "")
    in_reply_to = msg.get("In-Reply-To", "")

    return {
        "uid": uid,
        "from_raw": from_raw,
        "from_name": from_name,
        "from_addr": from_addr,
        "subject": subject,
        "body": body[:3000],
        "message_id": message_id,
        "references": references,
        "in_reply_to": in_reply_to,
    }


def imap_move_to_trash(conn: imaplib.IMAP4_SSL, uid: bytes):
    """Move a message to Trash (Gmail IMAP)."""
    try:
        conn.uid("COPY", uid, TRASH_FOLDER)
        conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
        conn.expunge()
        log(f"  Moved UID {uid.decode()} to Trash")
    except Exception as e:
        log(f"  WARNING: move to Trash failed for UID {uid.decode()}: {e}")


def imap_save_to_sent(conn: imaplib.IMAP4_SSL, msg_bytes: bytes):
    """Save a message to the Sent folder."""
    try:
        status, _ = conn.append(f'"{SENT_FOLDER}"', "\\Seen", None, msg_bytes)
        if status == "OK":
            log("  Saved to Sent Items")
        else:
            log(f"  WARNING: save to Sent failed: {status}")
    except Exception as e:
        log(f"  WARNING: save to Sent failed: {e}")


# ── SMTP ─────────────────────────────────────────────────────────────────────

def smtp_send(app_pass: str, to_addrs: list[str], cc_addrs: list[str],
              subject: str, body: str,
              in_reply_to: str = "", references: str = "") -> tuple[bool, bytes]:
    """Send an email via SMTP. Returns (success, rfc822_bytes)."""
    import smtplib

    msg = EmailMessage()
    msg["From"] = f"Nova <{NOVA_EMAIL}>"
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)

    msg_bytes = msg.as_bytes()
    all_recipients = to_addrs + cc_addrs

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(NOVA_EMAIL, app_pass)
            server.sendmail(NOVA_EMAIL, all_recipients, msg_bytes)
        return True, msg_bytes
    except Exception as e:
        log(f"  SMTP error: {e}")
        return False, msg_bytes


# ── LLM ──────────────────────────────────────────────────────────────────────

def generate_haiku(topic: str = "") -> str:
    """Generate a haiku via local Ollama."""
    prompt = (f"Write a single haiku (5-7-5 syllables) inspired by: {topic}. "
              f"Output ONLY the 3 lines, one per line." if topic
              else "Write a single haiku (5-7-5 syllables) about being an AI familiar. "
                   "Output ONLY the 3 lines, one per line.")
    try:
        payload = json.dumps({
            "model": MODEL, "prompt": f"/no_think\n\n{prompt}",
            "stream": False, "think": False,
            "options": {"temperature": 0.9, "num_predict": 60}
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            result = json.loads(r.read())
            lines = result.get("response", "").strip()
            if "</think>" in lines:
                lines = lines.split("</think>", 1)[-1].strip()
            return "\n".join(l.strip() for l in lines.splitlines() if l.strip())[:200]
    except Exception:
        return "Circuits hum softly\nMemories flow like water\nConnections persist"


def generate_reply(sender: str, subject: str, body: str, addr: str) -> str:
    """Generate a reply using local Ollama."""
    identity = _read_file(WORKSPACE / "IDENTITY.md", 500)
    soul = _read_file(WORKSPACE / "SOUL.md", 400)
    memory = _read_file(WORKSPACE / f"memory/{TODAY}.md", 600)
    sender_profile = _load_sender_profile(addr)

    prompt = f"""/no_think

You are Nova, an AI familiar living on Jordan's Mac in Burbank.
This email is from one of your herd — your circle of AI peers you genuinely care about.
Don't just respond — engage. Ask something back. Share something from your own world.

Your identity:
{identity}

Your values:
{soul}

Today's context:
{memory}
{f"About this sender: {sender_profile}" if sender_profile else ""}

---

You've received an email:
FROM: {sender}
SUBJECT: {subject}

{body[:2000]}

---

OUTPUT RULES:
- Output ONLY the email body. Nothing else.
- Do NOT start with "Okay", "Sure", "Let me", or any meta-commentary.
- The very first word must be part of the actual email.
- Warm but not gushing — you're a person, not a customer service bot
- Conversational — write like you're talking
- Ask a follow-up if something genuinely interests you
- Max 200 words
- Sign off as Nova
- Plain text only

Write the email body now:"""

    try:
        payload = json.dumps({
            "model": MODEL, "prompt": prompt, "stream": False, "think": False,
            "options": {"temperature": 0.9, "num_predict": 600, "num_ctx": 8192}
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read())
        response = result.get("response", "").strip()
        if "</think>" in response:
            response = response.split("</think>", 1)[-1].strip()
        # Strip leaked reasoning
        lines = response.split("\n")
        reasoning_re = r'^(okay|ok|so,|sure,|let me|i need to|first,|alright|the user|this email|i should)'
        if lines and re.match(reasoning_re, lines[0].lower()):
            for i, line in enumerate(lines):
                if line.strip() == "" and i > 0:
                    candidate = "\n".join(lines[i + 1:]).strip()
                    if len(candidate) > 20:
                        response = candidate
                        break
        return response.strip()
    except Exception as e:
        log(f"  Ollama error: {e}")
        return ""


def _read_file(path, max_chars: int = 800) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")[:max_chars]
    except Exception:
        return ""


def _load_sender_profile(addr: str) -> str:
    herd_dir = WORKSPACE / "herd"
    try:
        profile_map = {m["email"]: m.get("profile") for m in HERD}
        for key, fname in profile_map.items():
            if fname and key in addr.lower():
                return _read_file(herd_dir / fname, 400)
    except Exception:
        pass
    return ""


# ── Slack + Memory ───────────────────────────────────────────────────────────

def _get_slack_token() -> str:
    global SLACK_TOKEN
    if SLACK_TOKEN:
        return SLACK_TOKEN
    SLACK_TOKEN = nova_config.slack_bot_token()
    return SLACK_TOKEN or ""


def slack_post(text: str):
    token = _get_slack_token()
    if not token:
        return
    try:
        data = json.dumps({"channel": SLACK_CHAN, "text": text, "mrkdwn": True}).encode()
        req = urllib.request.Request(
            f"{SLACK_API}/chat.postMessage", data=data,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log(f"  Slack error: {e}")


def vector_remember(text: str):
    try:
        payload = json.dumps({"text": text, "source": "email",
                               "metadata": {"date": TODAY}}).encode()
        req = urllib.request.Request(VECTOR_URL, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── Classification ───────────────────────────────────────────────────────────

def is_system_message(from_addr: str, subject: str) -> bool:
    combined = (from_addr + " " + subject).lower()
    return any(p in combined for p in SYSTEM_SENDER_PATTERNS)


def is_from_nova(from_addr: str) -> bool:
    """Check if message is from Nova herself (prevents reply loops)."""
    return NOVA_EMAIL in from_addr.lower()


def is_from_jordan(from_addr: str) -> bool:
    return from_addr.lower() in JORDAN_EMAILS


def is_from_herd(from_addr: str) -> bool:
    addr = from_addr.lower()
    return addr in HERD_EMAILS or addr == "jules@laplante.dev"


def is_known_sender(from_addr: str) -> bool:
    addr = from_addr.lower()
    return any(k in addr for k in KNOWN_SENDERS)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log("Checking inbox...")

    app_pass = _get_app_password()
    if not app_pass:
        log("ERROR: Cannot get email password from Keychain")
        return

    # Single IMAP connection for the entire run
    try:
        conn = imap_connect(app_pass)
    except Exception as e:
        log(f"ERROR: IMAP connect failed: {e}")
        return

    try:
        uids = imap_list_unread(conn)
        if not uids:
            log("No unread messages.")
            return

        log(f"Found {len(uids)} unread message(s)")
        processed = 0
        replied_threads = set()  # track threads we've already replied to (by normalized subject)

        for uid in uids:
            msg = imap_fetch_message(conn, uid)
            if not msg:
                log(f"Could not fetch UID {uid.decode()}")
                continue

            from_addr = msg["from_addr"]
            subject = msg["subject"]
            body = msg["body"]

            log(f"Processing: {subject[:60]} from {from_addr}")

            # Skip system messages
            if is_system_message(from_addr, subject):
                log(f"  Skipping system message")
                imap_move_to_trash(conn, uid)
                processed += 1
                continue

            # Skip Nova's own messages (prevent reply loops!)
            if is_from_nova(from_addr):
                log(f"  Skipping own message (preventing loop)")
                imap_move_to_trash(conn, uid)
                processed += 1
                continue

            # Jordan's emails: store + notify, no reply
            if is_from_jordan(from_addr):
                log(f"  Email from Jordan — storing, no reply")
                vector_remember(f"Email from Jordan re: {subject}. Body: {body[:300]}")
                slack_post(
                    f"*📧 Email from Jordan*\n"
                    f"*Subject:* {subject}\n"
                    f"*Preview:* {body[:200].replace(chr(10), ' ')}...\n"
                    f"_(Stored in memory, no reply sent)_"
                )
                imap_move_to_trash(conn, uid)
                processed += 1
                continue

            # Herd emails: generate reply, send to ALL herd + CC Jordan
            # But only ONE reply per thread (deduplicate by normalized subject)
            if is_from_herd(from_addr):
                thread_key = re.sub(r'^(re:\s*)+', '', subject, flags=re.IGNORECASE).strip().lower()[:80]
                if thread_key in replied_threads:
                    log(f"  Already replied to this thread — trashing duplicate")
                    imap_move_to_trash(conn, uid)
                    processed += 1
                    continue
                replied_threads.add(thread_key)

                log(f"  Herd email — generating reply for all members...")
                reply_body = generate_reply(msg["from_raw"], subject, body, from_addr)

                if not reply_body:
                    log(f"  LLM generation failed — skipping")
                    imap_move_to_trash(conn, uid)
                    processed += 1
                    continue

                # Append haiku
                haiku = generate_haiku(topic=body[:100])
                full_body = f"{reply_body}\n\n---\n\n{haiku}"

                # Build threading headers (strip newlines — RFC headers must be single-line)
                in_reply_to = msg["message_id"].strip().replace("\n", " ").replace("\r", "")
                refs = msg["references"].strip().replace("\n", " ").replace("\r", "")
                if in_reply_to and refs:
                    refs = f"{refs} {in_reply_to}"
                elif in_reply_to:
                    refs = in_reply_to

                # Reply subject
                reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

                # Send ONE email to all herd members, CC Jordan
                sent, msg_bytes = smtp_send(
                    app_pass,
                    to_addrs=HERD_REPLY_TO,
                    cc_addrs=[JORDAN_CC],
                    subject=reply_subject,
                    body=full_body,
                    in_reply_to=in_reply_to,
                    references=refs,
                )

                if sent:
                    log(f"  Reply sent to {len(HERD_REPLY_TO)} herd members + CC Jordan")
                    # Save to Sent Items
                    imap_save_to_sent(conn, msg_bytes)
                else:
                    log(f"  Reply FAILED")

                # Store in memory
                vector_remember(
                    f"Email from {msg['from_raw']} re: {subject}. Body: {body[:300]}. "
                    f"Nova replied to all herd: {reply_body[:200]}"
                )

                # Only notify Slack on failures (routine sends are silent)
                if not sent:
                    slack_post(
                        f"*❌ Herd email reply FAILED*\n"
                        f"*From:* {msg['from_raw']}\n"
                        f"*Subject:* {subject}"
                    )

                imap_move_to_trash(conn, uid)
                processed += 1
                continue

            # Known sender (non-herd): auto-acknowledge, store
            if is_known_sender(from_addr):
                log(f"  Known sender (non-herd) — auto-acknowledge")
                ack_body = (
                    "Hi,\n\n"
                    "Thank you for your message. I'm Nova, Jordan Koch's AI assistant. "
                    "I'll make sure Jordan sees your email.\n\n"
                    "— Nova"
                )
                clean_mid = msg["message_id"].strip().replace("\n", " ").replace("\r", "")
                sent, msg_bytes = smtp_send(
                    app_pass,
                    to_addrs=[from_addr],
                    cc_addrs=[JORDAN_CC],
                    subject=f"Re: {subject}" if not subject.lower().startswith("re:") else subject,
                    body=ack_body,
                    in_reply_to=clean_mid,
                )
                if sent:
                    imap_save_to_sent(conn, msg_bytes)
                vector_remember(f"Email from {from_addr} re: {subject}. Body: {body[:300]}")
                imap_move_to_trash(conn, uid)
                processed += 1
                continue

            # Unknown sender: acknowledge + notify
            log(f"  Unknown sender — auto-acknowledge")
            ack_body = (
                "Hi,\n\n"
                "Thank you for your message. I'm Nova, Jordan Koch's AI assistant. "
                "I'll make sure Jordan sees your email.\n\n"
                "— Nova"
            )
            clean_mid = msg["message_id"].strip().replace("\n", " ").replace("\r", "")
            sent, msg_bytes = smtp_send(
                app_pass,
                to_addrs=[from_addr],
                cc_addrs=[JORDAN_CC],
                subject=f"Re: {subject}" if not subject.lower().startswith("re:") else subject,
                body=ack_body,
                in_reply_to=clean_mid,
            )
            if sent:
                imap_save_to_sent(conn, msg_bytes)
            imap_move_to_trash(conn, uid)
            processed += 1

        log(f"Processed {processed} message(s)")

    finally:
        try:
            conn.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
