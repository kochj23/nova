#!/usr/bin/env python3
"""nova_ble_theengs.py — BLE scanner WITH device identification (bleak + Theengs Decoder).

The identification upgrade over the plain bluetoothctl scanner: instead of OUI vendor-guessing, this
decodes each BLE advertisement through Theengs Decoder — naming brand/model/type (Apple Watch, Tile
Smart Tracker, RuuviTag, ...) and, crucially, flagging TRACKERS (type=TRACK / track=true) and
private-random-MAC devices (prmac). That gives Nova two things it couldn't do before:
  1. Reliably tag its OWN Apple gear as brand=Apple -> excludable from the "unidentified" noise.
  2. FLAG a tracker/AirTag/Tile that appears near the house -> the counter-surveillance signal.

Writes to telemetry.bluetooth (observer=<hostname>), decoded identity in device_type + metadata.
Dependency: bleak + TheengsDecoder (pip). Insert via psql (no psycopg2 needed).
Run per-box. Manual: python3 nova_ble_theengs.py [scan_seconds]
"""
import asyncio
import json
import socket
import subprocess
import sys

from bleak import BleakScanner
from TheengsDecoder import decodeBLE

import nova_dsn as _nova_dsn  # noqa: E402
DSN = _nova_dsn.pg_dsn("nova_ops")
SCAN_S = int(sys.argv[1]) if len(sys.argv) > 1 else 25
OBS = socket.gethostname().split(".")[0]


def _theengs_input(addr, adv):
    d = {"id": addr, "rssi": adv.rssi if adv.rssi is not None else -100}
    if adv.local_name:
        d["name"] = adv.local_name
    if adv.manufacturer_data:
        cid, payload = next(iter(adv.manufacturer_data.items()))
        d["manufacturerdata"] = f"{cid & 0xff:02x}{(cid >> 8) & 0xff:02x}" + payload.hex()
    if adv.service_data:
        uuid, payload = next(iter(adv.service_data.items()))
        short = uuid.replace("-", "")
        d["servicedatauuid"] = short[4:8] if len(short) >= 8 else short
        d["servicedata"] = payload.hex()
    return d


def _decode(inp):
    try:
        r = decodeBLE(json.dumps(inp))
        return json.loads(r) if r else {}
    except Exception:
        return {}


async def scan():
    found = await BleakScanner.discover(timeout=SCAN_S, return_adv=True)
    rows = []
    for addr, (dev, adv) in found.items():
        dec = _decode(_theengs_input(addr, adv))
        name = dec.get("name") or adv.local_name or None
        brand, model = dec.get("brand"), dec.get("model")
        dtype = dec.get("type") or ("identified" if brand else "ble")
        track = bool(dec.get("track"))
        meta = {"brand": brand, "model": model, "track": track,
                "prmac": dec.get("prmac"), "scanner": "bleak+theengs"}
        rows.append((addr, name, adv.rssi, dtype, brand, track, meta))
    return rows


def _s(v):
    return "NULL" if v is None else "'" + str(v).replace("'", "''") + "'"


def main():
    rows = asyncio.run(scan())
    vals = []
    for addr, name, rssi, dtype, brand, track, meta in rows:
        vals.append(f"(now(), {_s(addr)}, {_s(name)}, {rssi if rssi is not None else 'NULL'}, "
                    f"{_s(dtype)}, {_s(json.dumps(meta))}::jsonb, {_s(OBS)})")
    if vals:
        sql = ("INSERT INTO telemetry.bluetooth (ts, device_mac, device_name, rssi, device_type, metadata, observer) "
               "VALUES " + ",".join(vals) + ";")
        try:
            subprocess.run(["psql", DSN, "-v", "ON_ERROR_STOP=1", "-q", "-c", sql],
                           capture_output=True, text=True, timeout=30, check=True)
        except Exception as e:
            print(f"[ble_theengs] insert failed: {getattr(e, 'stderr', e)}"); return 1
    ident = sum(1 for r in rows if r[4])
    trackers = sum(1 for r in rows if r[5])
    print(f"[ble_theengs] {OBS}: {len(rows)} devices, {ident} identified, {trackers} TRACKERS in {SCAN_S}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
