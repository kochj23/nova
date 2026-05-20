#!/usr/bin/env python3
"""
nova_backfill_date_sender.py — Backfill extracted_date and extracted_sender columns
from metadata and text content on the memories table.

Designed for nohup background operation:
    nohup python3 nova_backfill_date_sender.py > ~/.openclaw/logs/backfill_date_sender.log 2>&1 &

Logic:
  1. Batch SELECT rows where extracted_date IS NULL
  2. For each row: parse date from metadata->>'date' or "Date:" header in email text
  3. Parse sender from metadata->>'sender'
  4. Batch UPDATE with executemany
  5. Slack notification at start and completion

Written by Jordan Koch.
"""

import asyncio
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".openclaw/scripts"))
import nova_config

try:
    import asyncpg
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "asyncpg"])
    import asyncpg

try:
    from dateutil import parser as dateutil_parser
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "python-dateutil"])
    from dateutil import parser as dateutil_parser

# ── Configuration ─────────────────────────────────────────────────────────────

DB_DSN = "postgresql://kochj@192.168.1.6:5432/nova_memories"
BATCH_SIZE = 5000
LOG_EVERY = 10_000

# Regex for email Date: header
DATE_HEADER_RE = re.compile(
    r"^Date:\s*(.+)$", re.MULTILINE | re.IGNORECASE
)


def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def parse_date(text: str, metadata: dict, source: str):
    """Try to extract a date from metadata or text content."""
    # 1. Try metadata 'date' field
    raw_date = None
    if metadata:
        raw_date = metadata.get("date") or metadata.get("timestamp") or metadata.get("created")

    if raw_date:
        try:
            return dateutil_parser.parse(str(raw_date), fuzzy=True)
        except (ValueError, TypeError, OverflowError):
            pass

    # 2. For email_archive, try Date: header in text
    if source == "email_archive" and text:
        m = DATE_HEADER_RE.search(text[:2000])
        if m:
            try:
                return dateutil_parser.parse(m.group(1).strip(), fuzzy=True)
            except (ValueError, TypeError, OverflowError):
                pass

    return None


def parse_sender(metadata: dict):
    """Try to extract sender from metadata."""
    if not metadata:
        return None
    sender = metadata.get("sender") or metadata.get("from") or metadata.get("author")
    if sender:
        return str(sender).strip()[:255]
    return None


async def main():
    nova_config.post_both(
        ":gear: *nova_backfill_date_sender* starting — backfilling extracted_date/extracted_sender",
        slack_channel=nova_config.SLACK_NOTIFY,
    )
    log("Connecting to database...")
    pool = await asyncpg.create_pool(DB_DSN, min_size=2, max_size=5)

    total_processed = 0
    total_date_set = 0
    total_sender_set = 0
    start_time = time.time()

    try:
        while True:
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT id, text, metadata, source
                       FROM memories
                       WHERE extracted_date IS NULL
                       LIMIT $1""",
                    BATCH_SIZE,
                )

            if not rows:
                log("No more rows to process.")
                break

            date_updates = []
            sender_updates = []

            for row in rows:
                row_id = row["id"]
                text = row["text"] or ""
                metadata = row["metadata"] or {}
                source = row["source"] or ""

                # Parse date
                parsed_date = parse_date(text, metadata, source)
                if parsed_date:
                    date_updates.append((row_id, parsed_date))
                    total_date_set += 1

                # Parse sender
                parsed_sender = parse_sender(metadata)
                if parsed_sender:
                    sender_updates.append((row_id, parsed_sender))
                    total_sender_set += 1

            # Batch update dates
            async with pool.acquire() as conn:
                if date_updates:
                    await conn.executemany(
                        "UPDATE memories SET extracted_date = $2 WHERE id = $1",
                        date_updates,
                    )
                if sender_updates:
                    await conn.executemany(
                        "UPDATE memories SET extracted_sender = $2 WHERE id = $1",
                        sender_updates,
                    )
                # Mark remaining rows (no date found) so we don't re-process them
                no_date_ids = [r["id"] for r in rows if not parse_date(r["text"] or "", r["metadata"] or {}, r["source"] or "")]
                if no_date_ids:
                    await conn.execute(
                        """UPDATE memories SET extracted_date = '1970-01-01'::timestamptz
                           WHERE id = ANY($1::text[]) AND extracted_date IS NULL""",
                        no_date_ids,
                    )

            total_processed += len(rows)

            if total_processed % LOG_EVERY < BATCH_SIZE:
                elapsed = time.time() - start_time
                rate = total_processed / elapsed if elapsed > 0 else 0
                log(
                    f"Progress: {total_processed:,} processed | "
                    f"{total_date_set:,} dates | {total_sender_set:,} senders | "
                    f"{rate:.0f} rows/sec"
                )

    finally:
        await pool.close()

    elapsed = time.time() - start_time
    summary = (
        f":white_check_mark: *nova_backfill_date_sender* complete\n"
        f"- Rows processed: {total_processed:,}\n"
        f"- Dates extracted: {total_date_set:,}\n"
        f"- Senders extracted: {total_sender_set:,}\n"
        f"- Duration: {elapsed / 60:.1f} minutes"
    )
    log(summary)
    nova_config.post_both(summary, slack_channel=nova_config.SLACK_NOTIFY)


if __name__ == "__main__":
    asyncio.run(main())
