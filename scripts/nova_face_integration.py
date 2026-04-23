#!/usr/bin/env python3
"""
nova_face_integration.py — Face recognition + auto-enrollment

Runs on camera frames. When an unknown face is detected:
  1. Saves face crop to unknown/ folder
  2. Posts to Slack asking "Who is this?"
  3. On confirmation, enrolls that person

Cron: integrated into nova_camera_monitor every 15 minutes
"""

import subprocess
import json
import os
import sys
from datetime import datetime
from pathlib import Path
import urllib.request
import urllib.error

SAM_FACES_DIR = "/Volumes/Data/Nova/skills/sam-faces/sam_faces"

WORKSPACE = Path.home() / ".openclaw/workspace"
FACES_DIR = WORKSPACE / "faces"
UNKNOWN_DIR = FACES_DIR / "unknown"
CAMERA_FRAMES = WORKSPACE / "camera_frames"
MEMORY_URL = "http://127.0.0.1:18790"
SLACK_API = "https://slack.com/api"

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def remember(text, source="vision"):
    """Store to vector memory."""
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

def run_command(cmd, timeout=30):
    """Run shell command."""
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

def identify_faces(image_path):
    """Run sam-faces on image."""
    code, stdout, stderr = run_command(
        f"python3 {SAM_FACES_DIR}/identify_faces.py --photo {image_path} --no-save-unknowns",
        timeout=30
    )

    if code == 0:
        try:
            lines = stdout.strip().split("\n")
            json_start = next(i for i, l in enumerate(lines) if l.startswith("{"))
            return json.loads("\n".join(lines[json_start:]))
        except Exception:
            return None
    return None

def enroll_person(name, image_path):
    """Enroll a person in the face database."""
    cmd = f'python3 {SAM_FACES_DIR}/enroll_face.py --name "{name}" --photo {image_path} --face-index 0'
    code, stdout, stderr = run_command(cmd, timeout=30)
    
    if code == 0:
        log(f"✓ Enrolled {name} from {Path(image_path).name}")
        remember(f"Enrolled {name} in face recognition system", source="vision")
        return True
    else:
        log(f"✗ Failed to enroll {name}: {stderr}")
        return False

def process_camera_frame(camera_name, frame_path):
    """Analyze one camera frame for faces."""
    if not os.path.exists(frame_path):
        return []
    
    result = identify_faces(frame_path)
    
    if not result or result.get("face_count", 0) == 0:
        return []
    
    events = []
    
    for face in result.get("faces", []):
        name = face.get("name", "Unknown")
        confidence = face.get("confidence", 0)
        unknown = face.get("unknown", False)
        position = face.get("position_desc", "unknown position")
        
        event = {
            "camera": camera_name,
            "name": name,
            "confidence": confidence,
            "position": position,
            "unknown": unknown,
            "timestamp": datetime.now().isoformat(),
            "frame": frame_path
        }
        
        if unknown:
            # Unknown face detected
            log(f"⚠️  UNKNOWN at {camera_name}: {position} ({confidence:.0%})")
            event["status"] = "unknown_detected"
            
            # Store in memory
            remember(
                f"Unknown face detected at {camera_name} ({position}) - {confidence:.0%} confidence. Awaiting identification.",
                source="vision"
            )
        else:
            # Known face
            log(f"✓ {name} at {camera_name} ({position}, {confidence:.0%})")
            event["status"] = "known"
            
            # Store in memory
            remember(
                f"{name} spotted at {camera_name} ({position}) at {datetime.now().isoformat()}",
                source="vision"
            )
        
        events.append(event)
    
    return events

def main():
    """Process camera frames for faces."""
    log("Starting face integration monitor...")
    
    # Ensure directories exist
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    UNKNOWN_DIR.mkdir(parents=True, exist_ok=True)
    
    if not CAMERA_FRAMES.exists():
        log("No camera frames directory")
        return
    
    # Process latest frame from each camera
    cameras = [
        "front_door",
        "front_yard",
        "back_patio",
        "garage",
        "alley_north",
        "alley_south",
        "carport",
        "side_yard",
    ]
    
    all_events = []
    
    for camera in cameras:
        # Find latest frame for this camera
        pattern = f"{camera}_latest.jpg"
        frame_files = list(CAMERA_FRAMES.glob(pattern))
        
        if not frame_files:
            continue
        
        frame_path = frame_files[0]
        events = process_camera_frame(camera, str(frame_path))
        all_events.extend(events)
    
    # Summary
    known_count = len([e for e in all_events if not e.get("unknown")])
    unknown_count = len([e for e in all_events if e.get("unknown")])
    
    if all_events:
        log(f"Face monitor complete: {known_count} known, {unknown_count} unknown")
    else:
        log("No faces detected in camera frames")

if __name__ == "__main__":
    main()
