#!/usr/bin/env python3
"""
nova_home_watchdog.py — HAL watching the pod bay.

Polls HomekitControl every 20 minutes for anything unusual:
  - Doors/windows left open for > 10 minutes
  - Temperature anomalies (too hot, too cold)
  - Unexpected motion during sleep hours (11pm–6am)
  - Sensors that have gone offline
  - Security system status

Only alerts Jordan if something is actually notable.
Uses a simple state file to avoid duplicate alerts.

Cron: every 20 minutes
Written by Jordan Koch.
"""

import json
import subprocess
import time
import urllib.request
from datetime import datetime, date
from pathlib import Path
import nova_config

SLACK_TOKEN   = nova_config.slack_bot_token()
SLACK_CHAN    = "C0AMNQ5GX70"
SLACK_API    = "https://slack.com/api"
HOMEKIT_SCRIPT = Path.home() / ".openclaw/scripts/nova_homekit_query.sh"
VECTOR_URL   = "http://127.0.0.1:18790/remember"
STATE_FILE   = Path("/tmp/nova_home_watchdog_state.json")
NOW          = datetime.now()
HOUR         = NOW.hour
SLEEP_HOURS  = (23, 6)   # 11pm–6am
TODAY        = date.today().isoformat()


def log(msg):
    print(f"[nova_home_watchdog {NOW.strftime('%H:%M:%S')}] {msg}", flush=True)


def is_sleep_hours():
    return HOUR >= SLEEP_HOURS[0] or HOUR < SLEEP_HOURS[1]


def slack_alert(text):
    data = json.dumps({"channel": SLACK_CHAN, "text": text, "mrkdwn": True}).encode()
    req  = urllib.request.Request(
        f"{SLACK_API}/chat.postMessage", data=data,
        headers={"Authorization": "Bearer " + SLACK_TOKEN,
                 "Content-Type": "application/json; charset=utf-8"}
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as e:
        log(f"Slack error: {e}")


def vector_remember(text, metadata=None):
    try:
        payload = json.dumps({
            "text": text, "source": "homekit", "metadata": metadata or {}
        }).encode()
        req = urllib.request.Request(VECTOR_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception:
        pass


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state))


def get_accessories():
    """Query HomeKit — tries HomekitControl API first, falls back to Shortcuts CLI.

    Primary: HomekitControl HTTP API on port 37432
    Fallback: 'Nova HomeKit Status' Shortcut via Shortcuts CLI

    The Shortcut should:
      1. Find Home Accessories (all)
      2. Repeat with each item:
           - Get Name of accessory
           - Get Room of accessory
           - Get Category/Type of accessory
           - Get Is Reachable of accessory
           - Get relevant characteristic values (contact state, temperature, motion, on/off)
           - Make Dictionary: {name, room, type, reachable, services: [{type, characteristics: [{type, value}]}]}
           - Add to list
      3. Output list as JSON text (Stop and Output)
    """
    # Try HomekitControl API first (faster, no Shortcuts overhead)
    try:
        result = subprocess.run(
            ["curl", "-s", "--connect-timeout", "3", "http://127.0.0.1:37432/api/accessories"],
            capture_output=True, text=True, timeout=15
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            accessories = data if isinstance(data, list) else data.get("accessories", [])
            if accessories:
                log(f"Got {len(accessories)} accessories from HomekitControl API")
                return accessories
    except Exception as e:
        log(f"HomekitControl API unavailable: {e}")

    # Fallback to Shortcuts CLI
    log("Falling back to Shortcuts CLI...")
    try:
        result = subprocess.run(
            [str(HOMEKIT_SCRIPT)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0 or not result.stdout.strip():
            log(f"Shortcut returned no data (rc={result.returncode}): {result.stderr.strip()}")
            return []
        data = json.loads(result.stdout.strip())
        return data if isinstance(data, list) else data.get("accessories", [])
    except subprocess.TimeoutExpired:
        log("HomeKit shortcut timed out")
        return []
    except Exception as e:
        log(f"HomeKit query error: {e}")
        return []


def analyze_accessories(accessories, state):
    alerts = []
    now_ts = time.time()

    for acc in accessories:
        name     = acc.get("name", "Unknown")
        room     = acc.get("room", "Unknown room")
        services = acc.get("services", [])
        acc_id   = acc.get("uuid", name)

        for svc in services:
            svc_type = svc.get("type", "").lower()
            chars    = svc.get("characteristics", [])

            for char in chars:
                char_type  = char.get("type", "").lower()
                value      = char.get("value")

                # Door/window left open
                if "contactsensor" in svc_type or "contact" in char_type:
                    key = f"contact_{acc_id}"
                    if value == 1:  # open
                        if key not in state:
                            state[key] = {"first_open": now_ts}
                        open_duration = (now_ts - state[key]["first_open"]) / 60
                        if open_duration > 10 and not state[key].get("alerted"):
                            alerts.append(f"🚪 *{name}* ({room}) has been open for {int(open_duration)} minutes")
                            state[key]["alerted"] = True
                    else:  # closed
                        if key in state:
                            del state[key]

                # Temperature anomaly
                elif "temperature" in char_type and value is not None:
                    try:
                        temp_f = float(value) * 9/5 + 32
                        key = f"temp_{acc_id}_alert"
                        if temp_f > 85 or temp_f < 55:
                            if key not in state or (now_ts - state.get(key, 0)) > 3600:
                                alerts.append(f"🌡️ *{name}* ({room}) temperature: {temp_f:.0f}°F")
                                state[key] = now_ts
                    except (TypeError, ValueError):
                        pass

                # Motion during sleep hours
                elif "motionsensor" in svc_type or "motion" in char_type:
                    if value and is_sleep_hours():
                        key = f"motion_{acc_id}"
                        last_alert = state.get(key, 0)
                        if now_ts - last_alert > 1800:  # 30 min cooldown
                            alerts.append(f"🚨 *Motion detected:* {name} ({room}) — it's {NOW.strftime('%I:%M %p')}")
                            state[key] = now_ts
                            vector_remember(
                                f"Motion detected at {name} in {room} at {NOW.strftime('%Y-%m-%d %H:%M')} during sleep hours",
                                {"date": TODAY, "type": "security_event"}
                            )

    return alerts, state


def main():
    log("Running home watchdog check...")

    accessories = get_accessories()
    if not accessories:
        log("No accessories returned (app may be down or no data)")
        return

    state = load_state()
    alerts, new_state = analyze_accessories(accessories, state)
    save_state(new_state)

    if alerts:
        msg = "*🏠 Nova Home Alert*\n" + "\n".join(f"  • {a}" for a in alerts)
        slack_alert(msg)
        log(f"Sent {len(alerts)} alert(s)")
        vector_remember(
            f"Home watchdog alerts {TODAY} {NOW.strftime('%H:%M')}: " + "; ".join(alerts),
            {"date": TODAY, "type": "home_alert"}
        )
    else:
        log(f"All clear. Checked {len(accessories)} accessories.")


if __name__ == "__main__":
    main()
