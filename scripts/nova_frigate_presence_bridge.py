#!/usr/bin/env python3
"""
nova_frigate_presence_bridge.py — turn Frigate object detections into Nova
presence (#636). Subscribes to Frigate's MQTT `frigate/events`, maps the camera
to a room, and INSERTs into telemetry.presence with method='camera_vision'
(person) or 'vehicle_vision' (car/truck). nova_presence_engine already consumes
those rows (get_camera_presence / get_vehicle_presence) — so no engine change is
needed; this is purely the producer side.

Read-only against Frigate. Exterior cameras map to outdoor zones (perimeter
presence), interior cameras to real rooms. A short per-(camera,label) throttle
keeps Frigate's chatty update events from flooding the table.

launchd: net.digitalnoise.frigate-presence-bridge (on .6, next to the broker).
NOTE: requires Frigate mqtt.enabled: true pointing at the fleet broker (.6:1883).
"""
import json
import time

import paho.mqtt.client as mqtt
import psycopg2

DSN = "host=127.0.0.1 dbname=nova_ops user=kochj"
MQTT_HOST, MQTT_PORT = "127.0.0.1", 1883
EVENTS_TOPIC = "frigate/events"
MIN_INTERVAL_S = 20  # min seconds between inserts per (camera,label)

# camera -> room/zone. Interior cams drive room-level occupancy; exterior cams
# are outdoor zones (perimeter), which the presence engine must NOT collapse to
# "home" (it already guards on this). A few interior mappings are best-guess —
# confirm with Jordan (see design-636 doc).
CAMERA_ROOM = {
    "front_door": "entry", "front_door_patio": "entry", "interior_front_door": "entry",
    "front_yard": "front_yard", "front_yard_alt": "front_yard", "exterior_front_right": "front_yard",
    "carport": "carport", "garage": "garage",
    "alley_north": "alley", "alley_south": "alley", "exterior_garbage": "alley",
    "back_patio": "back_yard", "patio_1": "back_yard", "patio_2": "back_yard",
    "back_1_unas": "back_yard", "abundio_boundary": "boundary",
    "interior_living_room": "living_room", "interior_living_room_b": "living_room",
    "interior_lr_front": "living_room", "interior_kitchen_alley": "kitchen",
    "3d_printers": "office", "interior_printer_3d": "office", "interior_printers": "office",
}
PERSON_LABELS = {"person"}
VEHICLE_LABELS = {"car", "truck", "motorcycle", "bus"}

_conn = None
_last = {}  # (camera,label) -> last insert epoch


def _db():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(DSN)
        _conn.autocommit = True
    return _conn


def _drop_conn():
    # psycopg2 only flags .closed on a client-side close; a server-side drop
    # leaves it looking open and every execute fails silently. Null it so the
    # next message reconnects (lesson from the zigbee energy bridge gap).
    global _conn
    try:
        if _conn is not None and not _conn.closed:
            _conn.close()
    except Exception:
        pass
    _conn = None


def _insert(room, person, conf, method, cam, label):
    with _db().cursor() as cur:
        cur.execute(
            "INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata) "
            "VALUES (now(), %s, %s, %s, %s, %s)",
            (person, room, conf, method,
             json.dumps({"source": "frigate", "camera": cam, "label": label})))


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
    except Exception:
        return
    e = payload.get("after") or payload.get("before") or {}
    if payload.get("type") not in ("new", "update"):
        return
    cam = e.get("camera")
    label = e.get("label")
    room = CAMERA_ROOM.get(cam)
    if not room or not label:
        return
    if label in PERSON_LABELS:
        method, person = "camera_vision", "unknown"
    elif label in VEHICLE_LABELS:
        method, person = "vehicle_vision", "vehicle"
    else:
        return
    conf = float(e.get("top_score") or e.get("score") or 0.7)
    now = time.time()
    key = (cam, label)
    if now - _last.get(key, 0) < MIN_INTERVAL_S:
        return
    _last[key] = now
    try:
        _insert(room, person, conf, method, cam, label)
    except Exception:
        _drop_conn()
        try:
            _insert(room, person, conf, method, cam, label)
        except Exception as e2:
            print(f"[frigate-presence] write error {cam}/{label} (reconnect failed): {e2}", flush=True)
            _drop_conn()


def main():
    for _ in range(30):
        try:
            _db()
            break
        except Exception:
            time.sleep(2)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.subscribe(EVENTS_TOPIC)
    print(f"[frigate-presence] subscribing {EVENTS_TOPIC} @ {MQTT_HOST}:{MQTT_PORT} -> telemetry.presence", flush=True)
    client.loop_forever()


if __name__ == "__main__":
    main()
