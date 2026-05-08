#!/usr/bin/env python3
"""
nova_dead_letter_replay.py — Replay dead-lettered memory ingest items.

Runs weekly (Sunday 4am) or manually. Pulls all items from nova:memory:dead-letter,
resets their retry counter, and re-queues them to nova:memory:ingest.

Items fail into dead-letter after 3 consecutive embed/insert failures.
Most failures are transient (Ollama overloaded, PG reindex in progress) —
replaying a week later almost always succeeds.

Written by Jordan Koch.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_WARN, LOG_ERROR

REDIS_QUEUE       = "nova:memory:ingest"
REDIS_DEAD_LETTER = "nova:memory:dead-letter"


def main():
    try:
        import redis
        r = redis.from_url("redis://localhost:6379")
        r.ping()
    except Exception as e:
        log(f"Redis unavailable: {e}", level=LOG_ERROR, source="dead-letter-replay")
        sys.exit(1)

    total = r.llen(REDIS_DEAD_LETTER)
    if total == 0:
        log("Dead-letter queue empty — nothing to replay", level=LOG_INFO, source="dead-letter-replay")
        return

    log(f"Replaying {total} dead-lettered items", level=LOG_INFO, source="dead-letter-replay")
    replayed = 0
    skipped  = 0

    for _ in range(total):
        raw = r.lpop(REDIS_DEAD_LETTER)
        if raw is None:
            break
        try:
            item = json.loads(raw)
            # Reset retry counter so the ingest worker gives it fresh attempts
            item.pop("_retries", None)
            item.pop("_error", None)
            r.rpush(REDIS_QUEUE, json.dumps(item))
            replayed += 1
        except Exception as e:
            log(f"Skipping malformed dead-letter item: {e}", level=LOG_WARN,
                source="dead-letter-replay")
            skipped += 1

    msg = f":recycle: *Dead-Letter Replay*\n• Replayed: {replayed}\n• Skipped (malformed): {skipped}"
    nova_config.post_both(msg, slack_channel=nova_config.SLACK_NOTIFY)
    log(f"Replay complete: {replayed} replayed, {skipped} skipped",
        level=LOG_INFO, source="dead-letter-replay")


if __name__ == "__main__":
    main()
