#!/usr/bin/env python3
"""nova_meshtastic_bridge.py — runs on Jordans-Mac-mini.local, where the Heltec
Mesh Node T114 is physically plugged in over USB serial. Everything else in
Nova (notably nova_notifier.py, which runs on Office-M4-2) reaches the mesh
radio through this bridge's small local HTTP API instead of touching the
serial port directly, since only one process can own it at a time.

  GET  /status        -- node info + battery/telemetry, for a health check
  POST /send {"text": "..."}   -- broadcast a text message over the mesh

Also logs every INCOMING mesh message/telemetry update to shared_observations
so Nova sees what the mesh itself is picking up, not just what it sends.

Written by Jordan Koch (via Claude).
"""
import glob
import json
import os
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Lock

import psycopg2
from pubsub import pub

sys.path.insert(0, str(Path(__file__).parent))

import meshtastic.serial_interface

PORT = 37478
DEVICE = "/dev/cu.usbmodem31201"
import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
LOG_FILE = Path.home() / ".openclaw/logs/meshtastic_bridge.log"

# --- watchdog / honest-health config -------------------------------------
# If no mesh packet is RECEIVED in this many minutes, the serial FD is
# assumed stale (the Heltec's USB CDC re-enumerated out from under us) and
# the watchdog self-heals. Override with env MESH_RX_TIMEOUT_MIN.
RX_TIMEOUT_SEC = int(os.environ.get("MESH_RX_TIMEOUT_MIN", "30")) * 60
WATCHDOG_INTERVAL_SEC = 60          # how often the watchdog thread checks
MAX_SEND_TIMEOUTS = 3               # consecutive send failures before self-heal

_lock = Lock()
_iface = None
# Wall-clock time of the last packet RECEIVED off the mesh. Seeded to "now"
# at startup so a fresh process isn't instantly declared stale before its
# first rx. This is the ground truth for health -- unlike the meshtastic
# lib's in-memory cache, it cannot report a dead serial link as healthy.
_last_rx = time.time()
_send_timeouts = 0                  # consecutive send-timeout / send-failure count


def log(msg):
    line = f"[mesh-bridge {time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def record_observation(subject, observation, severity="info", metadata=None):
    try:
        conn = psycopg2.connect(DSN)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO shared_observations (observer, category, subject, observation, severity, metadata) "
            "VALUES ('nova_meshtastic_bridge', 'mesh', %s, %s, %s, %s)",
            (subject, observation, severity, json.dumps(metadata or {})))
        cur.close()
        conn.close()
    except Exception as e:
        log(f"failed to record observation: {e}")


def on_receive(packet, interface):
    # ANY packet off the mesh -- text, telemetry, position, anything -- proves
    # the serial link is live right now. Stamp it unconditionally BEFORE the
    # port-specific handling below; this is what the watchdog and /status read.
    global _last_rx
    _last_rx = time.time()
    decoded = packet.get("decoded", {})
    port = decoded.get("portnum", "")
    frm = packet.get("fromId", packet.get("from", "unknown"))
    if port == "TEXT_MESSAGE_APP":
        text = decoded.get("text", "")
        log(f"mesh message from {frm}: {text}")
        record_observation(f"message from {frm}", text)
    elif port in ("TELEMETRY_APP", "POSITION_APP"):
        pass  # too frequent/low-signal to log every one; /status covers current state


def current_device():
    """Resolve the live device node. After a USB CDC re-enumeration the old
    /dev/cu.usbmodem* path is gone and a new one exists, so prefer the
    configured DEVICE only if it still exists, else fall back to whatever
    cu.usbmodem* the OS created."""
    if os.path.exists(DEVICE):
        return DEVICE
    matches = sorted(glob.glob("/dev/cu.usbmodem*"))
    if matches:
        return matches[0]
    return DEVICE  # nothing found -- let SerialInterface raise so we heal/exit


def connect():
    global _iface
    dev = current_device()
    _iface = meshtastic.serial_interface.SerialInterface(devPath=dev)
    # pypubsub subscription is idempotent and keyed on the topic, not the
    # interface object, so it survives reconnects -- subscribe once here.
    pub.subscribe(on_receive, "meshtastic.receive")
    log(f"Connected to {dev}")


def reconnect():
    """Close the (possibly stale) SerialInterface and open a fresh one against
    the current device node. Raises on failure so the caller can decide to exit
    for a launchd restart."""
    global _iface, _last_rx, _send_timeouts
    with _lock:
        old = _iface
        _iface = None
        if old is not None:
            try:
                old.close()
            except Exception as e:
                log(f"error closing stale interface: {e}")
        dev = current_device()
        _iface = meshtastic.serial_interface.SerialInterface(devPath=dev)
        _last_rx = time.time()   # give the fresh link a clean rx window
        _send_timeouts = 0
        log(f"reconnected to {dev}")


def watchdog():
    """Background loop: self-heal if the mesh link looks dead. Two triggers --
    (a) no packet received in RX_TIMEOUT_SEC (stale serial FD after a USB
    re-enumeration -- the exact 2026-07-30 failure), or (b) MAX_SEND_TIMEOUTS
    consecutive send failures. On trip, reconnect; if reconnect fails, exit
    non-zero so launchd KeepAlive restarts the process."""
    while True:
        time.sleep(WATCHDOG_INTERVAL_SEC)
        try:
            since = time.time() - _last_rx
            stale = since > RX_TIMEOUT_SEC
            too_many_timeouts = _send_timeouts >= MAX_SEND_TIMEOUTS
            if not (stale or too_many_timeouts):
                continue
            reason = (f"no rx for {int(since)}s (>{RX_TIMEOUT_SEC}s)" if stale
                      else f"{_send_timeouts} consecutive send timeouts")
            log(f"watchdog tripped: {reason} -- attempting reconnect")
            record_observation("watchdog", f"self-heal reconnect: {reason}",
                               severity="warning")
            try:
                reconnect()
                log("watchdog: reconnect succeeded")
                record_observation("watchdog", "reconnect succeeded", "info")
            except Exception as e:
                log(f"watchdog: reconnect FAILED: {e} -- exiting for launchd restart")
                record_observation("watchdog",
                                   f"reconnect failed, exiting non-zero: {e}",
                                   severity="critical")
                os._exit(1)  # hard exit from thread -> launchd KeepAlive restarts
        except Exception as e:
            log(f"watchdog loop error: {e}")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass  # quiet -- nova_meshtastic_bridge.log already covers this

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/nodes":
            # Read-only dump of the radio's NodeDB — every node it has heard.
            # The NodeDB lives in the radio's own flash, so it survives bridge
            # restarts and accumulates 24/7 regardless of what we log.
            with _lock:
                if _iface is None:
                    return self._json(503, {"error": "not connected"})
                try:
                    nodes = []
                    for nid, n in (getattr(_iface, "nodes", None) or {}).items():
                        user = n.get("user", {}) or {}
                        pos = n.get("position", {}) or {}
                        metrics = n.get("deviceMetrics", {}) or {}
                        nodes.append({
                            "id": nid,
                            "longName": user.get("longName"),
                            "shortName": user.get("shortName"),
                            "hwModel": user.get("hwModel"),
                            "snr": n.get("snr"),
                            "hopsAway": n.get("hopsAway"),
                            "lastHeard": n.get("lastHeard"),
                            "batteryLevel": metrics.get("batteryLevel"),
                            "latitude": pos.get("latitude"),
                            "longitude": pos.get("longitude"),
                        })
                    return self._json(200, {"count": len(nodes), "nodes": nodes})
                except Exception as e:
                    return self._json(500, {"error": str(e)})
        if self.path != "/status":
            return self._json(404, {"error": "not found"})
        # seconds_since_last_rx is the HONEST health signal: it comes from the
        # real wall-clock time of the last received packet, NOT the meshtastic
        # lib's in-memory cache. A stale serial FD makes this climb without
        # bound even while `connected`/battery/uptime still look fine.
        since_rx = round(time.time() - _last_rx, 1)
        with _lock:
            if _iface is None:
                return self._json(503, {
                    "error": "not connected",
                    "connected": False,
                    "seconds_since_last_rx": since_rx,
                })
            try:
                info = _iface.getMyNodeInfo()
                metrics = (info or {}).get("deviceMetrics", {})
                self._json(200, {
                    "connected": True,
                    "longName": (info or {}).get("user", {}).get("longName"),
                    "batteryLevel": metrics.get("batteryLevel"),
                    "voltage": metrics.get("voltage"),
                    "uptimeSeconds": metrics.get("uptimeSeconds"),
                    "seconds_since_last_rx": since_rx,
                    "rx_timeout_seconds": RX_TIMEOUT_SEC,
                    "send_timeouts": _send_timeouts,
                })
            except Exception as e:
                self._json(500, {"error": str(e), "seconds_since_last_rx": since_rx})

    def do_POST(self):
        if self.path != "/send":
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length", 0))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        text = (payload.get("text") or "").strip()[:200]  # mesh packets are tiny -- keep it short
        if not text:
            return self._json(400, {"error": "text required"})
        with _lock:
            if _iface is None:
                return self._json(503, {"error": "not connected"})
            global _send_timeouts
            try:
                _iface.sendText(text)
                _send_timeouts = 0  # a clean send proves the link -- reset counter
                log(f"sent: {text}")
                self._json(200, {"sent": True})
            except Exception as e:
                _send_timeouts += 1  # feeds the watchdog's send-timeout trigger
                log(f"send failed ({_send_timeouts} in a row): {e}")
                self._json(500, {"error": str(e)})


def main():
    connect()
    threading.Thread(target=watchdog, name="watchdog", daemon=True).start()
    log(f"watchdog started (rx timeout {RX_TIMEOUT_SEC}s, "
        f"check every {WATCHDOG_INTERVAL_SEC}s)")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log(f"Listening on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
