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
from nova_notify import notify
from nova_logger import log, LOG_INFO, LOG_WARN, LOG_ERROR

REDIS_QUEUE       = "nova:memory:ingest"
REDIS_DEAD_LETTER = "nova:memory:dead-letter"


def main():
    try:
        import redis
        # SPOF plan 6a (2026-10-04): the dead-letter list lives in the memory server's redis on .6 (no AUTH there);
        # 'localhost' was .2's own password-protected redis when this task moved to scheduler-core -> NOAUTH every Sunday.
        r = redis.from_url(__import__("os").environ.get("REDIS_URL", "redis://192.168.1.6:6379"))
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

    # Scheduled-job completion digest — FYI status, not an alert. Weekly cadence,
    # so dedup on a stable key to collapse repeats.
    notify(
        "Dead-Letter Replay",
        body=f"• Replayed: {replayed}\n• Skipped (malformed): {skipped}",
        level="info", category="memory_ingest",
        dedup_key="dead-letter-replay",
        meta={"replayed": replayed, "skipped": skipped},
    )
    log(f"Replay complete: {replayed} replayed, {skipped} skipped",
        level=LOG_INFO, source="dead-letter-replay")


if __name__ == "__main__":
    main()
