#!/usr/bin/env python3
"""nova_zigbee_onboard.py — keep the zigbee network open and auto-onboard a new sensor.

One-shot helper (runs ~6h then closes the network). It:
  - re-issues permit_join every 230s (z2m caps a single call at 254s) until the deadline,
  - watches bridge/event for a NEW device joining,
  - renames the first new temp-exposing device to a target name (default garage_temp),
  - posts to #nova-info when it joins.

Usage: nova_zigbee_onboard.py [target_name] [hours]   (defaults: garage_temp 6)
Written by Jordan Koch (via Claude).
"""
import json
import sys
import time

import paho.mqtt.client as mqtt

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import nova_config

MQTT_HOST, MQTT_PORT = "127.0.0.1", 1883
REISSUE_EVERY = 230          # < z2m's 254s cap, with margin
TARGET = sys.argv[1] if len(sys.argv) > 1 else "garage_temp"
HOURS = float(sys.argv[2]) if len(sys.argv) > 2 else 6.0

# ieee addresses already on the network at launch — anything else is "new".
KNOWN = set()
joined = {"done": False}


def log(m):
    print(f"[onboard] {time.strftime('%H:%M:%S')} {m}", flush=True)


def slack(msg):
    try:
        nova_config.post_both(msg, slack_channel=nova_config.SLACK_INFO, discord_channel=None)
        log("posted to #nova-info")
    except Exception as e:
        log(f"slack post failed: {e}")


def reissue(client):
    client.publish("zigbee2mqtt/bridge/request/permit_join", json.dumps({"time": 254}))


def on_connect(client, *_):
    client.subscribe("zigbee2mqtt/bridge/devices")
    client.subscribe("zigbee2mqtt/bridge/event")
    reissue(client)
    log(f"connected; network open, target='{TARGET}', will keep open {HOURS}h")


def _exposes_temp(definition):
    for e in (definition or {}).get("exposes", []):
        if isinstance(e, dict) and (e.get("property") == "temperature"
                                    or any(f.get("property") == "temperature"
                                           for f in e.get("features", []) if isinstance(f, dict))):
            return True
    return False


def on_message(client, userdata, msg):
    if joined["done"]:
        return
    try:
        payload = json.loads(msg.payload)
    except Exception:
        return

    # seed KNOWN from the initial device list
    if msg.topic.endswith("bridge/devices") and not KNOWN:
        for d in payload:
            if d.get("type") != "Coordinator":
                KNOWN.add(d.get("ieee_address"))
        log(f"baseline: {len(KNOWN)} known devices")
        return

    if not msg.topic.endswith("bridge/event"):
        return
    if payload.get("type") not in ("device_interview", "device_joined"):
        return
    data = payload.get("data", {})
    ieee = data.get("ieee_address")
    if not ieee or ieee in KNOWN:
        return
    status = data.get("status")
    if payload["type"] == "device_interview" and status != "successful":
        log(f"interview {status} for {ieee} ...")
        return

    defn = data.get("definition") or {}
    desc = f"{defn.get('vendor','?')} {defn.get('model','?')} — {defn.get('description','')}"
    log(f"NEW device joined: {ieee} ({desc})")

    # rename to target (use the current friendly_name, which defaults to the ieee)
    frm = data.get("friendly_name") or ieee
    client.publish("zigbee2mqtt/bridge/request/device/rename",
                   json.dumps({"from": frm, "to": TARGET}))
    has_temp = _exposes_temp(defn)
    joined["done"] = True
    slack(f":thermometer: New Zigbee sensor onboarded: *{TARGET}* "
          f"({desc}, `{ieee}`). Temperature: {'yes' if has_temp else 'no'}. "
          f"Now feeding `telemetry.climate` (room=garage) for Grafana.")
    log(f"renamed {frm} -> {TARGET}; onboarding complete (network stays open until deadline)")


def main():
    deadline = time.time() + HOURS * 3600
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(MQTT_HOST, MQTT_PORT, 60)
    c.loop_start()
    last = time.time()
    try:
        while time.time() < deadline:
            time.sleep(5)
            if time.time() - last >= REISSUE_EVERY:
                reissue(c)
                last = time.time()
                rem = int((deadline - time.time()) / 60)
                log(f"permit_join re-issued ({rem} min left)" + (" [device already onboarded]" if joined["done"] else ""))
    finally:
        c.publish("zigbee2mqtt/bridge/request/permit_join", json.dumps({"time": 0}))
        time.sleep(1)
        c.loop_stop()
        log("deadline reached — network closed")


if __name__ == "__main__":
    main()
