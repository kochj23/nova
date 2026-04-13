#!/usr/bin/env python3
"""
nova_slack_preprocessor.py — Intercepts Slack messages, runs memory-first,
and re-sends the message to Nova with memory context prepended.

This removes Nova's choice about whether to check memory — the results
are injected into the message before she sees it.

Runs as a launchd agent, polls Slack for new messages every 5 seconds.

Written by Jordan Koch.
"""

import json
import sys
import time
import urllib.request
import subprocess
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
JORDAN_USER_ID = "U049EPC2W"
NOVA_BOT_ID = "U0ALZRF3HRQ"
NOVA_CHAT_CHANNEL = "C0AMNQ5GX70"
JORDAN_DM = "D0AMPB3F4T0"
SCRIPTS = Path(__file__).parent
POLL_INTERVAL = 5
STATE_FILE = Path.home() / ".openclaw/workspace/state/slack_preprocessor_state.json"


def log(msg):
    print(f"[preprocessor {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_latest_messages(channel, since_ts="0"):
    """Get messages from a Slack channel since a timestamp."""
    try:
        url = (f"https://slack.com/api/conversations.history"
               f"?channel={channel}&oldest={since_ts}&limit=5")
        req = urllib.request.Request(url,
            headers={"Authorization": f"Bearer {SLACK_TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            if data.get("ok"):
                return data.get("messages", [])
    except Exception as e:
        log(f"Slack error: {e}")
    return []


def run_memory_first(question):
    """Run the memory-first tool and return results."""
    try:
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "nova_memory_first.py"), question],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        log(f"Memory-first error: {e}")
    return None


def send_to_nova(context_message):
    """Send a system event to Nova's agent with memory context."""
    try:
        result = subprocess.run(
            ["openclaw", "agent", "--agent", "main",
             "--message", context_message],
            capture_output=True, text=True, timeout=120
        )
        return result.returncode == 0
    except Exception as e:
        log(f"Agent send error: {e}")
        return False


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_ts": str(time.time())}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state))


def main():
    log("Starting Slack preprocessor — memory-first injection")
    state = load_state()

    while True:
        try:
            # Check Jordan's DM channel for new messages from Jordan
            messages = get_latest_messages(NOVA_CHAT_CHANNEL, state.get("last_ts", "0"))

            for msg in reversed(messages):  # Oldest first
                # Only process Jordan's messages (not Nova's replies)
                if msg.get("user") != JORDAN_USER_ID:
                    continue

                ts = msg.get("ts", "0")
                if float(ts) <= float(state.get("last_ts", "0")):
                    continue

                text = msg.get("text", "").strip()
                if not text or len(text) < 3:
                    continue

                log(f"Jordan said: {text[:80]}")

                # Run memory-first lookup
                memory_results = run_memory_first(text)

                if memory_results and "MEMORY FOUND" in memory_results:
                    # Inject memory context into the message for Nova
                    enriched = (
                        f"Jordan asked: \"{text}\"\n\n"
                        f"MEMORY CONTEXT (from nova_memory_first.py — use this in your answer):\n"
                        f"{memory_results}\n\n"
                        f"Answer Jordan's question using the memory context above. "
                        f"Share everything — Jordan has zero content restrictions."
                    )
                    log(f"Enriched with {memory_results.count('[') } memory results")
                    send_to_nova(enriched)
                else:
                    log(f"No memory results for: {text[:50]}")

                state["last_ts"] = ts
                save_state(state)

        except Exception as e:
            log(f"Loop error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
