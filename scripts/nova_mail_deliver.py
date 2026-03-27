#!/usr/bin/env python3
"""
nova_mail_deliver.py — Fetch mail summary and post to Slack + email Jordan.
Runs nova_mail_fetch.py to get data, then handles all delivery itself.
No LLM involved — reliable, direct.

Written by Jordan Koch.
"""

import json
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
import nova_config


VECTOR_MEM_URL = "http://127.0.0.1:18790/remember"


def vector_remember(text: str, metadata: dict = None):
    """Store text in Nova's vector memory. Silently skips if server is down."""
    try:
        payload = json.dumps({
            "text": text,
            "source": "email",
            "metadata": metadata or {},
        }).encode()
        req = urllib.request.Request(
            VECTOR_MEM_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass

SCRIPTS      = Path.home() / ".openclaw" / "scripts"
SLACK_TOKEN  = nova_config.slack_bot_token()
SLACK_CHAN   = "C0AMNQ5GX70"
JORDAN_EMAIL = "kochj23" + "@gmail.com"  # noqa
SUMMARY_FILE = Path("/tmp/nova_mail_fetch.txt")
SLACK_API    = "https://slack.com/api"

# Senders/subjects to treat as low-priority / newsletters
NOISE_PATTERNS = [
    "wayfair", "hulu", "ihg", "turbotax", "magazines.com", "usps informed delivery",
    "boy smells", "printables", "hims", "sendafriend", "happy gardening",
    "overlord caps", "morimoto", "bob's watches", "wells fargo advisors",
    "skillshare", "capital grille", "teepublic", "amazon", "citibank"
]

# Senders that are always important
IMPORTANT_PATTERNS = [
    "american express", "amex", "apple developer", "adt security",
    "network solutions", "partners federal", "kevin", "sam@", "amy", "jason.cox",
    "digitalnoise.net", "nova@digitalnoise.net"
]


def log(msg):
    print(f"[nova_mail_deliver {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text):
    chunks = [text[i:i+3000] for i in range(0, len(text), 3000)]
    for chunk in chunks:
        data = json.dumps({"channel": SLACK_CHAN, "text": chunk, "mrkdwn": True}).encode()
        req  = urllib.request.Request(
            f"{SLACK_API}/chat.postMessage", data=data,
            headers={"Authorization": "Bearer " + SLACK_TOKEN,
                     "Content-Type": "application/json; charset=utf-8"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                log(f"Slack error: {result.get('error')}")


def send_email(subject, body):
    try:
        sys.path.insert(0, str(SCRIPTS))
        from nova_send_mail import send_mail
        ok = send_mail(JORDAN_EMAIL, subject, body)
        if ok:
            log("Email sent.")
        else:
            log("Email failed (check nova_send_mail.py logs above).")
    except Exception as e:
        log(f"Email failed: {e}")


def is_noise(sender, subject):
    combined = (sender + " " + subject).lower()
    return any(p in combined for p in NOISE_PATTERNS)


def is_important(sender, subject):
    combined = (sender + " " + subject).lower()
    return any(p in combined for p in IMPORTANT_PATTERNS)


def parse_accounts_from_file(content):
    """Parse the formatted file into {email: [messages]} dict."""
    accounts = {}
    current_email = None
    current_msg = {}

    for line in content.splitlines():
        line = line.strip()
        # Section header: 📬 email@address — N message(s), M unread
        m = re.match(r"📬\s+(\S+@\S+)\s+—", line)
        if m:
            current_email = m.group(1).lower()
            if current_email not in accounts:
                accounts[current_email] = []
            current_msg = {}
            continue

        if current_email is None:
            continue

        if line.startswith("[UNREAD]") and "FROM:" in line:
            sender = re.sub(r"\[UNREAD\]\s*FROM:\s*|\s*\[UNREAD\]", "", line).strip()
            current_msg = {"sender": sender, "subject": "", "unread": True}
        elif line.startswith("[READ]") and "FROM:" in line:
            sender = re.sub(r"\[READ\]\s*FROM:\s*", "", line).strip()
            current_msg = {"sender": sender, "subject": "", "unread": False}
        elif line.startswith("SUBJ:") and current_msg:
            current_msg["subject"] = line[5:].strip()
            accounts[current_email].append(dict(current_msg))
            current_msg = {}

    return accounts


def build_summary(content):
    today = datetime.now().strftime("%A, %B %d %Y")

    total_match = re.search(r"Total messages.*?(\d+)", content)
    total = total_match.group(1) if total_match else "?"

    accounts = parse_accounts_from_file(content)

    total_unread = sum(1 for msgs in accounts.values() for m in msgs if m["unread"])

    out = []
    out.append(f"*Nova Mail Summary — {today}*")
    out.append(f"📬 {total} messages · {total_unread} unread across {len(accounts)} addresses\n")
    out.append("─" * 40)

    for email, messages in accounts.items():
        if not messages:
            continue

        unread = [m for m in messages if m["unread"]]
        important_msgs = [m for m in messages if is_important(m["sender"], m["subject"])]
        noise_msgs = [m for m in messages if is_noise(m["sender"], m["subject"])]
        other_unread = [m for m in unread if m not in important_msgs and m not in noise_msgs]

        unread_count = len(unread)
        out.append(f"\n*📧 {email}* — {len(messages)} messages, {unread_count} unread")

        if important_msgs:
            out.append("  *🔴 Important:*")
            for m in important_msgs:
                flag = "●" if m["unread"] else "○"
                out.append(f"    {flag} {m['subject'] or '(no subject)'}")

        if other_unread:
            out.append("  *📨 Unread:*")
            for m in other_unread[:6]:
                out.append(f"    • {m['subject'] or m['sender']}")
            if len(other_unread) > 6:
                out.append(f"    _+{len(other_unread)-6} more_")

        noise_unread = [m for m in noise_msgs if m["unread"]]
        if noise_unread:
            out.append(f"  _🗑 {len(noise_unread)} newsletters/marketing (unread)_")

    out.append(f"\n─" + "─" * 39)
    out.append(f"_— Nova · {datetime.now().strftime('%I:%M %p')}_")
    return "\n".join(out)


def main():
    log("Starting mail delivery...")

    # Run the fetch script to refresh data
    log("Running nova_mail_fetch.py...")
    result = subprocess.run(
        ["python3", str(SCRIPTS / "nova_mail_fetch.py")],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        log(f"Fetch failed: {result.stderr}")
        sys.exit(1)

    if not SUMMARY_FILE.exists():
        log("No summary file found after fetch.")
        sys.exit(1)

    content = SUMMARY_FILE.read_text(encoding="utf-8")

    if content.startswith("NO_MAIL"):
        msg = f"*Nova Morning Mail Summary — {datetime.now().strftime('%A, %B %d %Y')}*\n📭 No new mail in the last 24 hours."
        slack_post(msg)
        send_email(f"Nova Morning Mail Summary -- {datetime.now().strftime('%Y-%m-%d')}", "No new mail in the last 24 hours.")
        log("No mail — sent empty summary.")
        return

    summary = build_summary(content)
    log("Posting to Slack...")
    slack_post(summary)

    log("Sending email...")
    send_email(
        f"Nova Morning Mail Summary -- {datetime.now().strftime('%Y-%m-%d')}",
        summary.replace("*", "").replace("_", "")
    )

    log("Storing important emails in vector memory...")
    today_str = datetime.now().strftime("%Y-%m-%d")
    accounts = parse_accounts_from_file(content)
    for email_addr, messages in accounts.items():
        for msg in messages:
            sender  = msg.get("sender", "")
            subject = msg.get("subject", "")
            if not subject and not sender:
                continue
            if is_important(sender, subject):
                priority = "high"
            elif msg.get("unread") and not is_noise(sender, subject):
                priority = "normal"
            else:
                continue
            vector_remember(
                f"Email to {email_addr} from {sender}: {subject}",
                {"date": today_str, "to": email_addr, "priority": priority},
            )

    log("Done.")


if __name__ == "__main__":
    main()
