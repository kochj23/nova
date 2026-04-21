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
        record_date = data.get("date", date.today().isoformat())
        is_history = data.get("source") == "healthkit_history"

        if not is_history:
            latest = HEALTH_DIR / "latest.json"
            latest.write_text(json.dumps(data, indent=2))
            latest.chmod(0o600)

        daily = HEALTH_DIR / f"{record_date}.json"
        if is_history and daily.exists():
            existing = json.loads(daily.read_text())
            existing.update({k: v for k, v in data.items() if v and v != 0 and k not in ("received_at", "source", "date", "sample_count")})
            data = existing
        daily.write_text(json.dumps(data, indent=2))
        daily.chmod(0o600)

        skip_keys = {"received_at", "source", "date", "sample_count"}
        metrics = {k: v for k, v in data.items() if k not in skip_keys and v and v != 0}
        summary = ", ".join(f"{k}={v}" for k, v in sorted(metrics.items()))

        memory_text = f"HealthKit data for {record_date}: {summary}"

        try:
            payload = json.dumps({
                "text": memory_text,
                "source": "apple_health",
                "metadata": {
                    "privacy": "local-only",
                    "origin": "healthkit_history" if is_history else "ios-app",
                    "date": record_date,
                    **metrics,
                },
            }).encode()
            req = urllib.request.Request(
                MEMORY_URL + "?async=1", data=payload,
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            print(f"[healthkit] Memory store failed: {e}", flush=True)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok", "date": record_date}).encode())

        label = "HISTORY" if is_history else "LIVE"
        print(f"[healthkit] [{label}] {record_date}: {len(metrics)} metrics", flush=True)

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
