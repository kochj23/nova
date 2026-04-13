#!/usr/bin/env python3
"""
nova_face_recognition.py — Local face recognition on exterior cameras.

Uses the face_recognition library (dlib-based) to identify known people
and detect unknown visitors from camera frames. Fully local — no cloud.

Workflow:
  1. Scans exterior camera frames for faces
  2. Compares against known face database (~/.openclaw/workspace/faces/known/)
  3. Known faces → log to vector memory ("Jordan arrived home at 3pm")
  4. Unknown faces → save crop, alert Slack with image, ask "Who is this?"
  5. Enrollment: save a photo to known/<name>/ and it auto-enrolls

Face database:
  ~/.openclaw/workspace/faces/known/<name>/   — photos of known people
  ~/.openclaw/workspace/faces/unknown/        — unidentified face crops
  ~/.openclaw/workspace/faces/encodings.json  — cached face encodings

Cron: every 15 min (or integrated into camera monitor)
Written by Jordan Koch.
"""

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, date
from pathlib import Path

# face_recognition lives in /Volumes/Data/AI/python_packages
os.environ["PYTHONPATH"] = "/Volumes/Data/AI/python_packages:" + os.environ.get("PYTHONPATH", "")
sys.path.insert(0, "/Volumes/Data/AI/python_packages")

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_CHAN
SLACK_API = nova_config.SLACK_API
VECTOR_URL = nova_config.VECTOR_URL
NOW = datetime.now()
TODAY = date.today().isoformat()

WORKSPACE = Path.home() / ".openclaw/workspace"
FACES_DIR = WORKSPACE / "faces"
KNOWN_DIR = FACES_DIR / "known"
UNKNOWN_DIR = FACES_DIR / "unknown"
ENCODINGS_FILE = FACES_DIR / "encodings.json"
CAMERA_FRAMES = WORKSPACE / "camera_frames"
STATE_FILE = Path.home() / ".openclaw/workspace/state/nova_face_state.json"

# Exterior cameras only — Nova doesn't need to see Jordan in his underwear
EXTERIOR_CAMERAS = [
    "front_door_latest.jpg",
    "front_door_patio_latest.jpg",
    "front_yard_latest.jpg",
    "front_yard_alt_latest.jpg",
    "carport_latest.jpg",
    "alley_north_latest.jpg",
    "alley_south_latest.jpg",
    "side_yard_latest.jpg",
    "garage_latest.jpg",
    "abundio_boundary_latest.jpg",
]

# Face matching tolerance (lower = stricter, 0.6 is default)
TOLERANCE = 0.55

# Cooldown: don't re-alert for same person within N seconds
PERSON_COOLDOWN = 1800  # 30 min
UNKNOWN_COOLDOWN = 600  # 10 min


def log(msg):
    print(f"[nova_face {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def slack_post(text, channel=None):
    data = json.dumps({
        "channel": channel or SLACK_CHAN, "text": text, "mrkdwn": True
    }).encode()
    req = urllib.request.Request(
        f"{SLACK_API}/chat.postMessage", data=data,
        headers={"Authorization": "Bearer " + SLACK_TOKEN,
                 "Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as e:
        log(f"Slack error: {e}")


def slack_upload_image(filepath, comment="", channel=None):
    """Upload an image to Slack."""
    import subprocess
    try:
        cmd = [
            "curl", "-s", "-F", f"file=@{filepath}",
            "-F", f"channels={channel or SLACK_CHAN}",
            "-F", f"initial_comment={comment}",
            "-H", f"Authorization: Bearer {SLACK_TOKEN}",
            "https://slack.com/api/files.upload"
        ]
        subprocess.run(cmd, capture_output=True, timeout=15)
    except Exception as e:
        log(f"Slack upload error: {e}")


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "face_recognition", "metadata": metadata or {}
        }).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


# ── Face database ────────────────────────────────────────────────────────────

def ensure_dirs():
    KNOWN_DIR.mkdir(parents=True, exist_ok=True)
    UNKNOWN_DIR.mkdir(parents=True, exist_ok=True)


def load_known_encodings():
    """Load cached face encodings, or rebuild from known/ directory."""
    import face_recognition
    import numpy as np

    # Check if cache is fresh
    if ENCODINGS_FILE.exists():
        try:
            cache = json.loads(ENCODINGS_FILE.read_text())
            cache_time = cache.get("built_at", "")
            # Check if any known/ photos are newer than cache
            newest_photo = 0
            for person_dir in KNOWN_DIR.iterdir():
                if person_dir.is_dir():
                    for photo in person_dir.glob("*.jpg"):
                        newest_photo = max(newest_photo, photo.stat().st_mtime)
                    for photo in person_dir.glob("*.png"):
                        newest_photo = max(newest_photo, photo.stat().st_mtime)

            if cache.get("built_ts", 0) >= newest_photo and cache.get("people"):
                # Convert lists back to numpy arrays
                people = {}
                for name, encs in cache["people"].items():
                    people[name] = [np.array(e) for e in encs]
                log(f"Loaded {len(people)} people from encoding cache")
                return people
        except Exception:
            pass

    # Rebuild from photos
    log("Building face encoding database...")
    people = {}

    for person_dir in KNOWN_DIR.iterdir():
        if not person_dir.is_dir():
            continue
        name = person_dir.name
        encodings = []

        for photo_path in sorted(person_dir.iterdir()):
            if photo_path.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                continue
            try:
                image = face_recognition.load_image_file(str(photo_path))
                face_encs = face_recognition.face_encodings(image)
                if face_encs:
                    encodings.append(face_encs[0])
                    log(f"  Encoded {name}/{photo_path.name}")
                else:
                    log(f"  No face found in {name}/{photo_path.name}")
            except Exception as e:
                log(f"  Error encoding {photo_path.name}: {e}")

        if encodings:
            people[name] = encodings
            log(f"  {name}: {len(encodings)} encoding(s)")

    # Cache to disk
    cache = {
        "built_at": NOW.isoformat(),
        "built_ts": time.time(),
        "people": {name: [enc.tolist() for enc in encs] for name, encs in people.items()},
    }
    ENCODINGS_FILE.write_text(json.dumps(cache))
    log(f"Built encodings for {len(people)} people, cached to {ENCODINGS_FILE.name}")

    return people


def identify_face(face_encoding, known_people):
    """Match a face encoding against known people. Returns (name, confidence) or (None, 0)."""
    import face_recognition
    import numpy as np

    best_name = None
    best_distance = 1.0

    for name, known_encodings in known_people.items():
        distances = face_recognition.face_distance(known_encodings, face_encoding)
        min_distance = np.min(distances)
        if min_distance < best_distance:
            best_distance = min_distance
            best_name = name

    if best_distance <= TOLERANCE:
        confidence = round((1 - best_distance) * 100, 1)
        return best_name, confidence
    return None, 0


# ── State management ─────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_seen": {}, "unknown_alerts": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Main scan ────────────────────────────────────────────────────────────────

def scan_cameras():
    """Scan all exterior cameras for faces."""
    import face_recognition
    from PIL import Image

    ensure_dirs()
    known_people = load_known_encodings()
    state = load_state()
    now_ts = time.time()

    detections = []
    unknown_count = 0

    for camera_file in EXTERIOR_CAMERAS:
        frame_path = CAMERA_FRAMES / camera_file
        if not frame_path.exists():
            continue

        # Skip if frame is old (>5 min)
        age = now_ts - frame_path.stat().st_mtime
        if age > 300:
            continue

        camera_name = camera_file.replace("_latest.jpg", "").replace("_", " ").title()

        try:
            image = face_recognition.load_image_file(str(frame_path))
            face_locations = face_recognition.face_locations(image, model="hog")

            if not face_locations:
                continue

            face_encodings = face_recognition.face_encodings(image, face_locations)
            log(f"{camera_name}: {len(face_locations)} face(s) detected")

            for i, (face_enc, face_loc) in enumerate(zip(face_encodings, face_locations)):
                name, confidence = identify_face(face_enc, known_people)

                if name:
                    # Known person
                    key = f"known_{name}"
                    last = state.get("last_seen", {}).get(key, 0)
                    if (now_ts - last) > PERSON_COOLDOWN:
                        detections.append({
                            "type": "known",
                            "name": name,
                            "camera": camera_name,
                            "confidence": confidence,
                        })
                        state.setdefault("last_seen", {})[key] = now_ts
                else:
                    # Unknown person — save face crop
                    key = f"unknown_{camera_name}_{i}"
                    last = state.get("unknown_alerts", {}).get(camera_name, 0)
                    if (now_ts - last) > UNKNOWN_COOLDOWN:
                        top, right, bottom, left = face_loc
                        # Add padding
                        pad = 40
                        h, w = image.shape[:2]
                        top = max(0, top - pad)
                        left = max(0, left - pad)
                        bottom = min(h, bottom + pad)
                        right = min(w, right + pad)

                        face_crop = image[top:bottom, left:right]
                        crop_filename = f"unknown_{camera_name}_{NOW.strftime('%Y%m%d_%H%M%S')}_{i}.jpg"
                        crop_path = UNKNOWN_DIR / crop_filename

                        try:
                            img = Image.fromarray(face_crop)
                            img.save(str(crop_path))
                        except Exception as e:
                            log(f"Error saving crop: {e}")
                            crop_path = None

                        detections.append({
                            "type": "unknown",
                            "camera": camera_name,
                            "crop_path": str(crop_path) if crop_path else None,
                        })
                        unknown_count += 1
                        state.setdefault("unknown_alerts", {})[camera_name] = now_ts

        except Exception as e:
            log(f"Error scanning {camera_name}: {e}")

    save_state(state)
    return detections


def post_detections(detections):
    """Post face detections to Slack and vector memory."""
    if not detections:
        return

    known = [d for d in detections if d["type"] == "known"]
    unknown = [d for d in detections if d["type"] == "unknown"]

    if known:
        lines = []
        for d in known:
            lines.append(f"*{d['name']}* seen at {d['camera']} ({d['confidence']}% match)")
            vector_remember(
                f"{d['name']} detected at {d['camera']} on {TODAY} at {NOW.strftime('%H:%M')}",
                {"date": TODAY, "type": "face_known", "person": d["name"], "camera": d["camera"]}
            )
        slack_post("*Face Detection*\n" + "\n".join(f"  {l}" for l in lines))

    if unknown:
        for d in unknown:
            msg = f"*Unknown person* detected at {d['camera']} — {NOW.strftime('%I:%M %p')}"
            if d.get("crop_path") and Path(d["crop_path"]).exists():
                slack_upload_image(d["crop_path"], msg)
            else:
                slack_post(msg)
            vector_remember(
                f"Unknown person detected at {d['camera']} on {TODAY} at {NOW.strftime('%H:%M')}",
                {"date": TODAY, "type": "face_unknown", "camera": d["camera"]}
            )


# ── Enrollment ───────────────────────────────────────────────────────────────

def enroll(name, photo_path):
    """Enroll a person by saving their photo to the known faces database."""
    import shutil
    person_dir = KNOWN_DIR / name.lower().replace(" ", "_")
    person_dir.mkdir(parents=True, exist_ok=True)

    dest = person_dir / Path(photo_path).name
    shutil.copy2(photo_path, dest)

    # Invalidate cache so it rebuilds on next scan
    ENCODINGS_FILE.unlink(missing_ok=True)

    log(f"Enrolled {name} from {photo_path}")
    return dest


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    log("Scanning exterior cameras for faces...")
    detections = scan_cameras()

    known_count = sum(1 for d in detections if d["type"] == "known")
    unknown_count = sum(1 for d in detections if d["type"] == "unknown")
    log(f"Results: {known_count} known, {unknown_count} unknown")

    post_detections(detections)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Face Recognition")
    parser.add_argument("--scan", action="store_true", help="Scan cameras (default)")
    parser.add_argument("--enroll", type=str, nargs=2, metavar=("NAME", "PHOTO"),
                        help="Enroll a person: --enroll 'Jordan Koch' /path/to/photo.jpg")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild face encoding cache")
    parser.add_argument("--status", action="store_true", help="Show database status")
    args = parser.parse_args()

    if args.enroll:
        name, photo = args.enroll
        dest = enroll(name, photo)
        print(f"Enrolled: {dest}")
    elif args.rebuild:
        ensure_dirs()
        load_known_encodings()
        print("Encoding cache rebuilt.")
    elif args.status:
        ensure_dirs()
        people = list(KNOWN_DIR.iterdir()) if KNOWN_DIR.exists() else []
        unknowns = list(UNKNOWN_DIR.glob("*.jpg")) if UNKNOWN_DIR.exists() else []
        print(f"Known people: {len([p for p in people if p.is_dir()])}")
        for p in people:
            if p.is_dir():
                photos = list(p.glob("*.jpg")) + list(p.glob("*.png"))
                print(f"  {p.name}: {len(photos)} photo(s)")
        print(f"Unknown crops: {len(unknowns)}")
        print(f"Exterior cameras: {len(EXTERIOR_CAMERAS)}")
    else:
        main()
