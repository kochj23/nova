#!/usr/bin/env python3
"""
nova_ingest_emlx.py — Ingest Apple Mail V10 .emlx files into Nova's vector memory.

Reads .emlx files from the Apple Mail V10 directory structure, extracts
email content, and stores via the async Redis queue endpoint.

Usage: python3 nova_ingest_emlx.py [base_dir]
Default: /Volumes/Data/Mail/V10

Uses POST /remember?async=1 — fire-and-forget, 8ms per call, worker handles embedding.

Author: Jordan Koch / kochj23
"""

import email
import email.policy
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

import re as _re

VECTOR_URL  = "http://127.0.0.1:18790/remember?async=1"

# Skip emails matching these patterns entirely
_SKIP = [_re.compile(p, _re.IGNORECASE) for p in [
    r'\b(porn|pornograph|nude photo|naked photo|explicit video|sex tape)\b',
    r'\b(incest|bestiality|underage|teen porn|child porn)\b',
    r'\b(horny-scrubbers|adult site|xxx)\b',
    r'http[s]?://[^\s]*(?:porn|xxx|adult|nude|cam|escort)[^\s]*',
]]

# Redact these patterns from body
_REDACT = [
    (_re.compile(r'\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b'), '[PHONE]'),
    (_re.compile(r'\b\d{3}-\d{2}-\d{4}\b'), '[SSN]'),
    (_re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'), '[EMAIL]'),
    (_re.compile(r'\b(?:erotic|aroused|naked|nude|masturbat\w+|orgasm\w*|horny|lust\w*|cum\b|cumming|fucking|fucked)\b', _re.IGNORECASE), '[REDACTED]'),
]

def _pii_filter(subject: str, body: str) -> tuple[bool, str]:
    """Returns (skip_entirely, redacted_body)."""
    text = f"{subject} {body}"
    if any(p.search(text) for p in _SKIP):
        return True, ""
    for pat, rep in _REDACT:
        body = pat.sub(rep, body)
    return False, body
BASE_DIR    = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/Volumes/Data/Mail/V10")
BATCH_LOG   = 5000   # log progress every N files

# Skip these mailbox types entirely
SKIP_FOLDERS = {"Trash", "Spam", "Junk", "Drafts", "Draft", "[Imap]/Trash",
                "Deleted Messages", "Deleted Items", "Junk E-Mail"}

def log(msg):
    print(f"[emlx_ingest {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def is_skip_folder(path: Path) -> bool:
    for part in path.parts:
        name = part.replace(".mbox", "")
        if name in SKIP_FOLDERS:
            return True
    return False

def parse_emlx(filepath: Path) -> dict | None:
    """Parse a .emlx file — raw RFC 2822 email with Apple metadata appended."""
    try:
        raw = filepath.read_bytes()
        # .emlx format: first line is byte count, then raw email, then Apple XML plist
        lines = raw.split(b"\n", 1)
        if len(lines) < 2:
            return None
        try:
            byte_count = int(lines[0].strip())
            email_bytes = lines[1][:byte_count]
        except ValueError:
            email_bytes = raw

        msg = email.message_from_bytes(email_bytes, policy=email.policy.default)
        sender  = str(msg.get("From", "unknown"))[:200]
        subject = str(msg.get("Subject", "(no subject)"))[:300]
        date_str = msg.get("Date", "")

        try:
            date_dt = parsedate_to_datetime(date_str).isoformat()
        except Exception:
            date_dt = datetime.utcnow().isoformat()

        # Extract text body
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ct = part.get_content_type()
                if ct == "text/plain":
                    try:
                        body = part.get_content()[:800]
                        break
                    except Exception:
                        pass
        else:
            try:
                body = msg.get_content()[:800]
            except Exception:
                body = str(msg.get_payload(decode=True) or "")[:800]

        if not body.strip() and not subject.strip():
            return None

        # PII / explicit content filter
        skip, body = _pii_filter(subject, body)
        if skip:
            return None

        # Determine folder name from path
        folder = "unknown"
        for part in filepath.parts:
            if part.endswith(".mbox"):
                folder = part[:-5]

        text = f"Email from {sender} (subject: {subject}): {body[:600]}"
        return {
            "text": text.strip(),
            "source": "email_archive",
            "metadata": {
                "folder": folder,
                "sender": sender[:100],
                "subject": subject[:150],
                "date": date_dt,
                "source_type": "emlx",
            }
        }
    except Exception:
        return None

def store(payload: dict) -> bool:
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            result = json.loads(r.read())
            return result.get("status") == "queued"
    except Exception as e:
        return False

def main():
    log(f"Scanning {BASE_DIR}")
    log("Using async queue — fire and forget via Redis")

    emlx_files = [
        f for f in BASE_DIR.rglob("*.emlx")
        if not is_skip_folder(f)
    ]
    total_files = len(emlx_files)
    log(f"Found {total_files:,} emlx files (excluding Trash/Spam/Junk/Drafts)")

    stored = 0
    skipped = 0
    errors = 0
    t0 = time.time()

    for i, filepath in enumerate(emlx_files):
        payload = parse_emlx(filepath)
        if payload is None:
            skipped += 1
            continue

        success = store(payload)
        if success:
            stored += 1
        else:
            # Brief retry on failure
            time.sleep(0.5)
            if store(payload):
                stored += 1
            else:
                errors += 1

        if (i + 1) % BATCH_LOG == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed
            remaining = (total_files - i - 1) / rate if rate > 0 else 0
            eta_h = int(remaining // 3600)
            eta_m = int((remaining % 3600) // 60)
            log(f"  {i+1:,}/{total_files:,} ({(i+1)/total_files*100:.1f}%) "
                f"— queued: {stored:,} skip: {skipped:,} err: {errors:,} "
                f"— {rate:.0f}/sec — eta {eta_h}h{eta_m}m")

    elapsed = time.time() - t0
    log(f"Scan complete in {elapsed/3600:.1f}h")
    log(f"  Queued for ingest: {stored:,}")
    log(f"  Skipped (empty):   {skipped:,}")
    log(f"  Errors:            {errors:,}")
    log(f"  Total files:       {total_files:,}")

if __name__ == "__main__":
    main()
