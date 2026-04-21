#!/usr/bin/env python3
"""
nova_watch_dogs.py — Monitor exterior cameras for Bruno, Jeremy, Sammy, and Preston.

Pulls snapshots from UniFi Protect UNVR (192.168.1.9) via API, analyzes with
qwen3.5-9b vision model on OpenRouter, and posts sightings to Slack.

Can run as:
  - On-demand: python3 nova_watch_dogs.py --scan
  - Continuous: python3 nova_watch_dogs.py --watch (polls every 5 min)
  - Single camera: python3 nova_watch_dogs.py --scan --camera "Exterior - Dylan"

Known dogs:
  - Jeremy: Small Chihuahua, dark/tan fur, often on patio or couch area
  - Bruno: Medium Chihuahua mix, broader build, troublemaker energy
  - Sammy: Energetic, playful, frequently in motion
  - Preston: Larger, limps from stroke history, slower movement

Only accesses Exterior/External cameras. Interior cameras are NEVER accessed.

Written by Jordan Koch.
"""

import base64
import json
import os
import signal
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

PROTECT_HOST = "192.168.1.9"
PROTECT_USER = "api"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
VISION_MODEL = "qwen/qwen3.5-9b"
SLACK_URL = "https://slack.com/api/chat.postMessage"
SLACK_CHANNEL = "C0AMNQ5GX70"  # #nova-chat for dog sightings
VECTOR_URL = "http://127.0.0.1:18790/remember"
POLL_INTERVAL = 300  # 5 minutes
SNAPSHOT_DIR = Path.home() / ".openclaw/workspace/dog_snapshots"
INTERIOR_PREFIX = "Interior"

DOG_PROMPT = """Security camera image from a residential property in Burbank.
Look carefully for any dogs. These are small Chihuahuas that may be hard to spot.

Known dogs:
- Jeremy: Small Chihuahua, dark/tan fur, often lounging
- Bruno: Medium Chihuahua mix, broader build, active/mischievous
- Sammy: Small, energetic, fast-moving
- Preston: Larger than the others, may limp (stroke history), moves slower

Describe any dogs you see: breed guess, size, color, location in frame, what they're doing.
If you can match to a known dog, say which one and why.
If no dogs visible, say "No dogs detected."
Be concise — 2-3 sentences max."""

shutdown_requested = False


def signal_handler(sig, frame):
    global shutdown_requested
    shutdown_requested = True


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def log(msg):
    print(f"[dog_watch {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_keychain(service):
    r = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", service, "-w"],
        capture_output=True, text=True, timeout=10
    )
    return r.stdout.strip()


class ProtectClient:
    def __init__(self):
        self._ctx = ssl.create_default_context()
        self._ctx.check_hostname = False
        self._ctx.verify_mode = ssl.CERT_NONE
        self._cj = http.cookiejar.CookieJar()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._cj),
            urllib.request.HTTPSHandler(context=self._ctx)
        )
        self._csrf = None

    def login(self):
        password = get_keychain("nova-unifi-protect-api")
        if not password:
            log("ERROR: No Protect password in Keychain")
            return False
        try:
            payload = json.dumps({"username": PROTECT_USER, "password": password}).encode()
            req = urllib.request.Request(
                f"https://{PROTECT_HOST}/api/auth/login",
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            resp = self._opener.open(req, timeout=10)
            self._csrf = resp.headers.get("X-CSRF-Token", "")
            return resp.status == 200
        except Exception as e:
            log(f"Login failed: {e}")
            return False

    def get_exterior_cameras(self):
        try:
            req = urllib.request.Request(f"https://{PROTECT_HOST}/proxy/protect/api/cameras")
            if self._csrf:
                req.add_header("X-CSRF-Token", self._csrf)
            resp = self._opener.open(req, timeout=10)
            cameras = json.loads(resp.read())
            return [c for c in cameras if not c.get("name", "").startswith(INTERIOR_PREFIX)
                    and c.get("state") == "CONNECTED"]
        except Exception as e:
            log(f"Camera list failed: {e}")
            return []

    def get_snapshot(self, camera_id, output_path):
        try:
            url = f"https://{PROTECT_HOST}/proxy/protect/api/cameras/{camera_id}/snapshot?force=true&w=1280"
            req = urllib.request.Request(url)
            if self._csrf:
                req.add_header("X-CSRF-Token", self._csrf)
            resp = self._opener.open(req, timeout=15)
            data = resp.read()
            if len(data) < 1000:
                return False
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(data)
            return True
        except Exception as e:
            log(f"Snapshot failed for {camera_id}: {e}")
            return False


def analyze_image(image_path):
    api_key = get_keychain("nova-openrouter-api-key")
    if not api_key:
        log("ERROR: No OpenRouter API key")
        return None

    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()

    payload = json.dumps({
        "model": VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": DOG_PROMPT},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        "max_tokens": 200,
        "temperature": 0.2,
    }).encode()

    req = urllib.request.Request(
        OPENROUTER_URL, data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        msg = data.get("choices", [{}])[0].get("message", {})
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning") or ""
        if not content and reasoning:
            content = reasoning
        if "<think>" in content:
            end = content.rfind("</think>")
            if end > 0:
                content = content[end + 8:]
        return content.strip() if content else None
    except Exception as e:
        log(f"Vision analysis failed: {e}")
        return None


def post_slack(text, image_path=None):
    token = get_keychain("nova-slack-bot-token")
    if not token:
        return

    if image_path and os.path.exists(image_path):
        try:
            filename = os.path.basename(image_path)
            file_size = os.path.getsize(image_path)
            params = urllib.parse.urlencode({"filename": filename, "length": file_size})
            req = urllib.request.Request(
                f"https://slack.com/api/files.getUploadURLExternal?{params}",
                headers={"Authorization": f"Bearer {token}"}
            )
            resp = urllib.request.urlopen(req, timeout=10)
            url_data = json.loads(resp.read())
            if url_data.get("ok"):
                upload_url = url_data["upload_url"]
                file_id = url_data["file_id"]
                with open(image_path, "rb") as f:
                    file_data = f.read()
                req2 = urllib.request.Request(upload_url, data=file_data,
                                              headers={"Content-Type": "application/octet-stream"})
                urllib.request.urlopen(req2, timeout=15)
                complete = json.dumps({
                    "files": [{"id": file_id, "title": filename}],
                    "channel_id": SLACK_CHANNEL,
                    "initial_comment": text,
                }).encode()
                req3 = urllib.request.Request(
                    "https://slack.com/api/files.completeUploadExternal",
                    data=complete,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                )
                urllib.request.urlopen(req3, timeout=10)
                return
        except Exception as e:
            log(f"Image upload failed, falling back to text: {e}")

    try:
        payload = json.dumps({"channel": SLACK_CHANNEL, "text": text, "mrkdwn": True}).encode()
        req = urllib.request.Request(
            SLACK_URL, data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def remember(text):
    try:
        payload = json.dumps({
            "text": text,
            "source": "dog_watch",
            "metadata": {"privacy": "local-only", "type": "dog_sighting"},
        }).encode()
        req = urllib.request.Request(
            VECTOR_URL, data=payload, headers={"Content-Type": "application/json"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def scan_cameras(client, camera_filter=None):
    cameras = client.get_exterior_cameras()
    if camera_filter:
        cameras = [c for c in cameras if camera_filter.lower() in c.get("name", "").lower()]

    if not cameras:
        log("No matching exterior cameras found")
        return []

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    sightings = []

    for cam in cameras:
        if shutdown_requested:
            break
        name = cam.get("name", "?")
        cam_id = cam.get("id", "")
        snap_path = SNAPSHOT_DIR / f"{cam_id}.jpg"

        if not client.get_snapshot(cam_id, str(snap_path)):
            continue

        desc = analyze_image(str(snap_path))
        if not desc:
            snap_path.unlink(missing_ok=True)
            continue

        if "no dogs detected" in desc.lower() or "no dogs" in desc.lower():
            log(f"  {name}: no dogs")
            snap_path.unlink(missing_ok=True)
            continue

        log(f"  {name}: {desc[:100]}")
        sightings.append({"camera": name, "description": desc, "snapshot": str(snap_path)})

    return sightings


def do_scan(camera_filter=None):
    log("Scanning exterior cameras for dogs...")
    client = ProtectClient()
    if not client.login():
        log("FATAL: Cannot authenticate to Protect API")
        return

    sightings = scan_cameras(client, camera_filter)

    if sightings:
        for s in sightings:
            msg = f":dog2: *Dog Spotted — {s['camera']}*\n{s['description']}"
            post_slack(msg, s.get("snapshot"))
            remember(f"Dog sighting on {s['camera']} at {datetime.now().isoformat()}: {s['description']}")
            try:
                Path(s["snapshot"]).unlink(missing_ok=True)
            except Exception:
                pass
        log(f"Found dogs on {len(sightings)} camera(s)")
    else:
        log("No dogs detected on any exterior camera")


def do_watch(camera_filter=None):
    log(f"Continuous dog watch started (poll every {POLL_INTERVAL}s)")
    post_slack(":dog2: *Dog Watch Active* — scanning exterior cameras every 5 minutes")

    while not shutdown_requested:
        do_scan(camera_filter)
        for _ in range(POLL_INTERVAL):
            if shutdown_requested:
                break
            time.sleep(1)

    log("Dog watch stopped")
    post_slack(":dog2: *Dog Watch Stopped*")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Monitor exterior cameras for dogs")
    parser.add_argument("--scan", action="store_true", help="One-time scan of all exterior cameras")
    parser.add_argument("--watch", action="store_true", help="Continuous monitoring (polls every 5 min)")
    parser.add_argument("--camera", type=str, default=None, help="Filter to specific camera name (partial match)")
    args = parser.parse_args()

    if args.watch:
        do_watch(args.camera)
    elif args.scan:
        do_scan(args.camera)
    else:
        do_scan(args.camera)


if __name__ == "__main__":
    main()
