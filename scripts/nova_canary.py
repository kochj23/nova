#!/usr/bin/env python3
"""
nova_canary.py — External heartbeat to ntfy.sh.

Runs every 5 minutes via scheduler. Sends a lightweight ping to a private
ntfy.sh topic. If the pings stop, your phone gets a notification regardless
of whether Slack/Discord/Signal/the gateway are alive.

Install the ntfy app on your phone and subscribe to the topic:
  https://ntfy.sh/  → subscribe to the topic stored in Keychain

Topic is stored in Keychain as nova-canary-topic (account: nova).
Subscribe at: https://ntfy.sh/<topic>

Written by Jordan Koch.
"""

import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


def _get_topic() -> str:
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-canary-topic", "-w"],
        capture_output=True, text=True,
    )
    return result.stdout.strip()


def _quick_status() -> dict:
    """Fast local checks — should complete in <3s."""
    status = {}
    import socket

    for name, port in [("gateway", 18789), ("memory", 18790), ("scheduler", 37460)]:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            status[name] = "up" if s.connect_ex(("127.0.0.1", port)) == 0 else "down"
            s.close()
        except Exception:
            status[name] = "down"

    # Redis ping
    try:
        import redis
        r = redis.from_url("redis://localhost:6379")
        r.ping()
        status["redis"] = "up"
    except Exception:
        status["redis"] = "down"

    # Ollama inference test — actually generate 1 token to verify Metal compute works
    try:
        import json
        data = json.dumps({
            "model": "deepseek-r1:8b",
            "prompt": "1+1=",
            "stream": False,
            "options": {"num_predict": 1, "num_ctx": 128}
        }).encode()
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=12)
        result = json.loads(resp.read())
        status["ollama_inference"] = "up" if result.get("done") else "down"
    except Exception:
        status["ollama_inference"] = "down"

    return status


def main():
    topic = _get_topic()
    if not topic:
        print("[canary] ERROR: nova-canary-topic not in Keychain. Run setup.", file=sys.stderr)
        sys.exit(1)

    status = _quick_status()
    all_up = all(v == "up" for v in status.values())
    ts = datetime.now().strftime("%H:%M")

    if all_up:
        title = f"✓ Nova alive {ts}"
        message = "gateway·memory·scheduler·redis all up"
        priority = "min"  # Silent notification — no sound, just updates the badge
    else:
        down = [k for k, v in status.items() if v == "down"]
        title = f"⚠ Nova degraded {ts}"
        message = f"DOWN: {', '.join(down)}"
        priority = "high"

    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": "robot",
                "Content-Type": "text/plain; charset=utf-8",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        print(f"[canary] Sent: {title} — {message}")
    except Exception as e:
        # Canary failure is not fatal — don't crash the scheduler
        print(f"[canary] WARNING: ntfy.sh unreachable: {e}", file=sys.stderr)
        sys.exit(0)  # Exit 0 so scheduler doesn't mark as failure


if __name__ == "__main__":
    main()
