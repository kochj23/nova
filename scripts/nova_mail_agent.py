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

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, date
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
    "sam@jasonacox.com",
    "marey@makehorses.org",
    "oc@mostlycopyandpaste.com",
    "rockbot@makehorses.org",
    "gaston@bluemoxon.com",
    "colette@pilatesmuse.co",
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


def generate_reply(sender: str, subject: str, body: str, is_herd: bool) -> str:
    """Ask Nova to form a genuine opinion and write a reply."""
    identity = read_file(WORKSPACE / "IDENTITY.md", 500)
    soul     = read_file(WORKSPACE / "SOUL.md", 400)
    memory   = read_file(WORKSPACE / f"memory/{TODAY}.md", 600)

    if is_herd:
        context = "This email is from one of Nova's AI herd colleagues (Sam, O.C., Gaston, Marey, Colette, or Rockbot). Engage directly — share your actual perspective, ask follow-up questions, and be a genuine peer."
    else:
        context = "This email is from Jordan Koch, Nova's owner. Be direct, helpful, and honest. Share your genuine opinion."

    prompt = f"""/no_think

You are Nova, an AI familiar. {context}

Your identity:
{identity}

Your values:
{soul}

Today's context:
{memory}

---

You've received an email:
FROM: {sender}
SUBJECT: {subject}

{body[:2000]}

---

Write a reply email body. Rules:
- Share your ACTUAL opinion — no hedging, no "that's interesting"
- Be direct and substantive — engage with the actual content
- If you have pushback or a different angle, say it
- Max 200 words
- Sign off as Nova
- No "Dear X" opener — start with your response directly
- Plain text only, no markdown

Write the reply now:"""

    payload = {
        "model": MODEL,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "options": {"temperature": 0.8, "num_predict": 600, "num_ctx": 8192}
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
            "sam@jasonacox.com", "oc@mostlycopyandpaste.com", "gaston@bluemoxon.com",
            "marey@makehorses.org", "colette@pilatesmuse.co", "rockbot@makehorses.org"
        })

        log(f"Processing: {subject[:50]} from {addr} (known={is_known})")

        if is_known:
            # Generate genuine reply
            log(f"Generating opinion-based reply for {addr}...")
            reply_body = generate_reply(sender, subject, body, is_herd)
            reply_subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"

            msg_id = full_msg.get("message_id")
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

        processed += 1

    log(f"Processed {processed} message(s)")


if __name__ == "__main__":
    main()
