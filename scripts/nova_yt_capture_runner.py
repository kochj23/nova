#!/usr/bin/env python3
"""nova_yt_capture_runner.py — drains the fishbowl capture queue.

Runs each capture as a SYNCHRONOUS child of this process, which runs under launchd, so
it inherits Full Disk Access — mlx_whisper can read the model on /Volumes/Data (a
detached my-shell capture can't, which was silently producing transcript=n). Sequential
(one mlx_whisper at a time — GPU-safe). Single-instance via a PG advisory lock so
overlapping launchd fires don't double-run.

Queue = nova_ops.yt_ingest_seen rows with status 'queued' (VOD) or 'queued_live' (live).
"""
import subprocess
import sys
from pathlib import Path

import psycopg2

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
PY = "/opt/homebrew/bin/python3"
CAP = str(Path(__file__).parent / "nova_yt_capture.py")
LOCK = 778901234  # advisory lock id (single-instance)

sys.path.insert(0, str(Path(__file__).parent))
from nova_yt_ingest_watch import CHANNELS  # noqa: E402  (main() is guarded; import is side-effect free)
VECTORS = {c["key"]: c["vector"] for c in CHANNELS}


def log(m):
    print(f"[capture-runner] {m}", flush=True)


def main():
    c = psycopg2.connect(DSN); c.autocommit = True; cur = c.cursor()
    cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK,))
    if not cur.fetchone()[0]:
        log("another instance holds the lock — exiting"); return
    processed = 0
    while True:
        cur.execute("SELECT video_id, status, title, channel FROM yt_ingest_seen "
                    "WHERE status IN ('queued','queued_live') ORDER BY seen_at LIMIT 1")
        row = cur.fetchone()
        if not row:
            break
        vid, status, title, chan = row
        vector = VECTORS.get(chan, "fishbowl")   # watch-news channels -> horology (2026-10-06)
        live = (status == "queued_live")
        cur.execute("UPDATE yt_ingest_seen SET status=%s WHERE video_id=%s",
                    ("recording" if live else "capturing", vid))
        log(f"capturing {vid} ({'live' if live else 'vod'}) {(title or '')[:50]}")
        args = [PY, CAP, vid, vector] + (["--live"] if live else [])
        try:
            subprocess.run(args, timeout=12 * 3600)
        except Exception as e:
            log(f"{vid} failed: {e}")
            cur.execute("UPDATE yt_ingest_seen SET status='failed' WHERE video_id=%s "
                        "AND status IN ('capturing','recording')", (vid,))
        processed += 1
    if processed:
        log(f"done — {processed} captured")
    cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK,))
    c.close()


if __name__ == "__main__":
    main()
