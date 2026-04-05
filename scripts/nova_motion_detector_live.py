#!/usr/bin/env python3
"""
TRACK 1: Motion-triggered clip capture — PRODUCTION LIVE
Monitors camera feeds, detects motion, captures clips, indexes them.
Runs every 30 seconds checking latest frames.
"""

import subprocess
import os
from pathlib import Path
from datetime import datetime, timedelta
import json
import urllib.request
import time
import sys

os.environ["PYTHONPATH"] = "/Volumes/Data/AI/python_packages:" + os.environ.get("PYTHONPATH", "")

WORKSPACE = Path.home() / ".openclaw/workspace"
CLIPS_DIR = Path("/Volumes/Data/motion_clips")
FRAMES_DIR = Path("/Volumes/Data/camera_frames")
MEMORY_URL = "http://127.0.0.1:18790"
RTSP_URL = "rtsp://192.168.1.9:554/Streaming/channels/101"  # Front door camera

def log(msg):
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] {msg}", flush=True)

def remember(text, source="vision"):
    try:
        data = json.dumps({"text": text, "source": source}).encode()
        req = urllib.request.Request(
            f"{MEMORY_URL}/remember",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            return result.get("id")
    except Exception as e:
        log(f"Memory store failed: {e}")
        return None

def get_latest_frame(camera_name="front_door"):
    """Get latest captured frame from storage."""
    frame_path = FRAMES_DIR / f"{camera_name}_latest.jpg"
    if frame_path.exists():
        return str(frame_path)
    return None

def detect_motion_in_frames(frame1_path, frame2_path, threshold=15):
    """
    Detect motion by comparing consecutive frames.
    Returns motion percentage (0-100).
    """
    try:
        import cv2
        import numpy as np
        
        img1 = cv2.imread(frame1_path, cv2.IMREAD_GRAYSCALE)
        img2 = cv2.imread(frame2_path, cv2.IMREAD_GRAYSCALE)
        
        if img1 is None or img2 is None:
            return 0
        
        # Resize to same dimensions if needed
        if img1.shape != img2.shape:
            img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))
        
        # Compute absolute difference
        diff = cv2.absdiff(img1, img2)
        
        # Threshold to binary
        _, thresh = cv2.threshold(diff, 30, 255, cv2.THRESH_BINARY)
        
        # Calculate percentage of image with motion
        motion_pct = (np.sum(thresh) / (thresh.size * 255)) * 100
        
        return motion_pct
    except Exception as e:
        log(f"Motion detection failed: {e}")
        return 0

def capture_clip(camera_rtsp, duration=20, quality="medium"):
    """
    Capture video clip from RTSP stream.
    quality: "low" (720p, ultrafast), "medium" (1080p, fast), "high" (1080p, medium)
    """
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    clip_path = CLIPS_DIR / f"motion_{timestamp}.mp4"
    
    # Preset based on quality
    presets = {
        "low": ("ultrafast", "720"),
        "medium": ("fast", "1080"),
        "high": ("medium", "1080")
    }
    preset, scale = presets.get(quality, ("fast", "1080"))
    
    cmd = [
        "ffmpeg",
        "-rtsp_transport", "tcp",
        "-i", camera_rtsp,
        "-t", str(duration),
        "-vf", f"scale={scale}:-1",
        "-c:v", "libx264",
        "-preset", preset,
        "-crf", "28",  # Quality (lower = better, 0-51)
        "-y",
        str(clip_path)
    ]
    
    try:
        log(f"Capturing clip ({quality}): {clip_path}")
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=duration + 10
        )
        
        if result.returncode == 0 and clip_path.exists():
            size_mb = clip_path.stat().st_size / (1024 * 1024)
            log(f"✓ Clip captured: {clip_path.name} ({size_mb:.1f}MB)")
            
            # Store to memory
            remember(
                f"Motion clip captured: {clip_path.name} ({duration}s, {size_mb:.1f}MB)",
                source="vision"
            )
            
            return str(clip_path)
        else:
            log(f"✗ Capture failed: {result.stderr[:200]}")
            return None
            
    except subprocess.TimeoutExpired:
        log(f"✗ Capture timeout")
        return None
    except Exception as e:
        log(f"✗ Capture error: {e}")
        return None

def cleanup_old_clips(days=7):
    """Remove clips older than N days."""
    if not CLIPS_DIR.exists():
        return 0
    
    cutoff = datetime.now() - timedelta(days=days)
    removed = 0
    
    for clip in CLIPS_DIR.glob("motion_*.mp4"):
        try:
            mtime = datetime.fromtimestamp(clip.stat().st_mtime)
            if mtime < cutoff:
                clip.unlink()
                removed += 1
        except:
            pass
    
    return removed

def get_storage_stats():
    """Check storage usage."""
    try:
        result = subprocess.run(
            ["du", "-sh", str(CLIPS_DIR)],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return result.stdout.split('\t')[0]
    except:
        pass
    return "unknown"

def motion_monitor_loop():
    """
    Main loop: continuously check for motion and capture clips.
    Runs every 30 seconds, compares frames, triggers capture on motion.
    """
    log("Motion monitor starting...")
    log(f"Monitoring RTSP: {RTSP_URL}")
    log(f"Clips stored at: {CLIPS_DIR}")
    
    prev_frame = None
    consecutive_motion = 0
    last_capture_time = datetime.now()
    clip_cooldown = 30  # Don't capture more than once per 30 seconds
    
    iteration = 0
    
    while True:
        try:
            iteration += 1
            
            # Every 5 minutes, cleanup old clips
            if iteration % 10 == 0:
                removed = cleanup_old_clips()
                if removed > 0:
                    storage = get_storage_stats()
                    log(f"Cleaned {removed} old clips. Storage: {storage}")
            
            # Get current frame
            current_frame = get_latest_frame()
            
            if current_frame is None:
                time.sleep(30)
                continue
            
            # Detect motion if we have previous frame
            motion_pct = 0
            if prev_frame:
                motion_pct = detect_motion_in_frames(prev_frame, current_frame, threshold=15)
                
                if motion_pct > 10:  # 10% threshold for motion
                    consecutive_motion += 1
                    
                    # Capture clip on sustained motion (3+ consecutive checks = 90 seconds)
                    if consecutive_motion >= 3:
                        time_since_last = (datetime.now() - last_capture_time).total_seconds()
                        
                        if time_since_last >= clip_cooldown:
                            log(f"🎬 MOTION DETECTED: {motion_pct:.1f}% — capturing clip...")
                            clip_path = capture_clip(RTSP_URL, duration=20, quality="medium")
                            
                            if clip_path:
                                remember(
                                    f"Motion event captured at {datetime.now().isoformat()}",
                                    source="vision"
                                )
                                last_capture_time = datetime.now()
                                consecutive_motion = 0
                        else:
                            log(f"Motion {motion_pct:.1f}% (cooldown {int(clip_cooldown - time_since_last)}s remaining)")
                else:
                    consecutive_motion = 0
            
            prev_frame = current_frame
            
            # Sleep before next check
            time.sleep(30)
            
        except KeyboardInterrupt:
            log("Motion monitor stopped by user")
            break
        except Exception as e:
            log(f"Monitor error: {e}")
            time.sleep(30)
            continue

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "cleanup":
        removed = cleanup_old_clips()
        print(f"Cleaned {removed} clips older than 7 days")
        return
    
    motion_monitor_loop()

if __name__ == "__main__":
    main()
