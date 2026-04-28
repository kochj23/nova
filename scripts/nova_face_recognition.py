#!/usr/bin/env python3
"""
nova_face_recognition.py — Local face recognition on exterior cameras.

Uses sam-faces skill (CNN + SQLite) for identification. Fully local — no cloud.

Workflow:
  1. Scans exterior camera frames for faces
  2. Compares against sam-faces SQLite database
  3. Known faces → log to vector memory ("Jordan arrived home at 3pm")
  4. Unknown faces → save crop, alert Slack with image, ask "Who is this?"
  5. Enrollment: use sam-faces enroll_face.py or drop photo in known/<name>/

Face database:
  /Volumes/Data/Nova/skills/sam-faces/faces/people.db  — SQLite (sam-faces)
  ~/.openclaw/workspace/faces/unknown/                 — unidentified face crops

Cron: every 15 min (or integrated into camera monitor)
Written by Jordan Koch.
"""

import json
import os
import sys
import time
import importlib.util
import urllib.request
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

SLACK_TOKEN = nova_config.slack_bot_token()
SLACK_CHAN = nova_config.SLACK_PHOTOS
SLACK_NOTIFY = nova_config.SLACK_PHOTOS
SLACK_API = nova_config.SLACK_API
VECTOR_URL = nova_config.VECTOR_URL
NOW = datetime.now()
TODAY = date.today().isoformat()

WORKSPACE = Path.home() / ".openclaw/workspace"
UNKNOWN_DIR = WORKSPACE / "faces" / "unknown"
CAMERA_FRAMES = WORKSPACE / "camera_frames"
STATE_FILE = WORKSPACE / "state" / "nova_face_state.json"

SAM_FACES_DIR = Path("/Volumes/Data/Nova/skills/sam-faces/sam_faces")

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

TOLERANCE = 0.55
PERSON_COOLDOWN = 1800  # 30 min
UNKNOWN_COOLDOWN = 600  # 10 min


def log(msg):
    print(f"[nova_face {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def _load_sam_faces():
    """Dynamically import sam-faces identify module."""
    spec = importlib.util.spec_from_file_location(
        "identify_faces", SAM_FACES_DIR / "identify_faces.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def slack_post(text, channel=None):
    data = json.dumps({
        "channel": channel or SLACK_NOTIFY, "text": text, "mrkdwn": True
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
    """Upload image to Slack using files.getUploadURLExternal."""
    import urllib.parse
    token = SLACK_TOKEN
    ch = channel or SLACK_NOTIFY
    try:
        filename = os.path.basename(filepath)
        file_size = os.path.getsize(filepath)
        params = urllib.parse.urlencode({"filename": filename, "length": file_size})
        req = urllib.request.Request(
            f"https://slack.com/api/files.getUploadURLExternal?{params}",
            headers={"Authorization": f"Bearer {token}"}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        url_data = json.loads(resp.read())
        if not url_data.get("ok"):
            log(f"Slack getUploadURL failed: {url_data.get('error','?')}")
            return False

        upload_url = url_data["upload_url"]
        file_id = url_data["file_id"]

        with open(filepath, "rb") as f:
            file_data = f.read()
        req2 = urllib.request.Request(upload_url, data=file_data,
                                       headers={"Content-Type": "application/octet-stream"})
        urllib.request.urlopen(req2, timeout=15)

        complete = json.dumps({
            "files": [{"id": file_id, "title": filename}],
            "channel_id": ch,
            "initial_comment": comment,
        }).encode()
        req3 = urllib.request.Request(
            "https://slack.com/api/files.completeUploadExternal",
            data=complete,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        resp3 = urllib.request.urlopen(req3, timeout=10)
        result = json.loads(resp3.read())
        return result.get("ok", False)
    except Exception as e:
        log(f"Slack upload error: {e}")
        return False


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


# ── State management ─────────────────────────────────────────────────────────

def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_seen": {}, "unknown_alerts": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Main scan ────────────────────────────────────────────────────────────────

def scan_cameras():
    """Scan all exterior cameras for faces using sam-faces."""
    sam = _load_sam_faces()
    UNKNOWN_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    now_ts = time.time()

    detections = []

    for camera_file in EXTERIOR_CAMERAS:
        frame_path = CAMERA_FRAMES / camera_file
        if not frame_path.exists():
            continue

        age = now_ts - frame_path.stat().st_mtime
        if age > 300:
            continue

        camera_name = camera_file.replace("_latest.jpg", "").replace("_", " ").title()

        try:
            result = sam.identify(str(frame_path), threshold=TOLERANCE,
                                  save_unknowns=True, save_crops=True)

            if result.get("face_count", 0) == 0:
                continue

            log(f"{camera_name}: {result['face_count']} face(s) detected")

            for face in result.get("faces", []):
                if not face.get("unknown"):
                    name = face["name"]
                    conf = int(face["confidence"] * 100)
                    key = f"known_{name}"
                    last = state.get("last_seen", {}).get(key, 0)
                    if (now_ts - last) > PERSON_COOLDOWN:
                        detections.append({
                            "type": "known",
                            "name": name,
                            "camera": camera_name,
                            "confidence": conf,
                        })
                        state.setdefault("last_seen", {})[key] = now_ts
                else:
                    last = state.get("unknown_alerts", {}).get(camera_name, 0)
                    if (now_ts - last) > UNKNOWN_COOLDOWN:
                        bb = face["bounding_box"]
                        crop_dir = SAM_FACES_DIR.parent / "faces" / "unknown"
                        crop_path = crop_dir / f"unknown_{frame_path.stem}_{bb['top']}_{bb['left']}.jpg"
                        detections.append({
                            "type": "unknown",
                            "camera": camera_name,
                            "crop_path": str(crop_path) if crop_path.exists() else None,
                        })
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
        slack_post(":bust_in_silhouette: *Face Detection*\n" + "\n".join(f"  {l}" for l in lines))

    if unknown:
        for d in unknown:
            msg = f":question: *Unknown person* at {d['camera']} — {NOW.strftime('%I:%M %p')}. Who is this?"
            if d.get("crop_path") and Path(d["crop_path"]).exists():
                slack_upload_image(d["crop_path"], msg)
            else:
                slack_post(msg)
            vector_remember(
                f"Unknown person detected at {d['camera']} on {TODAY} at {NOW.strftime('%H:%M')}",
                {"date": TODAY, "type": "face_unknown", "camera": d["camera"]}
            )


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
    parser.add_argument("--status", action="store_true", help="Show database status")
    args = parser.parse_args()

    if args.status:
        spec = importlib.util.spec_from_file_location("face_db", SAM_FACES_DIR / "face_db.py")
        db = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(db)
        db.init_db()
        people = db.list_people()
        unknowns = db.list_unknowns()
        print(f"Known people: {len(people)}")
        for p in people:
            print(f"  {p['name']}: {p['encoding_count']} encoding(s)")
        print(f"Unresolved unknowns: {len(unknowns)}")
        print(f"Exterior cameras: {len(EXTERIOR_CAMERAS)}")
    else:
        main()
