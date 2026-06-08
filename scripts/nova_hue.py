#!/opt/homebrew/bin/python3
"""
nova_hue.py — Full Philips Hue integration for Nova.

HTTP API on port 37476, background sensor monitoring, text command interface,
dashboard state writer, and simple automation rules.

Bridge: 192.168.1.195
API key: macOS Keychain (nova-hue-api-key)

Written by Jordan Koch.
"""

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import nova_config

# ── Configuration ────────────────────────────────────────────────────────────

HUE_BRIDGE = "192.168.1.195"
HTTP_PORT = 37476
SENSOR_POLL_INTERVAL = 30  # seconds
STATE_WRITE_INTERVAL = 60  # seconds
TEMP_LOG_INTERVAL = 3600   # 1 hour
NO_MOTION_TIMEOUT = 7200   # 2 hours (nobody home)

STATE_DIR = Path.home() / ".openclaw/workspace/state"
STATE_FILE = STATE_DIR / "nova_hue_state.json"

# ── API Key from Keychain ────────────────────────────────────────────────────

_api_key_cache = None


def get_api_key() -> str:
    global _api_key_cache
    if _api_key_cache is None:
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "nova", "-s", "nova-hue-api-key", "-w"],
            capture_output=True, text=True
        )
        if result.returncode != 0 or not result.stdout.strip():
            print("[nova_hue] ERROR: Cannot get API key from Keychain", file=sys.stderr)
            sys.exit(1)
        _api_key_cache = result.stdout.strip()
    return _api_key_cache


def hue_url(path: str) -> str:
    return f"http://{HUE_BRIDGE}/api/{get_api_key()}/{path}"


# ── Hue API helpers ──────────────────────────────────────────────────────────

def hue_get(path: str) -> dict | list | None:
    """GET request to Hue bridge."""
    try:
        req = urllib.request.Request(hue_url(path))
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[nova_hue] GET /{path} failed: {e}", file=sys.stderr)
        return None


def hue_put(path: str, body: dict) -> dict | list | None:
    """PUT request to Hue bridge."""
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(hue_url(path), data=data, method="PUT")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print(f"[nova_hue] PUT /{path} failed: {e}", file=sys.stderr)
        return None


# ── Data fetchers ────────────────────────────────────────────────────────────

def get_all_lights() -> dict:
    """Return all lights with their state."""
    data = hue_get("lights")
    if not data or isinstance(data, list):
        return {}
    result = {}
    for lid, info in data.items():
        state = info.get("state", {})
        result[lid] = {
            "id": lid,
            "name": info.get("name", "Unknown"),
            "type": info.get("type", "Unknown"),
            "on": state.get("on", False),
            "brightness": state.get("bri", 0),
            "hue": state.get("hue"),
            "saturation": state.get("sat"),
            "colormode": state.get("colormode"),
            "reachable": state.get("reachable", False),
        }
    return result


def get_all_rooms() -> dict:
    """Return all rooms (groups) with aggregate state."""
    data = hue_get("groups")
    if not data or isinstance(data, list):
        return {}
    result = {}
    for gid, info in data.items():
        if info.get("type") not in ("Room", "Zone"):
            continue
        action = info.get("action", {})
        state = info.get("state", {})
        result[gid] = {
            "id": gid,
            "name": info.get("name", "Unknown"),
            "type": info.get("type"),
            "lights": info.get("lights", []),
            "light_count": len(info.get("lights", [])),
            "any_on": state.get("any_on", False),
            "all_on": state.get("all_on", False),
            "on": action.get("on", False),
            "brightness": action.get("bri", 0),
        }
    return result


def get_all_sensors() -> dict:
    """Return all sensors with their readings."""
    data = hue_get("sensors")
    if not data or isinstance(data, list):
        return {}
    result = {}
    for sid, info in data.items():
        stype = info.get("type", "")
        # Only include physical sensors we care about
        if stype in ("ZLLPresence", "ZLLTemperature", "ZLLLightLevel",
                     "ZHAPresence", "ZHATemperature", "ZHALightLevel"):
            state = info.get("state", {})
            config = info.get("config", {})
            sensor_data = {
                "id": sid,
                "name": info.get("name", "Unknown"),
                "type": stype,
                "lastupdated": state.get("lastupdated"),
                "battery": config.get("battery"),
                "reachable": config.get("reachable", False),
            }
            if "presence" in stype.lower():
                sensor_data["presence"] = state.get("presence", False)
            elif "temperature" in stype.lower():
                sensor_data["temperature"] = state.get("temperature", 0) / 100.0
            elif "lightlevel" in stype.lower():
                sensor_data["lightlevel"] = state.get("lightlevel", 0)
                sensor_data["dark"] = state.get("dark", False)
                sensor_data["daylight"] = state.get("daylight", True)
            result[sid] = sensor_data
    return result


def get_all_scenes() -> dict:
    """Return all scenes grouped by room."""
    data = hue_get("scenes")
    if not data or isinstance(data, list):
        return {}
    result = {}
    for scene_id, info in data.items():
        group = info.get("group", "0")
        if group not in result:
            result[group] = []
        result[group].append({
            "id": scene_id,
            "name": info.get("name", "Unknown"),
            "group": group,
            "type": info.get("type", ""),
        })
    return result


# ── Automation state ─────────────────────────────────────────────────────────

class AutomationState:
    def __init__(self):
        self.last_motion_time: datetime | None = None
        self.last_temp_log: datetime | None = None
        self.carport_timer: threading.Timer | None = None
        self.nobody_home_reported: bool = False
        self.last_outdoor_temp: float | None = None
        self.last_outdoor_light: int | None = None
        self.last_outdoor_motion: bool = False

    def is_night(self) -> bool:
        now = datetime.now()
        return now.hour >= 23 or now.hour < 5


automation = AutomationState()


# ── Database helper ──────────────────────────────────────────────────────────

def insert_observation(observer: str, category: str, subject: str,
                       observation: str, severity: str = "info",
                       metadata: dict | None = None):
    """Insert into shared_observations table."""
    meta_json = json.dumps(metadata) if metadata else "{}"
    try:
        subprocess.run(
            ["psql", "-h", "localhost", "-d", "nova_ops", "-U", "kochj", "-c",
             f"INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata) "
             f"VALUES ('nova_hue', '{category}', '{subject}', "
             f"$obs${observation}$obs$, '{severity}', '{meta_json}'::jsonb)"],
            capture_output=True, text=True, timeout=10
        )
    except Exception as e:
        print(f"[nova_hue] DB insert failed: {e}", file=sys.stderr)


# ── Sensor monitoring thread ─────────────────────────────────────────────────

def find_outdoor_sensors(sensors: dict) -> dict:
    """Find outdoor motion, temp, and light sensors."""
    outdoor = {"motion": None, "temperature": None, "lightlevel": None}
    for sid, s in sensors.items():
        name = s.get("name", "").lower()
        if "outdoor" in name or "outside" in name or "carport" in name or "porch" in name:
            if "presence" in s.get("type", "").lower():
                outdoor["motion"] = s
            elif "temperature" in s.get("type", "").lower():
                outdoor["temperature"] = s
            elif "lightlevel" in s.get("type", "").lower():
                outdoor["lightlevel"] = s
    # Fallback: just grab the first of each type if no "outdoor" label
    if not any(outdoor.values()):
        for sid, s in sensors.items():
            stype = s.get("type", "").lower()
            if "presence" in stype and outdoor["motion"] is None:
                outdoor["motion"] = s
            elif "temperature" in stype and outdoor["temperature"] is None:
                outdoor["temperature"] = s
            elif "lightlevel" in stype and outdoor["lightlevel"] is None:
                outdoor["lightlevel"] = s
    return outdoor


def turn_on_carport_temporarily():
    """Turn on carport/outdoor light for 5 minutes then off."""
    rooms = get_all_rooms()
    carport_id = None
    for gid, room in rooms.items():
        name = room["name"].lower()
        if "carport" in name or "porch" in name or "outdoor" in name or "outside" in name:
            carport_id = gid
            break
    if carport_id:
        hue_put(f"groups/{carport_id}/action", {"on": True, "bri": 254})
        print(f"[nova_hue] Automation: carport light ON (motion at night)")

        def turn_off():
            hue_put(f"groups/{carport_id}/action", {"on": False})
            print(f"[nova_hue] Automation: carport light OFF (5 min timer)")

        if automation.carport_timer:
            automation.carport_timer.cancel()
        automation.carport_timer = threading.Timer(300, turn_off)
        automation.carport_timer.daemon = True
        automation.carport_timer.start()


def sensor_monitor_loop():
    """Background thread: poll sensors every 30s, apply rules."""
    print("[nova_hue] Sensor monitor started")
    while True:
        try:
            sensors = get_all_sensors()
            if not sensors:
                time.sleep(SENSOR_POLL_INTERVAL)
                continue

            outdoor = find_outdoor_sensors(sensors)
            now = datetime.now()

            # Motion detection
            motion_sensor = outdoor.get("motion")
            if motion_sensor:
                motion_detected = motion_sensor.get("presence", False)
                automation.last_outdoor_motion = motion_detected
                if motion_detected:
                    automation.last_motion_time = now
                    automation.nobody_home_reported = False

                    # Night motion alert
                    if automation.is_night():
                        insert_observation(
                            "nova_hue", "security", "outdoor_motion",
                            f"Motion detected outdoors at {now.strftime('%H:%M:%S')}",
                            severity="warning",
                            metadata={"sensor": motion_sensor.get("name"), "time": now.isoformat()}
                        )
                        nova_config.notify_local(
                            "Outdoor Motion",
                            f"Motion detected at {now.strftime('%H:%M')}",
                            critical=True
                        )
                        # Automation: turn on carport light
                        turn_on_carport_temporarily()

            # Temperature logging (every hour)
            temp_sensor = outdoor.get("temperature")
            if temp_sensor:
                temp_c = temp_sensor.get("temperature", 0)
                automation.last_outdoor_temp = temp_c
                if automation.last_temp_log is None or (now - automation.last_temp_log).seconds >= TEMP_LOG_INTERVAL:
                    insert_observation(
                        "nova_hue", "environment", "outdoor_temperature",
                        f"Outdoor temperature: {temp_c:.1f}°C ({temp_c * 9/5 + 32:.1f}°F)",
                        severity="info",
                        metadata={"temp_c": temp_c, "temp_f": temp_c * 9/5 + 32}
                    )
                    automation.last_temp_log = now

            # Light level
            light_sensor = outdoor.get("lightlevel")
            if light_sensor:
                automation.last_outdoor_light = light_sensor.get("lightlevel", 0)

            # Nobody home detection (no motion anywhere for 2 hours)
            if automation.last_motion_time:
                elapsed = (now - automation.last_motion_time).total_seconds()
                if elapsed >= NO_MOTION_TIMEOUT and not automation.nobody_home_reported:
                    insert_observation(
                        "nova_hue", "presence", "nobody_home",
                        f"No motion detected for {elapsed/3600:.1f} hours — possible nobody home",
                        severity="info",
                        metadata={"hours_inactive": elapsed / 3600}
                    )
                    automation.nobody_home_reported = True

        except Exception as e:
            print(f"[nova_hue] Sensor monitor error: {e}", file=sys.stderr)

        time.sleep(SENSOR_POLL_INTERVAL)


# ── Dashboard state writer thread ────────────────────────────────────────────

def state_writer_loop():
    """Background thread: write dashboard state every 60s."""
    print("[nova_hue] State writer started")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            lights = get_all_lights()
            rooms = get_all_rooms()
            sensors = get_all_sensors()

            lights_on = sum(1 for l in lights.values() if l.get("on"))
            rooms_on = sum(1 for r in rooms.values() if r.get("any_on"))

            outdoor = find_outdoor_sensors(sensors)

            state = {
                "last_update": datetime.now().isoformat(),
                "rooms_on": rooms_on,
                "total_lights": len(lights),
                "lights_on": lights_on,
                "outdoor_temp_c": automation.last_outdoor_temp,
                "outdoor_motion": automation.last_outdoor_motion,
                "outdoor_light_level": automation.last_outdoor_light,
                "rooms": [
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "any_on": r["any_on"],
                        "all_on": r["all_on"],
                        "light_count": r["light_count"],
                        "brightness": r["brightness"],
                    }
                    for r in rooms.values()
                ],
                "sensors": [
                    {
                        "id": s["id"],
                        "name": s["name"],
                        "type": s["type"],
                        **({k: v for k, v in s.items() if k not in ("id", "name", "type")})
                    }
                    for s in sensors.values()
                ],
            }

            STATE_FILE.write_text(json.dumps(state, indent=2))
        except Exception as e:
            print(f"[nova_hue] State writer error: {e}", file=sys.stderr)

        time.sleep(STATE_WRITE_INTERVAL)


# ── Nova command interface ───────────────────────────────────────────────────

# Color name to hue/sat mapping (Hue uses 0-65535 for hue, 0-254 for sat)
COLOR_MAP = {
    "red": {"hue": 0, "sat": 254},
    "orange": {"hue": 6000, "sat": 254},
    "yellow": {"hue": 12750, "sat": 254},
    "green": {"hue": 25500, "sat": 254},
    "cyan": {"hue": 34000, "sat": 254},
    "blue": {"hue": 46920, "sat": 254},
    "purple": {"hue": 48000, "sat": 254},
    "pink": {"hue": 56100, "sat": 254},
    "white": {"hue": 34076, "sat": 24},
    "warm": {"hue": 8597, "sat": 140},
    "cool": {"hue": 34076, "sat": 80},
}


def find_room_by_name(name: str) -> tuple[str, dict] | None:
    """Find a room by fuzzy name match."""
    rooms = get_all_rooms()
    name_lower = name.lower().strip()
    # Exact match
    for gid, room in rooms.items():
        if room["name"].lower() == name_lower:
            return gid, room
    # Partial match
    for gid, room in rooms.items():
        if name_lower in room["name"].lower():
            return gid, room
    return None


def execute_command(text: str) -> str:
    """Parse and execute a natural language Hue command. Returns confirmation string."""
    text = text.strip().lower()

    # Status query
    if text in ("what's on?", "what's on", "status", "whats on", "what is on"):
        rooms = get_all_rooms()
        on_rooms = [r for r in rooms.values() if r["any_on"]]
        if not on_rooms:
            return "All lights are off."
        lines = [f"Rooms with lights on ({len(on_rooms)}):"]
        for r in on_rooms:
            lines.append(f"  - {r['name']} (brightness: {int(r['brightness']/254*100)}%)")
        return "\n".join(lines)

    # All off
    if text in ("all lights off", "everything off", "all off", "lights off"):
        rooms = get_all_rooms()
        for gid in rooms:
            hue_put(f"groups/{gid}/action", {"on": False})
        return "All lights turned off."

    # All on
    if text in ("all lights on", "everything on", "all on", "lights on"):
        rooms = get_all_rooms()
        for gid in rooms:
            hue_put(f"groups/{gid}/action", {"on": True})
        return "All lights turned on."

    # Scene activation: "activate <scene> scene in <room>" / "activate <scene> in <room>"
    scene_match = re.match(r"activate\s+(.+?)\s+(?:scene\s+)?in\s+(.+)", text)
    if scene_match:
        scene_name = scene_match.group(1).strip()
        room_name = scene_match.group(2).strip()
        room_result = find_room_by_name(room_name)
        if not room_result:
            return f"Room '{room_name}' not found."
        gid, room = room_result
        scenes = get_all_scenes()
        room_scenes = scenes.get(gid, [])
        target_scene = None
        for sc in room_scenes:
            if scene_name in sc["name"].lower():
                target_scene = sc
                break
        if not target_scene:
            available = ", ".join(s["name"] for s in room_scenes) if room_scenes else "none"
            return f"Scene '{scene_name}' not found in {room['name']}. Available: {available}"
        hue_put(f"groups/{gid}/action", {"scene": target_scene["id"]})
        return f"Activated '{target_scene['name']}' in {room['name']}."

    # Color set: "<room> <color>" / "set <room> to <color>"
    color_match = re.match(r"(?:set\s+)?(.+?)\s+(?:to\s+)?(" + "|".join(COLOR_MAP.keys()) + r")$", text)
    if color_match:
        room_name = color_match.group(1).strip()
        color_name = color_match.group(2).strip()
        # Remove "set" prefix and "to" from room name
        room_name = re.sub(r"^set\s+", "", room_name)
        room_name = re.sub(r"\s+to$", "", room_name)
        room_result = find_room_by_name(room_name)
        if not room_result:
            return f"Room '{room_name}' not found."
        gid, room = room_result
        color = COLOR_MAP[color_name]
        hue_put(f"groups/{gid}/action", {"on": True, **color})
        return f"Set {room['name']} to {color_name}."

    # Dim: "<room> to <N>%" / "dim <room> to <N>%"
    dim_match = re.match(r"(?:dim\s+)?(.+?)\s+(?:to\s+)?(\d+)\s*%", text)
    if dim_match:
        room_name = dim_match.group(1).strip()
        pct = int(dim_match.group(2))
        room_result = find_room_by_name(room_name)
        if not room_result:
            return f"Room '{room_name}' not found."
        gid, room = room_result
        bri = max(1, min(254, int(pct / 100 * 254)))
        hue_put(f"groups/{gid}/action", {"on": True, "bri": bri})
        return f"Dimmed {room['name']} to {pct}%."

    # Turn on/off: "turn on/off <room>" or "<room> on/off"
    on_off_match = re.match(r"turn\s+(on|off)\s+(.+)", text)
    if not on_off_match:
        on_off_match = re.match(r"(.+?)\s+(on|off)$", text)
        if on_off_match:
            # Swap groups: room is group 1, state is group 2
            room_name = on_off_match.group(1).strip()
            state = on_off_match.group(2)
        else:
            return f"I don't understand: '{text}'. Try 'turn on kitchen', 'dim office to 50%', or 'status'."
    else:
        state = on_off_match.group(1)
        room_name = on_off_match.group(2).strip()

    room_result = find_room_by_name(room_name)
    if not room_result:
        return f"Room '{room_name}' not found."
    gid, room = room_result
    on = state == "on"
    hue_put(f"groups/{gid}/action", {"on": on})
    return f"Turned {'on' if on else 'off'} {room['name']}."


# ── HTTP API ─────────────────────────────────────────────────────────────────

class HueAPIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the Hue integration API."""

    def log_message(self, format, *args):
        """Custom log format."""
        print(f"[nova_hue] {self.address_string()} {format % args}")

    def _send_json(self, data: dict | list, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, indent=2).encode())

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        return json.loads(body)

    def do_GET(self):
        path = self.path.rstrip("/")

        if path == "/health":
            self._send_json({
                "status": "ok",
                "service": "nova_hue",
                "bridge": HUE_BRIDGE,
                "port": HTTP_PORT,
                "uptime_seconds": int(time.time() - _start_time),
                "timestamp": datetime.now().isoformat(),
            })

        elif path == "/lights":
            lights = get_all_lights()
            self._send_json({"count": len(lights), "lights": list(lights.values())})

        elif path == "/rooms":
            rooms = get_all_rooms()
            self._send_json({"count": len(rooms), "rooms": list(rooms.values())})

        elif path == "/sensors":
            sensors = get_all_sensors()
            self._send_json({"count": len(sensors), "sensors": list(sensors.values())})

        elif path == "/scenes":
            scenes = get_all_scenes()
            rooms = get_all_rooms()
            # Enrich with room names
            enriched = {}
            for gid, scene_list in scenes.items():
                room_name = rooms.get(gid, {}).get("name", f"Group {gid}")
                enriched[room_name] = scene_list
            self._send_json({"scenes_by_room": enriched})

        elif path == "/status":
            lights = get_all_lights()
            rooms = get_all_rooms()
            lights_on = sum(1 for l in lights.values() if l.get("on"))
            rooms_on = sum(1 for r in rooms.values() if r.get("any_on"))
            self._send_json({
                "rooms_on": rooms_on,
                "total_rooms": len(rooms),
                "total_lights": len(lights),
                "lights_on": lights_on,
                "outdoor_temp_c": automation.last_outdoor_temp,
                "outdoor_motion": automation.last_outdoor_motion,
                "outdoor_light_level": automation.last_outdoor_light,
                "timestamp": datetime.now().isoformat(),
            })

        else:
            self._send_json({"error": "not found", "endpoints": [
                "/health", "/lights", "/rooms", "/sensors", "/scenes", "/status"
            ]}, 404)

    def do_POST(self):
        path = self.path.rstrip("/")

        # POST /lights/{id}/state
        light_match = re.match(r"/lights/(\d+)/state", path)
        if light_match:
            light_id = light_match.group(1)
            body = self._read_body()
            result = hue_put(f"lights/{light_id}/state", body)
            if result is not None:
                self._send_json({"success": True, "light_id": light_id, "applied": body})
            else:
                self._send_json({"error": "failed to set light state"}, 500)
            return

        # POST /rooms/{id}/action
        room_match = re.match(r"/rooms/(\d+)/action", path)
        if room_match:
            group_id = room_match.group(1)
            body = self._read_body()
            # If scene name provided, resolve it
            if "scene" in body and not body["scene"].startswith("scene_"):
                scenes = get_all_scenes()
                room_scenes = scenes.get(group_id, [])
                scene_name = body["scene"].lower()
                for sc in room_scenes:
                    if scene_name in sc["name"].lower():
                        body["scene"] = sc["id"]
                        break
            result = hue_put(f"groups/{group_id}/action", body)
            if result is not None:
                self._send_json({"success": True, "group_id": group_id, "applied": body})
            else:
                self._send_json({"error": "failed to set room action"}, 500)
            return

        # POST /scenes/{id}/recall
        scene_match = re.match(r"/scenes/(.+)/recall", path)
        if scene_match:
            scene_id = scene_match.group(1)
            # Find which group this scene belongs to
            scenes_data = hue_get("scenes")
            if scenes_data and scene_id in scenes_data:
                group_id = scenes_data[scene_id].get("group", "0")
                result = hue_put(f"groups/{group_id}/action", {"scene": scene_id})
                if result is not None:
                    self._send_json({"success": True, "scene_id": scene_id, "group_id": group_id})
                else:
                    self._send_json({"error": "failed to recall scene"}, 500)
            else:
                self._send_json({"error": f"scene {scene_id} not found"}, 404)
            return

        # POST /command — text command interface
        if path == "/command":
            body = self._read_body()
            text = body.get("text", body.get("command", ""))
            if not text:
                self._send_json({"error": "missing 'text' field"}, 400)
                return
            result = execute_command(text)
            self._send_json({"result": result})
            return

        self._send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# ── Main ─────────────────────────────────────────────────────────────────────

_start_time = time.time()


def main():
    global _start_time
    _start_time = time.time()

    print(f"[nova_hue] Starting Hue integration service")
    print(f"[nova_hue] Bridge: {HUE_BRIDGE}")
    print(f"[nova_hue] Port: {HTTP_PORT}")

    # Validate API key
    get_api_key()
    print("[nova_hue] API key loaded from Keychain")

    # Quick connectivity check
    lights = hue_get("lights")
    if lights and isinstance(lights, dict):
        print(f"[nova_hue] Bridge connected — {len(lights)} lights found")
    else:
        print("[nova_hue] WARNING: Could not reach bridge or no lights found", file=sys.stderr)

    # Start background threads
    sensor_thread = threading.Thread(target=sensor_monitor_loop, daemon=True)
    sensor_thread.start()

    state_thread = threading.Thread(target=state_writer_loop, daemon=True)
    state_thread.start()

    # Start HTTP server
    server = HTTPServer(("0.0.0.0", HTTP_PORT), HueAPIHandler)
    print(f"[nova_hue] HTTP server listening on 0.0.0.0:{HTTP_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[nova_hue] Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
