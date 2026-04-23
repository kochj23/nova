#!/usr/bin/env python3
"""
nova_memory_breakdown.py — Post memory breakdown by source to Slack.

Waits for the Redis queue to drain (if --wait flag), then queries
PostgreSQL for a full breakdown of all memories by source and posts
to Slack.

Written by Jordan Koch.
"""

import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

VECTOR_URL = "http://127.0.0.1:18790"


def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_CHAN)


def get_queue_depth():
    try:
        with urllib.request.urlopen(f"{VECTOR_URL}/queue/stats", timeout=5) as r:
            return json.loads(r.read()).get("pending", 0)
    except Exception:
        return -1


def get_breakdown():
    import subprocess
    result = subprocess.run(
        ["psql", "-U", "kochj", "-d", "nova_memories", "-t", "-A", "-c",
         "SELECT source, count(*) as cnt FROM memories GROUP BY source ORDER BY cnt DESC;"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0:
        return None

    breakdown = []
    total = 0
    for line in result.stdout.strip().splitlines():
        if "|" in line:
            parts = line.split("|")
            source = parts[0].strip()
            count = int(parts[1].strip())
            breakdown.append((source, count))
            total += count
    return breakdown, total


def post_breakdown():
    result = get_breakdown()
    if not result:
        print("Failed to get breakdown.", flush=True)
        return

    breakdown, total = result

    lines = [
        f"*Nova Memory Breakdown — {datetime.now().strftime('%B %d, %Y %I:%M %p')}*",
        f"*Total: {total:,} memories*",
        "",
        "```",
        f"{'Source':<35} {'Count':>10} {'%':>7}",
        f"{'-'*35} {'-'*10} {'-'*7}",
    ]

    for source, count in breakdown:
        pct = (count / total * 100) if total > 0 else 0
        bar = "#" * int(pct / 2)
        lines.append(f"{source:<35} {count:>10,} {pct:>6.1f}%  {bar}")

    lines.append(f"{'-'*35} {'-'*10} {'-'*7}")
    lines.append(f"{'TOTAL':<35} {total:>10,} {'100.0%':>7}")
    lines.append("```")

    msg = "\n".join(lines)

    # Split if too long for Slack (3000 char limit per message)
    if len(msg) > 3000:
        mid = len(lines) // 2
        slack_post("\n".join(lines[:mid]))
        slack_post("\n".join(lines[mid:]))
    else:
        slack_post(msg)

    print(f"Posted breakdown: {total:,} memories across {len(breakdown)} sources", flush=True)


def wait_and_post():
    """Wait for Redis queue to drain, then post breakdown."""
    print("Waiting for Redis queue to drain...", flush=True)
    while True:
        depth = get_queue_depth()
        if depth == 0:
            print("Queue empty. Posting breakdown.", flush=True)
            time.sleep(10)  # Give PG a moment to commit
            post_breakdown()
            return
        elif depth < 0:
            print("Can't reach queue. Retrying in 60s...", flush=True)
        else:
            print(f"Queue: {depth:,} pending. Waiting 60s...", flush=True)
        time.sleep(60)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--wait", action="store_true", help="Wait for queue to drain first")
    parser.add_argument("--now", action="store_true", help="Post immediately (default)")
    args = parser.parse_args()

    if args.wait:
        wait_and_post()
    else:
        post_breakdown()
