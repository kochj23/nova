#!/usr/bin/env python3
"""
nova_nightly_protect.py — Nightly UniFi Protect camera digest.

Runs at 11:45 PM via launchd. Summarizes today's exterior camera activity:
camera health, smart detection counts, notable events.

STRICT: Only reports on non-Interior cameras.

Written by Jordan Koch.
"""

import json
import os
import ssl
import subprocess
import sys
import urllib.request
import http.cookiejar
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config
from nova_logger import log, LOG_INFO, LOG_ERROR
from nova_protect_monitor import ProtectClient

SLACK_CHAN = nova_config.SLACK_NOTIFY
INTERIOR_PREFIX = "Interior"
TODAY = datetime.now().strftime("%A, %B %d")


def slack_post(text):
    token = nova_config.slack_bot_token()
    if not token:
        return
    try:
        payload = json.dumps({"channel": SLACK_CHAN, "text": text}).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage", data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}
        )
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


def main():
    log("Nightly Protect digest starting", level=LOG_INFO, source="nightly_protect")

    client = ProtectClient()
    if not client.login():
        slack_post(":red_circle: *Nightly Protect Report* — Cannot connect to UNVR")
        return

    # Get camera info
    bootstrap = client.get_bootstrap()
    if not bootstrap:
        slack_post(":red_circle: *Nightly Protect Report* — Bootstrap failed")
        return

    cameras = bootstrap.get("cameras", [])
    exterior = [c for c in cameras if not c.get("name", "").startswith(INTERIOR_PREFIX)]
    connected = [c for c in exterior if c.get("state") == "CONNECTED"]
    disconnected = [c for c in exterior if c.get("state") != "CONNECTED"]

    # NVR info
    nvr = bootstrap.get("nvr", {})
    nvr_uptime = nvr.get("uptime", 0)
    nvr_uptime_d = nvr_uptime // 86400
    nvr_uptime_h = (nvr_uptime % 86400) // 3600
    storage_used = nvr.get("storageInfo", {}).get("totalSize", 0)
    storage_cap = nvr.get("storageInfo", {}).get("totalCapacity", 0)
    storage_pct = (storage_used / storage_cap * 100) if storage_cap else 0
    fw = nvr.get("firmwareVersion", "?")

    lines = [f"*:camera: Nightly Protect Report — {TODAY}*", ""]

    # NVR Status
    lines.append(f"*NVR:* Firmware {fw} / Uptime: {nvr_uptime_d}d {nvr_uptime_h}h")
    if storage_cap:
        storage_bar = "█" * int(storage_pct // 10) + "░" * (10 - int(storage_pct // 10))
        lines.append(f"  Storage: {storage_bar} {storage_pct:.0f}% ({storage_used/1024**4:.1f}TB / {storage_cap/1024**4:.1f}TB)")
    lines.append("")

    # Camera Health
    lines.append(f"*Cameras:* {len(connected)}/{len(exterior)} online")
    if disconnected:
        for c in disconnected:
            lines.append(f"  :red_circle: {c.get('name','?')} — OFFLINE ({c.get('type','')})")
    lines.append("")

    # Get today's events (exterior only)
    now = datetime.now(timezone.utc)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_ms = int(start_of_day.timestamp() * 1000)

    exterior_ids = {c["id"]: c["name"] for c in exterior}
    all_events = client.get_events(limit=100) or []
    events = [e for e in all_events if e.get("start", 0) >= start_ms]

    # Count events by type and camera (exterior only)
    smart_counts = {}  # {camera: {type: count}}
    motion_counts = {}  # {camera: count}
    total_events = 0

    for event in events:
        cam_id = event.get("camera", "")
        if cam_id not in exterior_ids:
            continue

        total_events += 1
        cam_name = exterior_ids[cam_id]
        event_type = event.get("type", "")
        smart_types = event.get("smartDetectTypes", [])

        if smart_types:
            smart_counts.setdefault(cam_name, {})
            for st in smart_types:
                smart_counts[cam_name][st] = smart_counts[cam_name].get(st, 0) + 1
        elif event_type == "motion":
            motion_counts[cam_name] = motion_counts.get(cam_name, 0) + 1

    # Smart detection summary
    lines.append(f"*Today's Activity:* {total_events} events on exterior cameras")

    if smart_counts:
        # Aggregate totals
        type_totals = {}
        for cam, types in smart_counts.items():
            for t, count in types.items():
                type_totals[t] = type_totals.get(t, 0) + count

        emoji_map = {"person": ":bust_in_silhouette:", "vehicle": ":car:",
                     "animal": ":dog:", "package": ":package:",
                     "alrmSmoke": ":fire:", "alrmSiren": ":rotating_light:"}
        for t, count in sorted(type_totals.items(), key=lambda x: -x[1]):
            emoji = emoji_map.get(t, ":grey_question:")
            lines.append(f"  {emoji} {t}: {count}")

        # Per-camera breakdown for smart detections
        lines.append("")
        lines.append("*Smart detections by camera:*")
        for cam_name in sorted(smart_counts.keys()):
            types = smart_counts[cam_name]
            type_str = ", ".join(f"{t}:{c}" for t, c in sorted(types.items(), key=lambda x: -x[1]))
            lines.append(f"  {cam_name}: {type_str}")

    # Top motion cameras
    if motion_counts:
        lines.append("")
        top_motion = sorted(motion_counts.items(), key=lambda x: -x[1])[:5]
        lines.append("*Top motion cameras:*")
        for cam, count in top_motion:
            lines.append(f"  {cam}: {count} events")

    msg = "\n".join(lines)
    slack_post(msg)
    log(f"Nightly Protect digest posted ({total_events} events)", level=LOG_INFO, source="nightly_protect")

    # Store in memory
    try:
        summary = f"Protect report {TODAY}: {len(connected)}/{len(exterior)} cameras online, {total_events} events."
        if smart_counts:
            summary += " Smart: " + ", ".join(f"{t}:{c}" for t, c in sorted(type_totals.items(), key=lambda x: -x[1]))
        payload = json.dumps({
            "text": summary,
            "source": "security",
            "metadata": {"type": "protect_nightly", "date": datetime.now().isoformat()}
        }).encode()
        req = urllib.request.Request(nova_config.VECTOR_URL, data=payload,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception:
        pass


if __name__ == "__main__":
    main()
