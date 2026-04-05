#!/usr/bin/env python3
"""
PHASE 2: Motion-triggered video clip capture
Detects motion in frames, captures 20-second clips, indexes them.
"""

import subprocess
import os
from pathlib import Path
from datetime import datetime, timedelta
import json
import urllib.request

os.environ["PYTHONPATH"] = "/Volumes/Data/AI/python_packages:" + os.environ.get("PYTHONPATH", "")

WORKSPACE = Path.home() / ".openclaw/workspace"
CLIPS_DIR = Path("/Volumes/Data/motion_clips")
MEMORY_URL = "http://127.0.0.1:18790"

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def remember(text, source="vision"):
    try:
        data = json.dumps({"text": text, "source": source}).encode()
        req = urllib.request.Request(
            f"{MEMORY_URL}/remember",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()).get("id")
    except:
        return None

def detect_motion(frame1, frame2):
    """Simple frame differencing for motion detection."""
    try:
        import cv2
        import numpy as np
        
        img1 = cv2.imread(frame1, cv2.IMREAD_GRAYSCALE)
        img2 = cv2.imread(frame2, cv2.IMREAD_GRAYSCALE)
        
        if img1 is None or img2 is None:
            return 0
        
        diff = cv2.absdiff(img1, img2)
        motion_score = np.mean(diff) / 255.0
        return motion_score
    except:
        return 0

def capture_clip(camera_name, rtsp_url, duration=20):
    """Capture a video clip from RTSP stream."""
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    clip_path = CLIPS_DIR / f"{camera_name}_{timestamp}.mp4"
    
    cmd = [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-t", str(duration),
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-y",
        str(clip_path)
    ]
    
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=duration+5)
        if result.returncode == 0:
            log(f"✓ Captured {camera_name}: {clip_path}")
            return str(clip_path)
    except:
        pass
    
    return None

def cleanup_old_clips(days=7):
    """Remove clips older than N days."""
    if not CLIPS_DIR.exists():
        return
    
    cutoff = datetime.now() - timedelta(days=days)
    removed = 0
    
    for clip in CLIPS_DIR.glob("*.mp4"):
        if datetime.fromtimestamp(clip.stat().st_mtime) < cutoff:
            clip.unlink()
            removed += 1
    
    if removed > 0:
        log(f"Cleaned up {removed} old clips")

def main():
    log("Motion clip monitor starting...")
    
    # For now, just set up infrastructure
    # Real motion detection would integrate into camera monitor
    cleanup_old_clips()
    
    log("Motion clip system ready (infrastructure initialized)")

if __name__ == "__main__":
    main()
