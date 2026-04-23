#!/usr/bin/env python3
"""
nova_ingest_monitor.py — Standalone Slack status reporter for email ingestion.

Runs independently as a nohup process. Posts to Slack every 5 minutes
with current memory count, Redis queue depth, and rate calculations.
Survives Claude Code session timeout.

Written by Jordan Koch.
"""

import json
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

INTERVAL = 300  # 5 minutes
VECTOR_URL = "http://127.0.0.1:18790"

def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_CHAN)


def get_stats():
    stats = {}
    # Memory count
    try:
        with urllib.request.urlopen(f"{VECTOR_URL}/health", timeout=5) as r:
            d = json.loads(r.read())
            stats["memories"] = d.get("count", 0)
    except Exception:
        stats["memories"] = "?"

    # Queue depth
    try:
        with urllib.request.urlopen(f"{VECTOR_URL}/queue/stats", timeout=5) as r:
            d = json.loads(r.read())
            stats["queue"] = d.get("pending", 0)
    except Exception:
        stats["queue"] = "?"

    return stats


def main():
    print(f"[ingest_monitor] Starting — Slack reports every {INTERVAL}s", flush=True)
    start_time = time.time()
    prev_count = 0

    while True:
        stats = get_stats()
        elapsed = time.time() - start_time
        count = stats.get("memories", 0)
        queue = stats.get("queue", 0)

        # Rate calculation
        if isinstance(count, int) and isinstance(prev_count, int) and prev_count > 0:
            rate = (count - prev_count) / INTERVAL
        else:
            rate = 0
        prev_count = count if isinstance(count, int) else prev_count

        # ETA for queue drain
        if isinstance(queue, int) and rate > 0:
            eta_sec = queue / rate
            eta = str(timedelta(seconds=int(eta_sec)))
        elif isinstance(queue, int) and queue == 0:
            eta = "complete"
        else:
            eta = "calculating..."

        msg = (
            f"*Email Ingest Monitor*\n"
            f"  Memories: *{count:,}* (queue: {queue:,})\n"
            f"  Rate: {rate:.1f}/sec\n"
            f"  Queue ETA: {eta}\n"
            f"  Uptime: {str(timedelta(seconds=int(elapsed)))}\n"
            f"  _All local — no PII leaves machine_"
        )
        slack_post(msg)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] memories={count} queue={queue} rate={rate:.1f}/s eta={eta}", flush=True)

        # Stop when queue is empty and has been for 2 cycles
        if isinstance(queue, int) and queue == 0 and elapsed > 600:
            slack_post(f"*Email Ingest Complete*\n  Final memory count: *{count:,}*\n  _Monitor shutting down._")
            print("[ingest_monitor] Queue empty. Done.", flush=True)
            break

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
