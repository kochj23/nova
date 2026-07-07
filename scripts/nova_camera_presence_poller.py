#!/opt/homebrew/bin/python3
"""
nova_camera_presence_poller.py — Continuous camera-based presence detection.

Reads latest frames from interior cameras (already captured by nova_camera_monitor.py)
and runs YOLOv8-nano person detection. Writes presence confidence to telemetry.presence.

Privacy: never stores images, never alerts, only writes room occupancy confidence.
Only processes interior cameras explicitly opted in via CAMERA_ROOMS map.

Written by Jordan Koch.
"""

import json
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import psycopg2
import psycopg2.extras

VERSION = "1.0.0"
DB_DSN = "host=localhost dbname=nova_ops user=kochj"
FRAME_DIR = Path.home() / ".openclaw/workspace/camera_frames"
LOG_FILE = Path.home() / ".openclaw/logs/nova_camera_presence.log"
POLL_INTERVAL = 60

# Interior cameras -> room mapping (only these are processed)
CAMERA_ROOMS = {
    "interior_front_door_latest.jpg": "hall",
    "interior_kitchen_alley_latest.jpg": "kitchen",
    "interior_living_room_latest.jpg": "living_room",
    "interior_living_room_b_latest.jpg": "living_room",
    "interior_lr_front_latest.jpg": "living_room",
}

PERSON_CLASS_ID = 0  # COCO class 0 = person
CONFIDENCE_THRESHOLD = 0.4

_shutdown = False
_model = None
_start_time = time.time()
_prev_state = {}  # room -> bool
_conn = None

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[cam_presence {ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def get_model():
    global _model
    if _model is None:
        from ultralytics import YOLO
        _model = YOLO("/Volumes/nas/nova/Nova/models/yolov8n.pt")
        log("YOLOv8-nano model loaded")
    return _model


def get_db():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(DB_DSN)
        _conn.autocommit = True
    return _conn


def detect_persons(image_path):
    """Run YOLOv8-nano on an image, return number of persons detected and max confidence."""
    model = get_model()
    results = model(str(image_path), verbose=False, conf=CONFIDENCE_THRESHOLD, classes=[PERSON_CLASS_ID])

    persons = []
    for r in results:
        for box in r.boxes:
            if int(box.cls[0]) == PERSON_CLASS_ID:
                persons.append(float(box.conf[0]))

    return len(persons), max(persons) if persons else 0.0


def poll_cameras():
    """Check all interior cameras for person presence."""
    global _prev_state

    room_detections = {}  # room -> {count, confidence}

    for filename, room in CAMERA_ROOMS.items():
        frame_path = FRAME_DIR / filename
        if not frame_path.exists():
            continue

        # Skip stale frames (>15 min old — camera_monitor captures every ~10min)
        age = time.time() - frame_path.stat().st_mtime
        if age > 900:
            continue

        try:
            count, confidence = detect_persons(frame_path)
            if room not in room_detections or confidence > room_detections[room]["confidence"]:
                room_detections[room] = {"count": count, "confidence": confidence}
        except Exception as e:
            log(f"Detection error on {filename}: {e}", "ERROR")

    # Write results
    conn = get_db()
    for room, data in room_detections.items():
        person_detected = data["count"] > 0
        prev_detected = _prev_state.get(room, None)

        state_changed = (person_detected != prev_detected)
        # Always write on state change; heartbeat every 5 minutes
        should_write = state_changed or (time.time() - _prev_state.get(f"{room}_ts", 0)) > 300

        if should_write:
            _prev_state[room] = person_detected
            _prev_state[f"{room}_ts"] = time.time()

            confidence = min(data["confidence"] * 1.1, 0.95) if person_detected else 0.0

            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata)
                    VALUES (NOW(), 'camera_detected', %s, %s, 'camera_vision', %s)
                """, (room, confidence, json.dumps({
                    "persons_count": data["count"],
                    "raw_confidence": data["confidence"],
                })))

            if state_changed:
                event = f"Person {'detected' if person_detected else 'no longer visible'} in {room}"
                log(event)
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO shared_observations (observer, category, subject, observation, severity)
                        VALUES ('camera_presence', 'presence', %s, %s, 'info')
                    """, (f"room_{room}", event))


def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log("Shutdown signal received")


def main():
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log(f"Nova Camera Presence Poller v{VERSION} starting...")
    log(f"Monitoring {len(CAMERA_ROOMS)} cameras across {len(set(CAMERA_ROOMS.values()))} rooms")
    log(f"Frame directory: {FRAME_DIR}")
    log(f"Poll interval: {POLL_INTERVAL}s")

    # Pre-load model
    get_model()

    while not _shutdown:
        try:
            poll_cameras()
        except Exception as e:
            log(f"Poll error: {e}", "ERROR")
        time.sleep(POLL_INTERVAL)

    log("Shutdown complete")


if __name__ == "__main__":
    main()
