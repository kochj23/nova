#!/usr/bin/env python3
"""Nova Z-Wave Energy Poller — subscribes to zwave-js-ui MQTT topics, writes energy_readings."""

import sys
sys.stdout.reconfigure(line_buffering=True)

import json
import psycopg2
import paho.mqtt.client as mqtt

MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
MQTT_TOPIC = "zwave/+/+/+/+"
DB_DSN = "host=localhost dbname=nova_ops user=kochj"


def get_conn():
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    return conn


def wait_for_pg():
    import time
    while True:
        try:
            return get_conn()
        except psycopg2.OperationalError:
            print("[nova-zwave-poller] Waiting for PostgreSQL...")
            time.sleep(5)


conn = wait_for_pg()


# Z-Wave Meter value IDs: command class 50, endpoint 0, value/<id>
# These numeric IDs encode the meter type and scale
METER_VALUE_MAP = {
    "65537": "watts",      # Electric, W
    "66049": "kwh",        # Electric, kWh
    "66561": "voltage",    # Electric, V
    "66817": "amperes",    # Electric, A
}

# nodeId -> friendly device_name (mirrors names set in zwave-js-ui). Numeric topics carry
# only the node id, so relabel here. Add an entry as each Z-Wave plug is named.
NODE_NAMES = {
    "2": "kitchen_tv",     # Shelly Wave Plug — Kitchen TV
}


def on_message(client, userdata, msg):
    global conn
    try:
        topic = msg.topic
        parts = topic.split("/")
        if len(parts) < 4:
            return

        # zwave-js-ui publishes: zwave/<nodeId>/<command_class>/<endpoint>/value/<valueId>
        device_name = parts[1] if len(parts) > 1 else "unknown"
        device_name = NODE_NAMES.get(device_name, device_name)  # friendly name if known
        command_class = parts[2] if len(parts) > 2 else ""

        # Only care about Meter class (50) or Multilevel Sensor (49)
        if command_class not in ("49", "50", "Meter", "Multilevel_Sensor"):
            if "meter" not in command_class.lower():
                return

        try:
            payload = json.loads(msg.payload)
        except (json.JSONDecodeError, TypeError):
            payload = {"value": msg.payload.decode("utf-8", errors="ignore")}

        value = payload.get("value") if isinstance(payload, dict) else payload

        if value is None:
            return

        try:
            value = float(value)
        except (ValueError, TypeError):
            return

        # Determine which metric this is from the value ID or property name
        value_id = parts[-1] if parts else ""
        metric = METER_VALUE_MAP.get(value_id)

        if not metric:
            # Fallback: try named properties
            prop_lower = value_id.lower()
            if "watt" in prop_lower and "hour" not in prop_lower:
                metric = "watts"
            elif "voltage" in prop_lower or prop_lower == "v":
                metric = "voltage"
            elif "ampere" in prop_lower or "current" in prop_lower:
                metric = "amperes"
            elif "kwh" in prop_lower or "energy" in prop_lower:
                metric = "kwh"

        if not metric:
            return

        watts = value if metric == "watts" else None
        voltage = value if metric == "voltage" else None
        amperes = value if metric == "amperes" else None
        total_kwh = value if metric == "kwh" else None

        device_id = f"zwave-{device_name}"

        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO energy_readings (ts, device_name, device_id, watts, voltage, amperes, total_kwh, relay_on)
                   VALUES (NOW(), %s, %s, %s, %s, %s, %s, NULL)""",
                (device_name, device_id, watts, voltage, amperes, total_kwh)
            )
        print(f"[nova-zwave-poller] node-{device_name}: {metric}={value}")
    except psycopg2.OperationalError:
        try:
            conn = get_conn()
        except Exception:
            pass
    except Exception as e:
        print(f"[nova-zwave-poller] Error: {e}")


def main():
    print("[nova-zwave-poller] Starting MQTT subscriber")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_message = on_message
    client.connect(MQTT_HOST, MQTT_PORT, 60)
    client.subscribe("zwave/#")
    print(f"[nova-zwave-poller] Subscribed to zwave/#")
    client.loop_forever()


if __name__ == "__main__":
    main()
