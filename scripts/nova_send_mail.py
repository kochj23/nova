#!/usr/bin/env python3
"""
nova_send_mail.py -- Send email from nova@digitalnoise.net via waggle-mail.

waggle-mail renders the body as Markdown -> plain text + styled HTML multipart.
When replying (in_reply_to provided), fetches the original via IMAP and appends
a properly quoted block -- real email threading.

Send path priority:
  1. waggle-mail via SMTP (smtp.gmail.com:587, Google App Password from Keychain)
  2. Fallback: macOS Mail.app via AppleScript (OAuth, always works)

Keychain entry (for waggle path):
  security add-generic-password \
    -a "nova@digitalnoise.net" \
    -s "nova-smtp-app-password" \
    -w "<16-char Google App Password>"

Usage (script):
  python3 nova_send_mail.py <to> <subject> <body_md> [image_path] [in_reply_to_msgid]

Usage (import):
  from nova_send_mail import send_mail
  send_mail("to@example.com", "Subject", "**Hello** from Nova")
  send_mail("to@example.com", "Re: Topic", "Sure!", in_reply_to="<msgid@gmail.com>")
  send_mail(["a@x.com","b@y.com"], "Subject", "body", bcc=True)

Written by Jordan Koch.
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

FROM_ADDR    = "nova@digitalnoise.net"
FROM_NAME    = "Nova"
SMTP_HOST    = "smtp.gmail.com"
SMTP_PORT    = 587
IMAP_HOST    = "imap.gmail.com"
IMAP_PORT    = 993
KEYCHAIN_SVC = "nova-smtp-app-password"
APPLESCRIPT  = str(Path(__file__).parent / "nova_send_mail.applescript")


def log(msg):
    print(f"[nova_send_mail {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_app_password():
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-a", FROM_ADDR, "-s", KEYCHAIN_SVC, "-w"],
            capture_output=True, text=True, timeout=10
        )
        pwd = result.stdout.strip()
        return pwd if result.returncode == 0 and pwd else None
    except Exception:
        return None


def _waggle_config(password):
    return {
        "host": SMTP_HOST, "port": SMTP_PORT, "tls": False,
        "user": FROM_ADDR, "password": password,
        "from_addr": FROM_ADDR, "from_name": FROM_NAME,
        "imap_host": IMAP_HOST, "imap_port": IMAP_PORT, "imap_tls": True,
    }


def _send_via_applescript(to, subject, body):
    """Send via macOS Mail.app (OAuth) -- always works as fallback."""
    recipients = [to] if isinstance(to, str) else list(to)
    success = True
    for recipient in recipients:
        try:
            result = subprocess.run(
                ["osascript", APPLESCRIPT, recipient, subject, body],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode == 0 and "SENT:" in result.stdout:
                log(f"[applescript] Sent to {recipient}: {subject}")
            else:
                log(f"[applescript] Failed to {recipient}: {result.stderr.strip() or result.stdout.strip()}")
                success = False
        except Exception as e:
            log(f"[applescript] Error: {e}")
            success = False
    return success


def send_mail(to, subject, body, image_path=None, bcc=False, in_reply_to=None, references=None, rich=False):
    """
    Send email from nova@digitalnoise.net.

    Body is treated as Markdown -- rendered to plain text + styled HTML.
    Existing plain-text callers work unchanged (plain text is valid Markdown).

    Args:
        to:           str or list -- recipient(s)
        subject:      str -- subject (use -- not em-dash per POLICIES.md)
        body:         str -- Markdown body
        image_path:   str or Path -- optional attachment
        bcc:          bool -- send each recipient as BCC
        in_reply_to:  str -- Message-ID for reply threading + IMAP quote fetch
        references:   str -- References header
        rich:         bool -- rich HTML rendering (opt-in)

    Returns: True on success, False on failure.
    """
    try:
        from waggle import send_email as waggle_send
        waggle_available = True
    except ImportError:
        waggle_available = False

    password = get_app_password() if waggle_available else None
    recipients = [to] if isinstance(to, str) else list(to)

    attachments = []
    if image_path:
        path = Path(image_path)
        if path.exists():
            attachments.append(str(path))
            log(f"Attachment: {path.name} ({path.stat().st_size} bytes)")
        else:
            log(f"Attachment not found: {image_path} -- skipping")

    # Try waggle (SMTP) if available and have credentials
    if waggle_available and password:
        cfg = _waggle_config(password)

        if bcc and len(recipients) > 1:
            success = True
            for recipient in recipients:
                try:
                    waggle_send(to=recipient, subject=subject, body_md=body,
                                in_reply_to=in_reply_to, references=references,
                                attachments=attachments or None, rich=rich, config=cfg)
                    log(f"BCC sent to {recipient}: {subject}")
                except Exception as e:
                    log(f"waggle failed for {recipient}: {e}")
                    success = _send_via_applescript(recipient, subject, body) and success
            return success

        to_str = recipients[0] if len(recipients) == 1 else ", ".join(recipients)
        try:
            waggle_send(to=to_str, subject=subject, body_md=body,
                        in_reply_to=in_reply_to, references=references,
                        attachments=attachments or None, rich=rich, config=cfg)
            log(f"Sent to {to_str}: {subject}")
            return True
        except Exception as e:
            log(f"waggle SMTP failed ({e}) -- falling back to Mail.app")

    # Fallback: macOS Mail.app via AppleScript (OAuth, no app password needed)
    return _send_via_applescript(
        recipients[0] if len(recipients) == 1 else recipients,
        subject,
        body
    )


def main():
    if len(sys.argv) < 4:
        print("Usage: nova_send_mail.py <to> <subject> <body> [image_path] [in_reply_to]")
        sys.exit(1)

    to          = sys.argv[1]
    subject     = sys.argv[2]
    body        = sys.argv[3]
    image_path  = sys.argv[4] if len(sys.argv) > 4 else None
    in_reply_to = sys.argv[5] if len(sys.argv) > 5 else None

    ok = send_mail(to, subject, body, image_path=image_path, in_reply_to=in_reply_to)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
