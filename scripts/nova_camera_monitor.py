#!/usr/bin/env python3
"""
nova_camera_monitor.py — Capture frames from all RTSP cameras.

Camera URLs are loaded from camera_config.py (GITIGNORED).
Uses absolute ffmpeg path for launchd compatibility.

Written by Jordan Koch.
"""
import subprocess
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

try:
    from camera_config import CAMERAS as cameras
except ImportError:
    print("ERROR: camera_config.py not found. Create it with CAMERAS dict.", flush=True)
    sys.exit(1)

FFMPEG = "/opt/homebrew/bin/ffmpeg"
storage_dir = os.path.expanduser("~/.openclaw/workspace/camera_frames")
os.makedirs(storage_dir, exist_ok=True)

status = {}

for name, rtsp_url in cameras.items():
    try:
        output_file = f"{storage_dir}/{name}_latest.jpg"
        cmd = [
            FFMPEG, "-rtsp_transport", "tcp", "-i", rtsp_url,
            "-frames:v", "1", "-update", "1", "-y", output_file
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        status[name] = "ok" if result.returncode == 0 else "error"
    except subprocess.TimeoutExpired:
        status[name] = "timeout"
    except Exception as e:
        status[name] = f"error: {e}"

timestamp = datetime.now().isoformat()
success_count = len([s for s in status.values() if s == "ok"])
print(f"[{timestamp}] Camera monitor: {success_count}/{len(cameras)} online")

for camera, state in status.items():
    if state != "ok":
        print(f"  {camera}: {state}")
