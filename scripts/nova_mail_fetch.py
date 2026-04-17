#!/usr/bin/env python3
"""
nova_mail_fetch.py — Run nova_mail_summary.applescript, format the output,
and write a clean summary file for Nova to read and post.

Written by Jordan Koch.
"""

import subprocess
import sys
import re
from datetime import datetime
from pathlib import Path

SCRIPTS  = Path.home() / ".openclaw" / "scripts"
OUT_FILE = Path.home() / ".openclaw/workspace/state/nova_mail_fetch.txt"


def run_applescript():
    result = subprocess.run(
        ["osascript", str(SCRIPTS / "nova_mail_summary.applescript")],
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        return None, result.stderr.strip()
    return result.stdout.strip(), None


def parse_messages(raw):
    """Parse the applescript output into a list of message dicts grouped by account."""
    accounts = {}
    current_account = None

    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("=== ACCOUNT:"):
            # === ACCOUNT: Digitalnoise Gmail (8 messages) ===
            m = re.match(r"=== ACCOUNT: .+? <(.+?)> \((\d+) messages\)", line)
            if m:
                current_account = m.group(1).lower()  # use email address, merge dupes
            else:
                # fallback: old format without email tag
                m2 = re.match(r"=== ACCOUNT: (.+?) \((\d+) messages\)", line)
                if m2:
                    current_account = m2.group(1)
            if current_account and current_account not in accounts:
                accounts[current_account] = []
        elif line.startswith("FROM:") and current_account is not None:
            accounts[current_account].append({"from": line[6:].strip(), "subject": "", "date": "", "body": "", "unread": "[UNREAD]" in line})
        elif line.startswith("SUBJECT:") and current_account and accounts[current_account]:
            accounts[current_account][-1]["subject"] = line[9:].strip()
        elif line.startswith("DATE:") and current_account and accounts[current_account]:
            accounts[current_account][-1]["date"] = line[6:].strip()
        elif line.startswith("BODY:") and current_account and accounts[current_account]:
            accounts[current_account][-1]["body"] = line[6:].strip()[:200]

    return accounts


def format_for_nova(accounts, total):
    lines = []
    lines.append(f"MAIL SUMMARY — {datetime.now().strftime('%A %B %d, %Y')}")
    lines.append(f"Total messages (last 24 hours): {total}")
    lines.append("=" * 60)

    for account, messages in accounts.items():
        if not messages:
            continue
        unread = [m for m in messages if m["unread"]]
        read   = [m for m in messages if not m["unread"]]

        lines.append(f"\n📬 {account} — {len(messages)} message(s), {len(unread)} unread")
        lines.append("-" * 40)

        for m in unread:
            lines.append(f"  [UNREAD] FROM: {m['from']}")
            lines.append(f"           SUBJ: {m['subject']}")
            lines.append(f"           DATE: {m['date']}")
            if m["body"]:
                lines.append(f"           BODY: {m['body'][:120]}...")
            lines.append("")

        for m in read:
            lines.append(f"  [READ]   FROM: {m['from']}")
            lines.append(f"           SUBJ: {m['subject']}")
            lines.append("")

    lines.append("=" * 60)
    lines.append("END OF MAIL SUMMARY")
    return "\n".join(lines)


def main():
    print(f"[nova_mail_fetch] Running mail summary script...", flush=True)

    raw, err = run_applescript()
    if err:
        OUT_FILE.write_text(f"ERROR running mail summary: {err}")
        print(f"[nova_mail_fetch] ERROR: {err}", flush=True)
        sys.exit(1)

    if raw.startswith("NO_MAIL"):
        OUT_FILE.write_text("NO_MAIL: No messages in the last 24 hours across all accounts.")
        print("[nova_mail_fetch] No mail found.", flush=True)
        return

    # Parse total count
    total = 0
    m = re.match(r"TOTAL:(\d+)", raw)
    if m:
        total = int(m.group(1))

    accounts = parse_messages(raw)
    formatted = format_for_nova(accounts, total)

    OUT_FILE.write_text(formatted, encoding="utf-8")
    print(f"[nova_mail_fetch] Done. {total} messages written to {OUT_FILE}", flush=True)
    print(f"SUMMARY_FILE: {OUT_FILE}", flush=True)


if __name__ == "__main__":
    main()
