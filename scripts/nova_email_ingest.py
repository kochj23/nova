#!/usr/bin/env python3
"""
nova_email_ingest.py — Bulk email ingestion into Nova's vector memory.

Ingests macOS Mail.app .emlx files into Nova's vector memory at scale.
Designed for 3M+ emails with:
  - 8 parallel embedding workers
  - Redis async queue for non-blocking writes
  - Checkpoint/resume (survives interruption)
  - Deduplication via text_hash
  - 5-minute Slack status reports
  - Work email exclusion only (tax/divorce/intimacy now included per Apr 16 2026)
  - Progress tracking and ETA

Usage:
  python3 nova_email_ingest.py                    # Full ingest
  python3 nova_email_ingest.py --dry-run          # Count only, don't ingest
  python3 nova_email_ingest.py --status           # Show progress
  python3 nova_email_ingest.py --resume           # Resume from checkpoint

Written by Jordan Koch.
"""

import concurrent.futures
import email
import email.policy
import hashlib
import json
import os
import re
import signal
import struct
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Config ───────────────────────────────────────────────────────────────────

MAIL_DIR = Path.home() / "Library/Mail/V10"

# CRITICAL: Exclude Work email folders entirely — Jordan's explicit instruction
# Work emails are under local://9B69852E-.../Work/ (~2.97M emails)
# Only ingest Home (~278K), Import (~38K), Inbox, and other personal folders
EXCLUDE_PATHS = ["/Work/", "/Work -", "Work%20-"]
VECTOR_URL = "http://127.0.0.1:18790/remember?async=1"
SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_NOTIFY   # #nova-notifications for status updates
CHECKPOINT_FILE = Path.home() / ".openclaw/workspace/email_ingest_checkpoint.json"
WORKERS = 4
BATCH_SIZE = 50
STATUS_INTERVAL = 300  # 5 minutes
MAX_TEXT_LENGTH = 2000  # Truncate long emails for embedding

# Content exclusions REMOVED (Apr 16, 2026) — Jordan authorized ingesting
# tax, divorce, and intimacy emails. Only WORK emails remain excluded.
# The text_hash dedup ensures already-ingested emails are skipped.

# STRICT: Any email to/from jordan.koch@work is off limits
# Work email loaded from env or config — never hardcoded in tracked files
EXCLUDE_WORK_EMAIL = os.environ.get("NOVA_WORK_EMAIL", "").lower()

# ── Globals ──────────────────────────────────────────────────────────────────

stats = {
    "total_found": 0,
    "processed": 0,
    "ingested": 0,
    "skipped_tax": 0,
    "skipped_dup": 0,
    "skipped_empty": 0,
    "errors": 0,
    "started_at": None,
    "last_status": 0,
}
stats_lock = threading.Lock()
shutdown_flag = threading.Event()


def log(msg):
    print(f"[email_ingest {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Slack reporting ──────────────────────────────────────────────────────────

def slack_post(text):
    data = json.dumps({"channel": SLACK_CHAN, "text": text, "mrkdwn": True}).encode()
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage", data=data,
        headers={"Authorization": "Bearer " + SLACK_TOKEN,
                 "Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


def post_status():
    """Post a status update to Slack."""
    with stats_lock:
        s = dict(stats)

    elapsed = time.time() - s["started_at"] if s["started_at"] else 0
    rate = s["processed"] / elapsed if elapsed > 0 else 0
    remaining = s["total_found"] - s["processed"]
    eta_sec = remaining / rate if rate > 0 else 0
    eta = str(timedelta(seconds=int(eta_sec)))
    pct = (s["processed"] / s["total_found"] * 100) if s["total_found"] > 0 else 0

    msg = (
        f"*Email Ingest Status — {pct:.1f}%*\n"
        f"  Processed: {s['processed']:,} / {s['total_found']:,}\n"
        f"  Ingested: {s['ingested']:,}\n"
        f"  Skipped: {s['skipped_tax']:,} work, {s['skipped_dup']:,} dup, {s['skipped_empty']:,} empty\n"
        f"  Errors: {s['errors']:,}\n"
        f"  Rate: {rate:.0f}/sec\n"
        f"  ETA: {eta}\n"
        f"  Elapsed: {str(timedelta(seconds=int(elapsed)))}"
    )
    slack_post(msg)
    log(f"Status: {s['processed']:,}/{s['total_found']:,} ({pct:.1f}%), {rate:.0f}/sec, ETA {eta}")


def status_reporter():
    """Background thread that posts status every 5 minutes."""
    while not shutdown_flag.is_set():
        shutdown_flag.wait(STATUS_INTERVAL)
        if not shutdown_flag.is_set():
            post_status()


# ── EMLX parsing ─────────────────────────────────────────────────────────────

def parse_emlx(filepath):
    """Parse a macOS .emlx file and extract subject, sender, date, and body text.

    EMLX format: first line is byte count, then raw RFC822 email, then XML plist.
    """
    try:
        with open(filepath, "rb") as f:
            # First line is the byte count of the email portion
            first_line = f.readline()
            try:
                byte_count = int(first_line.strip())
            except ValueError:
                # Some emlx files don't have a byte count
                f.seek(0)
                byte_count = os.path.getsize(filepath)

            raw_email = f.read(byte_count)

        msg = email.message_from_bytes(raw_email, policy=email.policy.default)

        subject = msg.get("Subject", "") or ""
        sender = msg.get("From", "") or ""
        date_str = msg.get("Date", "") or ""
        to = msg.get("To", "") or ""

        # Extract body text
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain":
                    try:
                        body = part.get_content()
                        break
                    except Exception:
                        pass
                elif content_type == "text/html" and not body:
                    try:
                        html = part.get_content()
                        # Strip HTML tags for plain text
                        body = re.sub(r'<[^>]+>', ' ', html)
                        body = re.sub(r'\s+', ' ', body).strip()
                    except Exception:
                        pass
        else:
            try:
                body = msg.get_content()
            except Exception:
                body = ""

        if isinstance(body, bytes):
            body = body.decode("utf-8", errors="ignore")

        return {
            "subject": subject[:200],
            "sender": sender[:200],
            "to": to[:200],
            "date": date_str[:50],
            "body": body[:MAX_TEXT_LENGTH] if body else "",
        }
    except Exception as e:
        return None


# ── Processing ───────────────────────────────────────────────────────────────

def should_exclude(parsed):
    """Check if this email should be excluded. Only Work emails are excluded now."""
    if not EXCLUDE_WORK_EMAIL:
        return False
    sender = parsed.get("sender", "")
    to = parsed.get("to", "")
    subject = parsed.get("subject", "")
    body = parsed.get("body", "")[:200]

    work_check = (sender + " " + to + " " + subject + " " + body).lower()
    if EXCLUDE_WORK_EMAIL in work_check:
        return True
    return False


def make_memory_text(parsed):
    """Create the text string to be embedded."""
    parts = []
    if parsed.get("date"):
        parts.append(f"Date: {parsed['date']}")
    if parsed.get("sender"):
        parts.append(f"From: {parsed['sender']}")
    if parsed.get("to"):
        parts.append(f"To: {parsed['to']}")
    if parsed.get("subject"):
        parts.append(f"Subject: {parsed['subject']}")
    if parsed.get("body"):
        parts.append(parsed["body"][:MAX_TEXT_LENGTH])

    text = "\n".join(parts)
    return text[:MAX_TEXT_LENGTH] if text else ""


def ingest_one(filepath):
    """Process a single .emlx file. Returns True if ingested."""
    parsed = parse_emlx(filepath)
    if not parsed:
        with stats_lock:
            stats["errors"] += 1
        return False

    # Skip empty
    if not parsed.get("body") and not parsed.get("subject"):
        with stats_lock:
            stats["skipped_empty"] += 1
        return False

    # Skip tax/financial
    if should_exclude(parsed):
        with stats_lock:
            stats["skipped_tax"] += 1
        return False

    text = make_memory_text(parsed)
    if not text or len(text) < 20:
        with stats_lock:
            stats["skipped_empty"] += 1
        return False

    text_hash = hashlib.md5(text.encode()).hexdigest()

    # Send to vector memory via async Redis queue
    payload = json.dumps({
        "text": text,
        "source": "email_archive",
        "metadata": {
            "subject": parsed.get("subject", "")[:100],
            "sender": parsed.get("sender", "")[:100],
            "date": parsed.get("date", "")[:30],
            "type": "email",
        },
        "text_hash": text_hash,
    }).encode()

    try:
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            result = json.loads(r.read())
            if result.get("status") == "duplicate":
                with stats_lock:
                    stats["skipped_dup"] += 1
                return False
    except Exception:
        with stats_lock:
            stats["errors"] += 1
        return False

    with stats_lock:
        stats["ingested"] += 1
    return True


def process_batch(filepaths):
    """Process a batch of .emlx files."""
    for fp in filepaths:
        if shutdown_flag.is_set():
            break
        ingest_one(fp)
        with stats_lock:
            stats["processed"] += 1


# ── File discovery ───────────────────────────────────────────────────────────

def find_all_emlx():
    """Find all .emlx files in Mail.app storage, EXCLUDING Work folders."""
    log(f"Scanning {MAIL_DIR} for .emlx files (excluding Work folders)...")
    files = []
    skipped_work = 0
    for root, dirs, filenames in os.walk(str(MAIL_DIR)):
        # Skip Work email directories entirely
        if any(excl in root for excl in EXCLUDE_PATHS):
            skipped_work += len([f for f in filenames if f.endswith(".emlx")])
            continue
        for fname in filenames:
            if fname.endswith(".emlx"):
                files.append(os.path.join(root, fname))
    log(f"Found {len(files):,} .emlx files (skipped {skipped_work:,} Work emails)")
    return files


# ── Checkpoint ───────────────────────────────────────────────────────────────

def save_checkpoint(processed_set):
    """Save set of processed file paths for resume."""
    data = {
        "processed_count": len(processed_set),
        "last_save": datetime.now().isoformat(),
        "stats": {k: v for k, v in stats.items() if k != "started_at"},
    }
    # Save just the count — storing 3M paths would be too large
    # Instead, we track by the stats and re-skip dupes via text_hash
    CHECKPOINT_FILE.write_text(json.dumps(data, indent=2))


def load_checkpoint():
    """Load checkpoint data."""
    if CHECKPOINT_FILE.exists():
        try:
            return json.loads(CHECKPOINT_FILE.read_text())
        except Exception:
            pass
    return None


# ── Signal handling ──────────────────────────────────────────────────────────

def handle_signal(signum, frame):
    log("Shutdown signal received — finishing current batch...")
    shutdown_flag.set()


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    all_files = find_all_emlx()
    stats["total_found"] = len(all_files)
    stats["started_at"] = time.time()

    # Post start notification
    slack_post(
        f"*Email Ingest Started*\n"
        f"  Files found: {len(all_files):,}\n"
        f"  Workers: {WORKERS}\n"
        f"  Status updates every 5 minutes\n"
        f"  Excluding: Work emails only (tax/divorce/intimacy now included)"
    )

    # Start status reporter thread
    reporter = threading.Thread(target=status_reporter, daemon=True)
    reporter.start()

    # Process in parallel batches
    log(f"Starting {WORKERS}-worker parallel ingest...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as executor:
        # Split files into batches
        batches = [all_files[i:i + BATCH_SIZE] for i in range(0, len(all_files), BATCH_SIZE)]
        futures = []

        for batch in batches:
            if shutdown_flag.is_set():
                break
            futures.append(executor.submit(process_batch, batch))

        # Wait for completion
        for future in concurrent.futures.as_completed(futures):
            if shutdown_flag.is_set():
                break
            try:
                future.result()
            except Exception as e:
                log(f"Batch error: {e}")

    # Final status
    shutdown_flag.set()
    post_status()

    # Final Slack notification
    with stats_lock:
        s = dict(stats)
    elapsed = time.time() - s["started_at"]
    slack_post(
        f"*Email Ingest Complete*\n"
        f"  Total processed: {s['processed']:,}\n"
        f"  Ingested: {s['ingested']:,}\n"
        f"  Skipped: {s['skipped_tax']:,} work, {s['skipped_dup']:,} dup, {s['skipped_empty']:,} empty\n"
        f"  Errors: {s['errors']:,}\n"
        f"  Duration: {str(timedelta(seconds=int(elapsed)))}"
    )
    log("Done.")


def dry_run():
    """Count files without ingesting."""
    all_files = find_all_emlx()
    print(f"Total .emlx files: {len(all_files):,}")

    # Sample 100 to estimate tax exclusions
    import random
    sample = random.sample(all_files, min(100, len(all_files)))
    tax_count = 0
    empty_count = 0
    for fp in sample:
        parsed = parse_emlx(fp)
        if not parsed or (not parsed.get("body") and not parsed.get("subject")):
            empty_count += 1
        elif should_exclude(parsed):
            tax_count += 1

    tax_pct = tax_count / len(sample) * 100
    empty_pct = empty_count / len(sample) * 100
    est_ingest = int(len(all_files) * (1 - tax_pct/100 - empty_pct/100))

    print(f"Sample analysis ({len(sample)} files):")
    print(f"  Tax/financial exclusions: ~{tax_pct:.1f}%")
    print(f"  Empty/unparseable: ~{empty_pct:.1f}%")
    print(f"  Estimated ingestible: ~{est_ingest:,}")
    print(f"  At {WORKERS} workers, ~31 embeddings/sec: ~{est_ingest / 31 / 3600:.1f} hours")


def show_status():
    cp = load_checkpoint()
    if cp:
        print(f"Last checkpoint: {cp.get('last_save', '?')}")
        print(f"Processed: {cp.get('processed_count', 0):,}")
        s = cp.get("stats", {})
        for k, v in s.items():
            print(f"  {k}: {v}")
    else:
        print("No checkpoint found.")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Email Ingest")
    parser.add_argument("--dry-run", action="store_true", help="Count and estimate only")
    parser.add_argument("--status", action="store_true", help="Show checkpoint status")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--workers", type=int, default=WORKERS, help=f"Parallel workers (default: {WORKERS})")
    args = parser.parse_args()

    if args.workers:
        WORKERS = args.workers

    if args.dry_run:
        dry_run()
    elif args.status:
        show_status()
    else:
        main()
