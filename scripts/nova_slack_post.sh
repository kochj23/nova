#!/bin/bash
# nova_slack_post.sh — Post a message to Slack AND Discord as Nova
# Usage: nova_slack_post.sh "message text" [channel_id]
# Default channel: #nova-chat (C0AMNQ5GX70 / Discord 1496990647062761483)
# Written by Jordan Koch

MESSAGE="${1:?Usage: nova_slack_post.sh \"message\" [channel_id]}"
CHANNEL="${2:-C0ATAF7NZG9}"

python3 - "$MESSAGE" "$CHANNEL" << 'PYEOF'
import sys
sys.path.insert(0, __import__("os").path.expanduser("~/.openclaw/scripts"))
import nova_config

message = sys.argv[1]
channel = sys.argv[2]

nova_config.post_both(message, slack_channel=channel)
print("OK")
PYEOF
