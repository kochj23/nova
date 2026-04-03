#!/usr/bin/env python3
"""
nova_send_mail.py — Thin wrapper around nova_herd_mail.sh (herd-mail).

All outbound mail from Nova MUST go through nova_herd_mail.sh, which loads
credentials from macOS Keychain. This file exists solely to preserve the
send_mail() import API used by other scripts (e.g. nova_mail_deliver.py).

Do NOT add direct SMTP, smtplib, waggle, or Mail.app logic here.
If nova_herd_mail.sh ever needs a new capability, add it to herd_mail.py.

Author: Jordan Koch
"""

import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERD_MAIL = str(Path(__file__).parent / "nova_herd_mail.sh")


def log(msg):
    print(f"[nova_send_mail {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def send_mail(to, subject, body, image_path=None, bcc=False, in_reply_to=None,
              references=None, rich=False):
    """
    Send email via nova_herd_mail.sh (credentials loaded from macOS Keychain).

    Args:
        to:          str or list of str — recipient address(es)
        subject:     str
        body:        str — plain text or Markdown
        image_path:  str | None — path to attach as a file
        bcc:         bool — if True and to is a list, sends individually per recipient
        in_reply_to: str | None — IMAP Message-ID for thread continuation
        references:  ignored (herd-mail handles thread headers internally)
        rich:        bool — pass --rich for HTML rendering
    Returns:
        bool — True if all sends succeeded
    """
    recipients = [to] if isinstance(to, str) else list(to)
    success = True

    for recipient in recipients:
        args = [HERD_MAIL, "send", "--to", recipient, "--subject", subject, "--body", body]

        if image_path:
            args += ["--attachment", str(image_path)]
        if in_reply_to:
            args += ["--message-id", in_reply_to]
        if rich:
            args.append("--rich")

        log(f"Sending to {recipient}: {subject}")
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=60)
            if result.returncode == 0:
                log(f"Sent OK to {recipient}")
            else:
                log(f"FAILED to {recipient}: {result.stderr.strip() or result.stdout.strip()}")
                success = False
        except Exception as e:
            log(f"ERROR sending to {recipient}: {e}")
            success = False

    return success


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: nova_send_mail.py <to> <subject> <body> [image_path] [in_reply_to]")
        sys.exit(1)

    ok = send_mail(
        to          = sys.argv[1],
        subject     = sys.argv[2],
        body        = sys.argv[3],
        image_path  = sys.argv[4] if len(sys.argv) > 4 else None,
        in_reply_to = sys.argv[5] if len(sys.argv) > 5 else None,
    )
    sys.exit(0 if ok else 1)
