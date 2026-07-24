#!/opt/homebrew/bin/python3
"""
nova_mmwave_poller.py — Aqara FP2 mmWave presence receiver for Nova.

Accepts presence data via two methods:
1. HTTP webhook (POST /presence) — from Apple HomeKit automations
2. Apple Shortcuts polling — Shortcut reads FP2 state and POSTs here

Stores zone-level presence in telemetry.presence and updates presence_state.

FP2 sensors use Apple MFi auth so can't pair with HA directly.
Instead, Apple Home automations fire webhooks on occupancy change.

Written by Jordan Koch.
"""

import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import ssl
from socketserver import ThreadingMixIn
from threading import Thread
from urllib.parse import parse_qs

try:
    import asyncpg
except ImportError:
    print("FATAL: pip install asyncpg", file=sys.stderr)
    sys.exit(1)

sys.path.insert(0, str(Path(__file__).parent))

VERSION = "3.0.0"
DB_DSN = "postgresql://kochj@pg-primary.digitalnoise.net:5432/nova_ops"
LISTEN_PORT = 8089
LOG_FILE = Path.home() / ".openclaw/logs/nova_mmwave.log"
POLL_INTERVAL = 10

# Room mapping for the 4 FP2 sensors (by their mDNS suffix)
FP2_SENSORS = {
    "66A8": {"room": "office", "ip": "192.168.1.37"},
    "688B": {"room": "living_room", "ip": "192.168.1.162"},
    "68C6": {"room": "master_bedroom", "ip": "192.168.1.43"},
    "67B8": {"room": "patio", "ip": "192.168.1.130"},
}

_shutdown = False
_pool = None
_start_time = time.time()
_last_state = {}  # room -> {presence: bool, ts: float, zones: dict}
_event_count = 0

LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def log(msg, level="INFO"):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[mmwave {ts}] [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── Database ──────────────────────────────────────────────────────────────────

_sync_pool = None


def get_sync_conn():
    import psycopg2
    return psycopg2.connect("host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj")


def write_presence_sync(room, presence, confidence, zones=None, metadata=None):
    """Write presence reading to telemetry.presence and update presence_state."""
    global _event_count
    try:
        conn = get_sync_conn()
        try:
            with conn.cursor() as cur:
                # Write to telemetry
                cur.execute("""
                    INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata)
                    VALUES (now(), 'jordan', %s, %s, 'mmwave', %s)
                """, (room, confidence, json.dumps(metadata or {})))

                # Upsert presence_state
                if presence:
                    cur.execute("""
                        INSERT INTO presence_state (person, room, confidence, source, activity_state)
                        VALUES ('jordan', %s, %s, 'mmwave', 'unknown')
                        ON CONFLICT (person) DO UPDATE SET
                            room = EXCLUDED.room,
                            confidence = EXCLUDED.confidence,
                            last_confirmed = now(),
                            source = 'mmwave'
                    """, (room, confidence))

            conn.commit()
            _event_count += 1
        finally:
            conn.close()
    except Exception as e:
        log(f"DB write error: {e}", "ERROR")


def write_observation_sync(room, observation):
    """Write presence transition to shared_observations."""
    try:
        conn = get_sync_conn()
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO shared_observations (observer, category, subject, observation, severity)
                    VALUES ('mmwave_poller', 'presence', %s, %s, 'info')
                """, (f"room_{room}", observation))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log(f"Observation write error: {e}", "ERROR")


# ── Presence Processing ───────────────────────────────────────────────────────

def process_presence_update(room, presence, zones=None, source="webhook"):
    """Process a presence update from any source."""
    now = time.time()
    prev = _last_state.get(room, {})
    prev_presence = prev.get("presence")
    prev_ts = prev.get("ts", 0)

    confidence = 0.95 if presence else 0.05
    state_changed = (presence != prev_presence)
    heartbeat_due = (now - prev_ts) > 60

    if state_changed or heartbeat_due:
        _last_state[room] = {
            "presence": presence,
            "ts": now,
            "zones": zones or {},
        }

        metadata = {"zones": zones or {}, "source": source}
        write_presence_sync(room, presence, confidence, zones, metadata)

        if state_changed:
            event = "enter" if presence else "leave"
            log(f"{room}: {'PRESENT' if presence else 'EMPTY'} (via {source})")
            write_observation_sync(room, f"mmWave: {event} detected in {room}")

    return state_changed


# ── HTTP Webhook Server ───────────────────────────────────────────────────────

class PresenceHandler(BaseHTTPRequestHandler):
    """Handles incoming presence webhooks from HomeKit automations."""

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logging

    def do_GET(self):
        if self.path == "/health":
            rooms_present = [r for r, s in _last_state.items() if s.get("presence")]
            health = {
                "status": "ok",
                "version": VERSION,
                "uptime_s": int(time.time() - _start_time),
                "events_received": _event_count,
                "rooms_tracked": len(_last_state),
                "occupied": rooms_present,
                "last_state": {r: {"presence": s["presence"], "age_s": int(time.time() - s["ts"])}
                               for r, s in _last_state.items()},
            }
            self._respond(200, health)
        elif self.path == "/state":
            self._respond(200, _last_state)
        else:
            self._respond(404, {"error": "Not found. Use POST /presence or GET /health"})

    def do_POST(self):
        if self.path != "/presence":
            self._respond(404, {"error": "POST to /presence"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length).decode("utf-8")

            # Accept both JSON and form-urlencoded
            content_type = self.headers.get("Content-Type", "")
            if "json" in content_type:
                data = json.loads(body)
            else:
                parsed = parse_qs(body)
                data = {k: v[0] for k, v in parsed.items()}

            room = data.get("room", "").lower().replace(" ", "_")
            presence = data.get("presence", data.get("occupied", ""))

            if not room:
                self._respond(400, {"error": "Missing 'room' field"})
                return

            # Parse presence value
            if isinstance(presence, bool):
                is_present = presence
            elif isinstance(presence, str):
                is_present = presence.lower() in ("true", "1", "yes", "on", "detected")
            else:
                is_present = bool(presence)

            zones = data.get("zones", {})
            source = data.get("source", "homekit_automation")

            changed = process_presence_update(room, is_present, zones, source)

            self._respond(200, {
                "ok": True,
                "room": room,
                "presence": is_present,
                "changed": changed,
            })

        except json.JSONDecodeError:
            self._respond(400, {"error": "Invalid JSON"})
        except Exception as e:
            log(f"Webhook error: {e}", "ERROR")
            self._respond(500, {"error": str(e)})

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


# ── Shortcut Poller (fallback) ────────────────────────────────────────────────

def run_shortcut_poll():
    """Run the Nova FP2 Presence Shortcut and parse output.
    Falls back gracefully if shortcut doesn't exist."""
    import subprocess
    try:
        result = subprocess.run(
            ["shortcuts", "run", "Nova FP2 Presence"],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            for room, occupied in data.items():
                process_presence_update(room.lower().replace(" ", "_"), occupied, source="shortcut")
            return True
    except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
        pass
    except Exception as e:
        log(f"Shortcut poll error: {e}", "ERROR")
    return False


# ── Main ──────────────────────────────────────────────────────────────────────

def health_loop():
    """Periodically log health stats."""
    while not _shutdown:
        time.sleep(300)
        rooms_present = [r for r, s in _last_state.items() if s.get("presence")]
        log(f"Health: up {int(time.time() - _start_time)}s, "
            f"events: {_event_count}, "
            f"rooms: {len(_last_state)}, "
            f"occupied: {rooms_present or 'none'}")


def shortcut_poll_loop():
    """Fallback: poll via Shortcut every POLL_INTERVAL seconds."""
    time.sleep(30)  # Give webhooks time to start flowing
    consecutive_failures = 0

    while not _shutdown:
        # Only poll via Shortcut if no webhook data received recently
        all_stale = all(
            time.time() - s.get("ts", 0) > 120
            for s in _last_state.values()
        ) if _last_state else True

        if all_stale:
            success = run_shortcut_poll()
            if success:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
                if consecutive_failures == 1:
                    log("Shortcut poll unavailable — relying on webhooks only", "WARN")
                if consecutive_failures > 5:
                    # Stop trying if shortcut doesn't exist
                    log("Shortcut polling disabled (not configured)", "WARN")
                    return

        time.sleep(POLL_INTERVAL)


def main():
    global _shutdown

    signal.signal(signal.SIGINT, lambda s, f: setattr(sys.modules[__name__], '_shutdown', True))
    signal.signal(signal.SIGTERM, lambda s, f: setattr(sys.modules[__name__], '_shutdown', True))

    log(f"Nova mmWave Presence Receiver v{VERSION}")
    log(f"HTTP webhook: http://0.0.0.0:{LISTEN_PORT}/presence")
    log(f"Health check: http://0.0.0.0:{LISTEN_PORT}/health")
    log(f"FP2 rooms: {[s['room'] for s in FP2_SENSORS.values()]}")

    # Verify DB connectivity
    try:
        conn = get_sync_conn()
        conn.close()
        log("Database connection verified")
    except Exception as e:
        log(f"Database connection failed: {e}", "ERROR")
        sys.exit(1)

    # Start HTTP server
    server = ThreadedHTTPServer(("0.0.0.0", LISTEN_PORT), PresenceHandler)
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    log(f"Webhook server listening on port {LISTEN_PORT} (HTTP)")

    # Start HTTPS server (required for Apple HomeKit automations)
    cert_dir = Path.home() / ".openclaw/certs"
    cert_file = cert_dir / "mmwave_cert.pem"
    key_file = cert_dir / "mmwave_key.pem"
    if cert_file.exists() and key_file.exists():
        ssl_server = ThreadedHTTPServer(("0.0.0.0", LISTEN_PORT + 1), PresenceHandler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert_file), str(key_file))
        ssl_server.socket = ctx.wrap_socket(ssl_server.socket, server_side=True)
        ssl_thread = Thread(target=ssl_server.serve_forever, daemon=True)
        ssl_thread.start()
        log(f"Webhook server listening on port {LISTEN_PORT + 1} (HTTPS)")
    else:
        log("No SSL certs found — HTTPS disabled (HomeKit automations may not work)", "WARN")

    # Start health reporter
    health_thread = Thread(target=health_loop, daemon=True)
    health_thread.start()

    # Start shortcut poller (fallback)
    poll_thread = Thread(target=shortcut_poll_loop, daemon=True)
    poll_thread.start()

    # Main loop — just wait for shutdown
    try:
        while not _shutdown:
            time.sleep(1)
    except KeyboardInterrupt:
        pass

    log("Shutting down...")
    server.shutdown()
    log("Shutdown complete")


if __name__ == "__main__":
    main()
