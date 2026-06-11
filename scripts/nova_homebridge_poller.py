#!/opt/homebrew/bin/python3
"""
nova_homebridge_poller.py — Poll Homebridge for camera motion events and NVR telemetry.

Reads motion sensors, occupancy sensors, and temperature from Homebridge API (192.168.1.10:8581).
Pushes events to telemetry tables and shared_observations for Nova.

Written by Jordan Koch (via Claude).
"""

import json, logging, os, signal, sys, time, urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import psycopg2
import psycopg2.extras

DB_DSN = "host=localhost dbname=nova_ops user=kochj"
HB_URL = "http://192.168.1.10:8581"
HB_USER = "admin"
HB_PASS = "admin"
POLL_INTERVAL = 10
LOG_FILE = Path.home() / ".openclaw/logs/homebridge_poller.log"

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger("homebridge_poller")

_shutdown = False
def _sig(s, f): global _shutdown; _shutdown = True
signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)

_token = None
_prev_motion = {}
_conn = None


def get_db():
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg2.connect(DB_DSN)
        _conn.autocommit = True
    return _conn


def hb_login():
    global _token
    data = json.dumps({"username": HB_USER, "password": HB_PASS}).encode()
    req = urllib.request.Request(f"{HB_URL}/api/auth/login", data=data,
                                headers={"Content-Type": "application/json"})
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        _token = json.loads(resp.read()).get("access_token")
        return True
    except Exception as e:
        log.error(f"HB login failed: {e}")
        return False


def hb_get(path):
    global _token
    if not _token:
        if not hb_login():
            return None
    req = urllib.request.Request(f"{HB_URL}{path}",
                                headers={"Authorization": f"Bearer {_token}"})
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _token = None
            if hb_login():
                return hb_get(path)
        log.error(f"HB API error: {e.code}")
        return None
    except Exception as e:
        log.error(f"HB request failed: {e}")
        return None


def poll_cycle():
    global _prev_motion
    accessories = hb_get("/api/accessories")
    if not accessories:
        return

    conn = get_db()
    cur = conn.cursor()
    now = datetime.now()
    motion_events = []
    temps = []

    for acc in accessories:
        name = acc.get("serviceName", "")
        acc_type = acc.get("type", "")
        chars = {c["description"]: c.get("value") for c in acc.get("serviceCharacteristics", [])
                 if c.get("value") is not None}

        if acc_type == "MotionSensor":
            motion = bool(chars.get("Motion Detected", 0))
            prev = _prev_motion.get(name, False)
            if motion and not prev:
                motion_events.append(name)
                cur.execute("""
                    INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata)
                    VALUES ('nova', 'security', 'camera-motion', %s, 'info', %s)
                """, (f"Motion detected: {name}", json.dumps({"camera": name, "ts": now.isoformat()})))
            _prev_motion[name] = motion

        elif acc_type == "TemperatureSensor":
            temp = chars.get("Current Temperature")
            if temp is not None:
                temps.append((name, temp))
                cur.execute("""
                    INSERT INTO telemetry.nova_meta (ts, metric, value, metadata)
                    VALUES (NOW(), %s, %s, %s)
                """, (f"nvr_temp_{name.lower().replace(' ','_')}", temp,
                      json.dumps({"source": "homebridge", "device": name})))

    cur.close()

    if motion_events:
        log.info(f"Motion: {', '.join(motion_events)}")
    if temps:
        log.info(f"NVR temps: {', '.join(f'{n}={t}°C' for n,t in temps)}")


def main():
    log.info(f"nova_homebridge_poller starting — polling every {POLL_INTERVAL}s")
    log.info(f"Homebridge: {HB_URL}")

    while not _shutdown:
        try:
            poll_cycle()
        except Exception as e:
            log.error(f"Poll error: {e}")
            global _conn
            _conn = None
        for _ in range(POLL_INTERVAL):
            if _shutdown:
                break
            time.sleep(1)

    log.info("Shutdown complete.")


if __name__ == "__main__":
    main()
