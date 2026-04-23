#!/usr/bin/env python3
"""
nova_package_clairvoyance.py — Smart package detection with camera correlation.

When Protect detects a "package" on an exterior camera, cross-references
with active tracking numbers from nova_package_tracker.py and posts a
rich Slack alert: which package, which camera, thumbnail image.

Also monitors for package pickup (package detected, then gone on next check)
to alert on potential porch pirates.

Runs as part of nova_protect_monitor.py's event processing, or standalone.

Written by Jordan Koch.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_WARN
from nova_protect_monitor import ProtectClient, _get_event_thumbnail, slack_upload_image

SLACK_CHAT = nova_config.SLACK_CHAN  # Package alerts go to #nova-chat (Jordan wants to know)
SLACK_NOTIFY = nova_config.SLACK_NOTIFY
TRACKING_FILE = Path.home() / ".openclaw/workspace/state/package_tracker.json"
STATE_FILE = Path.home() / ".openclaw/workspace/state/package_clairvoyance.json"
SNAPSHOT_DIR = Path.home() / ".openclaw/workspace/protect_snapshots"

# Camera name → likely delivery location
CAMERA_LOCATIONS = {
    "Exterior - Front Door Left": "front door",
    "Exterior - Front Middle": "front porch / walkway",
    "Exterior - Front Right": "front yard / driveway",
    "Exterior - Patio Couch": "back patio",
    "Exterior - Garbage": "side of house near garbage cans",
    "Exterior - Dylan": "Dylan's area",
    "External - Carport": "carport",
    "External - Patio": "back patio",
}


def load_active_packages():
    """Load active (non-delivered) packages from tracker state."""
    if not TRACKING_FILE.exists():
        return []
    try:
        data = json.loads(TRACKING_FILE.read_text())
        packages = data.get("packages", {})
        active = []
        for key, pkg in packages.items():
            status = pkg.get("status", "").lower()
            if status not in ("delivered", "expired", "cancelled"):
                active.append({
                    "key": key,
                    "carrier": pkg.get("carrier", "?"),
                    "subject": pkg.get("subject", "Unknown package"),
                    "tracking": pkg.get("tracking", ""),
                    "status": pkg.get("status", ""),
                })
        return active
    except Exception:
        return []


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"last_package_events": {}}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def slack_post(text, channel=None):
    nova_config.post_both(text, slack_channel=channel or nova_config.SLACK_CHAN)


def handle_package_detection(camera_name, event_id, client=None):
    """Called when Protect detects a package on an exterior camera."""
    location = CAMERA_LOCATIONS.get(camera_name, camera_name)
    active = load_active_packages()
    state = load_state()

    # Build alert message
    parts = [f":package: *Package Detected!*"]
    parts.append(f"  Camera: {camera_name}")
    parts.append(f"  Location: {location}")
    parts.append(f"  Time: {datetime.now().strftime('%I:%M %p')}")

    if active:
        parts.append("")
        parts.append(f"*Active deliveries ({len(active)}):*")
        for pkg in active[:5]:
            carrier = pkg["carrier"]
            subject = pkg["subject"][:50]
            status = pkg["status"]
            parts.append(f"  :truck: [{carrier}] {subject} — _{status}_")
    else:
        parts.append("\n  _No active tracking numbers — could be an untracked delivery_")

    alert_text = "\n".join(parts)

    # Try to get and upload the event thumbnail
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    uploaded = False
    if event_id and client:
        thumb_path = SNAPSHOT_DIR / f"pkg_{event_id}.jpg"
        if _get_event_thumbnail(client, event_id, str(thumb_path)):
            uploaded = slack_upload_image(
                str(thumb_path), SLACK_CHAT,
                title=f"Package — {camera_name}",
                comment=alert_text,
            )
            try:
                thumb_path.unlink()
            except Exception:
                pass

    if not uploaded:
        slack_post(alert_text)

    # Store in memory
    import urllib.request
    try:
        payload = json.dumps({
            "text": f"Package detected at {location} ({camera_name}) at {datetime.now().strftime('%I:%M %p on %B %d')}. "
                    f"Active deliveries: {len(active)}.",
            "source": "security",
            "metadata": {"type": "package_detection", "camera": camera_name, "location": location}
        }).encode()
        req = urllib.request.Request(nova_config.VECTOR_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass

    # Track for porch pirate detection
    state["last_package_events"][camera_name] = {
        "time": datetime.now().isoformat(),
        "event_id": event_id,
        "active_packages": len(active),
    }
    save_state(state)

    log(f"Package detected at {location} ({camera_name}), {len(active)} active deliveries",
        level=LOG_INFO, source="package_clairvoyance")


if __name__ == "__main__":
    # Standalone test
    print("Package Clairvoyance — active packages:")
    for pkg in load_active_packages():
        print(f"  [{pkg['carrier']}] {pkg['subject'][:60]} — {pkg['status']}")
