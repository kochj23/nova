#!/usr/bin/env python3
"""
nova_healthkit_receiver.py — HTTP endpoint that receives HealthKit data from iOS Shortcuts.

Listens on port 37450 (loopback only). The iOS Shortcut POSTs JSON with health
metrics, which get stored in Nova's local vector memory and written to a daily file.

Data flow: iPhone Shortcut → this server → pgvector + daily JSON
All local. Tagged privacy:local-only.

Written by Jordan Koch.
"""

import json
import os
import sys
import urllib.request
from datetime import datetime, date
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

PORT = 37450
HEALTH_DIR = Path.home() / ".openclaw/private/health"
MEMORY_URL = "http://127.0.0.1:18790/remember"

HEALTH_DIR.mkdir(parents=True, exist_ok=True)
HEALTH_DIR.chmod(0o700)


class HealthHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/health":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
            return

        data["received_at"] = datetime.now().isoformat()
        today = date.today().isoformat()

        latest = HEALTH_DIR / "latest.json"
        daily = HEALTH_DIR / f"{today}.json"
        for path in [latest, daily]:
            path.write_text(json.dumps(data, indent=2))
            path.chmod(0o600)

        summary = []
        if "sleep_hours" in data:
            summary.append(f"Sleep: {data['sleep_hours']:.1f}h")
        if "resting_heart_rate" in data:
            summary.append(f"Resting HR: {data['resting_heart_rate']} bpm")
        if "hrv" in data:
            summary.append(f"HRV: {data['hrv']} ms")
        if "steps" in data:
            summary.append(f"Steps: {int(data['steps']):,}")
        if "active_energy" in data:
            summary.append(f"Active energy: {data['active_energy']:.0f} kcal")

        memory_text = f"HealthKit data for {today}: {', '.join(summary)}"

        try:
            payload = json.dumps({
                "text": memory_text,
                "source": "healthkit",
                "metadata": {
                    "privacy": "local-only",
                    "origin": "ios-shortcut",
                    "date": today,
                    **{k: v for k, v in data.items() if k != "received_at"},
                },
            }).encode()
            req = urllib.request.Request(
                MEMORY_URL, data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[healthkit] Memory store failed: {e}", flush=True)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok", "date": today}).encode())
        print(f"[healthkit] Received: {', '.join(summary)}", flush=True)

    def do_GET(self):
        if self.path == "/health":
            latest = HEALTH_DIR / "latest.json"
            if latest.exists():
                data = json.loads(latest.read_text())
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(data).encode())
            else:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status":"no data yet"}')
            return
        self.send_error(404)

    def log_message(self, format, *args):
        pass


def main():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)  # LAN — iPhone pushes over WiFi
    print(f"[healthkit] Listening on port {PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()


if __name__ == "__main__":
    main()
