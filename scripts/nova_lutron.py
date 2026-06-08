#!/opt/homebrew/bin/python3
"""
nova_lutron.py — Lutron Caseta integration daemon for Nova.

Maintains a persistent TLS connection to the Lutron Smart Bridge,
exposes an HTTP API on port 37477 for device control and status,
and writes state to the dashboard JSON file every 30 seconds.

Written by Jordan Koch.
"""

import asyncio
import datetime
import json
import logging
import os
import re
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

# -- Configuration --

BRIDGE_HOST = "192.168.1.55"
CERT_DIR = Path.home() / ".openclaw" / "certs" / "lutron"
KEY_FILE = str(CERT_DIR / "caseta.key")
CERT_FILE = str(CERT_DIR / "caseta.crt")
CA_FILE = str(CERT_DIR / "caseta-bridge.crt")

HTTP_PORT = 37477
HTTP_HOST = "127.0.0.1"

STATE_FILE = Path.home() / ".openclaw" / "workspace" / "state" / "nova_lutron_state.json"
LOG_FILE = Path.home() / ".openclaw" / "logs" / "lutron.log"

# Device metadata (zone -> info)
DEVICE_MAP = {
    2: {"name": "Kitchen Main Lights", "type": "WallDimmer", "room": "kitchen"},
    1: {"name": "Living Room Main Lights 1", "type": "WallDimmer", "room": "living room"},
    3: {"name": "Front Porch", "type": "WallSwitch", "room": "porch"},
    4: {"name": "Living Room Main Lights 2", "type": "WallSwitch", "room": "living room"},
    5: {"name": "Outside Patio", "type": "WallSwitch", "room": "patio"},
}

# -- Logging --

os.makedirs(LOG_FILE.parent, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("nova_lutron")

# -- Globals --

_loop: asyncio.AbstractEventLoop | None = None
_bridge = None
_bridge_connected = False
_device_states: dict[int, dict] = {}  # zone -> {level: int, ...}
_last_state_write = 0.0


# -- Natural Language Command Parser --

def parse_command(text: str) -> list[dict]:
    """Parse natural language command into actions.

    Returns list of {zone: int, action: str, level: int|None}
    """
    text = text.strip().lower()
    actions = []

    # "all on" / "all off"
    if text in ("all on", "everything on", "lights on"):
        for zone in DEVICE_MAP:
            actions.append({"zone": zone, "action": "on", "level": 100})
        return actions
    if text in ("all off", "everything off", "lights off"):
        for zone in DEVICE_MAP:
            actions.append({"zone": zone, "action": "off", "level": 0})
        return actions

    # "status" / "what's on"
    if text in ("status", "what's on", "whats on", "what is on"):
        return [{"action": "status"}]

    # Room-based commands
    room_patterns = [
        (r"kitchen", [2]),
        (r"living\s*room", [1, 4]),
        (r"porch|front\s*porch", [3]),
        (r"patio|outside\s*patio", [5]),
    ]

    target_zones = []
    for pattern, zones in room_patterns:
        if re.search(pattern, text):
            target_zones.extend(zones)
            break

    if not target_zones:
        return []

    # Determine action
    # Check for percentage: "50%", "dim 75", "75 percent"
    pct_match = re.search(r"(\d{1,3})\s*%|dim\s+(\d{1,3})|(\d{1,3})\s*percent", text)
    if pct_match:
        level = int(pct_match.group(1) or pct_match.group(2) or pct_match.group(3))
        level = max(0, min(100, level))
        for zone in target_zones:
            actions.append({"zone": zone, "action": "set", "level": level})
    elif "off" in text:
        for zone in target_zones:
            actions.append({"zone": zone, "action": "off", "level": 0})
    elif "on" in text:
        for zone in target_zones:
            actions.append({"zone": zone, "action": "on", "level": 100})
    else:
        # Default: toggle on
        for zone in target_zones:
            actions.append({"zone": zone, "action": "on", "level": 100})

    return actions


def get_status_text() -> str:
    """Generate human-readable status string."""
    if not _bridge_connected:
        return "Bridge disconnected"

    lines = []
    on_count = 0
    for zone, info in sorted(DEVICE_MAP.items()):
        state = _device_states.get(zone, {})
        level = state.get("level", 0)
        if level > 0:
            on_count += 1
            if info["type"] == "WallDimmer":
                lines.append(f"  {info['name']}: {level}%")
            else:
                lines.append(f"  {info['name']}: ON")

    if on_count == 0:
        return "All lights are off."

    header = f"{on_count} light{'s' if on_count != 1 else ''} on:"
    return header + "\n" + "\n".join(lines)


async def execute_command(text: str) -> str:
    """Execute a natural language command against the bridge."""
    global _bridge, _bridge_connected

    if not _bridge or not _bridge_connected:
        return "Error: Bridge not connected"

    actions = parse_command(text)
    if not actions:
        return f"Could not understand command: '{text}'"

    # Status request
    if len(actions) == 1 and actions[0].get("action") == "status":
        return get_status_text()

    results = []
    for action in actions:
        zone = action["zone"]
        level = action.get("level", 0)
        info = DEVICE_MAP.get(zone, {})
        name = info.get("name", f"Zone {zone}")

        try:
            if action["action"] == "off":
                await _bridge.turn_off(str(zone))
                results.append(f"{name}: turned off")
            elif action["action"] == "on":
                await _bridge.turn_on(str(zone))
                results.append(f"{name}: turned on")
            elif action["action"] == "set":
                await _bridge.set_value(str(zone), level)
                results.append(f"{name}: set to {level}%")
        except Exception as e:
            results.append(f"{name}: ERROR - {e}")

    return "\n".join(results)


# -- State Management --

def write_state():
    """Write current state to JSON file for dashboard consumption."""
    global _last_state_write

    devices = []
    devices_on = 0
    for zone, info in sorted(DEVICE_MAP.items()):
        state = _device_states.get(zone, {})
        level = state.get("level", 0)
        if level > 0:
            devices_on += 1
        devices.append({
            "name": info["name"],
            "zone": zone,
            "type": info["type"],
            "room": info["room"],
            "level": level,
        })

    state_data = {
        "last_update": datetime.datetime.now().isoformat(),
        "bridge_connected": _bridge_connected,
        "devices_on": devices_on,
        "total_devices": len(DEVICE_MAP),
        "devices": devices,
    }

    os.makedirs(STATE_FILE.parent, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state_data, indent=2))
    _last_state_write = time.time()


# -- Bridge Connection --

async def on_device_changed(device_id: str):
    """Callback for device state changes."""
    global _device_states

    if not _bridge:
        return

    try:
        devices = _bridge.get_devices()
        device = devices.get(device_id)
        if device:
            zone = int(device.get("zone", device_id))
            level = int(device.get("current_state", 0))
            old_level = _device_states.get(zone, {}).get("level", -1)
            _device_states[zone] = {"level": level}

            info = DEVICE_MAP.get(zone, {})
            name = info.get("name", f"Zone {zone}")

            if old_level != level:
                if level > 0 and old_level <= 0:
                    log.info(f"Device ON: {name} (zone {zone}) -> {level}%")
                elif level <= 0 and old_level > 0:
                    log.info(f"Device OFF: {name} (zone {zone})")
                else:
                    log.info(f"Device change: {name} (zone {zone}) -> {level}%")

                # Write state immediately on change
                write_state()
    except Exception as e:
        log.error(f"Error in device change callback: {e}")


async def update_all_states():
    """Poll all device states from the bridge."""
    global _device_states

    if not _bridge:
        return

    try:
        devices = _bridge.get_devices()
        for device_id, device in devices.items():
            zone_str = device.get("zone", device_id)
            try:
                zone = int(zone_str)
            except (ValueError, TypeError):
                continue
            if zone in DEVICE_MAP:
                level = int(device.get("current_state", 0))
                _device_states[zone] = {"level": level}
    except Exception as e:
        log.error(f"Error updating states: {e}")


async def bridge_loop():
    """Main async loop: connect to bridge, subscribe to events, update state."""
    global _bridge, _bridge_connected

    from pylutron_caseta.smartbridge import Smartbridge

    while True:
        try:
            log.info(f"Connecting to Lutron Smart Bridge at {BRIDGE_HOST}...")
            _bridge = Smartbridge.create_tls(
                hostname=BRIDGE_HOST,
                keyfile=KEY_FILE,
                certfile=CERT_FILE,
                ca_certs=CA_FILE,
            )
            await _bridge.connect()
            _bridge_connected = True
            log.info("Connected to Lutron Smart Bridge")

            # Initial state fetch
            await update_all_states()
            write_state()

            # Subscribe to device changes
            devices = _bridge.get_devices()
            for device_id in devices:
                _bridge.add_subscriber(device_id, lambda did=device_id: asyncio.ensure_future(on_device_changed(did)))

            log.info(f"Subscribed to {len(devices)} device(s)")

            # Keep-alive loop: update state every 30s
            while _bridge_connected:
                await asyncio.sleep(30)
                await update_all_states()
                write_state()

        except Exception as e:
            _bridge_connected = False
            log.error(f"Bridge connection error: {e}")
            write_state()
            log.info("Reconnecting in 15 seconds...")
            await asyncio.sleep(15)


def start_bridge_loop():
    """Entry point for the bridge thread."""
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _loop.run_until_complete(bridge_loop())


# -- HTTP Server --

class LutronHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the Lutron API."""

    def log_message(self, format, *args):
        """Suppress default HTTP logging (we use our own logger)."""
        pass

    def _send_json(self, data: dict, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def _read_body(self) -> dict:
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}
        body = self.rfile.read(content_length)
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path == "/health":
            self._send_json({
                "status": "ok" if _bridge_connected else "disconnected",
                "bridge_connected": _bridge_connected,
                "bridge_host": BRIDGE_HOST,
                "devices_tracked": len(DEVICE_MAP),
                "uptime_s": int(time.time() - _start_time),
            })

        elif self.path == "/devices":
            devices = []
            for zone, info in sorted(DEVICE_MAP.items()):
                state = _device_states.get(zone, {})
                devices.append({
                    "zone": zone,
                    "name": info["name"],
                    "type": info["type"],
                    "room": info["room"],
                    "level": state.get("level", 0),
                    "on": state.get("level", 0) > 0,
                })
            self._send_json({"devices": devices, "bridge_connected": _bridge_connected})

        elif self.path == "/status":
            on_devices = []
            off_devices = []
            for zone, info in sorted(DEVICE_MAP.items()):
                state = _device_states.get(zone, {})
                level = state.get("level", 0)
                entry = {"name": info["name"], "zone": zone, "type": info["type"], "room": info["room"], "level": level}
                if level > 0:
                    on_devices.append(entry)
                else:
                    off_devices.append(entry)

            active_rooms = list(set(d["room"] for d in on_devices))
            self._send_json({
                "bridge_connected": _bridge_connected,
                "devices_on": len(on_devices),
                "devices_off": len(off_devices),
                "total_devices": len(DEVICE_MAP),
                "active_rooms": active_rooms,
                "on_devices": on_devices,
                "off_devices": off_devices,
                "summary": get_status_text(),
            })

        else:
            self._send_json({"error": "Not found"}, 404)

    def do_POST(self):
        # POST /devices/{zone}/set
        zone_match = re.match(r"/devices/(\d+)/set", self.path)
        if zone_match:
            zone = int(zone_match.group(1))
            if zone not in DEVICE_MAP:
                self._send_json({"error": f"Unknown zone: {zone}"}, 404)
                return

            body = self._read_body()
            info = DEVICE_MAP[zone]

            if _loop is None:
                self._send_json({"error": "Bridge loop not started"}, 503)
                return

            async def do_set():
                if "level" in body:
                    level = max(0, min(100, int(body["level"])))
                    if level == 0:
                        await _bridge.turn_off(str(zone))
                    else:
                        await _bridge.set_value(str(zone), level)
                    return f"Set {info['name']} to {level}%"
                elif "on" in body:
                    if body["on"]:
                        await _bridge.turn_on(str(zone))
                        return f"Turned on {info['name']}"
                    else:
                        await _bridge.turn_off(str(zone))
                        return f"Turned off {info['name']}"
                else:
                    return "No action specified (need 'level' or 'on')"

            try:
                future = asyncio.run_coroutine_threadsafe(do_set(), _loop)
                result = future.result(timeout=10)
                self._send_json({"ok": True, "response": result})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        # POST /command
        if self.path == "/command":
            body = self._read_body()
            command = body.get("command", "")
            if not command:
                self._send_json({"error": "No command specified"}, 400)
                return

            if _loop is None:
                self._send_json({"error": "Bridge loop not started"}, 503)
                return

            try:
                future = asyncio.run_coroutine_threadsafe(execute_command(command), _loop)
                result = future.result(timeout=10)
                self._send_json({"ok": True, "command": command, "response": result})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        self._send_json({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


# -- Main --

_start_time = time.time()

if __name__ == "__main__":
    log.info("Nova Lutron Caseta daemon starting...")
    log.info(f"Bridge: {BRIDGE_HOST}")
    log.info(f"HTTP API: http://{HTTP_HOST}:{HTTP_PORT}")
    log.info(f"Devices: {len(DEVICE_MAP)}")

    # Start bridge connection in background thread
    bridge_thread = threading.Thread(target=start_bridge_loop, daemon=True)
    bridge_thread.start()

    # Give bridge a moment to initialize
    time.sleep(2)

    # HTTP server in main thread
    server = HTTPServer((HTTP_HOST, HTTP_PORT), LutronHTTPHandler)
    log.info(f"HTTP server listening on {HTTP_HOST}:{HTTP_PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down...")
        server.shutdown()
