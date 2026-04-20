#!/usr/bin/env python3
"""
nova_slack_image.py — Download a Slack file and analyze it with qwen3-vl:4b.

Usage:
  python3 nova_slack_image.py <file_id_or_url> [prompt]

Downloads the Slack file attachment, sends it to Ollama's qwen3-vl:4b
for vision analysis, and prints the description.

Written by Jordan Koch.
"""

import base64
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
VISION_MODEL = "qwen/qwen3.5-9b"
USE_OPENROUTER = True


def get_slack_token():
    r = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-slack-bot-token", "-w"],
        capture_output=True, text=True, timeout=10
    )
    return r.stdout.strip()


def download_slack_file(file_id_or_url, token, channel="C0AMNQ5GX70"):
    file_id_or_url = file_id_or_url.strip()

    # If it's already a URL, download directly
    if file_id_or_url.startswith("http"):
        dl_req = urllib.request.Request(file_id_or_url, headers={"Authorization": f"Bearer {token}"})
        dl_resp = urllib.request.urlopen(dl_req, timeout=30)
        return dl_resp.read(), "image/jpeg", "slack_image"

    # Search recent channel history for the file
    search_id = file_id_or_url
    req = urllib.request.Request(
        f"https://slack.com/api/conversations.history?channel={channel}&limit=20",
        headers={"Authorization": f"Bearer {token}"}
    )
    resp = urllib.request.urlopen(req, timeout=15)
    data = json.loads(resp.read())

    for msg in data.get("messages", []):
        for f in msg.get("files", []):
            fid = f.get("id", "")
            fname = f.get("name", "")
            if search_id in fid or search_id in fname:
                url = f.get("url_private_download") or f.get("url_private")
                if url:
                    dl_req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
                    dl_resp = urllib.request.urlopen(dl_req, timeout=30)
                    return dl_resp.read(), f.get("mimetype", "image/jpeg"), fname

    raise RuntimeError(f"File {search_id} not found in recent {channel} history")


def get_openrouter_key():
    r = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-openrouter-api-key", "-w"],
        capture_output=True, text=True, timeout=10
    )
    return r.stdout.strip()


def analyze_image(image_bytes, prompt="Describe what you see in this image in detail."):
    b64 = base64.b64encode(image_bytes).decode()

    if USE_OPENROUTER:
        api_key = get_openrouter_key()
        payload = json.dumps({
            "model": VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }
            ],
            "max_tokens": 1024,
            "temperature": 0.3,
        }).encode()
        req = urllib.request.Request(
            OPENROUTER_URL, data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
        )
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        return data["choices"][0]["message"]["content"].strip()
    else:
        payload = json.dumps({
            "model": VISION_MODEL,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
            "options": {"temperature": 0.3, "num_predict": 1024},
        }).encode()
        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        return data.get("response", "").strip()


def main():
    if len(sys.argv) < 2:
        print("Usage: nova_slack_image.py <file_id> [prompt]")
        sys.exit(1)

    file_id = sys.argv[1]
    prompt = " ".join(sys.argv[2:]) if len(sys.argv) > 2 else "Describe what you see in this image in detail. Note any people, animals, vehicles, text, or other notable features."

    token = get_slack_token()
    if not token:
        print("ERROR: No Slack token in Keychain")
        sys.exit(1)

    print(f"Downloading Slack file {file_id}...", file=sys.stderr)
    image_bytes, mimetype, name = download_slack_file(file_id, token)
    print(f"Downloaded: {name} ({mimetype}, {len(image_bytes)} bytes)", file=sys.stderr)

    print(f"Analyzing with {VISION_MODEL}...", file=sys.stderr)
    description = analyze_image(image_bytes, prompt)
    print(description)


if __name__ == "__main__":
    main()
