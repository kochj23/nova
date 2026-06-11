#!/opt/homebrew/bin/python3
"""
nova_home_control.py — Unified home device control for Nova and Jordan.

Controls:
  - Bose Soundbars via UPnP SOAP (port 8091)
  - Onkyo Receivers via eISCP (port 60128)
  - Weather station (read from telemetry.weather in PG)
  - Scenes (coordinated multi-device actions)

CLI usage:
  nova_home_control.py bose bedroom volume 30
  nova_home_control.py bose all mute
  nova_home_control.py onkyo living_room input "STRM BOX"
  nova_home_control.py onkyo living_room power on
  nova_home_control.py onkyo living_room zone2 volume 25
  nova_home_control.py scene movie
  nova_home_control.py scene goodnight
  nova_home_control.py weather
  nova_home_control.py status

Also importable: from nova_home_control import bose, onkyo, scenes

Written by Jordan Koch + Nova.
"""

import socket
import struct
import sys
import time
import urllib.request
import urllib.error
from xml.etree import ElementTree

sys.path.insert(0, str(Path.home()) + "/.openclaw/scripts")
import nova_config

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Device Registry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

BOSE_DEVICES = {
    "bedroom":      {"ip": "192.168.1.25",  "name": "Bedroom Soundbar"},
    "guest_bedroom": {"ip": "192.168.1.82",  "name": "Guest Bedroom Soundbar"},
    "kitchen":      {"ip": "192.168.1.197", "name": "Kitchen Soundbar"},
}

ONKYO_DEVICES = {
    "living_room": {
        "ip": "192.168.1.98",
        "name": "Living Room Receiver",
        "model": "TX-NR696",
        "has_zone2": True,
    },
    "office": {
        "ip": "192.168.1.145",
        "name": "Office Receiver",
        "model": "TX-NR5100",
        "has_zone2": False,
    },
}

# Onkyo input selector codes (ISC command values)
ONKYO_INPUTS = {
    "CBL/SAT":    "01",
    "GAME":       "02",
    "AUX":        "03",
    "PC":         "05",
    "BD/DVD":     "10",
    "STRM BOX":   "11",
    "TV":         "12",
    "PHONO":      "22",
    "CD":         "23",
    "FM":         "24",
    "AM":         "25",
    "TUNER":      "26",
    "DLNA":       "27",
    "NET":        "2B",
    "BLUETOOTH":  "2E",
    "USB":        "29",
}

# Onkyo listening modes
ONKYO_MODES = {
    "stereo":       "00",
    "direct":       "01",
    "surround":     "02",
    "film":         "03",
    "thx":          "04",
    "action":       "05",
    "musical":      "06",
    "mono":         "07",
    "full_mono":    "0F",
    "pure_audio":   "11",
    "multiplex":    "12",
    "dolby":        "80",
    "dts":          "81",
    "all_stereo":   "0C",
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Bose UPnP SOAP Control (port 8091)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class bose:
    """Bose Soundbar control via UPnP SOAP on port 8091."""

    PORT = 8091
    RENDERING_CONTROL = "/RenderingControl"
    AV_TRANSPORT = "/AVTransport"
    TIMEOUT = 5

    @staticmethod
    def _resolve_device(device: str) -> dict:
        """Resolve device name to config dict."""
        if device == "all":
            return None  # caller handles iteration
        if device not in BOSE_DEVICES:
            raise ValueError(f"Unknown Bose device: {device}. Valid: {', '.join(BOSE_DEVICES.keys())}")
        return BOSE_DEVICES[device]

    @staticmethod
    def _soap_request(ip: str, path: str, service_type: str, action: str, body_xml: str) -> str:
        """Send a SOAP request and return the response body."""
        url = f"http://{ip}:{bose.PORT}{path}"
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"'
            ' s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            '<s:Body>'
            f'<u:{action} xmlns:u="{service_type}">'
            f'{body_xml}'
            f'</u:{action}>'
            '</s:Body>'
            '</s:Envelope>'
        )
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{service_type}#{action}"',
        }
        req = urllib.request.Request(url, data=envelope.encode("utf-8"), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=bose.TIMEOUT) as resp:
                return resp.read().decode("utf-8")
        except urllib.error.URLError as e:
            raise ConnectionError(f"Bose SOAP request failed ({ip}): {e}")

    @staticmethod
    def _rendering_action(ip: str, action: str, body_xml: str) -> str:
        svc = "urn:schemas-upnp-org:service:RenderingControl:1"
        return bose._soap_request(ip, bose.RENDERING_CONTROL, svc, action, body_xml)

    @staticmethod
    def _transport_action(ip: str, action: str, body_xml: str = "") -> str:
        svc = "urn:schemas-upnp-org:service:AVTransport:1"
        instance_body = "<InstanceID>0</InstanceID>" + body_xml
        return bose._soap_request(ip, bose.AV_TRANSPORT, svc, action, instance_body)

    @classmethod
    def set_volume(cls, device: str, level: int) -> dict:
        """Set volume (0-100) on a Bose device."""
        if device == "all":
            return {d: cls.set_volume(d, level) for d in BOSE_DEVICES}
        info = cls._resolve_device(device)
        level = max(0, min(100, int(level)))
        body = (
            "<InstanceID>0</InstanceID>"
            "<Channel>Master</Channel>"
            f"<DesiredVolume>{level}</DesiredVolume>"
        )
        cls._rendering_action(info["ip"], "SetVolume", body)
        return {"device": device, "action": "set_volume", "level": level, "ok": True}

    @classmethod
    def get_volume(cls, device: str) -> dict:
        """Get current volume from a Bose device."""
        if device == "all":
            return {d: cls.get_volume(d) for d in BOSE_DEVICES}
        info = cls._resolve_device(device)
        body = "<InstanceID>0</InstanceID><Channel>Master</Channel>"
        resp = cls._rendering_action(info["ip"], "GetVolume", body)
        # Parse volume from response XML
        try:
            root = ElementTree.fromstring(resp)
            ns = {"u": "urn:schemas-upnp-org:service:RenderingControl:1"}
            vol_el = root.find(".//{urn:schemas-upnp-org:service:RenderingControl:1}GetVolumeResponse/CurrentVolume")
            if vol_el is None:
                # Try without namespace
                vol_el = root.find(".//*[local-name()='CurrentVolume']")
            volume = int(vol_el.text) if vol_el is not None else -1
        except Exception:
            volume = -1
        return {"device": device, "volume": volume}

    @classmethod
    def mute(cls, device: str) -> dict:
        """Mute a Bose device."""
        if device == "all":
            return {d: cls.mute(d) for d in BOSE_DEVICES}
        info = cls._resolve_device(device)
        body = (
            "<InstanceID>0</InstanceID>"
            "<Channel>Master</Channel>"
            "<DesiredMute>1</DesiredMute>"
        )
        cls._rendering_action(info["ip"], "SetMute", body)
        return {"device": device, "action": "mute", "ok": True}

    @classmethod
    def unmute(cls, device: str) -> dict:
        """Unmute a Bose device."""
        if device == "all":
            return {d: cls.unmute(d) for d in BOSE_DEVICES}
        info = cls._resolve_device(device)
        body = (
            "<InstanceID>0</InstanceID>"
            "<Channel>Master</Channel>"
            "<DesiredMute>0</DesiredMute>"
        )
        cls._rendering_action(info["ip"], "SetMute", body)
        return {"device": device, "action": "unmute", "ok": True}

    @classmethod
    def play(cls, device: str) -> dict:
        """Play on a Bose device."""
        if device == "all":
            return {d: cls.play(d) for d in BOSE_DEVICES}
        info = cls._resolve_device(device)
        body = "<Speed>1</Speed>"
        cls._transport_action(info["ip"], "Play", body)
        return {"device": device, "action": "play", "ok": True}

    @classmethod
    def pause(cls, device: str) -> dict:
        """Pause a Bose device."""
        if device == "all":
            return {d: cls.pause(d) for d in BOSE_DEVICES}
        info = cls._resolve_device(device)
        cls._transport_action(info["ip"], "Pause")
        return {"device": device, "action": "pause", "ok": True}

    @classmethod
    def stop(cls, device: str) -> dict:
        """Stop playback on a Bose device."""
        if device == "all":
            return {d: cls.stop(d) for d in BOSE_DEVICES}
        info = cls._resolve_device(device)
        cls._transport_action(info["ip"], "Stop")
        return {"device": device, "action": "stop", "ok": True}

    @classmethod
    def next_track(cls, device: str) -> dict:
        """Skip to next track on a Bose device."""
        if device == "all":
            return {d: cls.next_track(d) for d in BOSE_DEVICES}
        info = cls._resolve_device(device)
        cls._transport_action(info["ip"], "Next")
        return {"device": device, "action": "next", "ok": True}

    @classmethod
    def previous_track(cls, device: str) -> dict:
        """Go to previous track on a Bose device."""
        if device == "all":
            return {d: cls.previous_track(d) for d in BOSE_DEVICES}
        info = cls._resolve_device(device)
        cls._transport_action(info["ip"], "Previous")
        return {"device": device, "action": "previous", "ok": True}

    @classmethod
    def status(cls, device: str) -> dict:
        """Get transport status from a Bose device."""
        if device == "all":
            return {d: cls.status(d) for d in BOSE_DEVICES}
        info = cls._resolve_device(device)
        try:
            resp = cls._transport_action(info["ip"], "GetTransportInfo")
            root = ElementTree.fromstring(resp)
            state_el = root.find(".//*[local-name()='CurrentTransportState']")
            state = state_el.text if state_el is not None else "UNKNOWN"
        except Exception as e:
            state = f"ERROR: {e}"
        vol_info = cls.get_volume(device)
        return {
            "device": device,
            "name": info["name"],
            "ip": info["ip"],
            "transport_state": state,
            "volume": vol_info.get("volume", -1),
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Onkyo eISCP Control (port 60128)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class onkyo:
    """Onkyo receiver control via eISCP (Integra Serial Communication Protocol)."""

    PORT = 60128
    TIMEOUT = 3

    @staticmethod
    def _resolve_device(device: str) -> dict:
        """Resolve device name to config dict."""
        if device not in ONKYO_DEVICES:
            raise ValueError(f"Unknown Onkyo device: {device}. Valid: {', '.join(ONKYO_DEVICES.keys())}")
        return ONKYO_DEVICES[device]

    @staticmethod
    def _build_eiscp(command: str) -> bytes:
        """Build an eISCP packet from an ISCP command string.

        eISCP packet format:
          Header: 'ISCP' (4 bytes)
          Header size: 16 (4 bytes, big-endian)
          Data size: len(iscp_message) (4 bytes, big-endian)
          Version: 0x01 (1 byte)
          Reserved: 0x00 0x00 0x00 (3 bytes)
          Data: '!1<command>\\r' (ISCP message with start char, unit type, command, CR)
        """
        iscp_msg = f"!1{command}\r".encode("ascii")
        header = b"ISCP"
        header_size = struct.pack(">I", 16)
        data_size = struct.pack(">I", len(iscp_msg))
        version = b"\x01\x00\x00\x00"
        return header + header_size + data_size + version + iscp_msg

    @staticmethod
    def _parse_eiscp(data: bytes) -> str:
        """Parse an eISCP response packet, return the ISCP command string."""
        if len(data) < 16:
            return ""
        # Header is 16 bytes, data follows
        header_size = struct.unpack(">I", data[4:8])[0]
        msg = data[header_size:]
        # Strip start character (!1), EOF, CR, LF
        decoded = msg.decode("ascii", errors="ignore")
        # Remove !1 prefix and trailing control chars
        decoded = decoded.strip("\r\n\x1a\x00")
        if decoded.startswith("!1"):
            decoded = decoded[2:]
        return decoded

    @classmethod
    def _send_command(cls, ip: str, command: str, expect_response: bool = True) -> str:
        """Send an eISCP command and optionally wait for a response."""
        packet = cls._build_eiscp(command)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(cls.TIMEOUT)
        try:
            sock.connect((ip, cls.PORT))
            sock.sendall(packet)
            if expect_response:
                # Read response — may need multiple reads
                response = b""
                try:
                    while True:
                        chunk = sock.recv(1024)
                        if not chunk:
                            break
                        response += chunk
                        # Check if we have a complete message (ends with \r\n or \x1a)
                        if b"\x1a" in chunk or b"\r\n" in chunk:
                            break
                except socket.timeout:
                    pass
                return cls._parse_eiscp(response)
            return ""
        except (socket.error, OSError) as e:
            raise ConnectionError(f"Onkyo eISCP failed ({ip}): {e}")
        finally:
            sock.close()

    @classmethod
    def _query(cls, ip: str, command_prefix: str) -> str:
        """Send a query command (appends QSTN) and return the value."""
        resp = cls._send_command(ip, f"{command_prefix}QSTN")
        if resp.startswith(command_prefix):
            return resp[len(command_prefix):]
        return resp

    # ── Power ──────────────────────────────────────────────────────────────────

    @classmethod
    def power_on(cls, device: str) -> dict:
        """Power on main zone."""
        info = cls._resolve_device(device)
        cls._send_command(info["ip"], "PWR01", expect_response=False)
        time.sleep(0.3)
        return {"device": device, "action": "power_on", "ok": True}

    @classmethod
    def power_off(cls, device: str) -> dict:
        """Power off main zone (standby)."""
        info = cls._resolve_device(device)
        cls._send_command(info["ip"], "PWR00", expect_response=False)
        return {"device": device, "action": "power_off", "ok": True}

    @classmethod
    def power_status(cls, device: str) -> str:
        """Query power state: '01' = on, '00' = off/standby."""
        info = cls._resolve_device(device)
        return cls._query(info["ip"], "PWR")

    # ── Volume ─────────────────────────────────────────────────────────────────

    @classmethod
    def set_volume(cls, device: str, level: int) -> dict:
        """Set main zone volume (0-80 for most models)."""
        info = cls._resolve_device(device)
        level = max(0, min(80, int(level)))
        hex_vol = f"{level:02X}"
        cls._send_command(info["ip"], f"MVL{hex_vol}", expect_response=False)
        return {"device": device, "action": "set_volume", "level": level, "ok": True}

    @classmethod
    def get_volume(cls, device: str) -> dict:
        """Get main zone volume."""
        info = cls._resolve_device(device)
        val = cls._query(info["ip"], "MVL")
        try:
            volume = int(val, 16)
        except (ValueError, TypeError):
            volume = -1
        return {"device": device, "volume": volume}

    @classmethod
    def mute_toggle(cls, device: str) -> dict:
        """Toggle mute on main zone."""
        info = cls._resolve_device(device)
        cls._send_command(info["ip"], "AMTTG", expect_response=False)
        return {"device": device, "action": "mute_toggle", "ok": True}

    @classmethod
    def mute_on(cls, device: str) -> dict:
        """Mute main zone."""
        info = cls._resolve_device(device)
        cls._send_command(info["ip"], "AMT01", expect_response=False)
        return {"device": device, "action": "mute_on", "ok": True}

    @classmethod
    def mute_off(cls, device: str) -> dict:
        """Unmute main zone."""
        info = cls._resolve_device(device)
        cls._send_command(info["ip"], "AMT00", expect_response=False)
        return {"device": device, "action": "mute_off", "ok": True}

    # ── Input Selection ────────────────────────────────────────────────────────

    @classmethod
    def set_input(cls, device: str, input_name: str) -> dict:
        """Set input source by name."""
        info = cls._resolve_device(device)
        input_upper = input_name.upper().strip()
        if input_upper not in ONKYO_INPUTS:
            raise ValueError(f"Unknown input: {input_name}. Valid: {', '.join(ONKYO_INPUTS.keys())}")
        code = ONKYO_INPUTS[input_upper]
        cls._send_command(info["ip"], f"SLI{code}", expect_response=False)
        return {"device": device, "action": "set_input", "input": input_upper, "ok": True}

    @classmethod
    def get_input(cls, device: str) -> dict:
        """Query current input source."""
        info = cls._resolve_device(device)
        val = cls._query(info["ip"], "SLI")
        # Reverse lookup
        input_name = "UNKNOWN"
        for name, code in ONKYO_INPUTS.items():
            if code == val:
                input_name = name
                break
        return {"device": device, "input_code": val, "input_name": input_name}

    # ── Listening Mode ─────────────────────────────────────────────────────────

    @classmethod
    def set_listening_mode(cls, device: str, mode: str) -> dict:
        """Set listening mode by name."""
        info = cls._resolve_device(device)
        mode_lower = mode.lower().strip()
        if mode_lower not in ONKYO_MODES:
            raise ValueError(f"Unknown mode: {mode}. Valid: {', '.join(ONKYO_MODES.keys())}")
        code = ONKYO_MODES[mode_lower]
        cls._send_command(info["ip"], f"LMD{code}", expect_response=False)
        return {"device": device, "action": "set_listening_mode", "mode": mode_lower, "ok": True}

    @classmethod
    def get_listening_mode(cls, device: str) -> dict:
        """Query current listening mode."""
        info = cls._resolve_device(device)
        val = cls._query(info["ip"], "LMD")
        mode_name = "UNKNOWN"
        for name, code in ONKYO_MODES.items():
            if code == val:
                mode_name = name
                break
        return {"device": device, "mode_code": val, "mode_name": mode_name}

    # ── Zone 2 (TX-NR696 only) ────────────────────────────────────────────────

    @classmethod
    def zone2_power_on(cls, device: str) -> dict:
        """Power on Zone 2."""
        info = cls._resolve_device(device)
        if not info.get("has_zone2"):
            raise ValueError(f"{device} ({info['model']}) does not support Zone 2")
        cls._send_command(info["ip"], "ZPW01", expect_response=False)
        return {"device": device, "action": "zone2_power_on", "ok": True}

    @classmethod
    def zone2_power_off(cls, device: str) -> dict:
        """Power off Zone 2."""
        info = cls._resolve_device(device)
        if not info.get("has_zone2"):
            raise ValueError(f"{device} ({info['model']}) does not support Zone 2")
        cls._send_command(info["ip"], "ZPW00", expect_response=False)
        return {"device": device, "action": "zone2_power_off", "ok": True}

    @classmethod
    def zone2_set_volume(cls, device: str, level: int) -> dict:
        """Set Zone 2 volume (0-80)."""
        info = cls._resolve_device(device)
        if not info.get("has_zone2"):
            raise ValueError(f"{device} ({info['model']}) does not support Zone 2")
        level = max(0, min(80, int(level)))
        hex_vol = f"{level:02X}"
        cls._send_command(info["ip"], f"ZVL{hex_vol}", expect_response=False)
        return {"device": device, "action": "zone2_set_volume", "level": level, "ok": True}

    @classmethod
    def zone2_set_input(cls, device: str, input_name: str) -> dict:
        """Set Zone 2 input source."""
        info = cls._resolve_device(device)
        if not info.get("has_zone2"):
            raise ValueError(f"{device} ({info['model']}) does not support Zone 2")
        input_upper = input_name.upper().strip()
        if input_upper not in ONKYO_INPUTS:
            raise ValueError(f"Unknown input: {input_name}. Valid: {', '.join(ONKYO_INPUTS.keys())}")
        code = ONKYO_INPUTS[input_upper]
        cls._send_command(info["ip"], f"SLZ{code}", expect_response=False)
        return {"device": device, "action": "zone2_set_input", "input": input_upper, "ok": True}

    # ── Status ─────────────────────────────────────────────────────────────────

    @classmethod
    def status(cls, device: str) -> dict:
        """Get full status of an Onkyo device."""
        info = cls._resolve_device(device)
        result = {
            "device": device,
            "name": info["name"],
            "model": info["model"],
            "ip": info["ip"],
        }
        try:
            pwr = cls.power_status(device)
            result["power"] = "on" if pwr == "01" else "standby"
        except Exception as e:
            result["power"] = f"error: {e}"
            return result  # If we can't get power, device is likely off

        if result["power"] == "on":
            try:
                vol = cls.get_volume(device)
                result["volume"] = vol.get("volume", -1)
            except Exception:
                result["volume"] = -1
            try:
                inp = cls.get_input(device)
                result["input"] = inp.get("input_name", "UNKNOWN")
            except Exception:
                result["input"] = "UNKNOWN"
            try:
                mode = cls.get_listening_mode(device)
                result["listening_mode"] = mode.get("mode_name", "UNKNOWN")
            except Exception:
                result["listening_mode"] = "UNKNOWN"
        return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Weather Station (read from PG telemetry.weather)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class weather:
    """Read-only weather station data from telemetry.weather table."""

    @staticmethod
    def get_current() -> dict:
        """Get latest weather reading from telemetry.weather."""
        import psycopg2
        try:
            conn = psycopg2.connect(host="localhost", dbname="nova_ops", user="kochj")
            cur = conn.cursor()
            cur.execute("""
                SELECT ts, temperature_f, humidity, pressure_inhg,
                       wind_speed_mph, wind_direction, rain_in,
                       uv_index, solar_radiation
                FROM telemetry.weather
                ORDER BY ts DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            cur.close()
            conn.close()
            if not row:
                return {"error": "No weather data available"}
            return {
                "timestamp": row[0].isoformat() if row[0] else None,
                "temperature_f": row[1],
                "humidity": row[2],
                "pressure_inhg": row[3],
                "wind_speed_mph": row[4],
                "wind_direction": row[5],
                "rain_in": row[6],
                "uv_index": row[7],
                "solar_radiation": row[8],
            }
        except Exception as e:
            return {"error": f"Weather query failed: {e}"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Scenes (coordinated multi-device actions)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class scenes:
    """Predefined multi-device scenes."""

    @staticmethod
    def _shortcuts_run(shortcut_name: str) -> bool:
        """Run a macOS Shortcut (for HomeKit light control)."""
        import subprocess
        try:
            result = subprocess.run(
                ["shortcuts", "run", shortcut_name],
                capture_output=True, timeout=10
            )
            return result.returncode == 0
        except Exception:
            return False

    @classmethod
    def movie_mode(cls) -> dict:
        """Movie mode: living room on, STRM BOX input, volume 40, lights dim."""
        results = {}
        try:
            results["power"] = onkyo.power_on("living_room")
            time.sleep(1)  # Give receiver time to power up
            results["input"] = onkyo.set_input("living_room", "STRM BOX")
            results["volume"] = onkyo.set_volume("living_room", 40)
            results["mode"] = onkyo.set_listening_mode("living_room", "surround")
        except Exception as e:
            results["onkyo_error"] = str(e)
        # Dim lights via Shortcuts
        results["lights"] = cls._shortcuts_run("Dim Living Room")
        return {"scene": "movie_mode", "results": results}

    @classmethod
    def music_everywhere(cls) -> dict:
        """Music everywhere: all Bose on, all Onkyo on, matching volumes."""
        results = {}
        # Bose soundbars
        try:
            results["bose_play"] = bose.play("all")
            results["bose_volume"] = bose.set_volume("all", 25)
        except Exception as e:
            results["bose_error"] = str(e)
        # Onkyo receivers
        for dev in ONKYO_DEVICES:
            try:
                onkyo.power_on(dev)
                time.sleep(0.5)
                onkyo.set_input(dev, "NET")
                onkyo.set_volume(dev, 30)
                results[f"onkyo_{dev}"] = "ok"
            except Exception as e:
                results[f"onkyo_{dev}_error"] = str(e)
        return {"scene": "music_everywhere", "results": results}

    @classmethod
    def goodnight(cls) -> dict:
        """Goodnight: all AV off, lights off."""
        results = {}
        # Bose off
        try:
            results["bose_stop"] = bose.stop("all")
        except Exception as e:
            results["bose_error"] = str(e)
        # Onkyo off
        for dev in ONKYO_DEVICES:
            try:
                onkyo.power_off(dev)
                results[f"onkyo_{dev}"] = "off"
            except Exception as e:
                results[f"onkyo_{dev}_error"] = str(e)
        # Zone 2 off on living room
        try:
            onkyo.zone2_power_off("living_room")
            results["zone2"] = "off"
        except Exception:
            pass
        # Lights off via Shortcuts
        results["lights"] = cls._shortcuts_run("All Lights Off")
        return {"scene": "goodnight", "results": results}

    @classmethod
    def morning(cls) -> dict:
        """Morning: kitchen Bose on low, living room Onkyo news input."""
        results = {}
        # Kitchen Bose at low volume
        try:
            results["kitchen_play"] = bose.play("kitchen")
            results["kitchen_vol"] = bose.set_volume("kitchen", 15)
        except Exception as e:
            results["kitchen_error"] = str(e)
        # Living room Onkyo — FM/news
        try:
            results["lr_power"] = onkyo.power_on("living_room")
            time.sleep(1)
            results["lr_input"] = onkyo.set_input("living_room", "FM")
            results["lr_volume"] = onkyo.set_volume("living_room", 25)
        except Exception as e:
            results["lr_error"] = str(e)
        return {"scene": "morning", "results": results}

    @classmethod
    def bedtime(cls) -> dict:
        """Bedtime: all AV off, bedroom lights dim warm, rest off."""
        results = {}
        try:
            results["bose_stop"] = bose.stop("all")
        except Exception as e:
            results["bose_error"] = str(e)
        for dev in ONKYO_DEVICES:
            try:
                onkyo.power_off(dev)
                results[f"onkyo_{dev}"] = "off"
            except Exception:
                pass
        results["lights_bedroom"] = cls._shortcuts_run("Bedtime Lights")
        results["lights_off"] = cls._shortcuts_run("All Lights Off Except Bedroom")
        return {"scene": "bedtime", "results": results}

    @classmethod
    def party(cls) -> dict:
        """Party: all Bose on, music input, volume up, colorful lights."""
        results = {}
        try:
            results["bose_play"] = bose.play("all")
            results["bose_volume"] = bose.set_volume("all", 45)
        except Exception as e:
            results["bose_error"] = str(e)
        for dev in ONKYO_DEVICES:
            try:
                onkyo.power_on(dev)
                time.sleep(0.5)
                onkyo.set_input(dev, "NET")
                onkyo.set_volume(dev, 40)
                results[f"onkyo_{dev}"] = "party"
            except Exception as e:
                results[f"onkyo_{dev}_error"] = str(e)
        results["lights"] = cls._shortcuts_run("Party Lights")
        return {"scene": "party", "results": results}

    @classmethod
    def work(cls) -> dict:
        """Work: office lights bright, office Onkyo low background, rest quiet."""
        results = {}
        try:
            results["office_power"] = onkyo.power_on("office")
            time.sleep(1)
            results["office_input"] = onkyo.set_input("office", "NET")
            results["office_volume"] = onkyo.set_volume("office", 15)
        except Exception as e:
            results["office_error"] = str(e)
        results["lights"] = cls._shortcuts_run("Work Lights")
        return {"scene": "work", "results": results}

    @classmethod
    def away(cls) -> dict:
        """Away: all AV off, all lights off, lock up."""
        results = {}
        try:
            results["bose_stop"] = bose.stop("all")
        except Exception as e:
            results["bose_error"] = str(e)
        for dev in ONKYO_DEVICES:
            try:
                onkyo.power_off(dev)
                results[f"onkyo_{dev}"] = "off"
            except Exception:
                pass
        results["lights"] = cls._shortcuts_run("All Lights Off")
        return {"scene": "away", "results": results}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Status (all devices)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def get_all_status() -> dict:
    """Get status of all devices."""
    result = {"bose": {}, "onkyo": {}}
    for dev in BOSE_DEVICES:
        try:
            result["bose"][dev] = bose.status(dev)
        except Exception as e:
            result["bose"][dev] = {"device": dev, "error": str(e)}
    for dev in ONKYO_DEVICES:
        try:
            result["onkyo"][dev] = onkyo.status(dev)
        except Exception as e:
            result["onkyo"][dev] = {"device": dev, "error": str(e)}
    return result


def _format_status(status: dict) -> str:
    """Pretty-print status dict for CLI output."""
    lines = []
    lines.append("=== Bose Soundbars ===")
    for dev, info in status.get("bose", {}).items():
        if "error" in info:
            lines.append(f"  {dev}: ERROR - {info['error']}")
        else:
            state = info.get("transport_state", "?")
            vol = info.get("volume", "?")
            lines.append(f"  {info.get('name', dev)}: state={state}, volume={vol}")
    lines.append("")
    lines.append("=== Onkyo Receivers ===")
    for dev, info in status.get("onkyo", {}).items():
        if "error" in info:
            lines.append(f"  {dev}: ERROR - {info['error']}")
        else:
            pwr = info.get("power", "?")
            line = f"  {info.get('name', dev)} ({info.get('model', '?')}): power={pwr}"
            if pwr == "on":
                line += f", vol={info.get('volume', '?')}, input={info.get('input', '?')}, mode={info.get('listening_mode', '?')}"
            lines.append(line)
    return "\n".join(lines)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLI Interface
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _cli_bose(args: list) -> None:
    """Handle: bose <device> <command> [value]"""
    if len(args) < 2:
        print("Usage: bose <device|all> <volume|mute|unmute|play|pause|stop|next|prev> [value]")
        sys.exit(1)
    device, cmd = args[0], args[1]
    try:
        if cmd == "volume":
            if len(args) < 3:
                result = bose.get_volume(device)
            else:
                result = bose.set_volume(device, int(args[2]))
        elif cmd == "mute":
            result = bose.mute(device)
        elif cmd == "unmute":
            result = bose.unmute(device)
        elif cmd == "play":
            result = bose.play(device)
        elif cmd == "pause":
            result = bose.pause(device)
        elif cmd == "stop":
            result = bose.stop(device)
        elif cmd == "next":
            result = bose.next_track(device)
        elif cmd in ("prev", "previous"):
            result = bose.previous_track(device)
        elif cmd == "status":
            result = bose.status(device)
        else:
            print(f"Unknown Bose command: {cmd}")
            sys.exit(1)
        print(_format_result(result))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _cli_onkyo(args: list) -> None:
    """Handle: onkyo <device> <command> [value]"""
    if len(args) < 2:
        print("Usage: onkyo <device> <power|volume|input|mode|mute|zone2> [value]")
        sys.exit(1)
    device, cmd = args[0], args[1]
    try:
        if cmd == "power":
            if len(args) < 3:
                pwr = onkyo.power_status(device)
                result = {"device": device, "power": "on" if pwr == "01" else "standby"}
            elif args[2].lower() in ("on", "1"):
                result = onkyo.power_on(device)
            else:
                result = onkyo.power_off(device)
        elif cmd == "volume":
            if len(args) < 3:
                result = onkyo.get_volume(device)
            else:
                result = onkyo.set_volume(device, int(args[2]))
        elif cmd == "input":
            if len(args) < 3:
                result = onkyo.get_input(device)
            else:
                result = onkyo.set_input(device, args[2])
        elif cmd == "mode":
            if len(args) < 3:
                result = onkyo.get_listening_mode(device)
            else:
                result = onkyo.set_listening_mode(device, args[2])
        elif cmd == "mute":
            if len(args) >= 3:
                if args[2].lower() in ("on", "1"):
                    result = onkyo.mute_on(device)
                elif args[2].lower() in ("off", "0"):
                    result = onkyo.mute_off(device)
                else:
                    result = onkyo.mute_toggle(device)
            else:
                result = onkyo.mute_toggle(device)
        elif cmd == "zone2":
            result = _cli_onkyo_zone2(device, args[2:])
        elif cmd == "status":
            result = onkyo.status(device)
        else:
            print(f"Unknown Onkyo command: {cmd}")
            sys.exit(1)
        print(_format_result(result))
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def _cli_onkyo_zone2(device: str, args: list) -> dict:
    """Handle zone2 subcommands: zone2 power on/off, zone2 volume N, zone2 input NAME."""
    if not args:
        print("Usage: onkyo <device> zone2 <power|volume|input> [value]")
        sys.exit(1)
    subcmd = args[0]
    if subcmd == "power":
        if len(args) < 2 or args[1].lower() in ("on", "1"):
            return onkyo.zone2_power_on(device)
        else:
            return onkyo.zone2_power_off(device)
    elif subcmd == "volume":
        if len(args) < 2:
            print("Usage: onkyo <device> zone2 volume <level>")
            sys.exit(1)
        return onkyo.zone2_set_volume(device, int(args[1]))
    elif subcmd == "input":
        if len(args) < 2:
            print("Usage: onkyo <device> zone2 input <name>")
            sys.exit(1)
        return onkyo.zone2_set_input(device, args[1])
    else:
        print(f"Unknown zone2 subcommand: {subcmd}")
        sys.exit(1)


def _cli_scene(args: list) -> None:
    """Handle: scene <name>"""
    if not args:
        print("Available scenes: movie, music_everywhere, goodnight, morning, bedtime, party, work, away")
        sys.exit(1)
    name = args[0].lower().replace("-", "_").replace(" ", "_")
    scene_map = {
        "movie": scenes.movie_mode,
        "movie_mode": scenes.movie_mode,
        "music": scenes.music_everywhere,
        "music_everywhere": scenes.music_everywhere,
        "goodnight": scenes.goodnight,
        "night": scenes.goodnight,
        "morning": scenes.morning,
        "bedtime": scenes.bedtime,
        "bed": scenes.bedtime,
        "party": scenes.party,
        "work": scenes.work,
        "office": scenes.work,
        "away": scenes.away,
        "leave": scenes.away,
        "leaving": scenes.away,
    }
    if name not in scene_map:
        print(f"Unknown scene: {name}. Available: {', '.join(scene_map.keys())}")
        sys.exit(1)
    result = scene_map[name]()
    print(_format_result(result))


def _format_result(result) -> str:
    """Format a result dict/value for CLI display."""
    import json
    if isinstance(result, dict):
        return json.dumps(result, indent=2, default=str)
    return str(result)


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Nova Home Control")
        print("Usage:")
        print("  nova_home_control.py bose <device|all> <command> [value]")
        print("  nova_home_control.py onkyo <device> <command> [value]")
        print("  nova_home_control.py scene <name>")
        print("  nova_home_control.py weather")
        print("  nova_home_control.py status")
        print("")
        print("Bose devices:", ", ".join(BOSE_DEVICES.keys()))
        print("Onkyo devices:", ", ".join(ONKYO_DEVICES.keys()))
        print("Scenes: movie, music_everywhere, goodnight, morning")
        sys.exit(0)

    category = sys.argv[1].lower()

    if category == "bose":
        _cli_bose(sys.argv[2:])
    elif category == "onkyo":
        _cli_onkyo(sys.argv[2:])
    elif category == "scene":
        _cli_scene(sys.argv[2:])
    elif category == "weather":
        result = weather.get_current()
        if "error" in result:
            print(f"ERROR: {result['error']}", file=sys.stderr)
            sys.exit(1)
        print(_format_result(result))
    elif category == "status":
        status = get_all_status()
        print(_format_status(status))
    else:
        print(f"Unknown category: {category}. Use: bose, onkyo, scene, weather, status")
        sys.exit(1)


if __name__ == "__main__":
    main()
