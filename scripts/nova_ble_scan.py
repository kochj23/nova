#!/usr/bin/env python3
"""nova_ble_scan.py — distributed BLE scanner (one vantage point per box).

Runs ON a nova-core box, scans nearby BLE advertisements via bluetoothctl (no pip installs), and
reports sightings to telemetry.bluetooth tagged observer=<hostname>. Until now the ENTIRE BLE
dataset came from a single observer (mac-studio); every nova-core box has an idle Bluetooth adapter,
so running this on each turns the fleet into a distributed BLE grid — far more likely to catch the
"new device that shows up for a few minutes" and gives rough triangulation (which box heard it).

Run per-box every few minutes (cron/scheduler). Manual: python3 nova_ble_scan.py [scan_seconds]
"""
import re
import socket
import subprocess
import sys
import time

DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
SCAN_S = int(sys.argv[1]) if len(sys.argv) > 1 else 25
OBS = socket.gethostname().split(".")[0]
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PROP = ("RSSI", "ManufacturerData", "UUIDs", "ServicesResolved", "Paired", "Connected",
         "TxPower", "ServiceData", "Adapter", "Alias", "LegacyPairing", "Blocked", "Trusted",
         "AddressType", "Icon", "Class", "Modalias", "Key:", "Value:", "AdvertisingFlags", "AdvertisingData", "WakeAllowed", "ServiceData:")


def scan():
    """{mac: {name, rssi}} from a bluetoothctl scan window."""
    try:
        p = subprocess.run(["bluetoothctl", "--timeout", str(SCAN_S), "scan", "on"],
                           capture_output=True, text=True, timeout=SCAN_S + 20)
    except Exception as e:
        print(f"[ble_scan] scan failed: {e}"); return {}
    devs = {}
    for raw in (p.stdout or "").splitlines():
        line = _ANSI.sub("", raw)
        m = re.search(r"Device ([0-9A-Fa-f:]{17})\s*(.*)$", line)
        if not m:
            continue
        mac = m.group(1).upper(); rest = m.group(2).strip()
        d = devs.setdefault(mac, {"name": "", "rssi": None})
        nm = re.match(r"Name:\s*(.+)", rest)
        if nm:
            d["name"] = nm.group(1)[:80]; continue
        rm = re.search(r"RSSI:\s*(-?\d+)", rest)
        if rm:
            d["rssi"] = int(rm.group(1))
            continue
        # a real name = leftover text that isn't a property update and isn't just the MAC
        if rest and not any(rest.startswith(p) for p in _PROP) \
           and not re.fullmatch(r"[0-9A-Fa-f:\-]{17}", rest) and "Key:" not in rest:
            d["name"] = rest[:80]
    return devs


def _sql_str(v):
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def main():
    devs = scan()
    # Insert via psql — dependency-free (psycopg2 isn't installed on every box, but psql is, since
    # they all talk to pg-primary). One multi-row INSERT, values escaped.
    vals = []
    for mac, d in devs.items():
        vals.append(f"(now(), {_sql_str(mac)}, {_sql_str(d['name'] or None)}, "
                    f"{d['rssi'] if d['rssi'] is not None else 'NULL'}, 'ble_hci', {_sql_str(OBS)})")
    if vals:
        sql = ("INSERT INTO telemetry.bluetooth (ts, device_mac, device_name, rssi, device_type, observer) "
               "VALUES " + ",".join(vals) + ";")
        try:
            subprocess.run(["psql", DSN, "-v", "ON_ERROR_STOP=1", "-q", "-c", sql],
                           capture_output=True, text=True, timeout=30, check=True)
        except Exception as e:
            print(f"[ble_scan] insert failed: {getattr(e, 'stderr', e)}"); return 1
    named = sum(1 for _, d in devs.items() if d["name"])
    print(f"[ble_scan] {OBS}: {len(vals)} BLE devices ({named} named) in {SCAN_S}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
