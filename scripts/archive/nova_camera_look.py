#!/usr/bin/env python3
"""
nova_camera_look.py — Take a snapshot from a specific camera and describe it.

Nova can exec this to answer "can you see me?" or "what's happening outside?"
Takes a snapshot, runs it through qwen3-vl for description, returns text.

STRICT: Only non-Interior cameras.

Usage:
  python3 nova_camera_look.py "patio"           # fuzzy match camera name
  python3 nova_camera_look.py "front door"       # fuzzy match
  python3 nova_camera_look.py --list              # list all accessible cameras
  python3 nova_camera_look.py --all-exterior      # snapshot + describe all exterior

Written by Jordan Koch.
"""

import base64
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from nova_protect_monitor import ProtectClient

INTERIOR_PREFIX = "Interior"
OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
VISION_MODEL = "qwen3-vl:4b"
SNAPSHOT_DIR = Path.home() / ".openclaw/workspace/protect_snapshots"


def get_cameras(client):
    """Get all non-Interior cameras."""
    bootstrap = client.get_bootstrap()
    if not bootstrap:
        return []
    return [c for c in bootstrap.get("cameras", [])
            if not c.get("name", "").startswith(INTERIOR_PREFIX)
            and c.get("state") == "CONNECTED"]


def fuzzy_match(query, cameras):
    """Find camera matching a fuzzy query."""
    query_lower = query.lower()
    # Exact substring match first
    for cam in cameras:
        if query_lower in cam.get("name", "").lower():
            return cam
    # Word match
    for cam in cameras:
        name_words = cam.get("name", "").lower().split()
        if any(query_lower in w for w in name_words):
            return cam
    return None


def take_snapshot(client, camera_id, camera_name):
    """Take a snapshot and return the file path."""
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SNAPSHOT_DIR / f"look_{camera_id[:8]}.jpg"
    if client.get_snapshot(camera_id, str(path)):
        return str(path)
    return None


def describe_image(image_path, camera_name):
    """Use qwen3-vl to describe what's in the image."""
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        payload = json.dumps({
            "model": VISION_MODEL,
            "prompt": f"This is a security camera image from '{camera_name}'. "
                      f"Describe what you see: people, animals, vehicles, activity, "
                      f"weather/lighting conditions. Be specific and concise.",
            "images": [img_b64],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 300},
        }).encode()

        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=120)
        data = json.loads(resp.read())
        return data.get("response", "").strip()
    except Exception as e:
        return f"(Vision analysis failed: {e})"


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 nova_camera_look.py \"camera name\" or --list")
        sys.exit(1)

    client = ProtectClient()
    if not client.login():
        print("ERROR: Cannot connect to UniFi Protect")
        sys.exit(1)

    cameras = get_cameras(client)

    if sys.argv[1] == "--list":
        print(f"Accessible cameras ({len(cameras)}):")
        for cam in sorted(cameras, key=lambda c: c.get("name", "")):
            print(f"  {cam['name']}")
        return

    if sys.argv[1] == "--all-exterior":
        for cam in sorted(cameras, key=lambda c: c.get("name", "")):
            name = cam["name"]
            path = take_snapshot(client, cam["id"], name)
            if path:
                desc = describe_image(path, name)
                print(f"\n{name}:")
                print(f"  {desc}")
                os.unlink(path)
            else:
                print(f"\n{name}: snapshot failed")
        return

    # Fuzzy match camera name
    query = " ".join(sys.argv[1:])
    cam = fuzzy_match(query, cameras)
    if not cam:
        print(f"No camera matching '{query}'. Available:")
        for c in sorted(cameras, key=lambda c: c.get("name", "")):
            print(f"  {c['name']}")
        sys.exit(1)

    name = cam["name"]
    print(f"Looking at: {name}")

    path = take_snapshot(client, cam["id"], name)
    if not path:
        print(f"ERROR: Could not take snapshot from {name}")
        sys.exit(1)

    desc = describe_image(path, name)
    print(f"\n{desc}")

    # Clean up
    try:
        os.unlink(path)
    except Exception:
        pass


if __name__ == "__main__":
    main()
