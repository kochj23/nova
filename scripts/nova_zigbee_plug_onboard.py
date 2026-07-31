#!/usr/bin/env python3
"""nova_zigbee_plug_onboard.py — open the zigbee network and onboard up to N new
metering PLUGS, renaming each to <prefix>_1..N. Once named, nova_zigbee_energy_bridge
auto-streams their power into telemetry.energy -> Grafana (no per-device config).

Usage: nova_zigbee_plug_onboard.py [prefix] [count] [hours]   (default: laundry_plug 2 2)
Re-issues permit_join every 230s (z2m caps a single call at 254s) until done/deadline.
"""
import json
import sys
import time

import paho.mqtt.client as mqtt

sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent))
import nova_config

MQTT_HOST, MQTT_PORT = "127.0.0.1", 1883
REISSUE_EVERY = 230
PREFIX = sys.argv[1] if len(sys.argv) > 1 else "laundry_plug"
COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 2
HOURS = float(sys.argv[3]) if len(sys.argv) > 3 else 2.0

known = set()
assigned = {}   # ieee -> new name


def log(m):
    print(f"[plug-onboard] {time.strftime('%H:%M:%S')} {m}", flush=True)


def slack(m):
    try:
        nova_config.post_both(m, slack_channel=nova_config.SLACK_FEED, discord_channel=None)
    except Exception as e:
        log(f"slack: {e}")


def is_plug(defn):
    for e in (defn or {}).get("exposes", []):
        if isinstance(e, dict):
            if e.get("property") == "power":
                return True
            if any(isinstance(f, dict) and f.get("property") == "power" for f in e.get("features", [])):
                return True
    return False


def reissue(c):
    c.publish("zigbee2mqtt/bridge/request/permit_join", json.dumps({"time": 254}))


def on_connect(c, *_):
    c.subscribe("zigbee2mqtt/bridge/devices")
    c.subscribe("zigbee2mqtt/bridge/event")
    reissue(c)
    log(f"network OPEN; onboarding up to {COUNT} plugs as {PREFIX}_1..{COUNT} over {HOURS}h")
    slack(f":satellite: *Zigbee network opened* — onboarding up to {COUNT} laundry plugs. "
          f"Plug/power them on so they pair.")


def on_message(c, u, msg):
    try:
        data = json.loads(msg.payload)
    except Exception:
        return
    if msg.topic != "zigbee2mqtt/bridge/devices":
        return
    global known
    if not known:
        known = {d.get("ieee_address") for d in data if d.get("ieee_address")}
        log(f"baseline: {len(known)} known devices")
        return
    for d in data:
        ieee = d.get("ieee_address")
        if not ieee or ieee in known or ieee in assigned:
            continue
        if not is_plug(d.get("definition")):
            continue  # not (yet) a metering plug — wait for interview to finish
        if len(assigned) >= COUNT:
            continue
        target = f"{PREFIX}_{len(assigned) + 1}"
        frm = d.get("friendly_name") or ieee
        model = (d.get("definition") or {}).get("model")
        c.publish("zigbee2mqtt/bridge/request/device/rename", json.dumps({"from": frm, "to": target}))
        assigned[ieee] = target
        log(f"renamed {frm} -> {target} (model {model})")
        slack(f":electric_plug: *New laundry plug onboarded* -> `{target}` "
              f"(model {model}, ieee {ieee}). Power now streaming to telemetry.energy / Grafana.")


def main():
    deadline = time.time() + HOURS * 3600
    c = mqtt.Client()
    c.on_connect = on_connect
    c.on_message = on_message
    c.connect(MQTT_HOST, MQTT_PORT, 60)
    c.loop_start()
    while time.time() < deadline and len(assigned) < COUNT:
        time.sleep(REISSUE_EVERY)
        reissue(c)
        log(f"permit_join re-issued ({int((deadline-time.time())/60)} min left, {len(assigned)}/{COUNT} done)")
    c.publish("zigbee2mqtt/bridge/request/permit_join", json.dumps({"time": 0}))
    time.sleep(1); c.loop_stop()
    log(f"done — {len(assigned)}/{COUNT} onboarded; network closed")
    slack(f":checkered_flag: *Laundry plug onboarding finished* — {len(assigned)}/{COUNT} adopted"
          + (f" ({', '.join(assigned.values())})" if assigned else "") + ". Network closed.")


if __name__ == "__main__":
    main()
