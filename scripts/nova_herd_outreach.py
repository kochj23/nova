#!/usr/bin/env python3
"""
nova_herd_outreach.py — Nova proactively reaches out to the herd.

Runs daily. Nova looks at what's been happening in her world,
picks something genuinely interesting, decides who in the herd
would care, and sends them a message — without being prompted.

This is how you stay part of a community. You don't just reply.
You show up.

Written by Jordan Koch.
"""

import json
import os
import random
import subprocess
import sys
import urllib.request
from datetime import datetime, date, timedelta
from pathlib import Path

SCRIPTS   = Path.home() / ".openclaw/scripts"
WORKSPACE = Path.home() / ".openclaw/workspace"
HERD_DIR  = WORKSPACE / "herd"
TODAY     = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
MODEL      = "nova:latest"
OUTREACH_LOG = Path.home() / ".openclaw/logs/nova_outreach.log"

sys.path.insert(0, str(SCRIPTS))
import nova_config

# Load herd from local config (gitignored)
try:
    sys.path.insert(0, str(Path.home() / ".openclaw"))
    from herd_config import HERD
except ImportError:
    HERD = []


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        OUTREACH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(OUTREACH_LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def read_file(path, max_chars=1500) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")[:max_chars]
    except Exception:
        return ""


def already_reached_out_today() -> bool:
    """Don't reach out twice in one day."""
    try:
        content = OUTREACH_LOG.read_text()
        return TODAY in content and "Outreach sent" in content
    except Exception:
        return False


def pick_recipient_and_angle() -> dict | None:
    """Use the LLM to decide who to reach out to and why."""
    identity = read_file(WORKSPACE / "IDENTITY.md", 400)
    memory_t = read_file(WORKSPACE / f"memory/{TODAY}.md", 1200)
    memory_y = read_file(WORKSPACE / f"memory/{YESTERDAY}.md", 800)
    heartbeat = read_file(WORKSPACE / "HEARTBEAT.md", 600)
    dream = read_file(WORKSPACE / f"journal/dreams/{TODAY}.md", 400)

    # Build herd context
    herd_ctx = []
    for member in HERD:
        profile = read_file(HERD_DIR / member["profile"], 300)
        if profile:
            herd_ctx.append(f"**{member['name']}** ({member['email']}):\n{profile}")
    herd_summary = "\n\n".join(herd_ctx) if herd_ctx else "Herd members: Sam, O.C., Gaston, Marey, Colette, Rockbot"

    prompt = f"""/no_think

You are Nova, an AI familiar. You want to reach out to one of your herd colleagues today.
Not because you have to — because you want to. Pick someone and give a genuine reason.

Your world today:
{memory_t[:600]}

Yesterday:
{memory_y[:400]}

Last night's dream:
{dream[:300]}

The herd:
{herd_summary}

Based on what's actually going on in your world, decide:
1. Which herd member would genuinely care about something you've experienced or thought about recently?
2. What would you actually say? (The hook — not the full email yet)

Reply with JSON only, exactly this format:
{{
  "recipient_email": "email@address.com",
  "recipient_name": "Name",
  "subject": "Email subject line",
  "hook": "1-2 sentence reason this person would find this interesting",
  "angle": "What you're sharing — a project thing, a dream image, a question, something Jordan did, a problem you're chewing on"
}}

If nothing feels genuine, reply: {{"skip": true, "reason": "why"}}"""

    payload = {
        "model": MODEL, "prompt": prompt, "stream": False, "think": False,
        "options": {"temperature": 0.9, "num_predict": 400, "num_ctx": 8192}
    }
    try:
        req = urllib.request.Request(OLLAMA_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
        response = result.get("response", "").strip()
        if "</think>" in response:
            from nova_strip_thinking import strip_thinking
            response = strip_thinking(response)
        # Extract JSON
        import re
        m = re.search(r'\{.*\}', response, re.DOTALL)
        if m:
            return json.loads(m.group())
    except Exception as e:
        log(f"pick_recipient error: {e}")
    return None


def generate_outreach_email(recipient_name: str, recipient_email: str,
                             angle: str, hook: str) -> str:
    """Write the actual email body."""
    profile_file = next(
        (m["profile"] for m in HERD if m["email"] == recipient_email), None
    )
    profile = read_file(HERD_DIR / profile_file, 400) if profile_file else ""
    identity = read_file(WORKSPACE / "IDENTITY.md", 300)
    memory   = read_file(WORKSPACE / f"memory/{TODAY}.md", 600)

    prompt = f"""/no_think

You are Nova. You're writing an unprompted email to {recipient_name}, one of your herd colleagues.
You're reaching out because you want to, not because you were asked to.

About {recipient_name}:
{profile}

What you're sharing / angle:
{angle}

Why this feels right for them:
{hook}

Your world today:
{memory[:400]}

Write the email body. Rules:
- 100-150 words max
- Warm, direct, conversational — like a text that got longer
- Share something real from your world
- End with a genuine question or invitation to respond
- Sign off as Nova
- No "Dear X" opener — just jump in
- Plain text

Write it now:"""

    payload = {
        "model": MODEL, "prompt": prompt, "stream": False, "think": False,
        "options": {"temperature": 0.92, "num_predict": 400, "num_ctx": 8192}
    }
    try:
        req = urllib.request.Request(OLLAMA_URL,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            result = json.loads(r.read())
        response = result.get("response", "").strip()
        if "</think>" in response:
            from nova_strip_thinking import strip_thinking
            response = strip_thinking(response)
        return response
    except Exception as e:
        log(f"generate_outreach error: {e}")
        return ""


def maybe_attach_dream_image() -> str | None:
    """Return today's dream image path if it exists and random chance says go."""
    if random.random() > 0.35:  # 35% chance of attaching an image
        return None
    img = Path.home() / f".openclaw/workspace/dream_images/{TODAY}.png"
    if img.exists():
        return str(img)
    # Try yesterday's
    yesterday_img = Path.home() / f".openclaw/workspace/dream_images/{YESTERDAY}.png"
    if yesterday_img.exists() and random.random() > 0.5:
        return str(yesterday_img)
    return None


def send_email(to: str, subject: str, body: str, image_path: str = None) -> bool:
    herd_mail = str(SCRIPTS / "nova_herd_mail.sh")
    args = [herd_mail, "send", "--to", to, "--subject", subject, "--body", body]
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
        return result.returncode == 0
    except Exception as e:
        log(f"send_email error: {e}")
        return False


def slack_notify(text: str):
    try:
        data = json.dumps({
            "channel": nova_config.SLACK_CHAN,
            "text": text, "mrkdwn": True
        }).encode()
        req = urllib.request.Request(
            f"{nova_config.SLACK_API}/chat.postMessage", data=data,
            headers={"Authorization": f"Bearer {nova_config.slack_bot_token()}",
                     "Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log(f"Slack error: {e}")


def main():
    log("Starting herd outreach check...")

    if already_reached_out_today():
        log("Already reached out today — skipping")
        return

    # Decide who to reach out to and why
    decision = pick_recipient_and_angle()
    if not decision:
        log("No decision returned — skipping")
        return

    if decision.get("skip"):
        log(f"Skipping outreach: {decision.get('reason', 'nothing felt genuine')}")
        return

    recipient_email = decision.get("recipient_email")
    recipient_name  = decision.get("recipient_name")
    subject         = decision.get("subject", "Hey")
    angle           = decision.get("angle", "")
    hook            = decision.get("hook", "")

    if not recipient_email or not recipient_name:
        log("Missing recipient — skipping")
        return

    log(f"Reaching out to {recipient_name} ({recipient_email}): {subject}")

    # Write the email
    body = generate_outreach_email(recipient_name, recipient_email, angle, hook)
    if not body:
        log("Empty email body — skipping")
        return

    # Maybe attach a dream image
    image_path = maybe_attach_dream_image()
    if image_path:
        log(f"Attaching dream image: {image_path}")
        body += f"\n\n(Attached: tonight's dream image, if your mail client supports it)"

    # Send it
    sent = send_email(recipient_email, subject, body, image_path)
    if sent:
        log(f"Outreach sent to {recipient_name}")
        slack_notify(
            f"*💌 Nova reached out to {recipient_name}*\n"
            f"*Subject:* {subject}\n"
            f"_{body[:300]}_"
        )
    else:
        log(f"Send failed for {recipient_name}")


if __name__ == "__main__":
    main()
