#!/usr/bin/env python3
"""Poll Slack for Nova's bot response after a voice query. Called by nova_voice_daemon.sh.
Prints the response text to stdout if found, nothing if not yet available."""
import sys, json, re, urllib.request

token     = sys.argv[1]
chan      = sys.argv[2]
ts_before = float(sys.argv[3])

url = (f"https://slack.com/api/conversations.history"
       f"?channel={chan}&oldest={int(ts_before)}&limit=10")
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})

try:
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read())
    for msg in reversed(data.get("messages", [])):
        if msg.get("bot_id") and float(msg.get("ts", 0)) > ts_before:
            text = msg.get("text", "")
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"[*_`#]", "", text)
            text = text.strip()
            if text:
                print(text[:500])
            break
except Exception:
    pass
