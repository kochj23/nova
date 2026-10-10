#!/opt/homebrew/bin/python3
"""
nova_fp2_presence.py — Aqara FP2 mmWave occupancy → telemetry.presence.

Sources FP2 zone occupancy from the NovaHomeKit bridge (port 37433), which
exposes the four FP2 sensors as HomeKit Occupancy Sensors with live values.
This replaces the dead webhook path (nova_mmwave_poller on :8089 received 0
events) — the FP2s aren't integrated into Home Assistant, but they ARE paired
to HomeKit, and NovaHomeKit surfaces them reliably.

Writes method='mmwave' rows so nova_presence_engine.py picks them up as the
high-weight presence signal it already expects (WEIGHTS['mmwave'] = 0.30).

Written by Jordan Koch (via Claude).
"""

import json
import signal
import sys
import time

import psycopg2

import nova_homekit_client as hk  # Bearer token (NovaHomeKit 51e7a91) + retry/backoff

NOVAHOMEKIT_URL = "http://127.0.0.1:37433/api/accessories"
import nova_dsn as _nova_dsn  # noqa: E402
PG_DSN = _nova_dsn.pg_dsn("nova_ops")
POLL_INTERVAL = 10  # seconds
OCCUPANCY_TYPE = "Occupancy Detected"  # HomeKit char UUID 00000071-...

# NovaHomeKit "room" label  ->  telemetry.presence room vocabulary
ROOM_MAP = {
    "Office": "office",
    "Living Room": "living_room",
    "Master Bedroom": "master_bedroom",
    "Outdoor": "patio",  # FP2 2 covers the patio
}

CONF_OCCUPIED = 0.95
CONF_EMPTY = 0.05

_shutdown = False


def log(msg, level="INFO"):
    print(f"[fp2 {time.strftime('%H:%M:%S')}] [{level}] {msg}", flush=True)


def fetch_fp2_occupancy():
    """Return {room: occupied_bool} for every FP2 currently reporting a value."""
    accs = hk.get_json(NOVAHOMEKIT_URL, timeout=8, retries=3)
    out = {}
    for a in accs:
        name = str(a.get("name", ""))
        if "FP2" not in name:
            continue
        room = ROOM_MAP.get(a.get("room", ""))
        if not room:
            continue
        value = None
        for svc in a.get("services", []):
            for ch in svc.get("characteristics", []):
                if ch.get("type") == OCCUPANCY_TYPE:
                    value = ch.get("value")
        if value is None:  # no fresh reading — don't assert a state
            continue
        out[room] = bool(value)
    return out


def write_presence(conn, occupancy):
    with conn.cursor() as cur:
        for room, occupied in occupancy.items():
            conf = CONF_OCCUPIED if occupied else CONF_EMPTY
            cur.execute(
                """INSERT INTO telemetry.presence (ts, person, room, confidence, method, metadata)
                   VALUES (now(), %s, %s, %s, 'mmwave', %s)""",
                ("occupant", room, conf, json.dumps({"occupied": occupied, "source": "fp2/novahomekit"})),
            )
    conn.commit()


def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True


def main():
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    conn = psycopg2.connect(PG_DSN)
    log(f"FP2 presence poller started (every {POLL_INTERVAL}s via {NOVAHOMEKIT_URL})")
    fails = 0
    last_health = 0
    while not _shutdown:
        try:
            occ = fetch_fp2_occupancy()
            if occ:
                write_presence(conn, occ)
            fails = 0
            now = time.time()
            if now - last_health > 300:
                occupied = [r for r, o in occ.items() if o]
                log(f"Health: {len(occ)} FP2 reporting, occupied: {occupied or 'none'}")
                last_health = now
        except (psycopg2.InterfaceError, psycopg2.OperationalError) as e:
            log(f"DB connection lost ({e}); reconnecting", "WARN")
            try:
                conn = psycopg2.connect(PG_DSN)
            except Exception as e2:
                log(f"reconnect failed: {e2}", "ERROR")
        except Exception as e:
            fails += 1
            if fails <= 3 or fails % 30 == 0:
                log(f"poll error ({fails}): {e}", "WARN")
        time.sleep(POLL_INTERVAL)
    conn.close()
    log("Shutdown complete.")


if __name__ == "__main__":
    main()
