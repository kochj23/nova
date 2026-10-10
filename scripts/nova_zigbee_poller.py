#!/usr/bin/env python3
"""Nova Zigbee Energy Poller — subscribes to zigbee2mqtt MQTT topics, writes energy_readings."""

import sys
sys.stdout.reconfigure(line_buffering=True)

import json
import time
import psycopg2
import paho.mqtt.client as mqtt

MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
MQTT_TOPIC = "zigbee2mqtt/+"
import nova_dsn as _nova_dsn  # noqa: E402
DB_DSN = _nova_dsn.pg_dsn("nova_ops")


def get_conn():
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    return conn


def wait_for_pg():
    while True:
        try:
            return get_conn()
        except psycopg2.OperationalError:
            print("[nova-zigbee-poller] Waiting for PostgreSQL...")
            time.sleep(5)


conn = wait_for_pg()


def on_message(client, userdata, msg):
    global conn
    try:
        topic = msg.topic
        device_name = topic.replace("zigbee2mqtt/", "")

        if device_name in ("bridge", "bridge/state", "bridge/info", "bridge/logging"):
            return

        payload = json.loads(msg.payload)

        # temperature-bearing sensors -> telemetry.climate (Aqara reports °C; table is °F)
        temp_c = payload.get("temperature")
        if temp_c is not None:
            # ponytail: derive room by stripping a trailing _temp/_sensor/_climate suffix
            room = device_name
            for suf in ("_temp", "_sensor", "_climate"):
                if room.endswith(suf):
                    room = room[: -len(suf)]
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO telemetry.climate (ts, room, source, temp_f, humidity) "
                        "VALUES (NOW(), %s, 'zigbee', %s, %s)",
                        (room, temp_c * 9 / 5 + 32, payload.get("humidity")),
                    )
                print(f"[nova-zigbee-poller] climate {room}: {temp_c*9/5+32:.1f}F "
                      f"{payload.get('humidity')}%")
            except psycopg2.OperationalError:
                conn = get_conn()

        watts = payload.get("power")
        voltage = payload.get("voltage")
        amperes = payload.get("current")
        total_kwh = payload.get("energy")
        relay_on = payload.get("state")

        if watts is None and voltage is None and total_kwh is None:
            return

        if relay_on is not None:
            relay_on = relay_on == "ON"

        device_id = f"zigbee-{payload.get('ieee_address', device_name)}"

        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO energy_readings (ts, device_name, device_id, watts, voltage, amperes, total_kwh, relay_on)
                   VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s)""",
                (device_name, device_id, watts, voltage, amperes, total_kwh, relay_on)
            )
        print(f"[nova-zigbee-poller] {device_name}: {watts}W {voltage}V {total_kwh}kWh")
    except psycopg2.OperationalError:
        try:
            conn = get_conn()
        except Exception:
            pass
    except Exception as e:
        print(f"[nova-zigbee-poller] Error processing {msg.topic}: {e}")


def main():
    print("[nova-zigbee-poller] Starting MQTT subscriber")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.subscribe(MQTT_TOPIC)
    print(f"[nova-zigbee-poller] Subscribed to {MQTT_TOPIC}")
    client.loop_forever()


if __name__ == "__main__":
    main()
