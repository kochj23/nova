#!/usr/bin/env python3
"""
nova_homekit_receiver.py — Receives HomeKit accessory data from Apple Shortcuts.

Listens on port 37432. Accepts POST /homekit/update with JSON body from a
Shortcut that reads all HomeKit accessory states.

Also serves GET /homekit/accessories to return the last-received data.

Written by Jordan Koch.
"""

import json
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from threading import Lock

sys.path.insert(0, str(Path(__file__).parent))

PORT = 37432
DATA_FILE = Path.home() / ".openclaw/workspace/state/homekit_accessories.json"
LOG_FILE = Path.home() / ".openclaw/logs/homekit_receiver.log"

_data_lock = Lock()
_cached_data = []
_last_update = 0


def log(msg):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[homekit-rx {ts}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_POST(self):
        if self.path == "/homekit/update":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
                global _cached_data, _last_update
                with _data_lock:
                    _cached_data = data
                    _last_update = time.time()
                DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
                DATA_FILE.write_text(json.dumps(data, indent=2))
                log(f"Received {len(data)} accessories")
                self._respond(200, {"status": "ok", "count": len(data)})
            except Exception as e:
                log(f"Error: {e}")
                self._respond(400, {"error": str(e)})
        else:
            self._respond(404, {"error": "not found"})

    def do_GET(self):
        if self.path == "/homekit/accessories":
            with _data_lock:
                self._respond(200, _cached_data)
        elif self.path == "/homekit/status" or self.path == "/health":
            self._respond(200, {
                "status": "ok",
                "accessories": len(_cached_data),
                "last_update": _last_update,
                "age_seconds": int(time.time() - _last_update) if _last_update else None,
            })
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def main():
    # Load cached data if exists
    global _cached_data, _last_update
    if DATA_FILE.exists():
        try:
            _cached_data = json.loads(DATA_FILE.read_text())
            _last_update = DATA_FILE.stat().st_mtime
            log(f"Loaded {len(_cached_data)} cached accessories")
        except Exception:
            pass

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    log(f"Listening on 0.0.0.0:{PORT}")
    log(f"  POST /homekit/update — receive accessory data from Shortcuts")
    log(f"  GET  /homekit/accessories — return last-received data")
    log(f"  GET  /homekit/status — health check")
    server.serve_forever()


if __name__ == "__main__":
    main()
