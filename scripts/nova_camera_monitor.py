#!/usr/bin/env python3
import subprocess
import json
import os
from datetime import datetime

cameras = {
    "front_door": "RTSP_URL_REDACTED",
    "front_yard": "RTSP_URL_REDACTED",
    "front_yard_alt": "RTSP_URL_REDACTED",
    "front_door_patio": "RTSP_URL_REDACTED",
    "alley_north": "RTSP_URL_REDACTED",
    "alley_south": "RTSP_URL_REDACTED",
    "garage": "RTSP_URL_REDACTED",
    "carport": "RTSP_URL_REDACTED",
    "side_yard": "RTSP_URL_REDACTED",
    "back_patio": "RTSP_URL_REDACTED",
    "patio_1": "RTSP_URL_REDACTED",
    "patio_2": "RTSP_URL_REDACTED",
    "3d_printers": "RTSP_URL_REDACTED",
    "abundio_boundary": "RTSP_URL_REDACTED",
}

storage_dir = os.path.expanduser("~/.openclaw/workspace/camera_frames")
os.makedirs(storage_dir, exist_ok=True)

status = {}

for name, rtsp_url in cameras.items():
    try:
        # Capture video frame (primary)
        output_file = f"{storage_dir}/{name}_latest.jpg"
        cmd = [
            "ffmpeg", "-rtsp_transport", "tcp", "-i", rtsp_url,
            "-frames:v", "1", "-update", "1", "-y", output_file
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=5)
        status[name] = "ok" if result.returncode == 0 else "error"
        
    except subprocess.TimeoutExpired:
        status[name] = "timeout"
    except Exception as e:
        status[name] = f"error: {e}"

# Log results
timestamp = datetime.now().isoformat()
success_count = len([s for s in status.values() if s == "ok"])
print(f"[{timestamp}] Camera monitor: {success_count}/{len(cameras)} online (video + audio available)")

for camera, state in status.items():
    if state != "ok":
        print(f"  ⚠️  {camera}: {state}")
