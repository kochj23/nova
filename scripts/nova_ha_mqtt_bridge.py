#!/usr/bin/env python3
"""
nova_ha_mqtt_bridge.py — publish ALL useful Nova telemetry into Home Assistant via
MQTT Discovery, so every Nova signal becomes a native HA entity with NO pairing
codes (Jordan 2026-06-24: "get everything useful into HA, implement everything").

How it works:
- On start, publishes a retained HA-discovery config to homeassistant/<comp>/nova/<uid>/config
  for each sensor → HA auto-creates the entity (grouped under "Nova <Device>").
- Then loops every PUBLISH_INTERVAL: queries PG for the latest value of each and
  publishes it to nova/<uid>/state. expire_after marks an entity unavailable if Nova
  stops publishing.

Two sensor kinds:
- STATIC: one query → one value → one entity.
- DYNAMIC: one query → many (key,value) rows → one entity per key (plugs, rooms, AV, disks).

stdlib + paho-mqtt. Runs as a launchd daemon.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import paho.mqtt.client as mqtt

MQTT_HOST, MQTT_PORT = "127.0.0.1", 1883
PG = ["psql", "-h", "localhost", "-U", "kochj", "-d", "nova_ops", "-tAF\t", "-c"]
PUBLISH_INTERVAL = 60          # seconds
DISCOVERY_PREFIX = "homeassistant"
EXPIRE_AFTER = 600             # entity goes unavailable if no update in 10 min


def log(m): print(f"[ha-mqtt {time.strftime('%H:%M:%S')}] {m}", flush=True)


def q(sql):
    """Run SQL, return list of column-tuples (tab-split)."""
    try:
        r = subprocess.run(PG + [sql], capture_output=True, text=True, timeout=20)
        return [ln.split("\t") for ln in r.stdout.strip().splitlines() if ln.strip()]
    except Exception as e:
        log(f"query error: {e}")
        return []


# ── STATIC sensors: (uid, name, sql→one scalar, unit, device_class, device_group, icon) ──
STATIC = [
    # Weather (Ambient station, latest row)
    ("wx_temp", "Outdoor Temperature", "SELECT round(temp_f::numeric,1) FROM telemetry.weather ORDER BY ts DESC LIMIT 1", "°F", "temperature", "Weather", None),
    ("wx_humidity", "Outdoor Humidity", "SELECT round(humidity::numeric,0) FROM telemetry.weather ORDER BY ts DESC LIMIT 1", "%", "humidity", "Weather", None),
    ("wx_feels", "Feels Like", "SELECT round(feels_like_f::numeric,1) FROM telemetry.weather ORDER BY ts DESC LIMIT 1", "°F", "temperature", "Weather", None),
    ("wx_wind", "Wind Speed", "SELECT round(wind_speed_mph::numeric,1) FROM telemetry.weather ORDER BY ts DESC LIMIT 1", "mph", "wind_speed", "Weather", "mdi:weather-windy"),
    ("wx_gust", "Wind Gust", "SELECT round(wind_gust_mph::numeric,1) FROM telemetry.weather ORDER BY ts DESC LIMIT 1", "mph", "wind_speed", "Weather", "mdi:weather-windy"),
    ("wx_pressure", "Barometric Pressure", "SELECT round(pressure_in::numeric,2) FROM telemetry.weather ORDER BY ts DESC LIMIT 1", "inHg", "pressure", "Weather", None),
    ("wx_rain_rate", "Rain Rate", "SELECT round(rain_rate_in::numeric,2) FROM telemetry.weather ORDER BY ts DESC LIMIT 1", "in/h", "precipitation_intensity", "Weather", "mdi:weather-rainy"),
    ("wx_rain_day", "Rain Today", "SELECT round(rain_daily_in::numeric,2) FROM telemetry.weather ORDER BY ts DESC LIMIT 1", "in", "precipitation", "Weather", "mdi:weather-rainy"),
    ("wx_solar", "Solar Radiation", "SELECT round(solar_radiation::numeric,0) FROM telemetry.weather ORDER BY ts DESC LIMIT 1", "W/m²", "irradiance", "Weather", "mdi:weather-sunny"),
    ("wx_uv", "UV Index", "SELECT round(uv_index::numeric,1) FROM telemetry.weather ORDER BY ts DESC LIMIT 1", "UV index", None, "Weather", "mdi:weather-sunny-alert"),
    # Air quality (AQIN, latest)
    ("aq_pm25", "Air PM2.5", "SELECT round(pm25::numeric,1) FROM telemetry.air_quality ORDER BY ts DESC LIMIT 1", "µg/m³", "pm25", "Air Quality", None),
    ("aq_pm10", "Air PM10", "SELECT round(pm10::numeric,1) FROM telemetry.air_quality ORDER BY ts DESC LIMIT 1", "µg/m³", "pm10", "Air Quality", None),
    ("aq_co2", "Air CO2", "SELECT round(co2::numeric,0) FROM telemetry.air_quality ORDER BY ts DESC LIMIT 1", "ppm", "carbon_dioxide", "Air Quality", None),
    ("aq_aqi", "AQI", "SELECT round(aqi::numeric,0) FROM telemetry.air_quality ORDER BY ts DESC LIMIT 1", "AQI", "aqi", "Air Quality", "mdi:air-filter"),
    # WAN
    ("wan_latency", "WAN Latency", "SELECT round(avg(latency_ms)::numeric,0) FROM telemetry.wan_quality WHERE ts>now()-interval '10 min'", "ms", None, "Network", "mdi:speedometer"),
    ("wan_loss", "WAN Packet Loss", "SELECT round(max(packet_loss_pct)::numeric,0) FROM telemetry.wan_quality WHERE ts>now()-interval '10 min'", "%", None, "Network", "mdi:lan-disconnect"),
    ("wan_down", "WAN Download", "SELECT round(down_mbps::numeric,0) FROM telemetry.wan_quality WHERE down_mbps IS NOT NULL ORDER BY ts DESC LIMIT 1", "Mbit/s", "data_rate", "Network", "mdi:download"),
    ("wan_up", "WAN Upload", "SELECT round(up_mbps::numeric,0) FROM telemetry.wan_quality WHERE up_mbps IS NOT NULL ORDER BY ts DESC LIMIT 1", "Mbit/s", "data_rate", "Network", "mdi:upload"),
    # Energy (house total)
    ("energy_total", "House Power", "SELECT round(sum(w)::numeric,0) FROM (SELECT DISTINCT ON (device_name) watts w FROM energy_readings WHERE ts>now()-interval '15 min' ORDER BY device_name, ts DESC) x", "W", "power", "Energy", None),
    # Presence (Jordan's room — now room-accurate after #634)
    ("presence_jordan_room", "Jordan Room", "SELECT room FROM presence_state WHERE person='jordan' ORDER BY last_confirmed DESC LIMIT 1", None, None, "Presence", "mdi:account"),
    ("presence_jordan_activity", "Jordan Activity", "SELECT activity_state FROM presence_state WHERE person='jordan' ORDER BY last_confirmed DESC LIMIT 1", None, None, "Presence", "mdi:run"),
]

# ── DYNAMIC sensors: (uid_prefix, name_suffix, sql→(key,value) rows, unit, device_class, device_group) ──
DYNAMIC = [
    ("plug", "Power", "SELECT DISTINCT ON (device_name) regexp_replace(lower(device_name),'[^a-z0-9]+','_','g'), round(watts::numeric,0) FROM energy_readings WHERE ts>now()-interval '15 min' AND watts IS NOT NULL ORDER BY device_name, ts DESC", "W", "power", "Energy"),
    ("room_temp", "Temp", "SELECT DISTINCT ON (room) regexp_replace(lower(room),'[^a-z0-9]+','_','g'), round(temp_f::numeric,1) FROM telemetry.climate WHERE ts>now()-interval '30 min' AND temp_f IS NOT NULL ORDER BY room, ts DESC", "°F", "temperature", "Climate"),
    ("room_hum", "Humidity", "SELECT DISTINCT ON (room) regexp_replace(lower(room),'[^a-z0-9]+','_','g'), round(humidity::numeric,0) FROM telemetry.climate WHERE ts>now()-interval '30 min' AND humidity IS NOT NULL ORDER BY room, ts DESC", "%", "humidity", "Climate"),
    ("storage", "Used", "SELECT DISTINCT ON (component_name) regexp_replace(lower(component_name),'[^a-z0-9]+','_','g'), round(used_pct::numeric,0) FROM telemetry.storage_metrics WHERE ts>now()-interval '1 hour' AND used_pct IS NOT NULL ORDER BY component_name, ts DESC", "%", None, "Storage"),
    ("av_vol", "Volume", "SELECT DISTINCT ON (device_id) regexp_replace(lower(device_id),'[^a-z0-9]+','_','g'), volume FROM telemetry.av_state WHERE ts>now()-interval '1 hour' AND volume IS NOT NULL ORDER BY device_id, ts DESC", None, None, "AV"),
]


def disc_topic(uid):  return f"{DISCOVERY_PREFIX}/sensor/nova/{uid}/config"
def state_topic(uid): return f"nova/{uid}/state"


def discovery_payload(uid, name, unit, dclass, group, icon=None):
    p = {
        "name": name, "unique_id": f"nova_{uid}", "state_topic": state_topic(uid),
        "expire_after": EXPIRE_AFTER, "force_update": True,
        "device": {"identifiers": [f"nova_{group.lower().replace(' ','_')}"],
                   "name": f"Nova {group}", "manufacturer": "Nova", "model": "telemetry-bridge"},
    }
    if unit:   p["unit_of_measurement"] = unit; p["state_class"] = "measurement"
    if dclass: p["device_class"] = dclass
    if icon:   p["icon"] = icon
    return json.dumps(p)


def main():
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="nova-ha-mqtt-bridge")
    c.connect(MQTT_HOST, MQTT_PORT, 60)
    c.loop_start()

    # publish discovery for static sensors once (retained)
    for uid, name, _sql, unit, dclass, group, icon in STATIC:
        c.publish(disc_topic(uid), discovery_payload(uid, name, unit, dclass, group, icon), retain=True)
    log(f"published discovery for {len(STATIC)} static sensors")

    known_dynamic = set()
    while True:
        # static state
        for uid, name, sql, *_ in STATIC:
            rows = q(sql)
            if rows and rows[0] and rows[0][0] not in ("", None):
                c.publish(state_topic(uid), rows[0][0])
        # dynamic: discover new entities + publish state
        for prefix, suffix, sql, unit, dclass, group in DYNAMIC:
            for row in q(sql):
                if len(row) < 2 or not row[0]:
                    continue
                key, val = row[0], row[1]
                uid = f"{prefix}_{key}"
                if uid not in known_dynamic:
                    nm = f"{key.replace('_',' ').title()} {suffix}"
                    c.publish(disc_topic(uid), discovery_payload(uid, nm, unit, dclass, group), retain=True)
                    known_dynamic.add(uid)
                if val not in ("", None):
                    c.publish(state_topic(uid), val)
        log(f"published states ({len(STATIC)} static + {len(known_dynamic)} dynamic entities)")
        time.sleep(PUBLISH_INTERVAL)


if __name__ == "__main__":
    main()
