#!/usr/bin/env python3
"""nova_eve_energy.py — capture Eve Energy real-time watts from NovaHomeKit into
telemetry.energy (so Eve strips show up alongside the Zigbee plugs).

The old nova_energy_poller.py was disabled, pointed at dead sources
(Shortcuts/homebridge proxies), and had its Eve UUIDs swapped. The working source
is the NovaHomeKit app's local API, and the live characteristic mapping is:
  E863F10C = real-time Watts   (confirmed: a strip reads ~552 W)
  E863F10D = cumulative kWh     (often 0 on these strips)
Standard HAP "On" (uuid 00000025) = relay state.

Writes one row per Eve power service to telemetry.energy. Never raises fatally.
Written by Jordan Koch (via Claude).
"""
import json
import sys
import urllib.request

import psycopg2

import nova_homekit_client as hk  # Bearer token (NovaHomeKit 51e7a91)

SRC = "http://127.0.0.1:37433/api/accessories"
DSN = "host=pg-primary.digitalnoise.net dbname=nova_ops user=kochj"
WATT = "e863f10c"      # real-time watts
KWH = "e863f10d"       # cumulative kWh
ON = "00000025"        # standard HAP On characteristic


def num(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def main():
    with urllib.request.urlopen(urllib.request.Request(SRC, headers=hk.auth_headers()), timeout=10) as r:
        data = json.load(r)
    acc = data if isinstance(data, list) else data.get("accessories", [])
    rows = []
    for a in acc:
        name = a.get("name", "?")
        for s in a.get("services", []):
            watts = kwh = on = None
            for c in s.get("characteristics", []):
                u = (c.get("uuid") or "").lower()
                if u.startswith(WATT):
                    watts = num(c.get("value"))
                elif u.startswith(KWH):
                    kwh = num(c.get("value"))
                elif u.startswith(ON):
                    v = c.get("value")
                    on = bool(v) if isinstance(v, bool) else (num(v) == 1 if num(v) is not None else None)
            if watts is None:
                continue
            sname = s.get("name") or name
            dev = sname if sname != name else name
            rows.append((dev, dev, watts, (kwh if kwh else None),
                         on if on is not None else (watts > 0.5)))
    if not rows:
        print("[eve-energy] no Eve power services found", file=sys.stderr)
        return 1
    c = psycopg2.connect(DSN); c.autocommit = True
    with c.cursor() as cur:
        for dev_id, dev_name, watts, kwh, on in rows:
            cur.execute(
                "INSERT INTO telemetry.energy (ts, device_id, device_name, watts, kwh_total, on_state) "
                "VALUES (now(), %s, %s, %s, %s, %s)",
                (f"eve:{dev_id}", dev_name, watts, kwh, on))
    c.close()
    tot = sum(r[2] for r in rows)
    print(f"[eve-energy] wrote {len(rows)} Eve services, total {tot:.0f} W")
    return 0


if __name__ == "__main__":
    sys.exit(main())
