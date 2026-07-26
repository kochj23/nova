#!/usr/bin/env python3
"""
nova_face_recognition.py — Local face recognition on exterior cameras.

Uses sam-faces skill (CNN + PostgreSQL nova_ops) for identification. Fully local — no cloud.

Workflow:
  1. Scans exterior camera frames for faces
  2. Compares against face_people / face_encodings in PostgreSQL (nova_ops)
  3. Known faces → log to vector memory ("Jordan arrived home at 3pm")
  4. Unknown faces → save crop, alert Slack with image, ask "Who is this?"
  5. Enrollment: use sam-faces enroll_face.py or drop photo in known/<name>/

Face database:
  PostgreSQL nova_ops — tables: face_people, face_encodings, face_unknown_candidates
  ~/.openclaw/workspace/faces/unknown/ — unidentified face crops

Cron: every 15 min (or integrated into camera monitor)
Written by Jordan Koch.
"""

import json
import os
import sys
import time
import importlib.util
import urllib.request
import psycopg2
from datetime import datetime, date, timedelta
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
KNOWN_DIR = WORKSPACE / "faces" / "known"      # crops of NAMED detections, so Slack alerts show the face
CAMERA_FRAMES = WORKSPACE / "camera_frames"
STATE_FILE = WORKSPACE / "state" / "nova_face_state.json"

SAM_FACES_DIR = Path.home() / ".openclaw/skills/sam-faces/sam_faces"

AWAY_THRESHOLD_MINUTES = 60  # Mark as "away" if not seen for this long
PG_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"

EXTERIOR_CAMERAS = [
    "front_door_latest.jpg",
    "front_door_patio_latest.jpg",
    "front_yard_latest.jpg",
    "front_yard_alt_latest.jpg",
    "carport_latest.jpg",
    "alley_north_latest.jpg",
    "alley_south_latest.jpg",
    # "exterior_garbage_latest.jpg",  # 2026-06-23: camera physically misaimed at an
    # indoor shelf — face-rec kept false-positiving on a Sunbonnet-tin label face.
    # Re-enable once the camera is re-aimed at the yard.
    "garage_latest.jpg",
    "abundio_boundary_latest.jpg",
]

TOLERANCE = 0.55
# Minimum match confidence (%) required to actually PUT A NAME on a face. Below this,
# the match is too weak to trust and is reported as an "unknown person" (with photo)
# instead of a false identity. Added 2026-07-25 after weak 47-54% matches confidently
# labeled a month-absent visitor ("Dave Bloom") and a DECEASED person ("Kathleen Koch").
# Calibration: identify() gives confidence = 1 - face_distance. The base TOLERANCE 0.55
# accepts matches down to 45% confidence — far too loose. Every observed false positive
# was <=54%; a genuine solid match sits ~65%+. 65 clears the noise with margin while still
# naming real matches. Asymmetry favors strictness: a missed name just becomes "unknown +
# photo" (you still see who it is), whereas a false name is alarming. Tune via the % shown
# in each alert; raise toward 75 if false names persist, lower toward 60 if real ones get missed.
MIN_NAME_CONFIDENCE = 65
PERSON_COOLDOWN = 1800  # 30 min
UNKNOWN_COOLDOWN = 600  # 10 min

OLLAMA_URL = "http://127.0.0.1:11434/api/generate"
VISION_MODEL = "qwen3-vl:4b"


def log(msg):
    print(f"[nova_face {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def volumes_ready():
    """sam-faces now lives locally under ~/.openclaw/skills (moved off the flaky
    /Volumes/nas mount 2026-07-19 — a 3.8MB package has no business depending on
    a network share). Kept as a cheap existence check, not a mount wait."""
    if not SAM_FACES_DIR.exists():
        log(f"{SAM_FACES_DIR} missing — skipping this run")
        return False
    return True


def make_context_crop(frame_path, bb, out_path, pad_frac=0.6, min_size=320):
    """Build a usable face crop from the FULL frame: pad the bounding box with
    context and upscale tiny/distant faces so the result is viewable in Slack.
    The raw sam-faces crop is the bare bounding box — for distant cameras that's
    a ~44px speck that renders as nothing. Returns out_path, or None on failure."""
    try:
        from PIL import Image
        im = Image.open(frame_path).convert("RGB")
        W, H = im.size
        top, right, bottom, left = bb["top"], bb["right"], bb["bottom"], bb["left"]
        fw, fh = max(1, right - left), max(1, bottom - top)
        pad_x, pad_y = int(fw * pad_frac), int(fh * pad_frac)
        box = (max(0, left - pad_x), max(0, top - pad_y),
               min(W, right + pad_x), min(H, bottom + pad_y))
        crop = im.crop(box)
        cw, ch = crop.size
        longest = max(cw, ch)
        if 0 < longest < min_size:
            scale = min_size / longest
            crop = crop.resize((max(1, int(cw * scale)), max(1, int(ch * scale))), Image.LANCZOS)
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        crop.save(out_path, "JPEG", quality=90)
        return out_path
    except Exception as e:
        log(f"context crop failed: {e}")
        return None


def _scene_has_no_people(desc: str) -> bool:
    """True if the scene caption clearly states there are no people present. Used to veto
    false-positive 'unknown person' alerts — glare/headlights/vehicles fool the person detector,
    but the VLM caption correctly says 'no people'. Only suppresses on an affirmative negative;
    an empty/uncertain caption still alerts (better to ask than miss a real person)."""
    d = (desc or "").lower()
    if not d:
        return False
    return any(p in d for p in (
        "no people", "no person", "no one", "no humans", "nobody",
        "no visible people", "no pedestrians", "no individuals",
        "no people present", "no people are present", "without any people",
    ))


def describe_scene(image_path):
    """Use local vision model to describe what's happening in a camera frame.
    Returns a short description or None on failure."""
    import base64
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()

        payload = json.dumps({
            "model": VISION_MODEL,
            "prompt": "Describe this security camera image in one sentence. Focus on: how many people, what they're doing, what they're carrying, vehicle presence, and anything unusual. Be specific and concise.",
            "images": [img_b64],
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 150}
        }).encode()

        req = urllib.request.Request(
            OLLAMA_URL, data=payload,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
        return result.get("response", "").strip()[:200]
    except Exception as e:
        log(f"Vision describe failed: {e}")
        return None


def looks_like_person(image_path):
    """Vision-gate for unknown-person alerts: dlib happily finds 'faces' in
    street-light glare at night (2026-07-26, the Dave Bloom lamp incident) and
    printed faces on tins (2026-06-23). One local vision call kills the whole
    not-a-human alert class. Fail-open on errors — an extra alert beats a
    silently missed person."""
    import base64
    try:
        with open(image_path, "rb") as f:
            img_b64 = base64.b64encode(f.read()).decode()
        payload = json.dumps({
            "model": VISION_MODEL,
            "prompt": ("Does this image contain a real, physically present human person or human face? "
                       "Bright lights, lens glare, lamps, headlights, animals, objects, and printed/pictured "
                       "faces on labels, posters, or screens do NOT count. Answer only YES or NO."),
            "images": [img_b64],
            "stream": False,
            # qwen3-vl thinks before answering; a small budget gets eaten by
            # thinking tokens and returns an empty response (= fail-open).
            "options": {"temperature": 0.0, "num_predict": 512},
        }).encode()
        req = urllib.request.Request(OLLAMA_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            ans = json.loads(resp.read()).get("response", "").strip().upper()
        if not ans:
            return True  # empty/thinking-only response — fail open
        return "NO" not in ans.split()[-1:][0]
    except Exception as e:
        log(f"Vision person-gate failed (fail-open): {e}")
        return True


def _load_sam_faces():
    """Import sam-faces identify module as a package."""
    sam_parent = str(SAM_FACES_DIR.parent)
    if sam_parent not in sys.path:
        sys.path.insert(0, sam_parent)
    from sam_faces.identify import identify
    class _Module:
        pass
    mod = _Module()
    mod.identify = identify
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


# ── Presence tracking ────────────────────────────────────────────────────────

def _get_pg():
    return psycopg2.connect(PG_DSN)


def update_presence(person_name: str, camera: str, confidence: int):
    """Upsert face_presence row. Returns 'arrived' if newly home, 'seen' if already home."""
    conn = _get_pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM face_people WHERE LOWER(name) = LOWER(%s) LIMIT 1",
                (person_name,)
            )
            row = cur.fetchone()
            person_id = row[0] if row else person_name.lower()

            # Check current state before upserting
            cur.execute(
                "SELECT is_home FROM face_presence WHERE person_id = %s",
                (person_id,)
            )
            existing = cur.fetchone()
            was_away = existing is None or not existing[0]

            cur.execute("""
                INSERT INTO face_presence (person_id, person_name, camera, confidence, first_seen, last_seen, is_home)
                VALUES (%s, %s, %s, %s, NOW(), NOW(), TRUE)
                ON CONFLICT (person_id) DO UPDATE SET
                    camera = EXCLUDED.camera,
                    confidence = EXCLUDED.confidence,
                    last_seen = NOW(),
                    is_home = TRUE
            """, (person_id, person_name, camera, confidence))
            conn.commit()

            return "arrived" if was_away else "seen"
    finally:
        conn.close()


def mark_departed():
    """Mark people as not home if not seen for AWAY_THRESHOLD_MINUTES. Returns list of departed names."""
    conn = _get_pg()
    departed = []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE face_presence
                SET is_home = FALSE
                WHERE is_home = TRUE
                  AND last_seen < NOW() - INTERVAL '%s minutes'
                RETURNING person_name
            """, (AWAY_THRESHOLD_MINUTES,))
            departed = [row[0] for row in cur.fetchall()]
            conn.commit()
    finally:
        conn.close()
    return departed


def get_who_is_home() -> list[dict]:
    """Query who is currently home."""
    conn = _get_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT person_name, camera, confidence, last_seen
                FROM face_presence WHERE is_home = TRUE
                ORDER BY last_seen DESC
            """)
            return [
                {"name": r[0], "camera": r[1], "confidence": r[2], "last_seen": r[3].isoformat()}
                for r in cur.fetchall()
            ]
    finally:
        conn.close()


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
                conf = int(face.get("confidence", 0) * 100)
                bb = face.get("bounding_box")
                # A face counts as NAMED only if the model matched it AND we're confident
                # enough to trust the name. A weak match (below MIN_NAME_CONFIDENCE) is
                # demoted to "unknown" so we never announce a false identity.
                named = (not face.get("unknown")) and conf >= MIN_NAME_CONFIDENCE

                if named:
                    name = face["name"]
                    key = f"known_{name}"
                    last = state.get("last_seen", {}).get(key, 0)
                    if (now_ts - last) > PERSON_COOLDOWN:
                        # save a crop so the Slack alert can SHOW the face (previously text-only)
                        crop_path = None
                        if bb:
                            KNOWN_DIR.mkdir(parents=True, exist_ok=True)
                            cp = KNOWN_DIR / f"known_{name.replace(' ','_')}_{frame_path.stem}_{bb['top']}_{bb['left']}.jpg"
                            crop_path = make_context_crop(str(frame_path), bb, str(cp))
                        detections.append({
                            "type": "known",
                            "name": name,
                            "camera": camera_name,
                            "confidence": conf,
                            "crop_path": crop_path,
                        })
                        state.setdefault("last_seen", {})[key] = now_ts
                else:
                    last = state.get("unknown_alerts", {}).get(camera_name, 0)
                    if (now_ts - last) > UNKNOWN_COOLDOWN and bb:
                        crop_path = UNKNOWN_DIR / f"unknown_{frame_path.stem}_{bb['top']}_{bb['left']}.jpg"
                        # Build a padded, min-size crop from the full frame so
                        # distant faces are actually viewable in Slack.
                        saved_crop = make_context_crop(str(frame_path), bb, str(crop_path))
                        # Street lights are not people, no matter how confident dlib feels.
                        if not looks_like_person(saved_crop or str(frame_path)):
                            log(f"{camera_name}: unknown-face detection rejected by vision gate (glare/blob/print)")
                            continue
                        det = {
                            "type": "unknown",
                            "camera": camera_name,
                            "crop_path": saved_crop,
                            "frame_path": str(frame_path),
                        }
                        # If this WAS a name match but too weak to trust, note it (transparency:
                        # "unknown, weak 56% match to X" — helps you eyeball the photo).
                        if not face.get("unknown"):
                            det["weak_match"] = f"{face.get('name','?')} {conf}%"
                        detections.append(det)
                        state.setdefault("unknown_alerts", {})[camera_name] = now_ts

        except Exception as e:
            log(f"Error scanning {camera_name}: {e}")

    save_state(state)
    return detections


def post_detections(detections):
    """Post face detections to Slack and vector memory, update presence."""
    if not detections:
        return

    known = [d for d in detections if d["type"] == "known"]
    unknown = [d for d in detections if d["type"] == "unknown"]

    if known:
        lines = []
        arrivals = []
        for d in known:
            status = update_presence(d["name"], d["camera"], d["confidence"])
            if status == "arrived":
                arrivals.append(d["name"])
            lines.append(f"*{d['name']}* seen at {d['camera']} ({d['confidence']}% match)")
            vector_remember(
                f"{d['name']} detected at {d['camera']} on {TODAY} at {NOW.strftime('%H:%M')}",
                {"date": TODAY, "type": "face_known", "person": d["name"], "camera": d["camera"]}
            )

        # crop per name, so each alert can carry the actual photo
        crop_by_name = {d["name"]: d.get("crop_path") for d in known if d.get("crop_path")}

        def _post_with_face(name, text):
            cp = crop_by_name.get(name)
            if cp and Path(cp).exists():
                slack_upload_image(cp, text)   # photo so you can verify it's really them
            else:
                slack_post(text)

        if arrivals:
            for name in arrivals:
                _post_with_face(name, f":house_with_garden: *{name} arrived home* — {NOW.strftime('%I:%M %p')}")
                vector_remember(
                    f"{name} arrived home on {TODAY} at {NOW.strftime('%H:%M')}",
                    {"date": TODAY, "type": "presence_arrived", "person": name}
                )
        else:
            text = ":bust_in_silhouette: *Face Detection*\n" + "\n".join(f"  {l}" for l in lines)
            # one detection -> upload the face with the caption; several -> text then each photo
            named_crops = [d["crop_path"] for d in known if d.get("crop_path") and Path(d["crop_path"]).exists()]
            if len(named_crops) == 1:
                slack_upload_image(named_crops[0], text)
            elif named_crops:
                slack_post(text)
                for cp in named_crops:
                    slack_upload_image(cp, "")
            else:
                slack_post(text)

    if unknown:
        for d in unknown:
            # Use vision model to describe what the person is doing
            scene_desc = ""
            if d.get("frame_path") and Path(d["frame_path"]).exists():
                scene_desc = describe_scene(d["frame_path"]) or ""

            # VETO false positives: if the scene caption says there are no people, this is
            # glare/headlights/a vehicle that fooled the person detector — don't page Jordan.
            if _scene_has_no_people(scene_desc):
                log(f"Suppressed false 'unknown person' at {d['camera']} — scene says no people: {scene_desc[:80]}")
                continue

            desc_line = f"\n  _Scene: {scene_desc}_" if scene_desc else ""
            weak_line = f"\n  _(weak {d['weak_match']} match — not trusted; verify from photo)_" if d.get("weak_match") else ""
            msg = f":question: *Unknown person* at {d['camera']} — {NOW.strftime('%I:%M %p')}. Who is this?{weak_line}{desc_line}"

            if d.get("crop_path") and Path(d["crop_path"]).exists():
                slack_upload_image(d["crop_path"], msg)
            else:
                slack_post(msg)

            memory_text = f"Unknown person detected at {d['camera']} on {TODAY} at {NOW.strftime('%H:%M')}"
            if scene_desc:
                memory_text += f". Scene: {scene_desc}"
            vector_remember(
                memory_text,
                {"date": TODAY, "type": "face_unknown", "camera": d["camera"]}
            )


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if not volumes_ready():
        return
    log("Scanning exterior cameras for faces...")
    detections = scan_cameras()

    known_count = sum(1 for d in detections if d["type"] == "known")
    unknown_count = sum(1 for d in detections if d["type"] == "unknown")
    log(f"Results: {known_count} known, {unknown_count} unknown")

    post_detections(detections)

    departed = mark_departed()
    for name in departed:
        log(f"Departure: {name} marked away (not seen for {AWAY_THRESHOLD_MINUTES}min)")
        slack_post(f":wave: *{name} left home* — last seen {AWAY_THRESHOLD_MINUTES}+ min ago")
        vector_remember(
            f"{name} departed home on {TODAY} at {NOW.strftime('%H:%M')}",
            {"date": TODAY, "type": "presence_departed", "person": name}
        )


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Nova Face Recognition")
    parser.add_argument("--scan", action="store_true", help="Scan cameras (default)")
    parser.add_argument("--status", action="store_true", help="Show database status")
    parser.add_argument("--who-is-home", action="store_true", help="Show who is currently home")
    args = parser.parse_args()

    if args.who_is_home:
        home = get_who_is_home()
        if home:
            print(f"{len(home)} person(s) home:")
            for p in home:
                print(f"  {p['name']} — last seen at {p['camera']} ({p['confidence']}% confidence, {p['last_seen']})")
        else:
            print("Nobody detected home.")
    elif args.status:
        sam_parent = str(SAM_FACES_DIR.parent)
        if sam_parent not in sys.path:
            sys.path.insert(0, sam_parent)
        from sam_faces.database import init_db, list_people, list_unknowns
        init_db()
        people = list_people()
        unknowns = list_unknowns()
        print(f"Known people: {len(people)}")
        for p in people:
            print(f"  {p['name']}: {p['encoding_count']} encoding(s)")
        print(f"Unresolved unknowns: {len(unknowns)}")
        print(f"Exterior cameras: {len(EXTERIOR_CAMERAS)}")
        home = get_who_is_home()
        print(f"Currently home: {', '.join(p['name'] for p in home) if home else 'nobody'}")
    else:
        main()
