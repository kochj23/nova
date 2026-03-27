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
SLACK_TOKEN  = nova_config.slack_bot_token()
SLACK_CHAN   = nova_config.SLACK_CHAN
SLACK_API    = nova_config.SLACK_API

# Senders who get a real reply vs. who gets a generic bounce
KNOWN_SENDERS = {
    "kochj23" + "@gmail.com",        # Jordan  # noqa
    "kochj" + "@digitalnoise.net",   # Jordan  # noqa
    "mjramos76" + "@gmail.com",      # Mark    # noqa
    "jordan.koch" + "@disney.com",   # Jordan  # noqa
    "jason.cox" + "@disney.com",     # Jason   # noqa
    "james.tatum" + "@disney.com",   # James   # noqa
    "kevin.duane" + "@disney.com",   # Kevin   # noqa
    "amy.mccain" + "@gmail.com",     # Amy     # noqa
    "amy.mccain" + "@disney.com",    # Amy     # noqa
    "mark.ramos" + "@disney.com",    # Mark    # noqa
    "nova@digitalnoise.net",     # Nova herself (for replies to her own sent mail)
    "sam@jasonacox.com",         # Sam (Jason Cox's AI)
    "marey@makehorses.org",      # Marey (James Tatum's AI)
    "oc@mostlycopyandpaste.com", # O.C. (Kevin Duane's AI)
    "rockbot@makehorses.org",    # Rockbot (Colin's AI)
    "gaston@bluemoxon.com",      # Gaston (Mark's AI)
    "colette@pilatesmuse.co",    # Colette (Nadia's AI)
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
    profile_map = {
        "sam@jasonacox.com": "sam.md",
        "oc@mostlycopyandpaste.com": "oc.md",
        "gaston@bluemoxon.com": "gaston.md",
        "marey@makehorses.org": "marey.md",
        "colette@pilatesmuse.co": "colette.md",
        "rockbot@makehorses.org": "rockbot.md",
        "nova@digitalnoise.net": None,
    }
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

Write a reply. Tone guide:
- Warm but not gushing — you're a person, not a customer service bot
- Direct with personality — say what you actually think
- Conversational — write like you're talking, not filing a report
- Curious — ask a follow-up if something genuinely interests you
- If you have a different take, say so naturally ("honestly, I'd push back on that...")
- Max 200 words
- Sign off as Nova
- No "Dear X" opener — jump straight in
- Plain text only

Write the reply now:"""

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
        if "</think>" in response:
            response = response.split("</think>", 1)[-1].strip()
        return response
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
        is_herd  = any(h in addr for h in {
            "nova@digitalnoise.net",
            "sam@jasonacox.com", "oc@mostlycopyandpaste.com", "gaston@bluemoxon.com",
            "marey@makehorses.org", "colette@pilatesmuse.co", "rockbot@makehorses.org"
        })

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
