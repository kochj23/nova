#!/usr/bin/env python3
"""
nova_zigbee_presence_bridge.py — bridge Aqara FP300 (Zigbee/zigbee2mqtt) presence +
climate into Nova's existing pipelines.

The FP2 sensors reach Nova via an HTTP webhook (HomeKit). The FP300s pair over
Zigbee instead, so this subscribes to their MQTT topics and writes:
  - presence (mmWave) -> telemetry.presence (method='mmwave') + presence_state
    (exactly what nova_mmwave_poller does, so nova_presence_engine fuses it).
  - temperature/humidity/illuminance -> telemetry.climate (source='fp300').

Adding a new FP300: pair it, rename it in zigbee2mqtt to <room>_presence, then add
one line to ZIGBEE_PRESENCE below.
"""
import json
import time

import paho.mqtt.client as mqtt
import psycopg2

try:
    from nova_notify import notify          # central bus -> telemetry.events -> Slack
except Exception:
    notify = None

DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"
MQTT_HOST, MQTT_PORT = "127.0.0.1", 1883

# zigbee2mqtt friendly_name -> Nova room. Add each FP300 here as it's set up.
ZIGBEE_PRESENCE = {
    "master_bedroom_presence": "master_bedroom",
    "dylans_room_presence": "dylans_room",
    "office_presence": "office",
    "living_room_presence": "living_room",
    "patio_presence": "patio",
}

PRESENCE_CONFIDENCE = 0.85  # FP300 mmWave is a high-confidence indoor signal

# Slack info ping on presence. mmWave fires constantly, so we only ping on an
# ARRIVAL (absent/unknown -> present) and at most once per room per cooldown.
PRESENCE_NOTIFY_COOLDOWN = 300  # seconds between pings for the same room
_last_present: dict[str, bool] = {}
_last_notified: dict[str, float] = {}


def maybe_notify_presence(room: str, present: bool):
    """Send an info-level Slack ping on arrival transitions, rate-limited per room."""
    was = _last_present.get(room)
    _last_present[room] = present
    if not present or was:           # only on the absent/unknown -> present edge
        return
    now = time.time()
    if now - _last_notified.get(room, 0) < PRESENCE_NOTIFY_COOLDOWN:
        return
    _last_notified[room] = now
    if notify is None:
        return
    pretty = room.replace("_", " ").title()
    try:
        notify(f"Presence detected — {pretty}",
               body=f"mmWave presence picked up in {pretty}.",
               level="info", category="presence",
               source="nova_zigbee_presence_bridge.py",
               meta={"room": room}, dedup_key=f"presence:{room}")
    except Exception as e:
        print(f"[fp300-bridge] notify failed for {room}: {e}", flush=True)
_conn = None


def _db():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(DSN)
        _conn.autocommit = True
    return _conn


def _c_to_f(c):
    try:
        return round(float(c) * 9 / 5 + 32, 1)
    except (TypeError, ValueError):
        return None


def write_presence(room, present, payload):
    """Mirror nova_mmwave_poller.write_presence_sync: telemetry.presence + presence_state."""
    meta = {"source": "fp300", "pir": payload.get("pir_detection"),
            "target_distance": payload.get("target_distance"),
            "illuminance": payload.get("illuminance")}
    with _db().cursor() as cur:
        cur.execute(
            "INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata) "
            "VALUES (now(), 'jordan', %s, %s, 'mmwave', %s)",
            (room, PRESENCE_CONFIDENCE if present else 0.0, json.dumps(meta)))
        if present:
            cur.execute(
                "INSERT INTO presence_state (person, room, confidence, source, activity_state, "
                "entered_at, last_confirmed) "
                "VALUES ('jordan', %s, %s, 'fp300', 'present', now(), now()) "
                "ON CONFLICT (person) DO UPDATE SET room=EXCLUDED.room, "
                "confidence=EXCLUDED.confidence, source=EXCLUDED.source, last_confirmed=now()",
                (room, PRESENCE_CONFIDENCE))


def write_climate(room, payload):
    """temp/humidity/lux -> telemetry.climate (only when present in this message)."""
    temp_f = _c_to_f(payload.get("temperature"))
    hum = payload.get("humidity")
    lux = payload.get("illuminance")
    motion = payload.get("presence")
    if temp_f is None and hum is None and lux is None:
        return
    with _db().cursor() as cur:
        cur.execute(
            "INSERT INTO telemetry.climate (ts, room, source, temp_f, humidity, light_lux, motion) "
            "VALUES (now(), %s, 'fp300', %s, %s, %s, %s)",
            (room, temp_f, hum, lux, bool(motion) if motion is not None else None))


def on_message(client, userdata, msg):
    device = msg.topic.replace("zigbee2mqtt/", "")
    room = ZIGBEE_PRESENCE.get(device)
    if not room:
        return  # not one of our presence sensors
    try:
        payload = json.loads(msg.payload.decode())
    except Exception:
        return
    try:
        if "presence" in payload:
            present = bool(payload["presence"])
            write_presence(room, present, payload)
            maybe_notify_presence(room, present)
        write_climate(room, payload)
    except Exception as e:
        print(f"[fp300-bridge] write error for {device}/{room}: {e}", flush=True)


def main():
    # wait for PG
    for _ in range(30):
        try:
            _db(); break
        except Exception:
            time.sleep(2)
    client = mqtt.Client()
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    for dev in ZIGBEE_PRESENCE:
        client.subscribe(f"zigbee2mqtt/{dev}")
    print(f"[fp300-bridge] watching {len(ZIGBEE_PRESENCE)} FP300(s): "
          f"{', '.join(ZIGBEE_PRESENCE.values())}", flush=True)
    client.loop_forever()


if __name__ == "__main__":
    main()
