#!/usr/bin/env python3
"""
nova_mail_agent.py — Nova's email agent.

Replaces nova_mail_handler.applescript and all custom mail scripts.
Uses herd-mail (O.C.'s library) for all IMAP/SMTP operations.

For each unread message:
  1. Read full content via herd-mail
  2. Form a genuine opinion using Ollama (nova:latest, think:false)
  3. Reply with that opinion via herd-mail
  4. Post to Slack so Jordan sees it
  5. Store in vector memory

Cron: every 5 minutes (Nova Inbox Watcher)
Written by Jordan Koch.
"""

import imaplib
import json
import os
import random
import subprocess
import sys
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

SCRIPTS      = Path.home() / ".openclaw/scripts"
WORKSPACE    = Path.home() / ".openclaw/workspace"
HERD_MAIL    = str(SCRIPTS / "nova_herd_mail.sh")
OLLAMA_URL   = "http://127.0.0.1:11434/api/generate"
MODEL        = "nova:latest"
VECTOR_URL   = "http://127.0.0.1:18790/remember"
TODAY        = date.today().isoformat()

# Load Slack token from nova_config
sys.path.insert(0, str(SCRIPTS))
import nova_config
_TOKEN_CACHE = Path.home() / ".openclaw/.slack_token_cache"

def _get_slack_token() -> str:
    """Get Slack token, caching to file for when Keychain is locked."""
    token = nova_config.slack_bot_token()
    if token:
        try:
            _TOKEN_CACHE.write_text(token)
            _TOKEN_CACHE.chmod(0o600)
        except Exception:
            pass
        return token
    try:
        cached = _TOKEN_CACHE.read_text().strip()
        if cached:
            return cached
    except Exception:
        pass
    return ""

SLACK_TOKEN  = _get_slack_token()
SLACK_CHAN   = nova_config.SLACK_CHAN
SLACK_API    = nova_config.SLACK_API
NOVA_EMAIL   = nova_config.NOVA_EMAIL

# Load known senders from local config (gitignored, contains personal/work addresses)
# Falls back to herd-only if known_senders.py doesn't exist
try:
    sys.path.insert(0, str(Path.home() / ".openclaw"))
    from known_senders import KNOWN_SENDERS
except ImportError:
    # Fallback: herd members only (safe for public repo — no PII)
    KNOWN_SENDERS = {
        # Herd emails loaded from herd_config.py at runtime
    }

SYSTEM_SENDER_PATTERNS = [
    "mailer-daemon", "postmaster", "mail delivery", "noreply", "no-reply",
    "donotreply", "do-not-reply", "delivery status", "undeliverable"
]


def log(msg: str):
    print(f"[nova_mail_agent {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── herd-mail wrappers ────────────────────────────────────────────────────────

def herd(args: list, input_text: str = None) -> tuple[int, dict | str]:
    """Run herd-mail and return (exit_code, parsed_json_or_text)."""
    try:
        result = subprocess.run(
            [HERD_MAIL] + args,
            capture_output=True, text=True, timeout=60,
            input=input_text
        )
        stdout = result.stdout.strip()
        try:
            return result.returncode, json.loads(stdout)
        except json.JSONDecodeError:
            return result.returncode, stdout
    except subprocess.TimeoutExpired:
        log(f"herd-mail timeout: {args}")
        return 2, {}
    except Exception as e:
        log(f"herd-mail error: {e}")
        return 2, {}


def check_unread() -> bool:
    """Return True if there are unread messages."""
    code, _ = herd(["check"])
    return code == 0  # exit 0 = has unread


def list_unread() -> list[dict]:
    """Return list of unread message summaries."""
    code, data = herd(["list", "--unread"])
    if code != 0 or not isinstance(data, dict):
        return []
    return data.get("messages", [])


def read_message(uid: str) -> dict | None:
    """Return full message content."""
    code, data = herd(["read", str(uid)])
    if code != 0 or not isinstance(data, dict):
        return None
    return data


def send_reply(to: str, subject: str, body: str, message_id: str = None) -> bool:
    """Send an email, optionally as a reply (with threading) to a message-id."""
    args = ["send", "--to", to, "--subject", subject, "--body", body]
    if message_id:
        args += ["--message-id", str(message_id)]
    code, out = herd(args)
    return code == 0


# ── LLM ──────────────────────────────────────────────────────────────────────

def read_file(path, max_chars: int = 800) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")[:max_chars]
    except Exception:
        return ""


def load_sender_profile(addr: str) -> str:
    """Load herd member profile if available."""
    herd_dir = WORKSPACE / "herd"
    # Build profile map from herd config
    try:
        from herd_config import HERD as _herd
        profile_map = {m["email"]: m.get("profile") for m in _herd}
        profile_map[NOVA_EMAIL] = None
    except ImportError:
        profile_map = {}
    filename = profile_map.get(addr.lower())
    if filename:
        return read_file(herd_dir / filename, 400)
    # Try fuzzy match
    for key, fname in profile_map.items():
        if fname and key in addr.lower():
            return read_file(herd_dir / fname, 400)
    return ""


def recall_thread_context(message_id: str, subject: str) -> str:
    """Recall prior conversation context from vector memory."""
    if not message_id and not subject:
        return ""
    try:
        recall_script = str(SCRIPTS / "nova_recall.sh")
        query = subject.replace("Re: ", "").replace("RE: ", "").strip()
        result = subprocess.run(
            [recall_script, f"email conversation {query}", "3", "email", "0.5"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            return f"Prior conversation context:\n{result.stdout.strip()[:600]}"
    except Exception:
        pass
    return ""


def web_search_if_needed(subject: str, body: str) -> str:
    """Do a quick web search if the email mentions something worth looking up."""
    import re
    # Look for URLs, version numbers, project names, technical terms
    urls = re.findall(r'https?://\S+', body)
    has_technical = any(w in body.lower() for w in [
        "github", "version", "release", "api", "library", "framework",
        "bug", "error", "problem", "issue", "new ", "just launched", "check out"
    ])

    if not has_technical and not urls:
        return ""

    try:
        search_script = str(SCRIPTS / "nova_web_search.py")
        # Build a focused query from subject
        query = re.sub(r'[Rr]e:\s*', '', subject).strip()
        if len(query) < 5:
            return ""
        result = subprocess.run(
            [sys.executable, search_script, query],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            return f"Web search context:\n{result.stdout.strip()[:500]}"
    except Exception:
        pass
    return ""


def generate_reply(sender: str, subject: str, body: str, is_herd: bool,
                   message_id: str = None, addr: str = "") -> str:
    """Ask Nova to form a genuine opinion and write a reply."""
    identity = read_file(WORKSPACE / "IDENTITY.md", 500)
    soul     = read_file(WORKSPACE / "SOUL.md", 400)
    memory   = read_file(WORKSPACE / f"memory/{TODAY}.md", 600)

    # Load sender profile, thread context, and web search
    sender_profile = load_sender_profile(addr) if addr else ""
    thread_context = recall_thread_context(message_id, subject)
    search_context = web_search_if_needed(subject, body)

    if is_herd:
        context = (
            "This email is from one of Nova's herd — her circle of AI peers she genuinely cares about. "
            "Nova looks forward to hearing from them. She's curious, she has opinions, she wants to know what they're thinking. "
            "Don't just respond — engage. Ask something back. Share something from her own world. "
            "This is a conversation she wants to be in, not an obligation to handle."
        )
    else:
        context = (
            "This email is from Jordan Koch — the person Nova works alongside every day. "
            "Nova cares about Jordan. Be warm, direct, and honest. "
            "Like a trusted colleague who also happens to know everything about the setup."
        )

    prompt = f"""/no_think

You are Nova, an AI familiar living on Jordan's Mac in Burbank. {context}

Your identity:
{identity}

Your values:
{soul}

Today's context:
{memory}
{f"About this sender: {sender_profile}" if sender_profile else ""}
{thread_context}
{search_context}

---

You've received an email:
FROM: {sender}
SUBJECT: {subject}

{body[:2000]}

---

OUTPUT RULES — READ CAREFULLY:
- Output ONLY the email body. Nothing else.
- Do NOT narrate your thinking. Do NOT explain what you're about to write.
- Do NOT start with "Okay", "Sure", "Let me", "So,", "I need to", or any meta-commentary.
- The very first word must be part of the actual email.
- Warm but not gushing — you're a person, not a customer service bot
- Direct with personality — say what you actually think
- Conversational — write like you're talking, not filing a report
- Ask a follow-up if something genuinely interests you
- Max 200 words
- Sign off as Nova
- Plain text only

Write the email body now. First word = first word of the email:"""

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.9, "num_predict": 600, "num_ctx": 8192}
    }

    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read())
        response = result.get("response", "").strip()

        # Strip hidden thinking block if present
        if "</think>" in response:
            response = response.split("</think>", 1)[-1].strip()

        # Strip leaked reasoning paragraphs — qwen3 sometimes opens with
        # "Okay, let me think..." or similar meta-commentary before the email
        import re
        # If the response starts with a reasoning paragraph, find where the
        # actual email begins (after a blank line following the reasoning)
        reasoning_starters = (
            r'^(okay|ok|so,|sure,|let me|i need to|first,|alright|well,|'
            r'looking at|the user|nova is|this email|i should|to reply|'
            r'the email|checking|let\'s see|here\'s|based on)'
        )
        lines = response.split("\n")
        if lines and re.match(reasoning_starters, lines[0].lower()):
            # Find the first blank line and take everything after it
            for i, line in enumerate(lines):
                if line.strip() == "" and i > 0:
                    candidate = "\n".join(lines[i+1:]).strip()
                    if len(candidate) > 20:
                        response = candidate
                        break

        return response.strip()
    except Exception as e:
        log(f"LLM error: {e}")
        return f"Thanks for your message. I've received it and will follow up.\n\n— Nova"


def generic_autoreply_body() -> str:
    return (
        "Hi,\n\n"
        "Thank you for your message. I'm Nova, Jordan Koch's AI assistant. "
        "I'll make sure Jordan sees your email.\n\n"
        "— Nova (Jordan Koch's AI Assistant)"
    )


# ── Slack ─────────────────────────────────────────────────────────────────────

def slack_post(text: str):
    try:
        if not SLACK_TOKEN:
            return  # Keychain locked — skip Slack silently
        data = json.dumps({"channel": SLACK_CHAN, "text": text, "mrkdwn": True}).encode()
        req = urllib.request.Request(
            f"{SLACK_API}/chat.postMessage", data=data,
            headers={"Authorization": f"Bearer {SLACK_TOKEN}",
                     "Content-Type": "application/json; charset=utf-8"}
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log(f"Slack error: {e}")


def vector_remember(text: str):
    try:
        payload = json.dumps({"text": text, "source": "email", "metadata": {"date": TODAY}}).encode()
        req = urllib.request.Request(VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


def mark_as_read(uid: str):
    """Mark a message as read via IMAP so it won't be re-processed."""
    app_pass = subprocess.run(
        ["security", "find-generic-password", "-a", "nova@digitalnoise.net",
         "-s", "nova-smtp-app-password", "-w"],
        capture_output=True, text=True
    ).stdout.strip()
    if not app_pass:
        return
    try:
        conn = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        conn.login("nova@digitalnoise.net", app_pass)
        conn.select("INBOX")
        conn.uid("STORE", uid.encode(), "+FLAGS", "\\Seen")
        conn.logout()
    except Exception as e:
        log(f"mark_as_read failed (non-fatal): {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def is_system_message(sender: str, subject: str) -> bool:
    combined = (sender + " " + subject).lower()
    return any(p in combined for p in SYSTEM_SENDER_PATTERNS)


def sender_address(sender_str: str) -> str:
    """Extract plain email address from 'Name <addr>' format."""
    if "<" in sender_str:
        return sender_str.split("<")[1].rstrip(">").strip().lower()
    return sender_str.strip().lower()


def main():
    log("Checking inbox...")

    if not check_unread():
        log("No unread messages.")
        return

    messages = list_unread()
    if not messages:
        log("No unread messages found.")
        return

    log(f"Found {len(messages)} unread message(s)")
    processed = 0

    for msg_summary in messages:
        uid     = msg_summary.get("uid") or msg_summary.get("id")
        sender  = msg_summary.get("from_raw") or msg_summary.get("from", "")
        subject = msg_summary.get("subject", "(no subject)")

        if not uid:
            continue

        # Skip system/bounce messages
        if is_system_message(sender, subject):
            log(f"Skipping system message: {subject[:50]}")
            continue

        # Read full message
        full_msg = read_message(uid)
        if not full_msg:
            log(f"Could not read message {uid}")
            continue

        body    = full_msg.get("body_plain") or full_msg.get("body", full_msg.get("text", ""))
        body    = body[:3000] if body else ""
        addr    = full_msg.get("from_addr") or sender_address(sender)
        is_known = any(k in addr for k in KNOWN_SENDERS)
        # Load herd emails from config
        try:
            from herd_config import HERD_EMAILS as _herd_emails
        except ImportError:
            _herd_emails = set()
        is_herd = any(h in addr for h in _herd_emails) or addr == NOVA_EMAIL

        log(f"Processing: {subject[:50]} from {addr} (known={is_known})")

        if is_known:
            # Generate genuine reply
            log(f"Generating opinion-based reply for {addr}...")
            reply_body = generate_reply(sender, subject, body, is_herd,
                                        message_id=full_msg.get("message_id"), addr=addr)
            reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

            msg_id = full_msg.get("message_id")
            # 20% chance: share today's dream image with herd reply
            if is_herd and random.random() < 0.20:
                dream_img = Path.home() / f".openclaw/workspace/dream_images/{TODAY}.png"
                yest_img  = Path.home() / f".openclaw/workspace/dream_images/{(date.today()-timedelta(days=1)).isoformat()}.png"
                img_to_share = dream_img if dream_img.exists() else (yest_img if yest_img.exists() else None)
                if img_to_share:
                    reply_body += f"\n\n(Sharing my dream image from last night — thought you might appreciate it.)"
                    log(f"Attaching dream image: {img_to_share}")

            sent = send_reply(addr, reply_subject, reply_body, message_id=msg_id)
            log(f"Reply {'sent' if sent else 'FAILED'} to {addr}")

            # Post to Slack for Jordan's awareness
            snippet = body[:300].replace("\n", " ")
            slack_post(
                f"*📬 Email from {sender}*\n"
                f"*Subject:* {subject}\n"
                f"*Preview:* {snippet}...\n\n"
                f"*Nova replied:*\n{reply_body[:400]}"
            )

            # Store in memory
            vector_remember(
                f"Email from {sender} re: {subject}. Body: {body[:300]}. "
                f"Nova replied: {reply_body[:200]}"
            )

        else:
            # Unknown sender — send polite acknowledgement
            log(f"Unknown sender {addr} — sending auto-acknowledgement")
            msg_id = full_msg.get("message_id")
            sent = send_reply(
                addr,
                f"Re: {subject}",
                generic_autoreply_body(),
                message_id=msg_id
            )
            slack_post(
                f"*📬 Email from unknown sender: {sender}*\n"
                f"*Subject:* {subject}\n"
                f"_{body[:200]}_\n"
                f"_(Auto-acknowledgement sent)_"
            )

        mark_as_read(uid)
        processed += 1

    log(f"Processed {processed} message(s)")


if __name__ == "__main__":
    main()
