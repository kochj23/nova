#!/opt/homebrew/bin/python3
"""
nova_av_poller.py — AV State Poller Daemon

Monitors Onkyo receivers via eISCP and Bose soundbars via UPnP SOAP,
recording state snapshots to telemetry.av_state and power transitions
to telemetry.device_power_events.

Devices:
  - Onkyo TX-NR696  @ 192.168.1.98  (eISCP, Zone 2)
  - Onkyo TX-NR5100 @ 192.168.1.145 (eISCP, single zone)
  - Bose Bedroom    @ 192.168.1.25  (UPnP 8091)
  - Bose Guest Bedroom @ 192.168.1.82 (UPnP 8091)
  - Bose Kitchen    @ 192.168.1.197 (UPnP 8091)

Written by Jordan Koch.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import json
import logging
import os
import socket
import struct
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras

import nova_config

# ── Logging ──────────────────────────────────────────────────────────────────

LOG_PATH = str(Path.home() / ".openclaw/logs/av_poller.log")
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("av_poller")

# ── Device Definitions ───────────────────────────────────────────────────────

ONKYO_DEVICES = [
    {
        "name": "Onkyo TX-NR696",
        "ip": "192.168.1.98",
        "port": 60128,
        "zones": ["main", "zone2"],
    },
    {
        "name": "Onkyo TX-NR5100",
        "ip": "192.168.1.145",
        "port": 60128,
        "zones": ["main"],
    },
]

BOSE_DEVICES = [
    {"name": "Bose Bedroom", "ip": "192.168.1.25", "port": 8091},
    {"name": "Bose Guest Bedroom", "ip": "192.168.1.82", "port": 8091},
    {"name": "Bose Kitchen", "ip": "192.168.1.197", "port": 8091},
]

# ── Poll Intervals ───────────────────────────────────────────────────────────

POLL_ACTIVE_SEC = 30
POLL_STANDBY_SEC = 300

# ── Database ─────────────────────────────────────────────────────────────────

DB_DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"


def get_db():
    """Get a psycopg2 connection."""
    conn = psycopg2.connect(DB_DSN)
    conn.autocommit = True
    return conn


def ensure_tables(conn):
    """Schema already created by telemetry_schema.sql — just verify connectivity."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM telemetry.av_state LIMIT 0")
        cur.execute("SELECT 1 FROM telemetry.device_power_events LIMIT 0")


def record_state(conn, device_name, zone, state):
    """Insert a state snapshot into telemetry.av_state."""
    power_bool = state.get("power") in ("on", "ON", True, "PWR00")
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO telemetry.av_state
                (ts, device_id, device_type, power, volume, mute,
                 source_input, listening_mode, media_title, zone)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            device_name,
            state.get("device_type", "unknown"),
            power_bool,
            state.get("volume"),
            state.get("muted", False),
            state.get("input_source", ""),
            state.get("listening_mode", ""),
            state.get("media_uri", ""),
            zone,
        ))


AV_ROOM_MAP = {
    "Onkyo TX-NR5100": "living_room",
    "Onkyo TX-NR696": "living_room",
    "Bose Bedroom": "bedroom",
    "Bose Guest Bedroom": "guest_bedroom",
    "Bose Kitchen": "kitchen",
}


def record_power_event(conn, device_name, zone, old_state, new_state):
    """Insert a power state transition and feed presence engine."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO telemetry.device_power_events
                (ts, device_id, event, source_input)
            VALUES (NOW(), %s, %s, %s)
        """, (device_name, new_state, zone))
    log.info(f"Power event: {device_name}/{zone} {old_state} -> {new_state}")

    # Feed presence engine on power-on events
    room = AV_ROOM_MAP.get(device_name)
    if room and new_state in ("on", "ON", "PWR01"):
        confidence = 0.7 if "Onkyo" in device_name else 0.5
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata)
                    VALUES (NOW(), 'av_inferred', %s, %s, 'av_power', %s)
                """, (room, confidence, json.dumps({"device": device_name, "zone": zone})))
        except Exception as e:
            log.warning(f"Presence write failed: {e}")


# ── eISCP Protocol ───────────────────────────────────────────────────────────

def eiscp_build_frame(command: str) -> bytes:
    """Build an eISCP TCP frame for a command like 'PWRQSTN'."""
    # Data portion: !1<CMD>\r
    data = f"!1{command}\r".encode("ascii")
    # Header: ISCP + header_size(16) + data_size + version(1) + 3 reserved
    header_size = 16
    data_size = len(data)
    version = 1
    frame = b"ISCP"
    frame += struct.pack(">I", header_size)
    frame += struct.pack(">I", data_size)
    frame += struct.pack("B", version)
    frame += b"\x00\x00\x00"
    frame += data
    return frame


def eiscp_parse_response(data: bytes) -> str | None:
    """Parse an eISCP frame and return the command payload (e.g., 'PWR01')."""
    if len(data) < 16:
        return None
    if data[:4] != b"ISCP":
        return None
    header_size = struct.unpack(">I", data[4:8])[0]
    data_size = struct.unpack(">I", data[8:12])[0]
    payload = data[header_size:header_size + data_size]
    # Strip framing: !1....\r\n or !1....\x1a\r\n
    payload_str = payload.decode("ascii", errors="ignore")
    # Remove leading !1 and trailing control chars
    payload_str = payload_str.strip("\r\n\x1a\x00")
    if payload_str.startswith("!1"):
        payload_str = payload_str[2:]
    return payload_str.strip()


class OnkyoConnection:
    """Persistent TCP connection to an Onkyo receiver via eISCP."""

    def __init__(self, ip: str, port: int, name: str):
        self.ip = ip
        self.port = port
        self.name = name
        self.sock: socket.socket | None = None
        self.lock = threading.Lock()
        self._buffer = b""

    def connect(self):
        """Establish TCP connection."""
        with self.lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
            try:
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(5.0)
                self.sock.connect((self.ip, self.port))
                self.sock.settimeout(2.0)
                self._buffer = b""
                log.info(f"Connected to {self.name} at {self.ip}:{self.port}")
            except Exception as e:
                log.error(f"Connection failed to {self.name}: {e}")
                self.sock = None

    def is_connected(self) -> bool:
        return self.sock is not None

    def send_command(self, cmd: str) -> str | None:
        """Send a command and wait for a matching response."""
        with self.lock:
            if not self.sock:
                return None
            try:
                frame = eiscp_build_frame(cmd)
                self.sock.sendall(frame)
                return self._read_response(cmd[:3])
            except Exception as e:
                log.warning(f"Send failed to {self.name}: {e}")
                self.sock = None
                return None

    def _read_response(self, prefix: str, timeout: float = 2.0) -> str | None:
        """Read responses until we find one starting with the expected prefix."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = self.sock.recv(4096)
                if not chunk:
                    self.sock = None
                    return None
                self._buffer += chunk
            except socket.timeout:
                pass
            except Exception:
                self.sock = None
                return None

            # Try to parse frames from buffer
            while len(self._buffer) >= 16:
                if self._buffer[:4] != b"ISCP":
                    # Scan for next ISCP marker
                    idx = self._buffer.find(b"ISCP", 1)
                    if idx == -1:
                        self._buffer = b""
                        break
                    self._buffer = self._buffer[idx:]
                    continue

                header_size = struct.unpack(">I", self._buffer[4:8])[0]
                data_size = struct.unpack(">I", self._buffer[8:12])[0]
                frame_len = header_size + data_size
                if len(self._buffer) < frame_len:
                    break  # Incomplete frame

                frame = self._buffer[:frame_len]
                self._buffer = self._buffer[frame_len:]
                resp = eiscp_parse_response(frame)
                if resp and resp.startswith(prefix):
                    return resp

        return None

    def close(self):
        with self.lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None


def query_onkyo(conn: OnkyoConnection, zone: str) -> dict:
    """Query all state for a zone. Returns state dict."""
    state = {"power": "unknown", "volume": None, "muted": None,
             "input_source": None, "listening_mode": None}

    if not conn.is_connected():
        conn.connect()
    if not conn.is_connected():
        state["power"] = "unreachable"
        return state

    # Zone prefixes for commands
    if zone == "zone2":
        pwr_cmd, vol_cmd, mute_cmd, inp_cmd = "ZPWQSTN", "ZVLQSTN", "ZMTQSTN", "ZSLQSTN"
        pwr_pfx, vol_pfx, mute_pfx, inp_pfx = "ZPW", "ZVL", "ZMT", "ZSL"
    else:
        pwr_cmd, vol_cmd, mute_cmd, inp_cmd = "PWRQSTN", "MVLQSTN", "AMTQSTN", "SLIQSTN"
        pwr_pfx, vol_pfx, mute_pfx, inp_pfx = "PWR", "MVL", "AMT", "SLI"

    # Power
    resp = conn.send_command(pwr_cmd)
    if resp:
        val = resp[len(pwr_pfx):]
        if val == "01":
            state["power"] = "on"
        elif val == "00":
            state["power"] = "standby"
        else:
            state["power"] = val

    # Only query details if powered on
    if state["power"] == "on":
        # Volume (hex value)
        resp = conn.send_command(vol_cmd)
        if resp:
            try:
                state["volume"] = int(resp[len(vol_pfx):], 16)
            except ValueError:
                pass

        # Mute
        resp = conn.send_command(mute_cmd)
        if resp:
            val = resp[len(mute_pfx):]
            state["muted"] = (val == "01")

        # Input
        resp = conn.send_command(inp_cmd)
        if resp:
            state["input_source"] = resp[len(inp_pfx):]

        # Listening mode (main zone only)
        if zone == "main":
            resp = conn.send_command("LMDQSTN")
            if resp:
                state["listening_mode"] = resp[3:]

    return state


# ── UPnP SOAP for Bose ──────────────────────────────────────────────────────

SOAP_ENVELOPE = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
    ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
    '<s:Body>{body}</s:Body></s:Envelope>'
)

UPNP_NS = {
    "s": "http://schemas.xmlsoap.org/soap/envelope/",
    "u": "urn:schemas-upnp-org:service:RenderingControl:1",
    "avt": "urn:schemas-upnp-org:service:AVTransport:1",
}


def soap_request(ip: str, port: int, path: str, service_type: str,
                 action: str, args: str = "") -> str | None:
    """Send a UPnP SOAP request and return the response body XML."""
    body = (
        f'<u:{action} xmlns:u="{service_type}">'
        f'<InstanceID>0</InstanceID>{args}'
        f'</u:{action}>'
    )
    envelope = SOAP_ENVELOPE.format(body=body)
    url = f"http://{ip}:{port}{path}"
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{service_type}#{action}"',
    }
    req = urllib.request.Request(url, data=envelope.encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.debug(f"SOAP request to {ip} failed: {e}")
        return None


def query_bose(device: dict) -> dict:
    """Query Bose soundbar state via UPnP."""
    ip = device["ip"]
    port = device["port"]
    state = {"power": "unknown", "volume": None, "muted": None,
             "transport_state": None, "media_uri": None}

    rc_service = "urn:schemas-upnp-org:service:RenderingControl:1"
    avt_service = "urn:schemas-upnp-org:service:AVTransport:1"

    # Get Volume
    resp = soap_request(ip, port, "/RenderingControl/Control",
                        rc_service, "GetVolume",
                        '<Channel>Master</Channel>')
    if resp:
        try:
            root = ET.fromstring(resp)
            vol_el = root.find(".//{urn:schemas-upnp-org:service:RenderingControl:1}GetVolumeResponse/CurrentVolume")
            if vol_el is None:
                # Try without namespace prefix
                for el in root.iter():
                    if "CurrentVolume" in el.tag:
                        state["volume"] = int(el.text)
                        break
            else:
                state["volume"] = int(vol_el.text)
        except Exception:
            pass

    # Get Mute
    resp = soap_request(ip, port, "/RenderingControl/Control",
                        rc_service, "GetMute",
                        '<Channel>Master</Channel>')
    if resp:
        try:
            root = ET.fromstring(resp)
            for el in root.iter():
                if "CurrentMute" in el.tag:
                    state["muted"] = el.text in ("1", "true", "True")
                    break
        except Exception:
            pass

    # Get Transport Info
    resp = soap_request(ip, port, "/AVTransport/Control",
                        avt_service, "GetTransportInfo", "")
    if resp:
        try:
            root = ET.fromstring(resp)
            for el in root.iter():
                if "CurrentTransportState" in el.tag:
                    state["transport_state"] = el.text
                    break
        except Exception:
            pass

    # Get Media Info
    resp = soap_request(ip, port, "/AVTransport/Control",
                        avt_service, "GetMediaInfo", "")
    if resp:
        try:
            root = ET.fromstring(resp)
            for el in root.iter():
                if "CurrentURI" in el.tag and el.text:
                    state["media_uri"] = el.text
                    break
        except Exception:
            pass

    # Determine power state from available info
    if state["volume"] is not None:
        # If we got a volume response, device is reachable
        if state["transport_state"] in ("PLAYING", "TRANSITIONING"):
            state["power"] = "on"
        elif state["transport_state"] == "STOPPED" and state["volume"] is not None:
            state["power"] = "on"
        else:
            state["power"] = "standby"
    else:
        state["power"] = "unreachable"

    return state


# ── Main Daemon Loop ─────────────────────────────────────────────────────────

class AVPoller:
    """Main daemon class that manages all device polling."""

    def __init__(self):
        self.db = get_db()
        ensure_tables(self.db)
        self.onkyo_connections: dict[str, OnkyoConnection] = {}
        self.last_states: dict[str, dict] = {}  # key: "device_name/zone"
        self.last_poll_times: dict[str, float] = {}
        self.running = True

        # Initialize Onkyo connections
        for dev in ONKYO_DEVICES:
            key = dev["name"]
            self.onkyo_connections[key] = OnkyoConnection(
                dev["ip"], dev["port"], dev["name"]
            )

    def get_poll_interval(self, device_key: str) -> float:
        """Return poll interval based on last known power state."""
        last = self.last_states.get(device_key, {})
        power = last.get("power", "unknown")
        if power in ("on", "unknown"):
            return POLL_ACTIVE_SEC
        return POLL_STANDBY_SEC

    def check_power_transition(self, device_name: str, zone: str, new_state: dict):
        """Detect and record power state changes."""
        key = f"{device_name}/{zone}"
        old = self.last_states.get(key, {})
        old_power = old.get("power")
        new_power = new_state.get("power")

        if old_power and new_power and old_power != new_power:
            if new_power != "unreachable":
                try:
                    record_power_event(self.db, device_name, zone, old_power, new_power)
                except Exception as e:
                    log.error(f"Failed to record power event: {e}")
                    self._reconnect_db()

    def _reconnect_db(self):
        """Reconnect to PostgreSQL on error."""
        try:
            self.db.close()
        except Exception:
            pass
        try:
            self.db = get_db()
            log.info("Reconnected to PostgreSQL")
        except Exception as e:
            log.error(f"DB reconnection failed: {e}")

    def poll_onkyo(self, device: dict):
        """Poll one Onkyo receiver (all zones)."""
        name = device["name"]
        conn = self.onkyo_connections[name]

        for zone in device["zones"]:
            key = f"{name}/{zone}"
            now = time.time()
            interval = self.get_poll_interval(key)

            last_poll = self.last_poll_times.get(key, 0)
            if now - last_poll < interval:
                continue

            self.last_poll_times[key] = now

            try:
                state = query_onkyo(conn, zone)
            except Exception as e:
                log.error(f"Error polling {name}/{zone}: {e}")
                state = {"power": "unreachable"}

            if state["power"] == "unreachable":
                log.debug(f"{name}/{zone} unreachable")
                # Don't record unreachable as a snapshot, but track it
                self.last_states[key] = state
                continue

            # Check for power transitions
            self.check_power_transition(name, zone, state)

            # Record state snapshot
            try:
                record_state(self.db, name, zone, state)
            except Exception as e:
                log.error(f"Failed to record state for {name}/{zone}: {e}")
                self._reconnect_db()

            self.last_states[key] = state
            log.debug(f"{name}/{zone}: {state}")

    def poll_bose(self, device: dict):
        """Poll one Bose soundbar."""
        name = device["name"]
        zone = "main"
        key = f"{name}/{zone}"
        now = time.time()
        interval = self.get_poll_interval(key)

        last_poll = self.last_poll_times.get(key, 0)
        if now - last_poll < interval:
            return

        self.last_poll_times[key] = now

        try:
            state = query_bose(device)
        except Exception as e:
            log.error(f"Error polling {name}: {e}")
            state = {"power": "unreachable"}

        if state["power"] == "unreachable":
            log.debug(f"{name} unreachable")
            self.last_states[key] = state
            return

        # Check for power transitions
        self.check_power_transition(name, zone, state)

        # Record state snapshot
        try:
            record_state(self.db, name, zone, state)
        except Exception as e:
            log.error(f"Failed to record state for {name}: {e}")
            self._reconnect_db()

        self.last_states[key] = state
        log.debug(f"{name}: {state}")

    def run(self):
        """Main loop — poll all devices forever."""
        log.info("nova_av_poller starting up")
        log.info(f"Monitoring {len(ONKYO_DEVICES)} Onkyo receivers, "
                 f"{len(BOSE_DEVICES)} Bose soundbars")

        # Initial connection attempt for Onkyo devices
        for dev in ONKYO_DEVICES:
            conn = self.onkyo_connections[dev["name"]]
            conn.connect()

        while self.running:
            try:
                # Poll all Onkyo devices
                for dev in ONKYO_DEVICES:
                    self.poll_onkyo(dev)

                # Poll all Bose devices
                for dev in BOSE_DEVICES:
                    self.poll_bose(dev)

            except KeyboardInterrupt:
                log.info("Shutting down (keyboard interrupt)")
                self.running = False
                break
            except Exception as e:
                log.error(f"Unexpected error in main loop: {e}")

            # Sleep a short interval before next check
            # (individual device timing is handled by get_poll_interval)
            time.sleep(5)

        # Cleanup
        for conn in self.onkyo_connections.values():
            conn.close()
        try:
            self.db.close()
        except Exception:
            pass
        log.info("nova_av_poller shut down cleanly")


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    poller = AVPoller()
    try:
        poller.run()
    except KeyboardInterrupt:
        poller.running = False
        log.info("Exiting.")
