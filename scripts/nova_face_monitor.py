#!/usr/bin/env python3
"""
nova_face_monitor.py — Face recognition integration for camera feeds

Pulls frames from each camera, identifies faces, alerts on unknowns.
First-time unknowns trigger enrollment request.

Cron: every 15 minutes (integrated into existing camera monitor)
"""

import subprocess
import json
import os
import sys
from datetime import datetime
from pathlib import Path
import urllib.request
import urllib.error

CAMERA_MONITOR_SCRIPT = str(Path.home() / ".openclaw/scripts/nova_camera_monitor.py")
WORKSPACE = Path.home() / ".openclaw/workspace"
FACES_DB = WORKSPACE / "faces/people.db"
MEMORY_URL = "http://127.0.0.1:18790"

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def run_command(cmd, timeout=30):
    """Run shell command and return (code, stdout, stderr)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            shell=isinstance(cmd, str)
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", "Timeout"
    except Exception as e:
        return 1, "", str(e)

def identify_faces_in_image(image_path):
    """Run sam-faces on an image, return results."""
    try:
        code, stdout, stderr = run_command(
            f"sam-faces --photo {image_path} --json",
            timeout=15
        )
        
        if code == 0 and stdout:
            return json.loads(stdout)
        return None
    except Exception as e:
        log(f"Error identifying faces: {e}")
        return None

def remember(text, source="vision"):
    """Store finding to vector memory."""
    try:
        data = json.dumps({"text": text, "source": source}).encode()
        req = urllib.request.Request(
            f"{MEMORY_URL}/remember",
            data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()).get("id")
    except Exception:
        return None

def enroll_face(name, image_path):
    """Enroll a new face in the database."""
    try:
        code, stdout, stderr = run_command(
            f'python3 ~/.openclaw/workspace/skills/sam-faces/scripts/enroll_face.py --name "{name}" --photo {image_path}',
            timeout=30
        )
        
        if code == 0:
            log(f"✓ Enrolled {name}")
            return True
        else:
            log(f"Failed to enroll {name}: {stderr}")
            return False
    except Exception as e:
        log(f"Error enrolling face: {e}")
        return False

def process_frame(camera_name, frame_path):
    """Process one camera frame for faces."""
    if not os.path.exists(frame_path):
        return None
    
    # Identify faces
    result = identify_faces_in_image(frame_path)
    
    if not result or result.get("face_count", 0) == 0:
        return None
    
    log(f"{camera_name}: {result['face_count']} face(s) detected")
    
    # Process each face
    for face in result.get("faces", []):
        name = face.get("name", "Unknown")
        confidence = face.get("confidence", 0)
        unknown = face.get("unknown", False)
        position = face.get("position_desc", "unknown position")
        
        if unknown:
            # Unknown face — need human identification
            log(f"  ⚠️  UNKNOWN FACE at {camera_name} ({position}, {confidence:.0%})")
            log(f"     Saved to: {WORKSPACE}/faces/unknown/")
            
            # Alert to Slack
            send_slack_alert(
                f"Unknown face detected at {camera_name}",
                f"Position: {position}\nConfidence: {confidence:.0%}\nCamera frame: {frame_path}"
            )
        else:
            # Known face
            log(f"  ✓ {name} at {camera_name} ({position}, {confidence:.0%})")
            
            # Store in memory
            remember(
                f"{name} spotted at {camera_name} ({position}) on {datetime.now().isoformat()}",
                source="vision"
            )
    
    return result

def send_slack_alert(title, details):
    """Send alert to Slack #nova-chat."""
    # This would integrate with your Slack API
    # For now, just log it
    log(f"ALERT: {title}")
    log(f"  {details}")

def main():
    log("Starting face monitor...")
    
    # List camera frames from last run
    frames_dir = WORKSPACE / "camera_frames"
    if not frames_dir.exists():
        log("No camera frames directory found")
        return
    
    # Process most recent frame from each camera
    cameras = {
        "front_door": "front_door_*.jpg",
        "front_yard": "front_yard_*.jpg",
        "alley_north": "alley_north_*.jpg",
        "garage": "garage_*.jpg",
        # Add more as needed
    }
    
    processed = 0
    for camera_name, pattern in cameras.items():
        frames = sorted(frames_dir.glob(pattern), reverse=True)
        
        if frames:
            latest_frame = frames[0]
            result = process_frame(camera_name, str(latest_frame))
            if result:
                processed += 1
    
    log(f"Face monitor complete ({processed} cameras with detections)")

if __name__ == "__main__":
    main()
