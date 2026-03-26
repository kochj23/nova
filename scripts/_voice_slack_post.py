#!/usr/bin/env python3
"""Post a voice utterance to Slack as Jordan. Called by nova_voice_daemon.sh."""
import sys, json, urllib.request

text  = sys.argv[1]
token = sys.argv[2]
chan  = sys.argv[3]

data = json.dumps({
    "channel": chan,
    "text": f"🎙️ [voice] {text}",
    "mrkdwn": False
}).encode()

req = urllib.request.Request(
    "https://slack.com/api/chat.postMessage", data=data,
    headers={"Authorization": f"Bearer {token}",
             "Content-Type": "application/json; charset=utf-8"}
)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        result = json.loads(r.read())
        if not result.get("ok"):
            print(f"Slack error: {result.get('error')}", file=sys.stderr)
except Exception as e:
    print(f"Post failed: {e}", file=sys.stderr)
