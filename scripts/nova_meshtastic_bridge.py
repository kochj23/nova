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
import json
import sys
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
DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
LOG_FILE = Path.home() / ".openclaw/logs/meshtastic_bridge.log"

_lock = Lock()
_iface = None


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
    decoded = packet.get("decoded", {})
    port = decoded.get("portnum", "")
    frm = packet.get("fromId", packet.get("from", "unknown"))
    if port == "TEXT_MESSAGE_APP":
        text = decoded.get("text", "")
        log(f"mesh message from {frm}: {text}")
        record_observation(f"message from {frm}", text)
    elif port in ("TELEMETRY_APP", "POSITION_APP"):
        pass  # too frequent/low-signal to log every one; /status covers current state


def connect():
    global _iface
    _iface = meshtastic.serial_interface.SerialInterface(devPath=DEVICE)
    pub.subscribe(on_receive, "meshtastic.receive")
    log(f"Connected to {DEVICE}")


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
        if self.path != "/status":
            return self._json(404, {"error": "not found"})
        with _lock:
            if _iface is None:
                return self._json(503, {"error": "not connected"})
            try:
                info = _iface.getMyNodeInfo()
                metrics = (info or {}).get("deviceMetrics", {})
                self._json(200, {
                    "connected": True,
                    "longName": (info or {}).get("user", {}).get("longName"),
                    "batteryLevel": metrics.get("batteryLevel"),
                    "voltage": metrics.get("voltage"),
                    "uptimeSeconds": metrics.get("uptimeSeconds"),
                })
            except Exception as e:
                self._json(500, {"error": str(e)})

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
            try:
                _iface.sendText(text)
                log(f"sent: {text}")
                self._json(200, {"sent": True})
            except Exception as e:
                log(f"send failed: {e}")
                self._json(500, {"error": str(e)})


def main():
    connect()
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log(f"Listening on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
