#!/usr/bin/env python3
"""
nova_zigbee_energy_bridge.py — stream ThirdReality (and any) Zigbee smart-plug
power readings from zigbee2mqtt into telemetry.energy, so the plugs' whole point
(per-room power monitoring) shows up in Grafana alongside the Eve Energy plugs.

Subscribes to all zigbee2mqtt device topics; any payload carrying both `power`
and `voltage` is a metering plug -> insert (watts/volts/amps/kwh/state). Light
per-device throttle so chatty plugs don't flood the table. Never raises.

New plugs are picked up automatically (no per-device config) — pair it, and its
power starts flowing.
"""
import json
import time

import paho.mqtt.client as mqtt
import psycopg2

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
MQTT_HOST, MQTT_PORT = "127.0.0.1", 1883
MIN_INTERVAL_S = 15          # min seconds between inserts per device
_conn = None
_last = {}                   # device -> last insert epoch


def _db():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(DSN)
        _conn.autocommit = True
    return _conn


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _drop_conn():
    # psycopg2 only sets _conn.closed on a CLIENT-side close; a server-side drop
    # (PG restart, network blip, idle timeout) leaves .closed == 0 and every
    # subsequent execute fails silently. Null it so the next message reconnects.
    global _conn
    try:
        if _conn is not None and not _conn.closed:
            _conn.close()
    except Exception:
        pass
    _conn = None


def _insert(device, p):
    with _db().cursor() as cur:
        cur.execute(
            "INSERT INTO telemetry.energy "
            "(ts, device_id, device_name, watts, volts, amps, kwh_total, on_state) "
            "VALUES (now(), %s, %s, %s, %s, %s, %s, %s)",
            (device, device, _f(p.get("power")), _f(p.get("voltage")),
             _f(p.get("current")), _f(p.get("energy")),
             (str(p.get("state")).upper() == "ON") if p.get("state") is not None else None))


def on_message(client, userdata, msg):
    device = msg.topic.replace("zigbee2mqtt/", "")
    if "/" in device or device.startswith("bridge"):
        return  # sub-topics (/availability, /get) and bridge events
    try:
        p = json.loads(msg.payload.decode())
    except Exception:
        return
    # a metering plug reports both power and voltage
    if not isinstance(p, dict) or "power" not in p or "voltage" not in p:
        return
    now = time.time()
    if now - _last.get(device, 0) < MIN_INTERVAL_S:
        return
    _last[device] = now
    try:
        _insert(device, p)
    except Exception as e:
        # Drop the (likely broken) connection and retry once on a fresh one so a
        # transient PG drop costs at most one reading, not a silent multi-hour gap.
        _drop_conn()
        try:
            _insert(device, p)
        except Exception as e2:
            print(f"[zigbee-energy] write error for {device} (reconnect failed): {e2}", flush=True)
            _drop_conn()


def main():
    for _ in range(30):
        try:
            _db(); break
        except Exception:
            time.sleep(2)
    client = mqtt.Client()
    client.on_message = on_message
    # RE-SUBSCRIBE ON EVERY (RE)CONNECT. loop_forever() auto-reconnects after a network
    # drop, but the broker forgets our subscription on disconnect — without an on_connect
    # handler the client stays connected yet receives NO messages, so telemetry.energy
    # silently stops while the process looks alive (the recurring wedge that keeps paging
    # nova-warning). Subscribing inside on_connect fixes it for every reconnect. 2026-09-09.
    def on_connect(c, _userdata, _flags, rc):
        c.subscribe("zigbee2mqtt/+")
        print(f"[zigbee-energy] (re)connected rc={rc}, subscribed zigbee2mqtt/+", flush=True)
    client.on_connect = on_connect
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    print("[zigbee-energy] streaming Zigbee plug power -> telemetry.energy", flush=True)
    client.loop_forever()


if __name__ == "__main__":
    main()
