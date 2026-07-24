#!/usr/bin/env python3
"""
nova_hue_poller.py — Poll Philips Hue Bridge and store light/room state.

Polls every 60s, stores current state in PG for Grafana dashboards.
Also exposes HTTP API on port 37476 for Nova queries.

Written by Jordan Koch.
"""

import json
import subprocess
import sys
import time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Thread, Lock

sys.path.insert(0, str(Path(__file__).parent))

PORT = 37476
BRIDGE_IP = "192.168.1.152"
POLL_INTERVAL = 60
PG_DSN = "dbname=nova_ops user=kochj host=pg-primary.digitalnoise.net"

_data_lock = Lock()
_lights = {}
_groups = {}
_last_poll = 0


def get_token():
    r = subprocess.run(
        ["security", "find-generic-password", "-a", "nova", "-s", "nova-hue-api-token", "-w"],
        capture_output=True, text=True
    )
    return r.stdout.strip()


def poll_bridge():
    global _lights, _groups, _last_poll
    token = get_token()
    if not token:
        return

    try:
        with urllib.request.urlopen(f"http://{BRIDGE_IP}/api/{token}/lights", timeout=10) as r:
            lights = json.loads(r.read())
        with urllib.request.urlopen(f"http://{BRIDGE_IP}/api/{token}/groups", timeout=10) as r:
            groups = json.loads(r.read())

        with _data_lock:
            _lights = lights
            _groups = groups
            _last_poll = time.time()

        store_to_pg(lights, groups)
    except Exception as e:
        print(f"[hue-poller] Poll failed: {e}", flush=True)


def store_to_pg(lights, groups):
    try:
        import psycopg2
        conn = psycopg2.connect(PG_DSN, connect_timeout=5)
        conn.autocommit = True
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS hue_light_state (
                light_id TEXT NOT NULL,
                name TEXT NOT NULL,
                room TEXT,
                is_on BOOLEAN,
                brightness INTEGER,
                reachable BOOLEAN,
                polled_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (light_id)
            )
        """)

        room_map = {}
        for gid, g in groups.items():
            if g.get("type") == "Room":
                for lid in g.get("lights", []):
                    room_map[lid] = g.get("name", "Unknown")

        for lid, light in lights.items():
            state = light.get("state", {})
            cur.execute("""
                INSERT INTO hue_light_state (light_id, name, room, is_on, brightness, reachable, polled_at)
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (light_id) DO UPDATE SET
                    name = EXCLUDED.name, room = EXCLUDED.room,
                    is_on = EXCLUDED.is_on, brightness = EXCLUDED.brightness,
                    reachable = EXCLUDED.reachable, polled_at = NOW()
            """, (lid, light.get("name"), room_map.get(lid), state.get("on"), state.get("bri"), state.get("reachable")))

        conn.close()
    except Exception as e:
        print(f"[hue-poller] PG store failed: {e}", flush=True)


class HueHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == "/status" or self.path == "/health":
            with _data_lock:
                on_count = sum(1 for l in _lights.values() if l.get("state", {}).get("on"))
                self._json(200, {
                    "status": "ok",
                    "lights_total": len(_lights),
                    "lights_on": on_count,
                    "rooms": len([g for g in _groups.values() if g.get("type") == "Room"]),
                    "last_poll": _last_poll,
                    "age_seconds": int(time.time() - _last_poll) if _last_poll else None,
                })
        elif self.path == "/lights":
            with _data_lock:
                result = []
                room_map = {}
                for gid, g in _groups.items():
                    if g.get("type") == "Room":
                        for lid in g.get("lights", []):
                            room_map[lid] = g.get("name")
                for lid, light in _lights.items():
                    state = light.get("state", {})
                    result.append({
                        "id": lid,
                        "name": light.get("name"),
                        "room": room_map.get(lid, "Unknown"),
                        "on": state.get("on"),
                        "brightness": int(state.get("bri", 0) / 254 * 100),
                        "reachable": state.get("reachable"),
                    })
                self._json(200, result)
        elif self.path == "/rooms":
            with _data_lock:
                result = []
                for gid, g in _groups.items():
                    if g.get("type") == "Room":
                        result.append({
                            "name": g.get("name"),
                            "all_on": g.get("state", {}).get("all_on"),
                            "any_on": g.get("state", {}).get("any_on"),
                            "lights": len(g.get("lights", [])),
                        })
                self._json(200, result)
        elif self.path == "/lights/on":
            with _data_lock:
                result = []
                for lid, light in _lights.items():
                    if light.get("state", {}).get("on"):
                        result.append(light.get("name"))
                self._json(200, result)
        else:
            self._json(404, {"error": "not found"})

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def poll_loop():
    while True:
        poll_bridge()
        time.sleep(POLL_INTERVAL)


def main():
    print(f"[hue-poller] Starting — Bridge: {BRIDGE_IP}, Port: {PORT}, Interval: {POLL_INTERVAL}s", flush=True)
    poll_bridge()
    print(f"[hue-poller] Initial poll: {len(_lights)} lights, {len([g for g in _groups.values() if g.get('type')=='Room'])} rooms", flush=True)

    Thread(target=poll_loop, daemon=True).start()

    server = HTTPServer(("0.0.0.0", PORT), HueHandler)
    print(f"[hue-poller] HTTP API on 0.0.0.0:{PORT} — /lights, /rooms, /status, /lights/on", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
