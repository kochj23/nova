#!/usr/bin/env python3
"""
nova_discord_mirror.py — Mirror Slack messages to Discord.

Monitors Slack channels and reposts content to matching Discord channels.
Runs as a lightweight daemon alongside the gateway.

Channel mapping:
  Slack C0AMNQ5GX70 (#nova-chat)          -> Discord 1496990647062761483
  Slack C0ATAF7NZG9 (#nova-notifications)  -> Discord 1496990332250886246

Usage:
  python3 nova_discord_mirror.py              # Run once: mirror recent unmirrored
  python3 nova_discord_mirror.py --daemon     # Run continuously (poll every 60s)

Written by Jordan Koch.
"""

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.expanduser("~/.openclaw/scripts"))
import nova_config

STATE_FILE = os.path.expanduser("~/.openclaw/cache/discord_mirror_state.json")

CHANNEL_MAP = {
    nova_config.SLACK_CHAN: nova_config.DISCORD_CHAT,
    nova_config.SLACK_NOTIFY: nova_config.DISCORD_NOTIFY,
}


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def get_slack_history(channel_id, oldest="0", limit=20):
    token = nova_config.slack_bot_token()
    if not token:
        return []
    url = f"{nova_config.SLACK_API}/conversations.history?channel={channel_id}&oldest={oldest}&limit={limit}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
            if data.get("ok"):
                return data.get("messages", [])
    except Exception as e:
        print(f"[mirror] Slack history failed for {channel_id}: {e}", file=sys.stderr)
    return []


def post_to_discord(channel_id, text):
    if len(text) > 2000:
        text = text[:1997] + "..."
    return nova_config.post_discord(text, channel_id)


def mirror_once():
    state = load_state()
    total = 0
    for slack_ch, discord_ch in CHANNEL_MAP.items():
        last_ts = state.get(slack_ch, "0")
        messages = get_slack_history(slack_ch, oldest=last_ts)
        if not messages:
            continue
        messages.sort(key=lambda m: float(m.get("ts", "0")))
        # Only mirror bot messages (Nova's own posts), not human messages
        for msg in messages:
            if msg.get("ts", "0") == last_ts:
                continue
            if msg.get("bot_id") or msg.get("subtype") == "bot_message":
                text = msg.get("text", "")
                if text:
                    post_to_discord(discord_ch, text)
                    total += 1
            state[slack_ch] = msg.get("ts", last_ts)
    save_state(state)
    return total


def main():
    daemon = "--daemon" in sys.argv
    if daemon:
        print("[mirror] Starting Discord mirror daemon (poll every 60s)")
        while True:
            try:
                n = mirror_once()
                if n > 0:
                    print(f"[mirror] Mirrored {n} message(s)")
            except Exception as e:
                print(f"[mirror] Error: {e}", file=sys.stderr)
            time.sleep(60)
    else:
        n = mirror_once()
        print(f"Mirrored {n} message(s)")


if __name__ == "__main__":
    main()
