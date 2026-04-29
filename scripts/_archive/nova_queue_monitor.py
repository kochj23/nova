#!/usr/bin/env python3
"""
nova_queue_monitor.py — Monitor Redis ingest queue, post to Slack every 15 min.

Runs until the queue is drained, then posts a final summary and exits.

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

try:
    import redis
except ImportError:
    print("ERROR: pip3 install redis")
    sys.exit(1)

REDIS_KEY = "nova:memory:ingest"
INTERVAL = 900  # 15 minutes
VECTOR_URL = "http://127.0.0.1:18790/stats"


def slack_post(text):
    nova_config.post_both(text, slack_channel=nova_config.SLACK_NOTIFY)


def get_memory_count():
    try:
        resp = urllib.request.urlopen(VECTOR_URL, timeout=5)
        data = json.loads(resp.read())
        return data.get("count", 0)
    except Exception:
        return 0


def main():
    r = redis.from_url("redis://localhost:6379", decode_responses=True)

    start_depth = r.llen(REDIS_KEY)
    start_time = time.time()
    start_memories = get_memory_count()
    prev_depth = start_depth

    if start_depth == 0:
        slack_post(":white_check_mark: *Redis Queue Monitor* — Queue is already empty. Nothing to monitor.")
        return

    slack_post(
        f":bar_chart: *Redis Queue Monitor Started*\n"
        f"  Queue depth: {start_depth:,}\n"
        f"  Total memories: {start_memories:,}\n"
        f"  Reporting every 15 minutes until drained"
    )

    while True:
        time.sleep(INTERVAL)

        depth = r.llen(REDIS_KEY)
        elapsed = time.time() - start_time
        drained = start_depth - depth
        rate = drained / (elapsed / 60) if elapsed > 60 else 0
        interval_drained = prev_depth - depth
        memories_now = get_memory_count()
        new_memories = memories_now - start_memories

        if depth == 0:
            slack_post(
                f":white_check_mark: *Redis Queue Drained*\n"
                f"  Total processed: {drained:,} items\n"
                f"  New memories: {new_memories:,} (total: {memories_now:,})\n"
                f"  Duration: {str(timedelta(seconds=int(elapsed)))}\n"
                f"  Avg rate: {rate:,.0f}/min"
            )
            return

        if rate > 0:
            eta_min = depth / rate
            eta = str(timedelta(minutes=int(eta_min)))
        else:
            eta = "unknown"

        pct = (drained / start_depth * 100) if start_depth else 0

        slack_post(
            f":bar_chart: *Redis Queue Status*\n"
            f"  Remaining: {depth:,} ({100 - pct:.0f}% left)\n"
            f"  Drained this interval: {interval_drained:,}\n"
            f"  Total drained: {drained:,}/{start_depth:,} ({pct:.0f}%)\n"
            f"  Rate: {rate:,.0f}/min\n"
            f"  New memories: {new_memories:,} (total: {memories_now:,})\n"
            f"  Elapsed: {str(timedelta(seconds=int(elapsed)))}\n"
            f"  ETA: {eta}"
        )

        prev_depth = depth


if __name__ == "__main__":
    main()
