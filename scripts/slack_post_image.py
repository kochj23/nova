#!/usr/bin/env python3
"""
slack_post_image.py — Post an image file to a Slack channel.

Usage: python3 slack_post_image.py <image_path> [channel_id] [caption]

Defaults:
  channel_id = C0AMNQ5GX70  (#nova-chat)
  caption    = (empty)

Author: Jordan Koch
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path
import nova_config

SLACK_TOKEN   = nova_config.slack_bot_token()
SLACK_API     = "https://slack.com/api"
DEFAULT_CHAN  = "C0AMNQ5GX70"


def slack_post(endpoint, payload):
    data = json.dumps(payload).encode()
    req  = urllib.request.Request(
        f"{SLACK_API}/{endpoint}",
        data=data,
        headers={
            "Authorization": "Bearer " + SLACK_TOKEN,
            "Content-Type":  "application/json; charset=utf-8",
        }
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def upload_image(image_path, channel, caption=""):
    path = Path(image_path)
    if not path.exists():
        print(f"ERROR: File not found: {image_path}", file=sys.stderr)
        sys.exit(1)

    file_size = path.stat().st_size

    # Step 1: Get upload URL
    req = urllib.request.Request(
        SLACK_API + "/files.getUploadURLExternal",
        data=urllib.parse.urlencode({
            "filename": path.name,
            "length":   str(file_size),
        }).encode(),
        headers={"Authorization": "Bearer " + SLACK_TOKEN}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        url_data = json.loads(resp.read())

    if not url_data.get("ok"):
        print(f"ERROR: getUploadURLExternal: {url_data.get('error')}", file=sys.stderr)
        sys.exit(1)

    upload_url = url_data["upload_url"]
    file_id    = url_data["file_id"]

    # Step 2: PUT the file bytes
    with open(path, "rb") as f:
        file_bytes = f.read()

    req = urllib.request.Request(
        upload_url,
        data=file_bytes,
        method="POST",
        headers={"Content-Type": "application/octet-stream"}
    )
    with urllib.request.urlopen(req, timeout=60):
        pass

    # Step 3: Complete — share to channel
    payload = {
        "files":   [{"id": file_id, "title": path.stem}],
        "channel_id": channel,
    }
    if caption:
        payload["initial_comment"] = caption

    result = slack_post("files.completeUploadExternal", payload)
    if result.get("ok"):
        print(f"Image posted to {channel}: {path.name}")
    else:
        print(f"ERROR: completeUploadExternal: {result.get('error')}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: slack_post_image.py <image_path> [channel_id] [caption]")
        sys.exit(1)

    image_path = sys.argv[1]
    channel    = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_CHAN
    caption    = sys.argv[3] if len(sys.argv) > 3 else ""

    upload_image(image_path, channel, caption)
