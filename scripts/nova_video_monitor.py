#!/usr/bin/env python3
"""
nova_video_monitor.py — Slack status reporter for video batch transcription.

Posts to Slack every 10 minutes with progress. Auto-stops when the
batch process finishes. Runs under nohup, survives session timeout.

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_CHAN
LOG_FILE = Path("/Volumes/Data/nova-video-batch.log")
INTERVAL = 600  # 10 minutes


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


def get_progress():
    if not LOG_FILE.exists():
        return None

    content = LOG_FILE.read_text(encoding="utf-8", errors="ignore")
    lines = content.splitlines()

    processed = 0
    total_chars = 0
    last_file = ""
    errors = 0
    done = False

    for line in lines:
        if "Processed video" in line:
            processed += 1
            # Extract char count
            if "char transcript" in line:
                try:
                    parts = line.split()
                    for i, p in enumerate(parts):
                        if p == "char":
                            total_chars += int(parts[i - 1])
                            break
                except Exception:
                    pass
        if "Processing:" in line:
            last_file = line.split("Processing:")[-1].strip()
        if "Error" in line or "error" in line:
            errors += 1
        if "Found" in line and "video(s)" in line:
            pass

    # Check if batch process is still running
    result = subprocess.run(["pgrep", "-f", "nova_video_ingest.*yt"],
                           capture_output=True, text=True)
    still_running = result.returncode == 0

    return {
        "processed": processed,
        "total_chars": total_chars,
        "last_file": last_file[:60],
        "errors": errors,
        "running": still_running,
    }


def main():
    print(f"[video_monitor] Starting — Slack reports every {INTERVAL}s", flush=True)
    start = time.time()

    while True:
        time.sleep(INTERVAL)

        progress = get_progress()
        if not progress:
            continue

        elapsed = time.time() - start
        elapsed_str = str(timedelta(seconds=int(elapsed)))

        msg = (
            f"*Video Transcription Progress*\n"
            f"  Processed: *{progress['processed']}* videos\n"
            f"  Transcript data: {progress['total_chars']:,} characters\n"
            f"  Current: _{progress['last_file']}_\n"
            f"  Errors: {progress['errors']}\n"
            f"  Elapsed: {elapsed_str}\n"
            f"  Status: {'running' if progress['running'] else 'FINISHED'}"
        )
        slack_post(msg)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {progress['processed']} videos, "
              f"{progress['total_chars']:,} chars, "
              f"{'running' if progress['running'] else 'done'}", flush=True)

        if not progress["running"]:
            slack_post(
                f"*Video Batch Transcription Complete*\n"
                f"  Total: {progress['processed']} videos transcribed\n"
                f"  Transcript data: {progress['total_chars']:,} characters\n"
                f"  Duration: {elapsed_str}\n"
                f"  _All stored in Nova's memory (source: video)_"
            )
            print("[video_monitor] Batch finished. Exiting.", flush=True)
            break


if __name__ == "__main__":
    main()
