#!/bin/bash
# nova_slack_post.sh — Post a message to Slack as Nova
# Usage: nova_slack_post.sh "message text" [channel_id]
# Default channel: #nova-chat (C0AMNQ5GX70)
# Written by Jordan Koch

MESSAGE="${1:?Usage: nova_slack_post.sh \"message\" [channel_id]}"
CHANNEL="${2:-C0AMNQ5GX70}"

python3 - "$MESSAGE" "$CHANNEL" << 'PYEOF'
import json, urllib.request, sys
sys.path.insert(0, sys.path[0] or ".")
sys.path.insert(0, __import__("os").path.expanduser("~/.openclaw/scripts"))
import nova_config

token = nova_config.slack_bot_token()
if not token:
    print("ERROR: No Slack token", file=sys.stderr)
    sys.exit(1)

message = sys.argv[1]
channel = sys.argv[2]

data = json.dumps({"channel": channel, "text": message, "mrkdwn": True}).encode()
req = urllib.request.Request(
    "https://slack.com/api/chat.postMessage",
    data=data,
    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
)
with urllib.request.urlopen(req, timeout=10) as r:
    resp = json.loads(r.read())
    if resp.get("ok"):
        print("OK")
    else:
        print(f"ERROR: {resp.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)
PYEOF
