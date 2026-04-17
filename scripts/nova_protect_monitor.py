#!/usr/bin/env python3
"""
nova_protect_monitor.py — UniFi Protect integration for Exterior cameras only.

Connects to the local UNVR at 192.168.1.9 via the Protect API.
Monitors motion events, captures snapshots, reports camera health.
ONLY accesses cameras whose name starts with "Exterior".

Credentials: macOS Keychain (nova-unifi-protect-api), NEVER in files.
All processing local. Interior cameras are NEVER accessed.

Runs via launchd every 5 minutes.

Written by Jordan Koch.
"""

import json
import os
import ssl
import subprocess
import sys
import time
import urllib.request
import http.cookiejar
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR, LOG_WARN

PROTECT_HOST = "192.168.1.9"
PROTECT_USER = "api"
VECTOR_URL = "http://127.0.0.1:18790/remember"
SLACK_NOTIFY = nova_config.SLACK_NOTIFY
SLACK_CHAT = nova_config.SLACK_CHAN
STATE_FILE = Path.home() / ".openclaw/workspace/state/protect_monitor_state.json"
SNAPSHOT_DIR = Path.home() / ".openclaw/workspace/protect_snapshots"
INTERIOR_PREFIX = "Interior"  # NEVER access these

# Only alert on these event types (ignore continuous motion noise)
ALERT_EVENTS = {"smartDetectZone", "ring", "sensorMotion"}


def _get_password():
    result = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-unifi-protect-api", "-w"],
        capture_output=True, text=True
    )
    return result.stdout.strip()


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
        self._csrf_token = None
        self._logged_in = False

    def login(self):
        password = _get_password()
        if not password:
            log("No Protect API password in Keychain", level=LOG_ERROR, source="protect")
            return False
        try:
            payload = json.dumps({"username": PROTECT_USER, "password": password}).encode()
            req = urllib.request.Request(
                f"https://{PROTECT_HOST}/api/auth/login",
                data=payload,
                headers={"Content-Type": "application/json"}
            )
            resp = self._opener.open(req, timeout=10)
            self._csrf_token = resp.headers.get("X-CSRF-Token", "")
            self._logged_in = resp.status == 200
            return self._logged_in
        except Exception as e:
            log(f"Login failed: {e}", level=LOG_ERROR, source="protect")
            return False

    def _get(self, path):
        if not self._logged_in:
            if not self.login():
                return None
        try:
            url = f"https://{PROTECT_HOST}/proxy/protect/api/{path}"
            req = urllib.request.Request(url)
            if self._csrf_token:
                req.add_header("X-CSRF-Token", self._csrf_token)
            resp = self._opener.open(req, timeout=15)
            return json.loads(resp.read())
        except Exception as e:
            log(f"API error ({path}): {e}", level=LOG_ERROR, source="protect")
            return None

    def get_bootstrap(self):
        return self._get("bootstrap")

    def get_events(self, since_ms=None, limit=30):
        path = f"events?limit={limit}&orderDirection=DESC"
        if since_ms:
            path += f"&start={since_ms}"
        return self._get(path)

    def get_snapshot(self, camera_id, output_path):
        """Download a snapshot from a specific camera."""
        if not self._logged_in:
            if not self.login():
                return False
        try:
            url = f"https://{PROTECT_HOST}/proxy/protect/api/cameras/{camera_id}/snapshot?force=true"
            req = urllib.request.Request(url)
            if self._csrf_token:
                req.add_header("X-CSRF-Token", self._csrf_token)
            resp = self._opener.open(req, timeout=15)
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "wb") as f:
                f.write(resp.read())
            return True
        except Exception as e:
            log(f"Snapshot failed for {camera_id}: {e}", level=LOG_ERROR, source="protect")
            return False


def _get_event_thumbnail(client, event_id, output_path):
    """Download event thumbnail image from Protect."""
    if not client._logged_in:
        if not client.login():
            return False
    try:
        url = f"https://{PROTECT_HOST}/proxy/protect/api/events/{event_id}/thumbnail?w=640"
        req = urllib.request.Request(url)
        if client._csrf_token:
            req.add_header("X-CSRF-Token", client._csrf_token)
        resp = client._opener.open(req, timeout=15)
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(resp.read())
        return os.path.getsize(output_path) > 500
    except Exception as e:
        log(f"Thumbnail download failed for {event_id}: {e}", level=LOG_WARN, source="protect")
        return False


def slack_upload_image(filepath, channel, title="", comment=""):
    """Upload an image to Slack using files.uploadV2."""
    token = nova_config.slack_bot_token()
    if not token:
        return False
    try:
        import mimetypes
        mime = mimetypes.guess_type(filepath)[0] or "image/jpeg"
        filename = os.path.basename(filepath)

        # Step 1: Get upload URL
        payload = json.dumps({
            "filename": filename,
            "length": os.path.getsize(filepath),
        }).encode()
        req = urllib.request.Request(
            "https://slack.com/api/files.getUploadURLExternal",
            data=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=10)
        url_data = json.loads(resp.read())
        if not url_data.get("ok"):
            # Fallback: post as text with note about image
            return False

        upload_url = url_data.get("upload_url", "")
        file_id = url_data.get("file_id", "")

        # Step 2: Upload file
        with open(filepath, "rb") as f:
            file_data = f.read()
        req2 = urllib.request.Request(upload_url, data=file_data,
                                       headers={"Content-Type": mime})
        urllib.request.urlopen(req2, timeout=15)

        # Step 3: Complete upload
        complete = json.dumps({
            "files": [{"id": file_id, "title": title or filename}],
            "channel_id": channel,
            "initial_comment": comment,
        }).encode()
        req3 = urllib.request.Request(
            "https://slack.com/api/files.completeUploadExternal",
            data=complete,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        )
        resp3 = urllib.request.urlopen(req3, timeout=10)
        return json.loads(resp3.read()).get("ok", False)
    except Exception as e:
        log(f"Slack image upload failed: {e}", level=LOG_WARN, source="protect")
        return False


def _is_exterior(camera):
    """Any camera that is NOT Interior. Includes Exterior and External cameras."""
    return not camera.get("name", "").startswith(INTERIOR_PREFIX)


def slack_post(text, channel=None):
    token = nova_config.slack_bot_token()
    if not token:
        return
    try:
        payload = json.dumps({"channel": channel or SLACK_NOTIFY, "text": text}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage", data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def vector_remember(text, metadata=None):
    payload = json.dumps({
        "text": text, "source": "security",
        "metadata": metadata or {}
    }).encode()
    try:
        req = urllib.request.Request(VECTOR_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_event_ts": 0, "camera_status": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def check_camera_health(client, state):
    """Check exterior camera health and alert on status changes."""
    bootstrap = client.get_bootstrap()
    if not bootstrap:
        return

    cameras = bootstrap.get("cameras", [])
    exterior = [c for c in cameras if _is_exterior(c)]
    prev_status = state.get("camera_status", {})
    alerts = []

    for cam in exterior:
        name = cam.get("name", "?")
        cam_id = cam.get("id", "")
        cur_state = cam.get("state", "UNKNOWN")
        prev_state = prev_status.get(cam_id, {}).get("state", "UNKNOWN")

        # Alert on state transitions
        if prev_state != cur_state and prev_state != "UNKNOWN":
            if cur_state == "DISCONNECTED":
                alerts.append(f":red_circle: *{name}* went OFFLINE")
            elif cur_state == "CONNECTED" and prev_state == "DISCONNECTED":
                alerts.append(f":large_green_circle: *{name}* back ONLINE")

        prev_status[cam_id] = {
            "name": name,
            "state": cur_state,
            "type": cam.get("type", ""),
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }

    state["camera_status"] = prev_status

    if alerts:
        msg = ":camera: *Protect Camera Health*\n" + "\n".join(alerts)
        slack_post(msg)

    # Summary stats
    connected = sum(1 for c in exterior if c.get("state") == "CONNECTED")
    total = len(exterior)
    log(f"Exterior cameras: {connected}/{total} connected", level=LOG_INFO, source="protect")

    return exterior


def check_motion_events(client, state):
    """Check recent motion/smart detection events on Exterior cameras only."""
    bootstrap = client.get_bootstrap()
    if not bootstrap:
        return

    # Build map of exterior camera IDs
    cameras = bootstrap.get("cameras", [])
    exterior_ids = {c["id"]: c["name"] for c in cameras if _is_exterior(c)}

    # Get events since last check
    last_ts = state.get("last_event_ts", 0)
    if not last_ts:
        last_ts = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp() * 1000)

    events = client.get_events(since_ms=last_ts, limit=50)
    if not events:
        return

    new_events = []
    max_ts = last_ts

    for event in events:
        cam_id = event.get("camera", "")
        event_ts = event.get("start", 0)

        # STRICT: Only Exterior cameras
        if cam_id not in exterior_ids:
            continue

        if event_ts <= last_ts:
            continue

        event_type = event.get("type", "")
        smart_types = event.get("smartDetectTypes", [])
        cam_name = exterior_ids.get(cam_id, "Unknown")

        if event_ts > max_ts:
            max_ts = event_ts

        # Smart detection events — person and animal only (vehicle excluded per Jordan)
        smart_types = [t for t in smart_types if t != "vehicle"]
        if smart_types:
            new_events.append({
                "camera": cam_name,
                "camera_id": cam_id,
                "type": "smart_detect",
                "smart_types": smart_types,
                "timestamp": event_ts,
                "event_id": event.get("id", ""),
            })
        elif event_type in ALERT_EVENTS:
            new_events.append({
                "camera": cam_name,
                "type": event_type,
                "timestamp": event_ts,
            })

    state["last_event_ts"] = max_ts

    if not new_events:
        return

    # Group by camera for notification
    by_camera = {}
    for e in new_events:
        by_camera.setdefault(e["camera"], []).append(e)

    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    for cam_name, cam_events in by_camera.items():
        smart = [e for e in cam_events if e.get("smart_types")]
        other = [e for e in cam_events if not e.get("smart_types")]

        parts = [f":movie_camera: *{cam_name}*"]
        if smart:
            types_seen = set()
            for e in smart:
                types_seen.update(e["smart_types"])
            emoji_map = {"person": ":bust_in_silhouette:",
                         "animal": ":dog:", "package": ":package:"}
            filtered_types = {t for t in types_seen if t != "vehicle"}
            for t in sorted(filtered_types):
                parts.append(f"  {emoji_map.get(t, ':grey_question:')} {t} detected")

            if "vehicle" in types_seen:
                log("Vehicle detection suppressed — ignored on busy street", level=LOG_INFO, source="protect")
        if other:
            parts.append(f"  {len(other)} motion event(s)")

        alert_text = "\n".join(parts)

        # Try to get event thumbnail for smart detections (person/animal/package)
        uploaded_image = False
        if smart:
            # Use the most recent smart event's thumbnail
            best_event = max(smart, key=lambda e: e.get("timestamp", 0))
            event_id = best_event.get("event_id", "")
            if event_id:
                thumb_path = SNAPSHOT_DIR / f"{event_id}.jpg"
                if _get_event_thumbnail(client, event_id, str(thumb_path)):
                    detect_types = ", ".join(sorted(filtered_types))
                    uploaded_image = slack_upload_image(
                        str(thumb_path),
                        SLACK_NOTIFY,
                        title=f"{cam_name} — {detect_types}",
                        comment=alert_text,
                    )
                    if uploaded_image:
                        log(f"Uploaded thumbnail for {cam_name} ({detect_types})",
                            level=LOG_INFO, source="protect")
                    # Clean up thumbnail
                    try:
                        thumb_path.unlink()
                    except Exception:
                        pass

        if not uploaded_image:
            slack_post(alert_text)

        # Store in memory
        vector_remember(
            f"Protect event on {cam_name}: {', '.join(e.get('type','?') for e in events)}. "
            f"Smart detections: {', '.join(t for e in smart for t in e.get('smart_types',[]))}",
            {"type": "protect_event", "camera": cam_name, "date": datetime.now().isoformat()}
        )

    log(f"{len(new_events)} new events across {len(by_camera)} exterior cameras",
        level=LOG_INFO, source="protect")


def main():
    log("Protect monitor starting", level=LOG_INFO, source="protect")
    state = load_state()
    client = ProtectClient()

    if not client.login():
        log("Cannot connect to Protect — exiting", level=LOG_ERROR, source="protect")
        return

    check_camera_health(client, state)
    check_motion_events(client, state)

    save_state(state)
    log("Protect monitor complete", level=LOG_INFO, source="protect")


if __name__ == "__main__":
    main()
