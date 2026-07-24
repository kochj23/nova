#!/usr/bin/env python3
"""Memory-pipeline health monitor.

Alerts #nova-critical when the ingest worker STALLS — queue backed up but nothing being
written — which is exactly the failure that ran silently for ~14h on 2026-07-07 (embed node
went away, queue grew to 1.19M, zero alerts). Also logs queue depth + write-rate to
telemetry.memory_pipeline for a Grafana panel.

Detection is delta-based (does the server's total written count grow between runs?), so it
does NOT false-alarm while a large backlog drains healthily (created_at can't be used — it's
the queue-time, and a FIFO drain writes old-timestamped items first).

Run on a timer (every ~15m) via the scheduler.
"""
import json
import os
import time
import urllib.request

import nova_config

HEALTH_URL = "http://127.0.0.1:18790/health"
STATE = os.path.expanduser("~/.openclaw/state/memory_health.json")
DSN_OPS = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

STALL_QUEUE = 5000       # queued items above which "0 written" is a real stall
STALL_MINUTES = 10       # min elapsed before we trust a 0-written reading
BACKLOG_WARN = 300_000   # sustained backlog worth a (non-critical) heads-up


def _log_telemetry(queue, count, written, mins):
    try:
        import psycopg2
        con = psycopg2.connect(DSN_OPS)
        with con, con.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS telemetry_memory_pipeline (
                ts timestamptz DEFAULT now(), queue_length bigint, total_written bigint,
                written_since_last bigint, mins_since_last double precision)""")
            cur.execute("INSERT INTO telemetry_memory_pipeline "
                        "(queue_length,total_written,written_since_last,mins_since_last) "
                        "VALUES (%s,%s,%s,%s)", (queue, count, written, mins))
        con.close()
    except Exception as e:
        print("telemetry log failed:", e)


def main():
    try:
        h = json.loads(urllib.request.urlopen(HEALTH_URL, timeout=10).read())
    except Exception as e:
        nova_config.post_both(f":rotating_light: *Memory server unreachable* — {e} "
                              "(nova-memory-server on .6:18790)", slack_channel=nova_config.SLACK_BB)
        print("health unreachable:", e)
        return

    q = int(h.get("queue_length", 0))
    count = int(h.get("count", 0))
    now = time.time()

    prev = {}
    try:
        prev = json.load(open(STATE))
    except Exception:
        pass
    written = count - prev.get("count", count)
    mins = (now - prev.get("ts", now)) / 60.0

    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    json.dump({"count": count, "ts": now, "queue": q}, open(STATE, "w"))
    _log_telemetry(q, count, written, mins)

    # STALL: queue is backed up but the worker wrote nothing since last check
    if prev and mins >= STALL_MINUTES and written <= 0 and q > STALL_QUEUE:
        nova_config.post_both(
            f":rotating_light: *Memory ingest STALLED* — {q:,} items queued, *0 written* in "
            f"{mins:.0f} min. Embed worker is down. Check: core3 Ollama (.5:11434, "
            f"`ollama ps` shows nomic-embed-text) and `nova-memory-server` on .6.",
            slack_channel=nova_config.SLACK_BB)
        print(f"ALERT: stalled — queue={q:,} written=0 in {mins:.0f}m")
        return

    prev_q = prev.get("queue", q)
    if q > BACKLOG_WARN and q > prev_q + 5000:   # only nag when the backlog is GROWING (intake > drain), not while draining
        nova_config.post_both(
            f":warning: *Memory queue GROWING* — {q:,} pending, up {q - prev_q:,} since last check "
            f"(intake outrunning drain). Total written: {count:,}.", slack_channel=nova_config.SLACK_NOTIFY)

    print(f"OK — queue={q:,} total={count:,} written_since_last={written:,} elapsed={mins:.0f}m")


if __name__ == "__main__":
    main()
