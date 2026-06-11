#!/opt/homebrew/bin/python3
"""
nova_telemetry_observer.py — Nova's AI observer for home telemetry data.

Runs hourly via launchd. Queries all telemetry tables, detects patterns and
anomalies, posts observations to Slack #nova-notifications and records them
to shared_observations in nova_ops.

Domains analyzed:
  1. Weather anomalies
  2. AV insights
  3. Energy patterns
  4. Network anomalies
  5. Climate comfort
  6. Nova meta (memory, disk, gateway, VRAM)

Written by Jordan Koch / Nova.
"""

import sys
import os
import logging
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional


class _SafeEncoder(json.JSONEncoder):
    """Handle psycopg2 Decimal and datetime values in metadata dicts."""

    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, (datetime,)):
            return o.isoformat()
        return super().default(o)


def _safe_dumps(obj) -> str:
    return json.dumps(obj, cls=_SafeEncoder)

sys.path.insert(0, str(Path.home()) + "/.openclaw/scripts")
import nova_config

import psycopg2
import psycopg2.extras

# ── Logging ───────────────────────────────────────────────────────────────────

LOG_PATH = str(Path.home()) + "/.openclaw/logs/telemetry_observer.log"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("telemetry_observer")

# ── Constants ─────────────────────────────────────────────────────────────────

ENERGY_RATE_KWH = 0.28  # California rate $/kWh
CRITICAL_SEVERITIES = {"critical", "warning"}

# Thresholds
TEMP_SWING_THRESHOLD_F = 15.0
PRESSURE_DROP_THRESHOLD = 0.1
UV_EXTREME = 8.0
WIND_GUST_THRESHOLD = 30.0
AV_ON_HOURS_THRESHOLD = 8
AV_UNUSUAL_HOUR_START = 0  # midnight
AV_UNUSUAL_HOUR_END = 5    # 5am
AV_VOLUME_HIGH = 70
VAMPIRE_LOAD_W = 5.0
ENERGY_SPIKE_MULTIPLIER = 2.0
SIGNAL_POOR_DBM = -75
BANDWIDTH_HIGH_BYTES_HOUR = 1_073_741_824  # 1GB
ROOM_TEMP_HIGH_F = 78.0
ROOM_TEMP_LOW_F = 65.0
HUMIDITY_HIGH = 60
HUMIDITY_LOW = 30
MEMORY_RATE_HIGH_MULT = 2.0
MEMORY_RATE_LOW_MULT = 0.5
DISK_USAGE_WARN_PCT = 85


# ── Database helpers ──────────────────────────────────────────────────────────

def get_nova_ops_conn():
    """Connect to nova_ops database."""
    return psycopg2.connect(host="localhost", dbname="nova_ops", user="kochj")


def get_nova_memories_conn():
    """Connect to nova_memories database."""
    return psycopg2.connect(host="localhost", dbname="nova_memories", user="kochj")


def query(conn, sql, params=None):
    """Execute a query and return all rows as dicts."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def query_one(conn, sql, params=None):
    """Execute a query and return one row as dict or None."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()


# ── Observation collector ─────────────────────────────────────────────────────

class Observation:
    """A single observation with category, severity, and message."""

    def __init__(self, category: str, subject: str, message: str,
                 severity: str = "info", metadata: Optional[dict] = None):
        self.category = category
        self.subject = subject
        self.message = message
        self.severity = severity  # info, warning, critical
        self.metadata = metadata or {}

    def __repr__(self):
        return f"<Observation [{self.severity}] {self.category}/{self.subject}: {self.message}>"


observations: list[Observation] = []


def observe(category: str, subject: str, message: str,
            severity: str = "info", metadata: Optional[dict] = None):
    """Record an observation."""
    obs = Observation(category, subject, message, severity, metadata)
    observations.append(obs)
    log.info(f"[{severity}] {category}/{subject}: {message}")


# ── Domain 1: Weather Anomalies ───────────────────────────────────────────────

def analyze_weather(conn):
    """Check for weather anomalies in the last hour and 4-hour window."""
    now = datetime.now(timezone.utc)
    hour_ago = now - timedelta(hours=1)
    four_hours_ago = now - timedelta(hours=4)
    two_hours_ago = now - timedelta(hours=2)

    # Temperature swing in 4 hours
    temps_4h = query(conn, """
        SELECT ts, temp_f FROM telemetry.weather
        WHERE ts >= %s AND temp_f IS NOT NULL
        ORDER BY ts
    """, (four_hours_ago,))

    if len(temps_4h) >= 2:
        temps = [r["temp_f"] for r in temps_4h]
        max_t, min_t = max(temps), min(temps)
        swing = max_t - min_t
        if swing >= TEMP_SWING_THRESHOLD_F:
            observe("weather", "temp_swing",
                    f"Temperature swung {swing:.1f}F in 4 hours ({min_t:.0f}F to {max_t:.0f}F). That's wild.",
                    severity="warning",
                    metadata={"swing_f": swing, "min_f": min_t, "max_f": max_t})

    # Pressure drop in 2 hours (storm signal)
    pressure_2h = query(conn, """
        SELECT ts, pressure_in FROM telemetry.weather
        WHERE ts >= %s AND pressure_in IS NOT NULL
        ORDER BY ts
    """, (two_hours_ago,))

    if len(pressure_2h) >= 2:
        first_p = pressure_2h[0]["pressure_in"]
        last_p = pressure_2h[-1]["pressure_in"]
        drop = first_p - last_p
        if drop >= PRESSURE_DROP_THRESHOLD:
            observe("weather", "pressure_drop",
                    f"Barometric pressure dropped {drop:.3f} inHg in 2 hours. Storm likely incoming.",
                    severity="warning",
                    metadata={"drop_inhg": drop, "current": last_p})

    # UV index extreme
    latest_uv = query_one(conn, """
        SELECT ts, uv_index FROM telemetry.weather
        WHERE ts >= %s AND uv_index IS NOT NULL
        ORDER BY ts DESC LIMIT 1
    """, (hour_ago,))

    if latest_uv and latest_uv["uv_index"] >= UV_EXTREME:
        observe("weather", "uv_extreme",
                f"UV index hit {latest_uv['uv_index']:.0f} — extreme sun. Skin damage in <15 minutes outdoors.",
                severity="warning",
                metadata={"uv_index": latest_uv["uv_index"]})

    # Wind gusts
    gusts = query(conn, """
        SELECT ts, wind_gust_mph FROM telemetry.weather
        WHERE ts >= %s AND wind_gust_mph IS NOT NULL AND wind_gust_mph >= %s
        ORDER BY wind_gust_mph DESC LIMIT 1
    """, (hour_ago, WIND_GUST_THRESHOLD))

    if gusts:
        g = gusts[0]
        observe("weather", "wind_gust",
                f"Wind gusted to {g['wind_gust_mph']:.0f} mph. Patio furniture check.",
                severity="warning" if g["wind_gust_mph"] >= 40 else "info",
                metadata={"gust_mph": g["wind_gust_mph"]})

    # Rain transitions
    rain_data = query(conn, """
        SELECT ts, rain_rate_in FROM telemetry.weather
        WHERE ts >= %s AND rain_rate_in IS NOT NULL
        ORDER BY ts
    """, (hour_ago,))

    if len(rain_data) >= 2:
        first_raining = rain_data[0]["rain_rate_in"] > 0
        last_raining = rain_data[-1]["rain_rate_in"] > 0
        if not first_raining and last_raining:
            observe("weather", "rain_started",
                    f"Rain started — current rate {rain_data[-1]['rain_rate_in']:.2f} in/hr.",
                    severity="info",
                    metadata={"rate_in_hr": rain_data[-1]["rain_rate_in"]})
        elif first_raining and not last_raining:
            observe("weather", "rain_stopped",
                    "Rain stopped. Skies clearing.",
                    severity="info")

    # Daily records (check against last 30 days)
    thirty_days_ago = now - timedelta(days=30)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    today_max = query_one(conn, """
        SELECT MAX(temp_f) as max_temp FROM telemetry.weather
        WHERE ts >= %s AND temp_f IS NOT NULL
    """, (today_start,))

    if today_max and today_max["max_temp"]:
        historical_max = query_one(conn, """
            SELECT MAX(temp_f) as max_temp FROM telemetry.weather
            WHERE ts >= %s AND ts < %s AND temp_f IS NOT NULL
        """, (thirty_days_ago, today_start))

        if historical_max and historical_max["max_temp"]:
            if today_max["max_temp"] > historical_max["max_temp"]:
                observe("weather", "daily_record",
                        f"New 30-day high: {today_max['max_temp']:.0f}F today beats the previous {historical_max['max_temp']:.0f}F.",
                        severity="info",
                        metadata={"today_max": today_max["max_temp"], "prev_max": historical_max["max_temp"]})


# ── Domain 2: AV Insights ────────────────────────────────────────────────────

def analyze_av(conn):
    """Check AV device state for unusual patterns."""
    now = datetime.now(timezone.utc)
    hour_ago = now - timedelta(hours=1)

    # Devices powered on > 8 hours continuously
    # Check device_power_events for last power-on without subsequent power-off
    powered_devices = query(conn, """
        WITH latest_on AS (
            SELECT device_id, MAX(ts) as last_on
            FROM telemetry.device_power_events
            WHERE event = 'power_on'
            GROUP BY device_id
        ),
        latest_off AS (
            SELECT device_id, MAX(ts) as last_off
            FROM telemetry.device_power_events
            WHERE event = 'power_off'
            GROUP BY device_id
        )
        SELECT lo.device_id, lo.last_on,
               EXTRACT(EPOCH FROM (NOW() - lo.last_on)) / 3600.0 as hours_on
        FROM latest_on lo
        LEFT JOIN latest_off lf ON lo.device_id = lf.device_id
        WHERE (lf.last_off IS NULL OR lo.last_on > lf.last_off)
          AND lo.last_on < NOW() - INTERVAL '8 hours'
    """)

    for d in powered_devices:
        hours = d["hours_on"]
        observe("av", d["device_id"],
                f"{d['device_id']} has been on for {hours:.0f} hours straight. Someone forgot to turn it off.",
                severity="warning" if hours > 12 else "info",
                metadata={"device": d["device_id"], "hours_on": hours})

    # Unusual listening hours (midnight to 5am)
    unusual_activity = query(conn, """
        SELECT DISTINCT device_id, source_input, media_title
        FROM telemetry.av_state
        WHERE ts >= %s AND power = true
          AND EXTRACT(HOUR FROM ts AT TIME ZONE 'America/Los_Angeles')
              BETWEEN %s AND %s
    """, (hour_ago, AV_UNUSUAL_HOUR_START, AV_UNUSUAL_HOUR_END))

    for a in unusual_activity:
        local_hour = datetime.now(timezone.utc).astimezone().hour
        if AV_UNUSUAL_HOUR_START <= local_hour <= AV_UNUSUAL_HOUR_END:
            media = f" playing {a['media_title']}" if a.get("media_title") else ""
            observe("av", "unusual_hours",
                    f"{a['device_id']} active at {local_hour}:00{media}. Night owl or left on?",
                    severity="info",
                    metadata={"device": a["device_id"], "hour": local_hour})

    # Volume above 70% for extended periods
    loud_devices = query(conn, """
        SELECT device_id, AVG(volume) as avg_vol, COUNT(*) as samples
        FROM telemetry.av_state
        WHERE ts >= %s AND power = true AND volume > %s
        GROUP BY device_id
        HAVING COUNT(*) >= 3
    """, (hour_ago, AV_VOLUME_HIGH))

    for d in loud_devices:
        observe("av", d["device_id"],
                f"{d['device_id']} running at {d['avg_vol']:.0f}%% volume for most of this hour. That's loud.",
                severity="info",
                metadata={"device": d["device_id"], "avg_volume": float(d["avg_vol"])})

    # Total listening time per device today
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daily_usage = query(conn, """
        SELECT device_id, COUNT(*) as samples,
               (COUNT(*) * 5.0 / 60.0) as est_minutes
        FROM telemetry.av_state
        WHERE ts >= %s AND power = true
        GROUP BY device_id
        ORDER BY samples DESC
    """, (today_start,))

    for d in daily_usage:
        if d["est_minutes"] and d["est_minutes"] > 60:
            observe("av", "daily_usage",
                    f"{d['device_id']}: ~{d['est_minutes']:.0f} minutes of use today.",
                    severity="info",
                    metadata={"device": d["device_id"], "minutes": float(d["est_minutes"])})


# ── Domain 3: Energy Patterns ─────────────────────────────────────────────────

def analyze_energy(conn):
    """Check for energy anomalies."""
    now = datetime.now(timezone.utc)
    hour_ago = now - timedelta(hours=1)
    week_ago = now - timedelta(days=7)

    # Vampire loads (devices drawing > 5W when supposedly off)
    vampires = query(conn, """
        SELECT device_id, device_name, AVG(watts) as avg_watts
        FROM telemetry.energy
        WHERE ts >= %s AND on_state = false AND watts > %s
        GROUP BY device_id, device_name
        HAVING AVG(watts) > %s
    """, (hour_ago, VAMPIRE_LOAD_W, VAMPIRE_LOAD_W))

    for v in vampires:
        daily_cost = (v["avg_watts"] / 1000.0) * 24 * ENERGY_RATE_KWH
        observe("energy", v["device_id"],
                f"{v['device_name'] or v['device_id']} drawing {v['avg_watts']:.1f}W while 'off' — "
                f"${daily_cost:.2f}/day vampire load.",
                severity="warning",
                metadata={"device": v["device_id"], "watts": float(v["avg_watts"]),
                          "daily_cost": daily_cost})

    # Unusual spikes (> 2x 7-day average for a device)
    baselines = query(conn, """
        WITH hourly_avg AS (
            SELECT device_id, device_name, AVG(watts) as avg_w
            FROM telemetry.energy
            WHERE ts >= %s AND ts < %s
            GROUP BY device_id, device_name
        ),
        current_hour AS (
            SELECT device_id, device_name, AVG(watts) as avg_w
            FROM telemetry.energy
            WHERE ts >= %s
            GROUP BY device_id, device_name
        )
        SELECT c.device_id, c.device_name,
               c.avg_w as current_w, h.avg_w as baseline_w,
               c.avg_w / NULLIF(h.avg_w, 0) as ratio
        FROM current_hour c
        JOIN hourly_avg h ON c.device_id = h.device_id
        WHERE h.avg_w > 10
          AND c.avg_w > h.avg_w * %s
    """, (week_ago, hour_ago, hour_ago, ENERGY_SPIKE_MULTIPLIER))

    for b in baselines:
        observe("energy", b["device_id"],
                f"{b['device_name'] or b['device_id']} drawing {b['current_w']:.0f}W "
                f"(normal: {b['baseline_w']:.0f}W, {b['ratio']:.1f}x spike).",
                severity="warning",
                metadata={"device": b["device_id"], "current_w": float(b["current_w"]),
                          "baseline_w": float(b["baseline_w"]), "ratio": float(b["ratio"])})

    # Total power draw and cost this hour
    total = query_one(conn, """
        SELECT AVG(total_watts) as avg_watts FROM (
            SELECT ts, SUM(watts) as total_watts
            FROM telemetry.energy
            WHERE ts >= %s AND watts IS NOT NULL
            GROUP BY ts
        ) sub
    """, (hour_ago,))

    if total and total["avg_watts"]:
        hourly_cost = (total["avg_watts"] / 1000.0) * ENERGY_RATE_KWH
        # Compare to 7-day average
        baseline_total = query_one(conn, """
            SELECT AVG(total_watts) as avg_watts FROM (
                SELECT ts, SUM(watts) as total_watts
                FROM telemetry.energy
                WHERE ts >= %s AND ts < %s AND watts IS NOT NULL
                GROUP BY ts
            ) sub
        """, (week_ago, hour_ago))

        baseline_w = baseline_total["avg_watts"] if baseline_total and baseline_total["avg_watts"] else None
        msg = f"Power draw: {total['avg_watts']:.0f}W avg this hour (${hourly_cost:.2f}/hr)."
        if baseline_w:
            msg += f" Normal range: {baseline_w * 0.8:.0f}-{baseline_w * 1.2:.0f}W."
        observe("energy", "total_draw", msg, severity="info",
                metadata={"avg_watts": float(total["avg_watts"]), "hourly_cost": hourly_cost})

    # Devices left on overnight (check if 6-8am and devices were on since midnight)
    local_now = datetime.now()
    if 6 <= local_now.hour <= 8:
        overnight = query(conn, """
            SELECT device_id, device_name, COUNT(*) as samples
            FROM telemetry.energy
            WHERE ts >= NOW() - INTERVAL '8 hours'
              AND on_state = true AND watts > 20
            GROUP BY device_id, device_name
            HAVING COUNT(*) >= 80
        """)
        for d in overnight:
            observe("energy", d["device_id"],
                    f"{d['device_name'] or d['device_id']} was on all night.",
                    severity="info",
                    metadata={"device": d["device_id"]})


# ── Domain 4: Network Anomalies ──────────────────────────────────────────────

def analyze_network(conn):
    """Check for network anomalies."""
    now = datetime.now(timezone.utc)
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)

    # New devices: compare current clients against a persistent baseline of known MACs.
    # On first run the baseline is empty, so we SEED it silently rather than flagging
    # every known device as a critical intruder (cold-start false-positive storm).
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS telemetry.known_devices (
                client_mac TEXT PRIMARY KEY,
                client_name TEXT,
                ip TEXT,
                first_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("SELECT COUNT(*) FROM telemetry.known_devices")
        baseline_count = cur.fetchone()[0]
    conn.commit()

    current = query(conn, """
        SELECT DISTINCT client_mac, client_name, ip
        FROM telemetry.network
        WHERE ts >= %s AND client_mac IS NOT NULL
    """, (hour_ago,))

    known = {r["client_mac"] for r in query(conn,
             "SELECT client_mac FROM telemetry.known_devices")}

    truly_new = [d for d in current if d["client_mac"] not in known]

    # Always record newly-seen devices into the baseline.
    if truly_new:
        with conn.cursor() as cur:
            for d in truly_new:
                cur.execute("""
                    INSERT INTO telemetry.known_devices (client_mac, client_name, ip)
                    VALUES (%s, %s, %s) ON CONFLICT (client_mac) DO NOTHING
                """, (d["client_mac"], d["client_name"], d["ip"]))
        conn.commit()

    if baseline_count == 0:
        # First run ever: seed silently, no alerts.
        log.info(f"Seeded device baseline with {len(truly_new)} devices (first run, no alerts).")
    else:
        # Genuine new arrivals — but cap how many we shout about.
        for d in truly_new[:5]:
            name = d["client_name"] or "unknown"
            observe("network", "new_device",
                    f"New device on network: {name} ({d['client_mac']}) at {d['ip']}.",
                    severity="warning",
                    metadata={"mac": d["client_mac"], "name": name, "ip": d["ip"]})
        if len(truly_new) > 5:
            observe("network", "new_devices_bulk",
                    f"{len(truly_new)} new devices appeared on the network this hour (showing first 5).",
                    severity="warning",
                    metadata={"count": len(truly_new)})

    # Poor signal (below -75 dBm)
    poor_signal = query(conn, """
        SELECT client_mac, client_name, AVG(signal_dbm) as avg_signal
        FROM telemetry.network
        WHERE ts >= %s AND signal_dbm IS NOT NULL AND signal_dbm < %s
          AND is_wired = false
        GROUP BY client_mac, client_name
    """, (hour_ago, SIGNAL_POOR_DBM))

    for d in poor_signal:
        name = d["client_name"] or d["client_mac"]
        observe("network", "poor_signal",
                f"{name} has poor WiFi signal ({d['avg_signal']:.0f} dBm). Might drop.",
                severity="info",
                metadata={"client": name, "signal_dbm": float(d["avg_signal"])})

    # High bandwidth (> 1GB/hour single device)
    bandwidth_hogs = query(conn, """
        SELECT client_mac, client_name, ip,
               (MAX(rx_bytes) - MIN(rx_bytes) + MAX(tx_bytes) - MIN(tx_bytes)) as bytes_hour
        FROM telemetry.network
        WHERE ts >= %s
        GROUP BY client_mac, client_name, ip
        HAVING (MAX(rx_bytes) - MIN(rx_bytes) + MAX(tx_bytes) - MIN(tx_bytes)) > %s
    """, (hour_ago, BANDWIDTH_HIGH_BYTES_HOUR))

    for d in bandwidth_hogs:
        name = d["client_name"] or d["client_mac"]
        gb = d["bytes_hour"] / 1_073_741_824
        observe("network", "high_bandwidth",
                f"{name} ({d['ip']}) transferred {gb:.1f}GB in the last hour. Streaming or uploading?",
                severity="warning" if gb > 5 else "info",
                metadata={"client": name, "ip": d["ip"], "bytes": d["bytes_hour"], "gb": gb})

    # Devices that disappeared (were active yesterday but not in last hour)
    disappeared = query(conn, """
        WITH yesterday_active AS (
            SELECT DISTINCT client_mac, client_name
            FROM telemetry.network
            WHERE ts >= %s AND ts < %s
              AND uptime_s > 3600
        ),
        current_active AS (
            SELECT DISTINCT client_mac
            FROM telemetry.network
            WHERE ts >= %s
        )
        SELECT y.client_mac, y.client_name
        FROM yesterday_active y
        LEFT JOIN current_active c ON y.client_mac = c.client_mac
        WHERE c.client_mac IS NULL
    """, (day_ago - timedelta(days=1), day_ago, hour_ago))

    # Only report if a small number disappeared (mass outage is different)
    if 0 < len(disappeared) <= 5:
        for d in disappeared:
            name = d["client_name"] or d["client_mac"]
            observe("network", "device_offline",
                    f"{name} went offline — was active yesterday but absent now.",
                    severity="info",
                    metadata={"client": name, "mac": d["client_mac"]})


# ── Domain 5: Climate Comfort ────────────────────────────────────────────────

def analyze_climate(conn):
    """Check indoor climate comfort levels."""
    now = datetime.now(timezone.utc)
    hour_ago = now - timedelta(hours=1)

    # Rooms too hot or too cold
    room_temps = query(conn, """
        SELECT room, AVG(temp_f) as avg_temp, MAX(temp_f) as max_temp,
               MIN(temp_f) as min_temp
        FROM telemetry.climate
        WHERE ts >= %s AND temp_f IS NOT NULL
        GROUP BY room
    """, (hour_ago,))

    for r in room_temps:
        if r["max_temp"] and r["max_temp"] > ROOM_TEMP_HIGH_F:
            observe("climate", r["room"],
                    f"{r['room']} hit {r['max_temp']:.0f}F this hour. Getting toasty.",
                    severity="warning" if r["max_temp"] > 82 else "info",
                    metadata={"room": r["room"], "max_temp": float(r["max_temp"])})
        elif r["min_temp"] and r["min_temp"] < ROOM_TEMP_LOW_F:
            observe("climate", r["room"],
                    f"{r['room']} dropped to {r['min_temp']:.0f}F. Chilly.",
                    severity="info",
                    metadata={"room": r["room"], "min_temp": float(r["min_temp"])})

    # Humidity out of range
    room_humidity = query(conn, """
        SELECT room, AVG(humidity) as avg_hum
        FROM telemetry.climate
        WHERE ts >= %s AND humidity IS NOT NULL
        GROUP BY room
    """, (hour_ago,))

    for r in room_humidity:
        if r["avg_hum"] and r["avg_hum"] > HUMIDITY_HIGH:
            observe("climate", r["room"],
                    f"{r['room']} humidity at {r['avg_hum']:.0f}%% — sticky. Mold risk if sustained.",
                    severity="warning" if r["avg_hum"] > 70 else "info",
                    metadata={"room": r["room"], "humidity": float(r["avg_hum"])})
        elif r["avg_hum"] and r["avg_hum"] < HUMIDITY_LOW:
            observe("climate", r["room"],
                    f"{r['room']} humidity at {r['avg_hum']:.0f}%% — dry. Static shock city.",
                    severity="info",
                    metadata={"room": r["room"], "humidity": float(r["avg_hum"])})

    # Indoor/outdoor differential
    outdoor_temp = query_one(conn, """
        SELECT AVG(temp_f) as avg_temp FROM telemetry.weather
        WHERE ts >= %s AND temp_f IS NOT NULL
    """, (hour_ago,))

    if outdoor_temp and outdoor_temp["avg_temp"] and room_temps:
        out_t = outdoor_temp["avg_temp"]
        for r in room_temps:
            if r["avg_temp"]:
                diff = r["avg_temp"] - out_t
                if abs(diff) > 15:
                    direction = "warmer" if diff > 0 else "cooler"
                    observe("climate", "differential",
                            f"{r['room']} is {abs(diff):.0f}F {direction} than outside ({out_t:.0f}F). "
                            f"{'AC working hard.' if diff < -10 else 'Heat building up inside.'}",
                            severity="info",
                            metadata={"room": r["room"], "indoor": float(r["avg_temp"]),
                                      "outdoor": out_t, "diff": float(diff)})

    # Repeated pattern detection: "always hot at X time"
    local_hour = datetime.now().hour
    if room_temps:
        week_ago = now - timedelta(days=7)
        for r in room_temps:
            if r["max_temp"] and r["max_temp"] > ROOM_TEMP_HIGH_F:
                # Check if this room was hot at this same hour multiple days
                hot_days = query_one(conn, """
                    SELECT COUNT(DISTINCT DATE(ts AT TIME ZONE 'America/Los_Angeles')) as days
                    FROM telemetry.climate
                    WHERE room = %s AND ts >= %s AND temp_f > %s
                      AND EXTRACT(HOUR FROM ts AT TIME ZONE 'America/Los_Angeles') = %s
                """, (r["room"], week_ago, ROOM_TEMP_HIGH_F, local_hour))

                if hot_days and hot_days["days"] and hot_days["days"] >= 3:
                    observe("climate", "pattern",
                            f"{r['room']} is hot at {local_hour}:00 for the {hot_days['days']}th day running. "
                            f"That's a pattern, not a fluke.",
                            severity="info",
                            metadata={"room": r["room"], "hour": local_hour,
                                      "consecutive_days": hot_days["days"]})


# ── Domain 6: Nova Meta ───────────────────────────────────────────────────────

def analyze_nova_meta(conn, mem_conn):
    """Check Nova's own systems — memory, disk, gateway, VRAM."""
    now = datetime.now(timezone.utc)
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)

    # Memory ingest rate from nova_memories
    current_rate = query_one(mem_conn, """
        SELECT COUNT(*) as cnt FROM memories
        WHERE created_at >= %s
    """, (hour_ago,))

    avg_rate = query_one(mem_conn, """
        SELECT COUNT(*) / 168.0 as avg_per_hour FROM memories
        WHERE created_at >= %s
    """, (week_ago,))

    if current_rate and avg_rate and avg_rate["avg_per_hour"]:
        rate = current_rate["cnt"]
        baseline = avg_rate["avg_per_hour"]
        if baseline > 0:
            ratio = rate / baseline
            if ratio >= MEMORY_RATE_HIGH_MULT:
                observe("nova_meta", "memory_ingest",
                        f"Memory ingest rate spiking: {rate} memories this hour "
                        f"(normal: ~{baseline:.0f}/hr, {ratio:.1f}x). Bulk ingest running?",
                        severity="info",
                        metadata={"rate": float(rate), "baseline": float(baseline), "ratio": float(ratio)})
            elif ratio <= MEMORY_RATE_LOW_MULT and baseline > 5:
                observe("nova_meta", "memory_ingest",
                        f"Memory ingest slow: only {rate} this hour (normal: ~{baseline:.0f}/hr). "
                        f"Pipeline stalled?",
                        severity="warning",
                        metadata={"rate": float(rate), "baseline": float(baseline), "ratio": float(ratio)})

    # Disk usage from nova_meta telemetry
    disk_usage = query_one(conn, """
        SELECT value, metadata FROM telemetry.nova_meta
        WHERE metric = 'disk_usage_pct' AND ts >= %s
        ORDER BY ts DESC LIMIT 1
    """, (hour_ago,))

    if disk_usage and disk_usage["value"]:
        pct = disk_usage["value"]
        if pct >= DISK_USAGE_WARN_PCT:
            observe("nova_meta", "disk_usage",
                    f"Disk usage at {pct:.0f}%%. Getting tight. Time to clean or expand.",
                    severity="critical" if pct >= 95 else "warning",
                    metadata={"usage_pct": pct})

    # Gateway latency
    latency = query(conn, """
        SELECT AVG(value) as avg_ms, MAX(value) as max_ms
        FROM telemetry.nova_meta
        WHERE metric = 'gateway_latency_ms' AND ts >= %s
    """, (hour_ago,))

    if latency and latency[0]["avg_ms"]:
        avg_ms = latency[0]["avg_ms"]
        max_ms = latency[0]["max_ms"]
        if avg_ms > 500:
            observe("nova_meta", "gateway_latency",
                    f"Gateway latency averaging {avg_ms:.0f}ms (peak {max_ms:.0f}ms). Sluggish.",
                    severity="warning",
                    metadata={"avg_ms": float(avg_ms), "max_ms": float(max_ms)})

    # Ollama VRAM
    vram = query_one(conn, """
        SELECT value, metadata FROM telemetry.nova_meta
        WHERE metric = 'ollama_vram_pct' AND ts >= %s
        ORDER BY ts DESC LIMIT 1
    """, (hour_ago,))

    if vram and vram["value"]:
        pct = vram["value"]
        if pct >= 90:
            model = ""
            if vram.get("metadata") and isinstance(vram["metadata"], dict):
                model = f" ({vram['metadata'].get('model', 'unknown')})"
            observe("nova_meta", "vram_saturation",
                    f"Ollama VRAM at {pct:.0f}%%{model}. Near saturation — inference may slow.",
                    severity="warning",
                    metadata={"vram_pct": pct})


# ── Output & Posting ─────────────────────────────────────────────────────────

def format_digest(obs_list: list[Observation]) -> str:
    """Format observations into Nova's voice for Slack/Discord."""
    if not obs_list:
        return ""

    lines = ["\U0001f321️ *Home Telemetry — Hourly Digest*"]

    for obs in obs_list:
        # Pick an emoji based on category
        emoji_map = {
            "weather": "\U0001f326️",
            "av": "\U0001f3b5",
            "energy": "⚡",
            "network": "\U0001f4e1",
            "climate": "\U0001f3e0",
            "nova_meta": "\U0001f9e0",
        }
        emoji = emoji_map.get(obs.category, "•")
        severity_marker = ""
        if obs.severity == "critical":
            severity_marker = " \U0001f6a8"
        elif obs.severity == "warning":
            severity_marker = " ⚠️"

        lines.append(f"{emoji} {obs.message}{severity_marker}")

    return "\n".join(lines)


def save_observations(conn, obs_list: list[Observation]):
    """Write observations to shared_observations table."""
    if not obs_list:
        return

    with conn.cursor() as cur:
        for obs in obs_list:
            cur.execute("""
                INSERT INTO shared_observations
                    (observer, category, subject, observation, severity, metadata, observed_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
            """, (
                "nova_telemetry_observer",
                obs.category,
                obs.subject,
                obs.message,
                obs.severity,
                _safe_dumps(obs.metadata),
            ))
    conn.commit()
    log.info(f"Saved {len(obs_list)} observations to shared_observations")


def post_digest(obs_list: list[Observation]):
    """Post the formatted digest to Slack and Discord."""
    if not obs_list:
        log.info("No observations — staying silent.")
        return

    msg = format_digest(obs_list)
    if msg:
        nova_config.post_both(msg, nova_config.SLACK_NOTIFY)
        log.info("Posted digest to Slack/Discord")


def post_critical_immediately(obs_list: list[Observation]):
    """Post critical findings immediately — batched into ONE message to avoid
    rate-limiting (Discord 429) when many criticals fire at once."""
    criticals = [o for o in obs_list if o.severity == "critical"]
    if not criticals:
        return

    emoji_map = {
        "weather": "\U0001f326️", "av": "\U0001f3b5", "energy": "⚡",
        "network": "\U0001f4e1", "climate": "\U0001f3e0", "nova_meta": "\U0001f9e0",
    }
    MAX_SHOWN = 10
    lines = ["\U0001f6a8 *CRITICAL ALERTS*"]
    for obs in criticals[:MAX_SHOWN]:
        emoji = emoji_map.get(obs.category, "\U0001f6a8")
        lines.append(f"{emoji} {obs.message}")
    if len(criticals) > MAX_SHOWN:
        lines.append(f"…and {len(criticals) - MAX_SHOWN} more critical items (see digest).")

    nova_config.post_both("\n".join(lines), nova_config.SLACK_NOTIFY)
    log.warning(f"Posted {len(criticals)} critical alerts in one batched message.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    log.info("=== Telemetry Observer starting ===")

    ops_conn = None
    mem_conn = None

    try:
        ops_conn = get_nova_ops_conn()
        mem_conn = get_nova_memories_conn()

        # Run all domain analyses
        log.info("Analyzing weather...")
        analyze_weather(ops_conn)

        log.info("Analyzing AV state...")
        analyze_av(ops_conn)

        log.info("Analyzing energy...")
        analyze_energy(ops_conn)

        log.info("Analyzing network...")
        analyze_network(ops_conn)

        log.info("Analyzing climate comfort...")
        analyze_climate(ops_conn)

        log.info("Analyzing Nova meta...")
        analyze_nova_meta(ops_conn, mem_conn)

        # Post critical alerts immediately
        post_critical_immediately(observations)

        # Save all observations to DB
        save_observations(ops_conn, observations)

        # Post the full digest (only if there are observations)
        post_digest(observations)

        log.info(f"=== Telemetry Observer complete — {len(observations)} observations ===")

    except Exception as e:
        log.error(f"Fatal error: {e}", exc_info=True)
        # Try to alert on fatal errors
        try:
            nova_config.post_both(
                f"\U0001f6a8 *Telemetry Observer CRASHED*\n`{e}`",
                nova_config.SLACK_NOTIFY
            )
        except Exception:
            pass
        sys.exit(1)

    finally:
        if ops_conn:
            ops_conn.close()
        if mem_conn:
            mem_conn.close()


if __name__ == "__main__":
    main()
